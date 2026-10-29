"""Append a missing proof to the same confirmed modern Transfer payment.

No lifecycle/financial mutation. Caller supplies an already authorized student
storage resolver; this module validates persisted authority before invoking it.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
import uuid
from student_lifecycle.transfer_source_authority import resolve_for_cycle, SourceAuthorityError
from student_lifecycle.transfer_v2_cutover import is_new_transfer_cycle
from student_lifecycle.transfer_v2_reminders import payment_proof_occurrence_key, _close_reminders

class PaymentProofAppendError(ValueError):
    pass

class PaymentProofCleanupError(RuntimeError):
    """Rollback could not safely remove a request-created file; never success."""
    def __init__(self, code, original_path, retained_path=None, cleanup_directory=None):
        self.code = code
        self.original_path = str(original_path)
        self.retained_path = str(retained_path) if retained_path is not None else None
        self.cleanup_directory = str(cleanup_directory) if cleanup_directory is not None else None
        super().__init__(f'payment_proof_cleanup:{code}; original={self.original_path}; '
                         f'retained={self.retained_path}; cleanup_directory={self.cleanup_directory}')

@dataclass(frozen=True)
class PaymentProofAppendResult:
    payment_id: int
    payment_link_id: int
    cycle_id: int
    document_id: int
    receipt_path: str
    replayed: bool


def _rows(conn,sql,args=()):
    cursor=conn.execute(sql,args)
    return [dict(zip([c[0] for c in cursor.description],tuple(row))) for row in cursor.fetchall()]


def resolve_payment_proof_target(connection, *, student_id, payment_id, payment_link_id):
    try:
        if any(isinstance(v,bool) or not str(v).isdigit() or int(v)<=0 for v in (payment_id,payment_link_id)):
            raise PaymentProofAppendError('付款记录标识无效。')
        rows=_rows(connection,'''SELECT p.id AS payment_id,p.student_id,p.payment_category,p.receipt_file_path,
            l.id AS payment_link_id,l.case_id,l.transfer_application_id AS branch_id,
            b.application_cycle_id AS cycle_id,b.removed_at,c.status AS cycle_status,c.outcome,
            s.status AS case_status
            FROM payments p JOIN case_record_links l ON l.record_id=p.id AND l.record_type='payment'
            JOIN transfer_school_applications b ON b.id=l.transfer_application_id AND b.case_id=l.case_id
            JOIN application_cycles c ON c.id=b.application_cycle_id AND c.source_student_case_id=l.case_id
              AND c.student_id=p.student_id AND c.cycle_type='transfer'
            JOIN student_cases s ON s.id=l.case_id AND s.student_id=p.student_id AND s.case_type='transfer'
            JOIN transfer_cases t ON t.case_id=s.id AND t.v2_version=2
            WHERE p.id=? AND p.student_id=? AND l.id=?
              AND l.validity_status IN ('current','carried_forward')''',(int(payment_id),str(student_id),int(payment_link_id)))
        if len(rows)!=1:raise PaymentProofAppendError('付款记录关联不完整或不匹配，暂不能补交。')
        row=rows[0]
        owners=_rows(connection,"SELECT id FROM case_record_links WHERE record_type='payment' AND record_id=? AND validity_status IN ('current','carried_forward')",(int(payment_id),))
        confirmations=_rows(connection,'SELECT id FROM transfer_payment_confirmations WHERE payment_link_id=?',(int(payment_link_id),))
        # Evidence append is not payment confirmation. Published 035 requires
        # a receipt before confirmation; an owned unconfirmed payment may
        # therefore need this first proof, including a completed Case follow-up.
        if len(owners)!=1 or len(confirmations)>1 or row['removed_at'] is not None:
            raise PaymentProofAppendError('付款确认或归属不唯一，暂不能补交。')
        if not is_new_transfer_cycle(connection,row['cycle_id']):raise PaymentProofAppendError('历史转学记录只读，不能追加。')
        resolve_for_cycle(connection,str(student_id),row['cycle_id'])
        active=row['cycle_status']=='active' and row['case_status']=='in_progress'
        completed=row['cycle_status']=='closed' and row['outcome']=='completed' and row['case_status']=='completed'
        if completed:
            completed=len(_rows(connection,'SELECT id FROM transfer_completion_events WHERE application_cycle_id=?',(row['cycle_id'],)))==1
        if not(active or completed):raise PaymentProofAppendError('当前办理状态不允许补交。')
        return row
    except PaymentProofAppendError:
        raise
    except (sqlite3.Error,SourceAuthorityError,TypeError,KeyError,ValueError) as exc:
        raise PaymentProofAppendError('付款证据关联无法核验，暂不能补交。') from exc


def _folder(provider):
    folder=Path(provider())
    if not folder.is_absolute() or folder.is_symlink() or not folder.is_dir():
        raise PaymentProofAppendError('付款凭证目录不安全或不可用。')
    return folder.resolve()


def _open_directory(folder):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in folder.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except Exception:
        os.close(fd);raise


def _same_created_file(created_fd, directory_fd, name):
    # Query the still-held O_EXCL creation handle NOW. Some filesystems replace
    # the provisional pre-write inode when the file is materialized.
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    held = os.fstat(created_fd)
    return (stat.S_ISREG(held.st_mode) and stat.S_ISREG(named.st_mode)
            and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino))


def _capture_created_file(name, directory_fd, capture_fd):
    # The destination is empty in a newly exclusively created request namespace.
    os.rename(name, 'document', src_dir_fd=directory_fd, dst_dir_fd=capture_fd)


def _cleanup_created_file(path, created_fd, directory_fd):
    """Remove only the held creation, even if its public name gets replaced.

    Other requests never address the random cleanup namespace. This protects
    cooperative storage operations, not hostile same-user namespace mutation.
    A raced-in foreign object or failed unlink is retained with an explicit
    location. Never overwrite the public path in an attempted restoration.
    """
    try:
        same = _same_created_file(created_fd, directory_fd, path.name)
    except OSError as exc:
        raise PaymentProofCleanupError('ownership_check_failed', path, path) from exc
    if same is None:
        return
    if not same:
        raise PaymentProofCleanupError('ownership_changed', path, path)
    name = '.payment-rollback-' + uuid.uuid4().hex
    directory = path.parent / name
    captured = directory / 'document'
    capture_fd = None
    created = moved = removed = False
    stage = 'capture_failed'
    try:
        os.mkdir(name, 0o700, dir_fd=directory_fd)
        created = True
        capture_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            _capture_created_file(path.name, directory_fd, capture_fd)
        except FileNotFoundError:
            # Do not mistake a missing capture destination for a missing source.
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise
        moved = True
        stage = 'ownership_check_failed'
        same = _same_created_file(created_fd, capture_fd, 'document')
        if not same:
            raise PaymentProofCleanupError('ownership_changed', path,
                                          None if same is None else captured, directory)
        stage = 'remove_failed'
        os.unlink('document', dir_fd=capture_fd)
        removed = True
    except OSError as exc:
        raise PaymentProofCleanupError(stage, path, captured if moved else path, directory if created else None) from exc
    finally:
        if capture_fd is not None:
            os.close(capture_fd)
        if created and (not moved or removed):
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError as exc:
                raise PaymentProofCleanupError('empty_cleanup_directory_remove_failed', path,
                                              None if removed else path, directory) from exc


def append_payment_proof(database, *, student_id,payment_id,payment_link_id,filename,data,
                         actor_user_id,actor_username,occurred_at,folder_provider):
    suffix=Path(str(filename)).suffix.lower()
    if not data or suffix not in {'.pdf','.png','.jpg','.jpeg','.docx','.txt'}:
        raise PaymentProofAppendError('请选择支持的付款凭证文件。')
    reserved=None;created_fd=None;committed=False;directory_fd=None
    try:
        with database.transaction() as conn:
            row=resolve_payment_proof_target(conn,student_id=student_id,payment_id=payment_id,payment_link_id=payment_link_id)
            folder=_folder(folder_provider)
            directory_fd=_open_directory(folder)
            previous=str(row['receipt_file_path'] or '').strip()
            if previous:
                path=Path(previous)
                if path.parent!=folder or path.is_symlink() or not path.is_file():
                    raise PaymentProofAppendError('既有付款凭证无法安全核验。')
                docs=_rows(conn,'''SELECT d.id FROM student_documents d JOIN case_record_links l
                    ON l.record_type='student_document' AND l.record_id=d.id AND l.document_id=d.id
                    WHERE d.student_id=? AND d.file_path=? AND d.folder_category='Payments' AND d.is_active=1
                    AND l.case_id=? AND l.transfer_application_id=? AND l.link_role='payment_proof'
                    AND l.validity_status='current' ''',(str(student_id),previous,row['case_id'],row['branch_id']))
                fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory_fd)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd);raise PaymentProofAppendError("既有凭证不是普通文件。")
                with os.fdopen(fd,'rb') as stream: same=stream.read()==data
                if len(docs)!=1 or not same:raise PaymentProofAppendError('该付款已有凭证，不能覆盖。')
                return PaymentProofAppendResult(int(payment_id),int(payment_link_id),row['cycle_id'],docs[0]['id'],previous,True)
            reserved=folder/('Payment_'+str(payment_id)+'_proof_'+uuid.uuid4().hex+suffix)
            created_fd=os.open(reserved.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
            with os.fdopen(os.dup(created_fd),'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            document_type={"Service Fee":"ServiceFeeReceipt", "Application Fee":"ApplicationFeeReceipt",
                "Offer Deposit":"OfferDepositReceipt", "Tuition Fee":"TuitionReceipt", "OSHC":"OSHCReceipt",
                "Homestay":"HomestayPaymentReceipt", "Airport Pickup":"AirportPickupReceipt",
                "Refund":"RefundReceipt"}.get(row["payment_category"],"OtherPayment")
            doc=int(conn.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
                original_name,stored_name,file_path,version,file_size,uploaded_by,uploaded_at,is_active)
                VALUES(?,'Payments',?,?,?,?,?,?,?,?,1)''',
                (str(student_id),document_type,Path(str(filename)).name,reserved.name,str(reserved),1,len(data),int(actor_user_id),str(occurred_at))).lastrowid)
            conn.execute('''INSERT INTO case_record_links(case_id,transfer_application_id,record_type,record_id,
                document_id,link_role,validity_status,effective_from,created_by,created_at)
                VALUES(?,?,'student_document',?,?,'payment_proof','current',?,?,?)''',
                (row['case_id'],row['branch_id'],doc,doc,str(occurred_at),int(actor_user_id),str(occurred_at)))
            changed=conn.execute("UPDATE payments SET receipt_file_path=? WHERE id=? AND student_id=? AND length(trim(COALESCE(receipt_file_path,'')))=0",(str(reserved),int(payment_id),str(student_id))).rowcount
            if changed!=1:raise PaymentProofAppendError('付款凭证已发生变化，请刷新后重试。')
            conn.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
                record_id,file_path,student_id,status,created_at) VALUES(?,?,'append_payment_proof','Payments','payments',?,?,?,'success',?)''',
                (int(actor_user_id),str(actor_username),str(payment_id),str(reserved),str(student_id),str(occurred_at)))
            result=PaymentProofAppendResult(int(payment_id),int(payment_link_id),row['cycle_id'],doc,str(reserved),False)
        committed=True
        return result
    finally:
        try:
            if not committed and created_fd is not None:
                _cleanup_created_file(reserved, created_fd, directory_fd)
        finally:
            try:
                if created_fd is not None:os.close(created_fd)
            finally:
                if directory_fd is not None:os.close(directory_fd)


def close_payment_proof_reminders(database, result, *, student_id, occurred_at):
    """Only this payment/link's existing reminder occurrence; no lifecycle refresh."""
    occurrence=payment_proof_occurrence_key(result.payment_id,result.payment_link_id)
    rows=database.all("""SELECT * FROM reminders WHERE student_id=?
        AND source_module='transfer_v2' AND source_object_id=?
        AND source_event_type='transfer_v2.payment_proof' AND source_event_id=?
        AND status NOT IN ('Completed','Dismissed','Cancelled')""",
        (str(student_id),str(result.cycle_id),occurrence))
    return _close_reminders(database,rows,now=occurred_at,reason='payment proof provided')

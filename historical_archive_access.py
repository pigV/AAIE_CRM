"""Read original registered archives after obsolete temporary paths disappear.

No file generation or database mutation. Archive IDs and student/document roles
must be proven by their original event before file access is considered.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json

@dataclass(frozen=True)
class ArchiveDocument:
    document_id: int
    student_id: str
    file_path: str
    file_size: int
    stored_name: str
    sha256: str | None = None


def _json(value):
    try:
        parsed=json.loads(value or '{}')
        return parsed if isinstance(parsed,dict) else {}
    except (ValueError,TypeError):return {}


def _document(c,identifier,student_id,kind,sha256=None):
    if type(identifier) is not int or identifier<1:return None
    row=c.execute("""SELECT id,student_id,file_path,file_size,stored_name FROM student_documents
        WHERE id=? AND student_id=? AND folder_category='Applications' AND document_type=?
          AND is_active=1 AND document_status='active' AND voided_at IS NULL""",(identifier,str(student_id),kind)).fetchone()
    if not row or not row[2] or not row[3] or int(row[3])<=0:return None
    return ArchiveDocument(int(row[0]),str(row[1]),str(row[2]),int(row[3]),str(row[4]),sha256)


def school_archive(c,*,report_id,confirmation_id=None,kind='confirmation'):
    if kind not in {'report','confirmation'}:raise ValueError('unsupported archive kind')
    report=c.execute('SELECT student_id FROM school_plan_reports WHERE id=?',(report_id,)).fetchone()
    if not report:return None
    if confirmation_id is None:
        confirmation=c.execute("SELECT id,student_id FROM school_plan_report_confirmations WHERE report_id=? AND status='confirmed' ORDER BY confirmed_at DESC,id DESC LIMIT 1",(report_id,)).fetchone()
    else:
        confirmation=c.execute("SELECT id,student_id FROM school_plan_report_confirmations WHERE id=? AND report_id=? AND status='confirmed'",(confirmation_id,report_id)).fetchone()
    if not confirmation or str(confirmation[1])!=str(report[0]):return None
    for (raw,) in c.execute("SELECT metadata_json FROM school_plan_report_events WHERE report_id=? AND event_type='confirmation_archived_to_application' ORDER BY id DESC",(report_id,)):
        metadata=_json(raw)
        if metadata.get('confirmation_id')!=confirmation[0]:continue
        key='final_document_id' if kind=='report' else 'signature_document_id'
        role='FinalConfirmedSchoolPlan' if kind=='report' else 'ParentSignaturePage'
        sha=metadata.get('parent_pdf_sha256') if kind=='report' else metadata.get('signature_pdf_sha256')
        return _document(c,metadata.get(key),report[0],role,sha)
    return None


def contract_archive(c,*,contract_id,signature_id):
    row=c.execute("""SELECT s.student_id FROM contract_signatures s JOIN contracts contract
        ON contract.id=s.contract_id AND contract.student_id=s.student_id
        WHERE s.id=? AND s.contract_id=? AND s.status='signed' AND s.archive_status='archived'""",(signature_id,contract_id)).fetchone()
    if not row:return None
    for (raw,) in c.execute("""SELECT new_value FROM audit_logs WHERE action_type='contract_signature_archived_to_application'
        AND module_name='contracts' AND record_id=? AND student_id=? ORDER BY id DESC""",(str(contract_id),str(row[0]))):
        metadata=_json(raw)
        if metadata.get('signature_id')!=signature_id:continue
        return _document(c,metadata.get('confirmation_document_id'),row[0],'ContractSignatureConfirmationPage',metadata.get('confirmation_pdf_sha256'))
    return None


def archive_bytes(document,student_root):
    root=Path(student_root).resolve();path=Path(document.file_path).resolve()
    if root not in path.parents or not path.is_file():raise ValueError('归档文件不在该学生目录或不可读取。')
    payload=path.read_bytes()
    if len(payload)!=document.file_size or not payload.startswith(b'%PDF-'):
        raise ValueError('归档文件大小或PDF格式与正式登记不符。')
    if document.sha256 and hashlib.sha256(payload).hexdigest()!=document.sha256:
        raise ValueError('归档文件SHA与原归档证据不符。')
    return payload

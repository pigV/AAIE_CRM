"""Prepare immutable signing inputs, publish a final PDF and retained effects."""
import hashlib
from pathlib import Path
import shutil
import uuid

from .files import persist_verified_file
from .snapshots import read_scope


def generate_final_contract(handler,contract,operation,*,student_entry=False,manual=False):
    g=handler.business_change_runtime();database=g['db'];cid=int(contract['id'])
    source=Path(handler.contract_center_file_path(contract,'signed'))
    source_bytes=source.read_bytes();assets=tuple(Path(path) for path in g['active_contract_asset_paths']())
    asset_bytes=tuple(path.read_bytes() for path in assets)
    seal={'operation':'manual_stamp' if manual else 'auto_stamp','entry':'student' if student_entry else 'center',
        'inputs_sha256':[hashlib.sha256(data).hexdigest() for data in (source_bytes,*asset_bytes)]}
    def command(callback,conn=None):
        return handler.business_change_save_manual_record('Contracts',contract['student_id'],cid,callback,
            caller_connection=conn,payload_extra=seal,authorization_policy='contracts_stamp')
    def forbidden(view):raise AssertionError('receipt lookup executed final publication')
    with read_scope(database) as conn:
        receipt=command(forbidden,conn)
        if receipt is not None:return receipt
    if g['normalize_contract_status'](contract['contract_status'])!='ParentSigned':
        raise ValueError('只有 ParentSigned 合同可以加盖 AAIE 签名和公章。')
    g['require_transfer_contract_signable'](contract)
    g['require_transfer_pdf_bytes'](contract,source_bytes)
    student=database.one('SELECT * FROM students WHERE student_id=?',(contract['student_id'],)) if contract['student_id'] else None
    if contract['student_id'] and student is None:raise ValueError('关联学生不存在。')
    if student_entry and student is not None and student['nas_status']!='ready':student=g['ensure_student_storage'](student)
    uid=contract['contract_uid'] or g['contract_uid_for_today']()
    folder=g['forms_folder_for_student'](student) if student else g['standalone_contract_folder'](uid,'final')
    target,version=g['next_version_path'](folder,g['final_contract_base_filename'](contract,student))
    date=g['local_company_signing_date']()
    placement=g['contract_stamp_placement']() if not manual else None
    attempt=g['CONTRACT_SIGNING_DIR']/str(cid)/('manual_final_'+uuid.uuid4().hex)
    attempt.mkdir(parents=True,exist_ok=False)
    try:
        copies=[]
        for name,data in zip(('source.pdf','company.png','stamp.png'),(source_bytes,*asset_bytes)):
            path=attempt/name;persist_verified_file(path,data,lambda path,digest:None);copies.append(path)
        if manual:
            count=len(g['PdfReader'](str(copies[0])).pages)
            def number(name,default=0):
                try:return float(handler.form.get(name,default))
                except (ValueError,TypeError):return float(default)
            placement={'page_number':max(1,min(count,int(number('page_number',count)))),
                'page_rotation':int(number('page_rotation'))%360,
                **{key:number(key) for key in ('stamp_x','stamp_y','signature_x','signature_y','date_x','date_y')},
                **{key:max(12,number(key,default)) for key,default in
                   (('stamp_width',90),('stamp_height',90),('signature_width',130),('signature_height',50))},
                'date_font_size':max(6,min(24,number('date_font_size',10)))}
        rendered=attempt/'final.pdf'
        if manual:g['apply_manual_aaie_signature_and_stamp'](copies[0],rendered,placement,date,asset_paths=copies[1:])
        else:g['apply_aaie_signature_and_stamp'](copies[0],rendered,date,asset_paths=copies[1:],placement_snapshot=placement)
        g['require_transfer_pdf_file'](contract,rendered)
        prepared={'student':student,'source':source,'target':target,'version':version,'placement':placement,
            'company_signing_date':date,'signature_path':assets[0],'stamp_path':assets[1]}
        def save(path,digest):
            def persist(view):
                fresh=database.one('SELECT * FROM contracts WHERE id=?',(cid,))
                if fresh is None or dict(fresh)!=dict(contract):raise ValueError('合同已更新，请刷新后重试。')
                if not contract['contract_uid']:database.execute('UPDATE contracts SET contract_uid=? WHERE id=?',(uid,cid))
                return operation(view,prepared)
            return command(persist)
        result=persist_verified_file(target,rendered.read_bytes(),save)
        if result.replayed:target.unlink(missing_ok=True)
        return result
    finally:
        try:shutil.rmtree(attempt)
        except OSError as error:g['append_service_log']('admin','contract_final_scratch_cleanup_failed',str(error),contract_id=cid)

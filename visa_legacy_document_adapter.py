"""Case-only projections of explicitly linked Student documents. No GET writes."""
from pathlib import Path
import hashlib
from visa_documents_candidate import _row,register_document
from visa_document_metadata import effective_metadata,list_rules,normalize_keyword,LEGACY


def linked_source(conn,case_id,student_document_id):
    case=_row(conn,'SELECT * FROM visa_cases WHERE id=? AND archived_at IS NULL',(case_id,))
    source=_row(conn,'SELECT * FROM student_documents WHERE id=?',(student_document_id,))
    columns={r[1] for r in conn.execute('PRAGMA table_info(visa_case_document_links)')}
    expr='COALESCE(student_document_id,document_id)' if {'student_document_id','document_id'}<=columns else 'student_document_id' if 'student_document_id' in columns else 'document_id'
    status=" AND COALESCE(status,'Linked') <> 'Removed'" if 'status' in columns else ''
    link=conn.execute(f'SELECT 1 FROM visa_case_document_links WHERE visa_case_id=? AND {expr}=?'+status,(case_id,student_document_id)).fetchone()
    if (not case or not source or not link or source['student_id']!=case['student_id'] or not source['is_active']
            or str(source.get('document_status') or 'active').lower() in {'voided','archived','deleted','inactive'}):
        raise ValueError('An active document explicitly linked to this Case Student is required.')
    return source


def legacy_document_view(conn,case_id,student_document_id):
    source=linked_source(conn,case_id,student_document_id)
    doc=_row(conn,'SELECT * FROM visa_case_documents WHERE visa_case_id=? AND student_document_id=?',(case_id,student_document_id))
    if doc:
        return dict(doc,**effective_metadata(conn,case_id,doc['id']),legacy_student_document_id=student_document_id,classification_preview=False)
    normalized=normalize_keyword(source['original_name']);matches=[r for r in list_rules(conn) if r['active'] and r['normalized_keyword'] in normalized]
    category=matches[0]['category'] if matches else LEGACY.get(source['document_type'],'other')
    return dict(id=student_document_id,original_name=source['original_name'],display_name=source['original_name'],effective_category=category,auto_category=category,manual_override=False,archived_at=None,legacy_student_document_id=student_document_id,classification_preview=True)


def register_linked_document(conn,case_id,student_document_id,*,storage_root,actor_user_id,occurred_at,for_deletion=False):
    source=linked_source(conn,case_id,student_document_id)
    path=Path(source['file_path']);root=Path(storage_root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():raise ValueError('Source file is unavailable or outside the Student storage authority.')
    try:digest=hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:raise ValueError('The source file is unavailable for registration.') from exc
    return register_document(conn,case_id,student_document_id=student_document_id,file_path=source['file_path'],original_name=source['original_name'],version=source['version'],sha256=digest,actor_user_id=actor_user_id,occurred_at=occurred_at,for_deletion=for_deletion)


def official_case_projection(conn,case):
    """Read retained Case decision, or exact Case/Student/Cycle canonical record."""
    import json
    result={'grant_date':'','length_of_stay':'','expiry_date':'','source_href':''}
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'visa_case_confirmed_decisions' in tables:
        retained=_row(conn,'SELECT * FROM visa_case_confirmed_decisions WHERE visa_case_id=?',(case['id'],))
        if retained and retained.get('decision_type')!='grant':return result
        if retained and retained.get('decision_type')=='grant':
            values=json.loads(retained['confirmed_json'])
            return dict(grant_date=retained['decision_date'] or '',length_of_stay=values.get('length_of_stay') or '',expiry_date=values.get('expiry_date') or '',source_href=f'/visa-management/cases/{int(case["id"])}/workspace#visa-result')
    if {'visa_decision_events','visas'}<=tables and case.get('student_id') and case.get('application_cycle_id'):
        rows=conn.execute('''SELECT e.decision_date,v.visa_expiry_date,v.id
            FROM visa_decision_events e JOIN visas v ON v.id=e.visa_record_id AND v.student_id=e.student_id
            WHERE e.visa_case_id=? AND e.student_id=? AND e.application_cycle_id=? AND e.decision_type='grant' ''',
            (case['id'],case['student_id'],case['application_cycle_id'])).fetchall()
        if len(rows)==1:
            row=rows[0];result.update(grant_date=row[0] or '',expiry_date=row[1] or '',source_href=f'/visas/{int(row[2])}/preview')
    return result


def read_legacy_letter_text(path,pdf_reader):
    """Expected corrupt/unavailable evidence is a reviewable validation error."""
    from pypdf.errors import PdfReadError
    path=Path(path)
    try:
        if path.suffix.lower()=='.pdf':return '\n'.join(page.extract_text() or '' for page in pdf_reader(str(path)).pages)
        if path.suffix.lower()=='.txt':return path.read_text(encoding='utf-8')
        raise ValueError('Use a readable PDF or UTF-8 text letter for deterministic extraction.')
    except (PdfReadError,OSError,UnicodeError) as exc:
        raise ValueError('The source letter cannot be read. Review the original file before extraction.') from exc

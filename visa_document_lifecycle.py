"""Read-only, case-owned active evidence and upload controls. No schema or canonical writes."""
import hashlib
import json
from pathlib import Path
from visa_documents_candidate import _row, _rows, propose_letter
from visa_document_metadata import LEGACY, effective_metadata
from visa_processing_dates import parse_processing_dates, processing_summary, _field, _metrics

SINGLE_ACTIVE = {'grant_letter', 'immi_acknowledgment'}
KIND_CATEGORY = {'grant':'grant_letter', 'refusal':'refusal_letter', 'lodgement_receipt':'immi_acknowledgment', 'medical':'health_examination'}
INACTIVE = {'pending', 'archived', 'deleted', 'voided', 'inactive', 'pending_review'}
CANONICAL_MATERIAL = {
    'IMMI Grant Letter': 'grant_letter',
    'IMMI Acknowledge Letter': 'immi_acknowledgment',
    'IMMI Acknowledgment Letter': 'immi_acknowledgment',
    'IMMI Acknowledgement Letter': 'immi_acknowledgment',
    'Health Examination': 'health_examination',
}


def material_category(link, source):
    """Saved checklist scope/type is evidence; filenames and OCR are never types."""
    if (link.get('document_category') != 'Visa Result / S56'
            or link.get('used_in_this_case') != 1
            or str(link.get('status') or 'Linked').lower() != 'linked'):
        return None
    kind = link.get('document_type')
    if kind == 'IMMI Grant Letter / IMMI Refusal Letter':
        # This checklist slot contains either outcome. The saved upload type
        # disambiguates it; the combined slot itself does not confirm a Grant.
        return 'grant_letter' if source.get('document_type') == 'VisaGrantLetter' else None
    return CANONICAL_MATERIAL.get(kind)


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def active_documents(conn, case_id):
    case = _row(conn, 'SELECT * FROM visa_cases WHERE id=?', (case_id,))
    if not case or case.get('archived_at'):
        return []
    present = tables(conn)
    registered = _rows(conn, 'SELECT * FROM visa_case_documents WHERE visa_case_id=?', (case_id,)) if 'visa_case_documents' in present else []
    owned = {d['student_document_id'] for d in registered if d.get('student_document_id')}
    candidates = [d for d in registered if not d.get('archived_at')]
    links = {}
    if 'visa_case_document_links' in present:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(visa_case_document_links)')}
        expr = 'COALESCE(student_document_id,document_id)' if {'student_document_id','document_id'} <= cols else 'student_document_id' if 'student_document_id' in cols else 'document_id'
        for link in _rows(conn, f'SELECT *, {expr} AS sid FROM visa_case_document_links WHERE visa_case_id=?', (case_id,)):
            links.setdefault(link['sid'], []).append(link)
            if (str(link.get('status') or 'Linked').lower() != 'linked'
                    or link.get('used_in_this_case', 1) != 1):
                continue
            if link['sid'] in owned:
                continue
            source = _row(conn, 'SELECT * FROM student_documents WHERE id=? AND student_id=?', (link['sid'],case.get('student_id')))
            if source:
                candidates.append(dict(source, id=None, student_document_id=source['id'], visa_case_id=case_id))
                owned.add(source['id'])
    result = []
    for doc in candidates:
        sid = doc.get('student_document_id')
        source = _row(conn, 'SELECT * FROM student_documents WHERE id=?', (sid,)) if sid else None
        if sid and (not source or source['student_id'] != case.get('student_id') or not source.get('is_active') or str(source.get('document_status') or 'active').lower() in INACTIVE):
            continue
        category = LEGACY.get(source.get('document_type'),'other') if source else 'other'
        linked = links.get(sid, [])
        confirmed = {material_category(link, source or {}) for link in linked} - {None}
        authority_category = next(iter(confirmed)) if len(confirmed) == 1 else None
        selected_category = None
        manual_category=False
        if doc.get('id') and 'visa_document_metadata' in present:
            metadata=effective_metadata(conn, case_id, doc['id'])
            category=metadata['effective_category'];manual_category=metadata['manual_override']
        if doc.get('id'):
            selected = _row(conn, "SELECT after_json FROM visa_document_audit WHERE visa_case_id=? AND document_id=? AND action='document_category_selected' ORDER BY id DESC LIMIT 1", (case_id,doc['id'])) if 'visa_document_audit' in present else None
            if selected and not manual_category:
                category = json.loads(selected['after_json'])['category']
            selected_category = category if manual_category or (selected and json.loads(selected['after_json']).get('confirmed') is True) else None
        if selected_category is not None:
            # Dedicated case workspace classification is a staff confirmation.
            # For checklist-backed files it cannot turn Draft/Other/history into
            # Visa Result material or resurrect a removed/unused link.
            valid_scope = any(link.get('document_category') == 'Visa Result / S56'
                and link.get('used_in_this_case') == 1
                and str(link.get('status') or 'Linked').lower() == 'linked'
                and link.get('document_type') not in {'Draft', 'Other'} for link in linked)
            authority_category = selected_category if not linked or valid_scope else None
        result.append(dict(doc, category=category,manual_category=manual_category,
                           authority_category=authority_category))
    return result


def safe_text(conn, case, doc):
    """Read only the exact file inside the recorded owner's storage directory."""
    if not doc.get('file_path'):return ''
    path = Path(doc['file_path'])
    if not case.get('student_id'):
        return ''
    cols = {r[1] for r in conn.execute('PRAGMA table_info(students)')}
    if 'folder_path' not in cols:
        return ''
    student = _row(conn, 'SELECT folder_path FROM students WHERE student_id=?', (case['student_id'],))
    if not student or not student['folder_path']:
        return ''
    root = Path(student['folder_path']).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        return ''
    if doc.get('sha256') and hashlib.sha256(path.read_bytes()).hexdigest()!=doc['sha256']:return ''
    if path.suffix.lower() == '.txt':
        return path.read_text(encoding='utf-8')
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        return '\n'.join(p.extract_text() or '' for p in PdfReader(str(path)).pages)
    return ''


def check_upload(conn, case_id, digest, category=None, *, read_bytes=None, exclude_source_id=None, exclude_document_id=None):
    for doc in active_documents(conn, case_id):
        if exclude_document_id and doc.get('id')==exclude_document_id:continue
        if exclude_source_id and doc.get('student_document_id') == exclude_source_id:
            continue
        existing_hash = doc.get('sha256')
        if not existing_hash and read_bytes:
            existing_hash = hashlib.sha256(read_bytes(doc)).hexdigest()
        if digest and existing_hash and existing_hash == digest:
            raise ValueError('File already exists / 文件已存在')
        if category in SINGLE_ACTIVE and doc['authority_category'] == category:
            raise ValueError('Current Visa Case already has this document; soft-delete the old file first.')


def case_projection(conn, case_id, *, read_text=None):
    case = _row(conn, 'SELECT * FROM visa_cases WHERE id=?', (case_id,))
    docs = active_documents(conn, case_id)
    present = tables(conn)
    processing = processing_summary(conn,case_id) if 'visa_processing_date_revisions' in present else {'fields':{k:_field() for k in ('acknowledge_sent_on','health_exam_date','grant_sent_on','medical_completed_date')}}
    choices = {k:[] for k in processing['fields']}
    grants = []
    ack = False
    for doc in docs:
        category = doc['authority_category']
        if category not in {'grant_letter','immi_acknowledgment','health_examination'}:
            continue
        text = ''
        try:
            text = read_text(doc) if read_text else safe_text(conn,case,doc)
        except (OSError, ValueError, UnicodeError):
            pass
        proposal = propose_letter(text) if text else None
        if not proposal and doc.get('id') and 'visa_letter_extractions' in present:
            extraction = _row(conn,'SELECT proposed_json FROM visa_letter_extractions WHERE visa_case_id=? AND document_id=? ORDER BY id DESC LIMIT 1',(case_id,doc['id']))
            proposal = json.loads(extraction['proposed_json']) if extraction else None
        if category == 'grant_letter':
            grants.append((doc,proposal if proposal and proposal.get('document_kind') == 'grant' else {}))
        ack = ack or category == 'immi_acknowledgment'
        if text:
            parsed = parse_processing_dates(text)['fields']
            field_key = {'grant_letter':'grant_sent_on','immi_acknowledgment':'acknowledge_sent_on','health_examination':'health_exam_date'}.get(category)
            if field_key and parsed[field_key]['status'] != 'not_detected':
                choices[field_key].append(dict(parsed[field_key],document_id=doc.get('id')))
    for key, found in choices.items():
        if processing['fields'][key]['status'] == 'manual':
            continue
        if len(found) == 1:
            processing['fields'][key] = found[0]
        elif found:
            processing['fields'][key] = _field(status='ambiguous')
    metrics, validation = _metrics(processing['fields']);processing.update(metrics,validation=validation)
    active_ids={d.get('id') for d in docs if d.get('id')}
    for key, field in processing['fields'].items():
        if field['status'] != 'manual' and not choices[key] and field.get('document_id') not in active_ids:
            processing['fields'][key]=_field()
    metrics, validation = _metrics(processing['fields']);processing.update(metrics,validation=validation)
    grant_date = grants[0][1].get('decision_date') or '' if len(grants)==1 else ''
    auto_grant_date=grant_date
    # Exact retained decisions are staff-confirmed. Preserve dates which were
    # entered without a matching machine proposal; do not retain deleted auto dates.
    from visa_legacy_document_adapter import official_case_projection
    retained=official_case_projection(conn,case or {}) if case else {}
    retained_date=retained.get('grant_date') or ''
    proposal=None
    retained_decision=_row(conn,'SELECT extraction_id FROM visa_case_confirmed_decisions WHERE visa_case_id=?',(case_id,)) if 'visa_case_confirmed_decisions' in present else None
    if retained_decision:
        proposal=_row(conn,'SELECT proposed_json FROM visa_letter_extractions WHERE id=? AND visa_case_id=?',(retained_decision['extraction_id'],case_id))
    elif retained_date and {'visa_decision_events','visa_letter_extractions'} <= present:
        proposal=_row(conn,"""SELECT x.proposed_json FROM visa_decision_events e JOIN visa_case_documents d ON d.visa_case_id=e.visa_case_id AND d.student_document_id=e.student_document_id JOIN visa_letter_extractions x ON x.document_id=d.id AND x.visa_case_id=e.visa_case_id WHERE e.visa_case_id=? AND e.student_id=? AND e.application_cycle_id=? AND e.decision_type='grant' ORDER BY x.id LIMIT 1""" ,(case_id,case.get('student_id'),case.get('application_cycle_id')))
    machine_date=json.loads(proposal['proposed_json']).get('decision_date') if proposal else None
    if grants and retained_date and retained_date != machine_date:
        grant_date=retained_date
    # Existing staff edits are identifiable in retained reviews by their delta
    # from the original extraction, and survive deleting that source.
    if {'visa_letter_reviews','visa_letter_extractions'} <= present:
        reviews=_rows(conn, 'SELECT r.confirmed_json,e.proposed_json,e.document_id FROM visa_letter_reviews r JOIN visa_letter_extractions e ON e.id=r.extraction_id AND e.visa_case_id=r.visa_case_id WHERE r.visa_case_id=? ORDER BY r.id',(case_id,))
        for review in reviews:
            before=json.loads(review['proposed_json']);after=json.loads(review['confirmed_json'])
            if review['document_id'] in {d.get('id') for d, _ in grants} and after.get('document_kind')=='grant' and after.get('decision_date') and after.get('decision_date') != before.get('decision_date'):
                grant_date=after['decision_date']
    # Date-only corrections use the existing append-only document audit.
    manual = _row(conn, "SELECT after_json FROM visa_document_audit WHERE visa_case_id=? AND action='grant_date_corrected' ORDER BY id DESC LIMIT 1", (case_id,)) if 'visa_document_audit' in present else None
    if grants and manual:
        grant_date = json.loads(manual['after_json']).get('value') or auto_grant_date
    result = 'Granted' if grants else 'Pending'
    return {'documents':docs,'workflow_status':'已完成' if grants else '审理中' if ack else '未开始','result_status':result,'grant_date':grant_date,'processing':processing}


def student_projection(conn, student_id):
    if 'visa_case_documents' not in tables(conn):
        return None
    # An explicit active cycle constrains the current Case. Closed latest cycle is
    # used only when there is no active cycle; historical grants cannot leak in.
    cycle = _row(conn,"SELECT id FROM application_cycles WHERE student_id=? ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END,opened_at DESC,id DESC LIMIT 1",(student_id,)) if 'application_cycles' in tables(conn) else None
    sql = 'SELECT * FROM visa_cases WHERE student_id=? AND archived_at IS NULL'
    args = [student_id]
    if cycle:
        sql += ' AND application_cycle_id=?';args.append(cycle['id'])
    cols = {r[1] for r in conn.execute('PRAGMA table_info(visa_cases)')}
    if 'visa_subclass' in cols:sql += " AND COALESCE(visa_subclass,'500') <> '590'"
    sql += ' ORDER BY '+('created_at DESC,' if 'created_at' in cols else '')+'id DESC'
    cases = _rows(conn,sql,args)
    return case_projection(conn,cases[0]['id']) if cases else None

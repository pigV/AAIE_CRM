"""Read-only, case-owned active evidence and upload controls. No schema or canonical writes."""
import hashlib
import json
from pathlib import Path
from visa_documents_candidate import _row, _rows, propose_letter
from visa_document_metadata import LEGACY
from visa_processing_dates import parse_processing_dates, processing_summary, _field, _metrics
from snapshot_read_cache import memoized_read

SINGLE_ACTIVE = {'grant_letter', 'immi_acknowledgment'}
KIND_CATEGORY = {'bridging_visa':'bridging_visa', 'grant':'grant_letter', 'refusal':'refusal_letter', 'lodgement_receipt':'immi_acknowledgment', 'medical':'health_examination'}
INACTIVE = {'pending', 'archived', 'deleted', 'voided', 'inactive', 'pending_review'}
CANONICAL_MATERIAL = {
    'IMMI Grant Letter': 'grant_letter',
    'IMMI Acknowledge Letter': 'immi_acknowledgment',
    'IMMI Acknowledgment Letter': 'immi_acknowledgment',
    'IMMI Acknowledgement Letter': 'immi_acknowledgment',
    'Health Examination': 'health_examination',
}


def material_category(link, source, *, manual_grant=False):
    """Saved checklist scope/type is evidence; filenames and OCR are never types."""
    if (link.get('document_category') != 'Visa Result / S56'
            or link.get('used_in_this_case') != 1
            or str(link.get('status') or 'Linked').lower() != 'linked'):
        return None
    kind = link.get('document_type')
    if kind == 'IMMI Grant Letter / IMMI Refusal Letter':
        # This checklist slot contains either outcome. The saved upload type
        # disambiguates it; the combined slot itself does not confirm a Grant.
        return 'grant_letter' if source.get('document_type') == 'VisaGrantLetter' or (
            manual_grant and source.get('document_type') == 'VisaMaterialChecklist') else None
    return CANONICAL_MATERIAL.get(kind)


def tables(conn):
    return memoized_read(conn, ('visa_schema_tables',), lambda: frozenset(
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")))


def _columns(conn, table):
    return memoized_read(conn, ('visa_schema_columns', table), lambda: frozenset(
        row[1] for row in conn.execute(f'PRAGMA table_info({table})')))


def _batches(values):
    values = tuple(dict.fromkeys(values))
    for offset in range(0, len(values), 400):
        batch = values[offset:offset + 400]
        yield batch, ','.join('?' for _ in batch)


def active_documents(conn, case_id):
    # Workflow status and the student's grant date both read this evidence.
    # Reuse it only within one read-only snapshot and keep mutable rows private.
    documents = memoized_read(conn, ('visa_active_documents', case_id),
                              lambda: _active_documents(conn, case_id))
    return [dict(document) for document in documents]


def _active_documents(conn, case_id):
    case = _row(conn, 'SELECT * FROM visa_cases WHERE id=?', (case_id,))
    if not case or case.get('archived_at'):
        return []
    present = tables(conn)
    from visa_business_state import audited_legacy_manual_grant, legacy_manual_source_matches
    manual = audited_legacy_manual_grant(conn, case)
    registered = _rows(conn, 'SELECT * FROM visa_case_documents WHERE visa_case_id=?', (case_id,)) if 'visa_case_documents' in present else []
    owned = {doc['student_document_id'] for doc in registered if doc.get('student_document_id')}
    candidates = [doc for doc in registered if not doc.get('archived_at')]
    links = {}
    unregistered_sources = []
    if 'visa_case_document_links' in present:
        cols = _columns(conn, 'visa_case_document_links')
        expr = 'COALESCE(student_document_id,document_id)' if {'student_document_id','document_id'} <= cols else 'student_document_id' if 'student_document_id' in cols else 'document_id'
        for link in _rows(conn, f'SELECT *, {expr} AS sid FROM visa_case_document_links WHERE visa_case_id=?', (case_id,)):
            source_id = link['sid']
            links.setdefault(source_id, []).append(link)
            if (source_id is not None and source_id not in owned
                    and str(link.get('status') or 'Linked').lower() == 'linked'
                    and link.get('used_in_this_case', 1) == 1):
                unregistered_sources.append(source_id)

    source_ids = [doc['student_document_id'] for doc in candidates if doc.get('student_document_id')]
    sources = {}
    for batch, marks in _batches(source_ids + unregistered_sources):
        sources.update((row['id'], row) for row in _rows(
            conn, f'SELECT * FROM student_documents WHERE id IN ({marks}) AND student_id IS ?',
            (*batch, case.get('student_id'))))
    legacy_sources = dict.fromkeys(unregistered_sources) if case.get('student_id') is not None else {}
    for source_id in legacy_sources:
        if source := sources.get(source_id):
            candidates.append(dict(source, id=None, student_document_id=source['id'], visa_case_id=case_id))

    metadata = {}
    selections = {}
    if candidates and registered:
        if 'visa_document_metadata' in present:
            metadata = {row['document_id']: row for row in _rows(conn, """
                SELECT meta.document_id,meta.manual_category,meta.auto_category
                FROM visa_case_documents doc JOIN visa_document_metadata meta ON meta.document_id=doc.id
                WHERE doc.visa_case_id=? AND COALESCE(doc.archived_at,'')=''
            """, (case_id,))}
        if 'visa_document_audit' in present:
            selections = {row['document_id']: row for row in _rows(conn, """
                SELECT audit.document_id,audit.after_json
                FROM visa_document_audit audit
                JOIN (
                    SELECT MAX(id) id FROM visa_document_audit
                    WHERE visa_case_id=? AND action='document_category_selected' AND document_id IS NOT NULL
                    GROUP BY document_id
                ) latest ON latest.id=audit.id
            """, (case_id,))}

    result = []
    for doc in candidates:
        sid = doc.get('student_document_id')
        source = sources.get(sid)
        if sid and (not source or source['student_id'] != case.get('student_id')
                    or not source.get('is_active') or source.get('voided_at')
                    or str(source.get('document_status') or 'active').lower() in INACTIVE):
            continue
        category = LEGACY.get(source.get('document_type'), 'other') if source else 'other'
        linked = links.get(sid, [])
        manual_grant = bool(manual and legacy_manual_source_matches(conn, case, sid, manual=manual))
        confirmed = {material_category(link, source or {}, manual_grant=manual_grant) for link in linked} - {None}
        authority_category = next(iter(confirmed)) if len(confirmed) == 1 else None
        meta = metadata.get(doc.get('id'), {})
        manual_category = meta.get('manual_category') is not None
        category = meta.get('manual_category') or meta.get('auto_category') or category
        selected_row = selections.get(doc.get('id'))
        selected = None
        if selected_row and not manual_category:
            selected = json.loads(selected_row['after_json'])
            category = selected['category']
        selected_category = category if manual_category or (selected and selected.get('confirmed') is True) else None
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


class DuplicateVisaUpload(ValueError):
    def __init__(self, document):
        super().__init__('File already exists / 文件已存在')
        self.document = document


def check_upload(conn, case_id, digest, category=None, *, read_bytes=None, exclude_source_id=None, exclude_document_id=None):
    for doc in active_documents(conn, case_id):
        if exclude_document_id and doc.get('id')==exclude_document_id:continue
        if exclude_source_id and doc.get('student_document_id') == exclude_source_id:
            continue
        existing_hash = doc.get('sha256')
        if not existing_hash and read_bytes:
            existing_hash = hashlib.sha256(read_bytes(doc)).hexdigest()
        if digest and existing_hash and existing_hash == digest:
            raise DuplicateVisaUpload(doc)
        if category in SINGLE_ACTIVE and doc['authority_category'] == category:
            raise ValueError('Current Visa Case already has this document; soft-delete the old file first.')


def _latest_letter_extraction(conn, case_id, document_id):
    return _row(conn, '''
        SELECT proposed_json FROM visa_letter_extractions
        WHERE visa_case_id=? AND document_id=? ORDER BY id DESC LIMIT 1
    ''', (case_id, document_id))


def case_projection(conn, case_id, *, read_text=None, grant_date_only=False):
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
        if grant_date_only and category != 'grant_letter':
            continue
        saved_proposal = None
        can_load_extraction = doc.get('id') and 'visa_letter_extractions' in present
        if grant_date_only and can_load_extraction:
            # Read the proposal for this exact active document before opening its file.
            extraction = _latest_letter_extraction(conn, case_id, doc['id'])
            saved_proposal = json.loads(extraction['proposed_json']) if extraction else None
        has_saved_grant_date = (
            saved_proposal and saved_proposal.get('document_kind') == 'grant'
            and saved_proposal.get('decision_date')
        )
        text = ''
        if not has_saved_grant_date:
            try:
                text = read_text(doc) if read_text else safe_text(conn, case, doc)
            except (OSError, ValueError, UnicodeError):
                pass
        proposal = (propose_letter(text) if text else None) or saved_proposal
        if not proposal and can_load_extraction and not grant_date_only:
            extraction = _latest_letter_extraction(conn, case_id, doc['id'])
            proposal = json.loads(extraction['proposed_json']) if extraction else None
        if category == 'grant_letter':
            grants.append((doc,proposal if proposal and proposal.get('document_kind') == 'grant' else {}))
        ack = ack or category == 'immi_acknowledgment'
        if text and not grant_date_only:
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
    # Current active evidence supplies missing display fields without confirming
    # an official decision or overwriting retained staff-reviewed values.
    official = dict(retained)
    if len(grants) == 1:
        document, parsed = grants[0]
        original_values = json.loads(proposal['proposed_json']) if proposal else {}
        for key in ('length_of_stay', 'expiry_date'):
            kept = retained.get(key) or ''
            # As with the grant date, a delta from the original extraction is
            # a staff correction. Automatic values follow the active letter.
            staff_corrected = kept and (not original_values.get(key) or kept != original_values[key])
            official[key] = kept if staff_corrected else parsed.get(key) or ''
        if not official.get('source_href'):
            if document.get('student_document_id'):
                official['source_href'] = f'/documents/{int(document["student_document_id"])}/download'
            elif document.get('id'):
                official['source_href'] = f'/visa-management/cases/{int(case_id)}/documents/{int(document["id"])}/file'
    official['grant_date'] = grant_date
    acknowledgement = choices['acknowledge_sent_on']
    acknowledge_letter_date = acknowledgement[0]['value'] if len(acknowledgement) == 1 and acknowledgement[0]['status'] == 'detected' else ''
    result = 'Granted' if grants else 'Pending'
    if not grants:
        from visa_business_state import confirmed_legacy_outcome
        historical = confirmed_legacy_outcome(conn, case or {})
        if historical:
            result = historical['result']
            grant_date = historical['grant_date']
            official['grant_date'] = grant_date
    # Explicit, audited staff outcomes survive material projection. A bare
    # legacy case.status is not an override, and a Granted override still
    # requires current confirmed Grant evidence after a source is withdrawn.
    if 'visa_result_status_current' in present:
        current = _row(conn, 'SELECT * FROM visa_result_status_current WHERE visa_case_id=?', (case_id,))
        if current and current.get('manual_override'):
            manual_result = current.get('effective_result')
            if manual_result in {'Pending', 'Refused', 'Withdrawn'}:
                result = manual_result
            elif manual_result == 'Granted' and grants:
                result = 'Granted'
    from business_changes.runtime import enabled
    unified=enabled(conn)
    auto_detected_result='Granted' if any(proposal.get('document_kind')=='grant' and proposal.get('decision_type')=='grant'
        for document,proposal in grants) else None
    if unified and result=='Granted':
        from visa_business_state import has_confirmed_current_grant,confirmed_legacy_outcome
        independent=confirmed_legacy_outcome(conn,case or {})
        if not has_confirmed_current_grant(conn,case or {},[document for document,proposal in grants]) and not (
                independent and independent['result']=='Granted'):
            result='Pending'
    if result != 'Granted':
        grant_date = ''
        official['grant_date'] = ''
    if unified:
        from visa_business_state import formal_lodgement
        ack=ack or formal_lodgement(case or {})
        recorded_grant_date=(json.loads(manual['after_json']).get('value') or '') if manual else (
            grant_date or (case or {}).get('grant_date') or '')
    return {'documents':docs,'workflow_status':'已完成' if result in {'Granted','Refused','Withdrawn'} else '审理中' if ack else '未开始','result_status':result,'grant_date':grant_date,'official_decision':official,'acknowledge_letter_date':acknowledge_letter_date,'processing':processing,
            **({'auto_detected_result':auto_detected_result,'recorded_grant_date':recorded_grant_date} if unified else {})}


def _student_case_ids(conn, student_ids):
    """Choose the same current case(s) for the detail and list projections."""
    if not student_ids:
        return {}
    present = tables(conn)
    if 'visa_case_documents' not in present:
        return {}
    cols = _columns(conn, 'visa_cases')
    subclass = "AND COALESCE(visa.visa_subclass,'500') <> '590'" if 'visa_subclass' in cols else ''
    order = 'visa.created_at DESC,' if 'created_at' in cols else ''
    cycle = """(
        SELECT id FROM application_cycles
        WHERE student_id=requested.student_id
        ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END,opened_at DESC,id DESC LIMIT 1
    )""" if 'application_cycles' in present else 'NULL'
    cycle_scope = ('AND (scoped.cycle_id IS NULL OR visa.application_cycle_id=scoped.cycle_id)'
                   if 'application_cycles' in present else '')
    selected = {}
    for ids, _ in _batches(student_ids):
        values = ','.join('(?)' for _ in ids)
        for student_id, case_id in conn.execute(f"""
            WITH requested(student_id) AS (VALUES {values}), scoped AS (
                SELECT student_id,{cycle} AS cycle_id FROM requested
            )
            SELECT scoped.student_id,(
                SELECT visa.id FROM visa_cases visa
                WHERE visa.student_id=scoped.student_id AND visa.archived_at IS NULL
                {cycle_scope} {subclass}
                ORDER BY {order}visa.id DESC LIMIT 1
            )
            FROM scoped
        """, ids):
            if case_id is not None:
                selected[student_id] = case_id
    return selected


def student_grant_date_map(conn, student_ids):
    """Read list dates from saved extractions, with an exact legacy Grant-file fallback."""
    return {student_id: case_projection(conn, case_id, grant_date_only=True)['grant_date']
            for student_id, case_id in _student_case_ids(conn, student_ids).items()}


def student_projection(conn, student_id):
    case_id = _student_case_ids(conn, (student_id,)).get(student_id)
    return case_projection(conn, case_id) if case_id is not None else None

"""Case-owned visa evidence; explicit review precedes canonical decision writes.

All database access uses the caller's connection. This module never opens a DB,
creates schema, reads/writes physical files, or calls a network/AI provider.
Route handlers enforce the Visa module permission and CSRF before calling it.
Extraction is deliberately labelled deterministic: suggestions are not decisions.
"""
from contextlib import contextmanager
from datetime import date, datetime
import json
import re

from workflow_visa_events import record_first_valid_visa_decision


def _row(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), row)) if row is not None else None


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def _atomic(conn):
    conn.execute('SAVEPOINT visa_document_action')
    try:
        yield
        conn.execute('RELEASE SAVEPOINT visa_document_action')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT visa_document_action')
        conn.execute('RELEASE SAVEPOINT visa_document_action')
        raise


def _case(conn, case_id):
    case = _row(conn, 'SELECT * FROM visa_cases WHERE id=?', (int(case_id),))
    if case is None or case.get('archived_at'):
        raise ValueError('Active Visa Case is required')
    return case


def _actor(conn, actor_user_id, occurred_at):
    if isinstance(actor_user_id, bool) or not _row(conn, 'SELECT id FROM users WHERE id=?', (actor_user_id,)):
        raise ValueError('Recorded staff identity is required')
    try:
        datetime.fromisoformat(occurred_at)
    except (ValueError, TypeError):
        raise ValueError('Valid recorded time is required') from None


def _document(conn, case_id, document_id, *, allow_archived=False):
    _case(conn, case_id)
    document = _row(conn, 'SELECT * FROM visa_case_documents WHERE id=? AND visa_case_id=?', (int(document_id), int(case_id)))
    if document is None or (document['archived_at'] and not allow_archived):
        raise ValueError('Document does not belong to this active case document set')
    return document


def _student_cycle_id(conn, case):
    """Resolve the case's explicit cycle, or one unambiguous active cycle."""
    if not case.get('student_id'):
        return None
    if case.get('application_cycle_id'):
        return int(case['application_cycle_id'])
    try:
        rows = _rows(conn, "SELECT id FROM application_cycles WHERE student_id=? AND status='active' ORDER BY id DESC", (case['student_id'],))
    except Exception:
        rows = []
    if len(rows) == 1:
        return int(rows[0]['id'])
    if not rows:
        raise ValueError('Student-linked decision requires an active Application Cycle')
    raise ValueError('Student-linked decision requires one selected active Application Cycle')


def _audit(conn, case_id, document_id, action, before, after, actor_user_id, occurred_at):
    conn.execute('''INSERT INTO visa_document_audit(visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at)
                    VALUES(?,?,?,?,?,?,?)''', (case_id, document_id, action, _json(before), _json(after), actor_user_id, occurred_at))


def register_document(conn, case_id, *, file_path, original_name, version, sha256,
                      actor_user_id, occurred_at, student_document_id=None, category=None, for_deletion=False,
                      category_confirmed=True):
    """Register a caller-verified immutable physical version; do not copy the file."""
    case = _case(conn, case_id)
    _actor(conn, actor_user_id, occurred_at)
    if not str(file_path).startswith('/') or not str(original_name).strip() or int(version) < 1:
        raise ValueError('Absolute verified file path, name and positive version are required')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', str(sha256)):
        raise ValueError('SHA256 of the physical version is required')
    sha256 = str(sha256).lower()
    if student_document_id is not None:
        source = _row(conn, 'SELECT * FROM student_documents WHERE id=?', (int(student_document_id),))
        if (source is None or not case.get('student_id') or source['student_id'] != case['student_id']
                or not source['is_active'] or source.get('document_status', 'active') in {'voided','archived','deleted','inactive'}
                or source['file_path'] != file_path or int(source['version']) != int(version)):
            raise ValueError('Existing student document ownership/version is not valid for this case')
        existing = _row(conn, 'SELECT * FROM visa_case_documents WHERE visa_case_id=? AND student_document_id=?', (case_id, student_document_id))
    else:
        existing = _row(conn, 'SELECT * FROM visa_case_documents WHERE visa_case_id=? AND file_path=? AND version=?', (case_id, file_path, version))
    if for_deletion and student_document_id is None:raise ValueError("Deletion registration requires an owned legacy Student document.")
    if existing:
        if any(existing[key] != value for key, value in [('file_path',file_path),('version',int(version)),('sha256',sha256)]):
            raise ValueError('An existing physical version cannot be retargeted')
        return existing
    from visa_document_lifecycle import check_upload
    if not for_deletion:check_upload(conn, case_id, sha256, category, exclude_source_id=student_document_id)
    with _atomic(conn):
        cursor = conn.execute('''INSERT INTO visa_case_documents(visa_case_id,student_document_id,file_path,original_name,version,sha256,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)''', (case_id, student_document_id, file_path, original_name, int(version), sha256, actor_user_id, occurred_at))
        document = _row(conn, 'SELECT * FROM visa_case_documents WHERE id=?', (cursor.lastrowid,))
        _audit(conn, case_id, document['id'], 'document_registered', {}, document, actor_user_id, occurred_at)
        if category:
            from visa_document_metadata import CATEGORIES
            if category not in CATEGORIES: raise ValueError('Invalid Visa document category')
            _audit(conn, case_id, document['id'], 'document_category_selected', {},
                   {'category':category,'confirmed':category_confirmed is True}, actor_user_id, occurred_at)
        if for_deletion:
            document=set_document_archived(conn,case_id,document['id'],archived=True,actor_user_id=actor_user_id,occurred_at=occurred_at)
        elif conn.execute("SELECT 1 FROM sqlite_master WHERE name='visa_document_metadata'").fetchone():
            from visa_document_metadata import classify_document
            classify_document(conn, case_id, document['id'], actor_user_id=actor_user_id, occurred_at=occurred_at)
    return document


def mark_submitted(conn, case_id, document_id, *, actor_user_id, occurred_at, supersedes_id=None):
    """Pin the exact prepared version; superseding is explicit and retains history."""
    _actor(conn, actor_user_id, occurred_at)
    document = _document(conn, case_id, document_id)
    existing = _row(conn, 'SELECT * FROM visa_immi_submissions WHERE visa_case_id=? AND document_id=?', (case_id, document_id))
    if existing:
        if existing['status'] != 'submitted' or existing['supersedes_id'] != supersedes_id:
            raise ValueError('Submitted history cannot be rewritten or reactivated implicitly')
        return existing
    previous = None
    if supersedes_id is not None:
        previous = _row(conn, "SELECT * FROM visa_immi_submissions WHERE id=? AND visa_case_id=? AND status='submitted'", (supersedes_id, case_id))
        if previous is None:
            raise ValueError('Superseded submission must be a current submission in this case')
    with _atomic(conn):
        cursor = conn.execute('''INSERT INTO visa_immi_submissions(visa_case_id,document_id,file_path,version,sha256,status,submitted_by,submitted_at,supersedes_id)
            VALUES(?,?,?,?,?,'submitted',?,?,?)''', (case_id, document_id, document['file_path'], document['version'], document['sha256'], actor_user_id, occurred_at, supersedes_id))
        submission = _row(conn, 'SELECT * FROM visa_immi_submissions WHERE id=?', (cursor.lastrowid,))
        if previous:
            conn.execute("UPDATE visa_immi_submissions SET status='superseded' WHERE id=?", (supersedes_id,))
        _audit(conn, case_id, document_id, 'document_submitted', previous or {}, submission, actor_user_id, occurred_at)
    return submission


def set_document_archived(conn, case_id, document_id, *, archived, actor_user_id, occurred_at):
    _actor(conn, actor_user_id, occurred_at)
    if not isinstance(archived, bool):
        raise ValueError('Archive flag must be a boolean')
    document = _document(conn, case_id, document_id, allow_archived=True)
    if bool(document['archived_at']) == archived:
        return document
    with _atomic(conn):
        conn.execute('UPDATE visa_case_documents SET archived_at=?,archived_by=? WHERE id=? AND visa_case_id=?',
                     (occurred_at if archived else None, actor_user_id if archived else None, document_id, case_id))
        result = _document(conn, case_id, document_id, allow_archived=True)
        _audit(conn, case_id, document_id, 'document_archived' if archived else 'document_restored', document, result, actor_user_id, occurred_at)
    return result


def submitted_documents(conn, case_id):
    """The only source for final submission analysis; never resolve latest version."""
    _case(conn, case_id)
    return _rows(conn, '''SELECT s.*,d.original_name,d.student_document_id FROM visa_immi_submissions s
        JOIN visa_case_documents d ON d.id=s.document_id AND d.visa_case_id=s.visa_case_id
        WHERE s.visa_case_id=? AND s.status='submitted' AND d.archived_at IS NULL
        ORDER BY s.submitted_at,s.id''', (case_id,))


def _date(value):
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError('Business dates must be valid ISO dates') from None


_DATE_TOKEN = re.compile(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})\b')


def _text_date(value):
    match = _DATE_TOKEN.search(value)
    if not match:
        return ''
    token = match.group(0)
    for pattern in ('%Y-%m-%d','%d %B %Y','%d %b %Y','%B %d, %Y','%B %d %Y','%b %d, %Y','%d/%m/%Y'):
        try:
            return datetime.strptime(token, pattern).date().isoformat()
        except ValueError:
            continue
    return ''


def _label_value(lines, pattern):
    for index, line in enumerate(lines):
        match = re.match(pattern + r'\s*[:：\-]?\s*(.*)$', line, flags=re.I)
        if match:
            value = match.group(1).strip()
            return value or (lines[index + 1] if index + 1 < len(lines) else '')
    return ''


def propose_letter(text):
    """Extract label-scoped suggestions from official letter text; never confirm.

    This is a deterministic text parser, not an AI model. No receive timestamp,
    filename or arbitrary first/last date is used as a business-date fallback.
    """
    text = str(text or '')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]
    lowered = text.lower()
    grant = bool(re.search(r'grant notification|visa (?:has been )?granted|date of grant|visa grant date', lowered))
    refusal = bool(re.search(r'refusal|visa (?:has been )?refused', lowered))
    if grant and refusal:
        kind, decision = 'unknown', ''
    elif refusal:
        kind, decision = 'refusal', 'refusal'
    elif grant:
        kind, decision = 'grant', 'grant'
    elif re.search(r'health|medical|examination request', lowered):
        kind, decision = 'medical', ''
    elif re.search(r'lodgement receipt|acknowledgement of application|application acknowledgement|acknowledge letter|application received', lowered):
        kind, decision = 'lodgement_receipt', ''
    else:
        kind, decision = 'unknown', ''
    labels = {
        'grant': r'(?:date of grant|visa grant date|grant date)',
        'refusal': r'(?:date of decision|decision date|date of refusal|refusal date)',
        'medical': r'(?:date of (?:letter|issue)|letter date|issue date|date(?=\s*[:：]|\s*$))',
        'lodgement_receipt': r'(?:date of lodgement|lodgement date|application date|date of (?:letter|issue)|date(?=\s*[:：]|\s*$))',
    }
    decision_date = _text_date(_label_value(lines, labels[kind])) if decision else ''
    if decision:
        official = _text_date(_label_value(
            lines,
            r'(?:official document issued date|official document date|notification date|date of notification|document date|letter date|date of letter|date of issue|issue date)',
        ))
    else:
        official = _text_date(_label_value(lines, labels[kind])) if kind in labels else ''
    length = _label_value(lines, r'length of stay') if kind == 'grant' else ''
    expiry = _text_date(_label_value(lines, r'(?:visa expiry date|expiry date|stay until|valid until)')) if kind == 'grant' else ''
    if not expiry and length:
        expiry = _text_date(length)
    return dict(document_kind=kind, decision_type=decision, decision_date=decision_date,
                official_date=official, length_of_stay=length, expiry_date=expiry, medical_completed_date='')


def save_extraction(conn, case_id, document_id, *, text, actor_user_id, occurred_at):
    _actor(conn, actor_user_id, occurred_at)
    _document(conn, case_id, document_id)
    proposal = propose_letter(text)
    from visa_document_lifecycle import check_upload, KIND_CATEGORY
    check_upload(conn,case_id,None,KIND_CATEGORY.get(proposal['document_kind']),exclude_document_id=document_id)
    with _atomic(conn):
        cursor = conn.execute('''INSERT INTO visa_letter_extractions(visa_case_id,document_id,proposed_json,extraction_method,created_by,created_at)
            VALUES(?,?,?,'deterministic_letter_text_v1',?,?)''', (case_id, document_id, _json(proposal), actor_user_id, occurred_at))
        result = _row(conn, 'SELECT * FROM visa_letter_extractions WHERE id=?', (cursor.lastrowid,))
        _audit(conn, case_id, document_id, 'letter_extracted', {}, result, actor_user_id, occurred_at)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='visa_processing_date_extractions'").fetchone():
            from visa_processing_dates import save_processing_extraction
            save_processing_extraction(conn, case_id, document_id, text, actor_user_id=actor_user_id, occurred_at=occurred_at)
        if (proposal['decision_type'] in {'grant','refusal'}
                and conn.execute("SELECT 1 FROM sqlite_master WHERE name='visa_result_status_current'").fetchone()):
            from visa_result_status import record_auto_result
            label = 'Granted' if proposal['decision_type'] == 'grant' else 'Refused'
            record_auto_result(
                conn, case_id, document_id, label,
                {
                    'deterministic': True,
                    'document_role': 'visa_result',
                    'document_kind': proposal['document_kind'],
                    'extraction_id': result['id'],
                    'extraction_method': result['extraction_method'],
                },
                actor_user_id=actor_user_id,
                occurred_at=occurred_at,
            )
    return result


def confirm_letter(conn, case_id, extraction_id, *, values, actor_user_id, occurred_at):
    """Append a staff review, then decide through canonical authority atomically.

    A student-linked case requires its explicit cycle. A standalone applicant case
    records its case decision without creating a student/cycle or student KPI.
    Retained first decisions cannot be edited by this ordinary confirmation action.
    """
    _actor(conn, actor_user_id, occurred_at)
    case = _case(conn, case_id)
    extraction = _row(conn, 'SELECT * FROM visa_letter_extractions WHERE id=? AND visa_case_id=?', (extraction_id, case_id))
    if extraction is None:
        raise ValueError('Extraction does not belong to this case')
    document = _document(conn, case_id, extraction['document_id'])
    allowed = set(propose_letter('').keys())
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError('Unsupported letter review fields')
    effective = {key: str(values.get(key, '') or '').strip() for key in allowed}
    if effective['document_kind'] not in {'grant','refusal','medical','lodgement_receipt','unknown'}:
        raise ValueError('Unsupported letter kind')
    kind = effective['decision_type']
    if kind not in {'','grant','refusal'}:
        raise ValueError('Unsupported decision type')
    for field in ('decision_date','official_date','expiry_date','medical_completed_date'):
        effective[field] = _date(effective[field])
    if kind and (not effective['decision_date'] or effective['document_kind'] != kind):
        raise ValueError('Confirm the matching decision kind and official business date')
    existing = _row(conn, 'SELECT * FROM visa_case_confirmed_decisions WHERE visa_case_id=?', (case_id,))
    if kind and existing:
        if (existing['decision_type'] != kind or existing['decision_date'] != effective['decision_date']
                or json.loads(existing['confirmed_json']) != effective):
            raise ValueError('Retained first decision requires a separate audited correction workflow')
        return existing
    cycle_id = _student_cycle_id(conn, case) if kind and case.get('student_id') else None
    if kind and not case.get('student_id') and case.get('application_cycle_id'):
        raise ValueError('Standalone applicant cannot own a Student Application Cycle')
    with _atomic(conn):
        review_cursor = conn.execute('''INSERT INTO visa_letter_reviews(extraction_id,visa_case_id,confirmed_json,confirmed_by,confirmed_at)
            VALUES(?,?,?,?,?)''', (extraction_id, case_id, _json(effective), actor_user_id, occurred_at))
        result = _row(conn, 'SELECT * FROM visa_letter_reviews WHERE id=?', (review_cursor.lastrowid,))
        if effective['medical_completed_date']:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='visa_processing_date_revisions'").fetchone():
                raise ValueError('Medical completion requires the approved processing-date migration.')
            from visa_processing_dates import correct_processing_dates
            correct_processing_dates(conn,case_id,{'medical_completed_date':effective['medical_completed_date']},actor_user_id=actor_user_id,occurred_at=occurred_at)
        canonical_id = None
        from visa_document_lifecycle import active_documents
        grant_authority = any(d.get('id') == document['id'] and d['authority_category'] == 'grant_letter'
                              for d in active_documents(conn, case_id))
        project_decision = bool(kind) and (kind != 'grant' or grant_authority)
        if kind and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_result_status_current'"
        ).fetchone():
            label = 'Granted' if kind == 'grant' else 'Refused'
            proposed = json.loads(extraction['proposed_json'])
            if proposed.get('document_kind') == kind and proposed.get('decision_type') == kind:
                from visa_result_status import record_auto_result
                current_result = record_auto_result(
                    conn, case_id, document['id'], label,
                    {
                        'deterministic': True,
                        'document_role': 'visa_result',
                        'document_kind': kind,
                        'extraction_id': extraction['id'],
                        'extraction_method': extraction['extraction_method'],
                    },
                    actor_user_id=actor_user_id,
                    occurred_at=occurred_at,
                )
            else:
                current_result = _row(
                    conn,
                    'SELECT effective_result,authority,manual_override,conflict FROM visa_result_status_current WHERE visa_case_id=?',
                    (case_id,),
                )
            if current_result is None:
                from visa_document_lifecycle import case_projection
                current_result={'effective_result':case_projection(conn,case_id)['result_status']}
            project_decision = project_decision and bool(current_result and current_result['effective_result'] == label)
        if project_decision:
            if case.get('student_id'):
                cursor = conn.execute('''INSERT INTO visas(student_id,file_path,visa_type,visa_sector,visa_grant_date,visa_start_date,
                    visa_expiry_date,visa_conditions,visa_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (case['student_id'], document['file_path'], case.get('visa_type') or '', '', effective['decision_date'],
                     '',effective['expiry_date'],effective['length_of_stay'], 'granted' if kind=='grant' else 'refused', occurred_at))
                source_id = document['student_document_id']
                if source_id:
                    source = _row(conn, 'SELECT document_type FROM student_documents WHERE id=?', (source_id,))
                    expected = 'VisaGrantLetter' if kind=='grant' else 'VisaRefusalLetter'
                    if not source or source['document_type'] != expected:
                        source_id = None
                event = record_first_valid_visa_decision(conn, cycle_id, visa_case_id=case_id,
                    student_document_id=source_id,visa_record_id=cursor.lastrowid,decision_type=kind,
                    decision_date=effective['decision_date'],confirmed_at=occurred_at,actor_user_id=actor_user_id)
                if not event.created:
                    raise ValueError('Cycle already has a retained decision; no new case confirmation may replace it')
                canonical_id = event.event.id
            elif kind:
                label = 'Granted' if kind=='grant' else 'Refused'
                conn.execute('''UPDATE visa_cases SET status=?,result_status=?,decision_status=?,result_date=?,updated_at=? WHERE id=?''',
                             (label,label,label,effective['decision_date'],occurred_at,case_id))
            # Retained official letter values feed the case date projections only after confirmation.
            if kind:
                date_column = 'grant_date' if kind == 'grant' else 'refusal_date'
                conn.execute(f'UPDATE visa_cases SET {date_column}=?,expiry_date=?,updated_at=? WHERE id=?',
                             (effective['decision_date'],effective['expiry_date'] or None,occurred_at,case_id))
                cursor = conn.execute('''INSERT INTO visa_case_confirmed_decisions(visa_case_id,extraction_id,decision_type,decision_date,
                    confirmed_json,confirmed_by,confirmed_at,canonical_event_id) VALUES(?,?,?,?,?,?,?,?)''',
                    (case_id,extraction_id,kind,effective['decision_date'],_json(effective),actor_user_id,occurred_at,canonical_id))
                result = _row(conn, 'SELECT * FROM visa_case_confirmed_decisions WHERE id=?', (cursor.lastrowid,))
        _audit(conn, case_id, document['id'], 'letter_confirmed', json.loads(extraction['proposed_json']), effective, actor_user_id, occurred_at)
    return result

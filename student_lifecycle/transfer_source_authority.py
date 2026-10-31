"""Explicit, audited Transfer source evidence; caller owns the transaction.

This module never infers Existing from missing lifecycle records. Migration 036
owns the ordinary-relationship proof view and enforces immutable source evidence.
Verified evidence is revalidated before contract/Cycle use; consumed evidence is historical.
"""
from __future__ import annotations

import sqlite3
from uuid import uuid4

from student_case_feature_flags import read_student_case_feature_flags
from student_case_permissions import can_create_transfer_case

RULE_VERSION = 'transfer-source-v1'
EVIDENCE_FIELDS = (
    'parent_form_token', 'current_enrolment_id', 'ordinary_relationship_cycle_id',
    'ordinary_relationship_contract_id', 'private_school_application_id',
    'public_application_group_id',
)


class SourceAuthorityError(ValueError):
    """Source evidence requires manual resolution; no source was inferred."""


def reserve_unbound_transfer_application_draft(
    connection: sqlite3.Connection, student_id: str, *, actor_user_id: int,
    occurred_at: str, minimal_only: bool = True, require_non_signable: bool = True,
    without_fee: bool = True, without_file: bool = True,
    without_snapshot: bool = True, without_signing_link: bool = True,
) -> int:
    """Reserve the single inert Draft used to key source evidence."""
    if not all((minimal_only, require_non_signable, without_fee, without_file,
                without_snapshot, without_signing_link)):
        raise SourceAuthorityError("Transfer source Draft must remain minimal and non-signable")
    actor = _audit(connection, actor_user_id, occurred_at)
    pending = _rows(connection, """SELECT * FROM contracts
        WHERE student_id=? AND contract_type='TransferApplicationContract'
          AND contract_status='Draft' AND application_cycle_id IS NULL
          AND deleted_at IS NULL AND voided_at IS NULL ORDER BY id""", (student_id,))
    if len(pending) > 1:
        raise SourceAuthorityError(
            "SOURCE VERIFICATION HOLD: multiple unbound Transfer Drafts require manual resolution"
        )
    if pending:
        draft = pending[0]
        forbidden_values = (
            draft.get("contract_file_path"), draft.get("standalone_data_json"),
            draft.get("contract_instance_json"), draft.get("signing_link"),
            draft.get("consulting_and_visa_service_fee"), draft.get("final_fee_amount"),
            draft.get("signed_file_path"), draft.get("final_signed_file_path"),
            draft.get("signed_at"), draft.get("fully_executed_at"),
        )
        if any(value not in (None, "") for value in forbidden_values):
            raise SourceAuthorityError(
                "SOURCE VERIFICATION HOLD: existing Transfer Draft is already generated, priced, or signed"
            )
        contract_id = int(draft["id"])
        for table in (
            "contract_versions", "contract_signing_links",
            "contract_signing_logs", "contract_signatures",
        ):
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None and connection.execute(
                f"SELECT 1 FROM {table} WHERE contract_id=? LIMIT 1", (contract_id,)
            ).fetchone() is not None:
                raise SourceAuthorityError(
                    "SOURCE VERIFICATION HOLD: existing Transfer Draft is signable"
                )
        return contract_id
    uid = f"TRANSFER-SOURCE-DRAFT-{student_id}-{uuid4().hex}"
    available = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(contracts)")
    }
    proposed = {
        "student_id": str(student_id), "contract_uid": uid,
        "contract_type": "TransferApplicationContract",
        "contract_category": "转学合同", "contract_title": "K–12 Transfer",
        "contract_status": "Draft", "generated_by": actor,
        "generated_at": str(occurred_at), "updated_at": str(occurred_at),
    }
    fields = [field for field in proposed if field in available]
    try:
        return int(connection.execute(
            f"INSERT INTO contracts({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(proposed[field] for field in fields),
        ).lastrowid)
    except sqlite3.IntegrityError as exc:
        raise SourceAuthorityError(
            "Transfer Draft ownership is invalid; HOLD for manual resolution"
        ) from exc


def _rows(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def schema_ready(connection: sqlite3.Connection) -> bool:
    objects = dict(connection.execute(
        "SELECT name,type FROM sqlite_master WHERE name IN "
        "('transfer_source_authorities','transfer_existing_source_evidence')"
    ).fetchall())
    return (objects.get('transfer_source_authorities') == 'table'
            and objects.get('transfer_existing_source_evidence') == 'view')


def _require_schema(connection):
    if not schema_ready(connection):
        raise SourceAuthorityError('Transfer source schema unavailable; HOLD for manual resolution')


def _identifier(value, label):
    if isinstance(value, bool) or not str(value or '').isdigit() or int(value) <= 0:
        raise SourceAuthorityError(f'{label} must identify an exact record')
    return int(value)


def _audit(connection, actor_user_id, occurred_at):
    actor = _identifier(actor_user_id, 'Actor')
    # Resolve current persisted permissions inside the caller's transaction.
    # No role-name shortcut, actor-ID shortcut, or cached HTTP permission snapshot.
    try:
        users = _rows(connection, """SELECT user.id,role.permissions_json
            FROM users AS user JOIN roles AS role ON role.id=user.role_id
            WHERE user.id=? AND user.is_active=1 AND user.deleted_at IS NULL""", (actor,))
        settings = dict(connection.execute('SELECT key,value FROM system_settings').fetchall())
    except sqlite3.DatabaseError as exc:
        raise SourceAuthorityError('Source authorization schema unavailable; HOLD for manual resolution') from exc
    flags = read_student_case_feature_flags(settings.get)
    if len(users) != 1 or not can_create_transfer_case(users[0], flags):
        raise SourceAuthorityError('Transfer source permission denied or feature disabled')
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        raise SourceAuthorityError('Source verification timestamp is required')
    return actor


def _contract(connection, student_id, contract_id):
    matches = _rows(connection, '''SELECT * FROM contracts
        WHERE id=? AND student_id=? AND contract_type='TransferApplicationContract'
          AND deleted_at IS NULL AND voided_at IS NULL''', (contract_id, student_id))
    if len(matches) != 1:
        raise SourceAuthorityError('Transfer contract ownership is invalid; HOLD for manual resolution')
    return matches[0]


def _existing(connection, student_id):
    return _rows(connection, 'SELECT * FROM transfer_existing_source_evidence WHERE student_id=?', (student_id,))


def _classify(connection, student_id, evidence):
    token = evidence['parent_form_token']
    selected_existing = [evidence[field] for field in EVIDENCE_FIELDS[1:]]
    existing = _existing(connection, student_id)
    if token is not None:
        if any(value is not None for value in selected_existing):
            raise SourceAuthorityError('Conflicting source evidence; HOLD for manual resolution')
        intake = connection.execute('''SELECT 1 FROM parent_form_tokens
            WHERE token=? AND student_id=? AND intake_type='new_student_transfer'
              AND invalidated_at IS NULL AND length(trim(COALESCE(submitted_at,'')))>0''',
            (token, student_id)).fetchone()
        if not intake:
            raise SourceAuthorityError('Explicit submitted new-student Parent intake is required')
        if existing:
            raise SourceAuthorityError('Parent intake conflicts with Existing dual evidence; HOLD for manual resolution')
        return 'new_transfer'
    competing_intakes = connection.execute('''SELECT count(*) FROM parent_form_tokens
        WHERE student_id=? AND intake_type='new_student_transfer'
          AND invalidated_at IS NULL AND length(trim(COALESCE(submitted_at,'')))>0''',
        (student_id,)).fetchone()[0]
    if competing_intakes:
        raise SourceAuthorityError('Parent intake conflicts with Existing dual evidence; HOLD for manual resolution')
    current_count = connection.execute('''SELECT count(*) FROM student_school_enrolments
        WHERE student_id=? AND enrolment_role='current' AND status='active' ''', (student_id,)).fetchone()[0]
    if (evidence['current_enrolment_id'] is None
            or evidence['ordinary_relationship_cycle_id'] is None
            or sum(evidence[field] is not None for field in EVIDENCE_FIELDS[3:]) != 1
            or current_count != 1):
        raise SourceAuthorityError('Exact Current Enrolment and ordinary AAIE relationship are both required; HOLD for manual resolution')
    matches = [row for row in existing if all(row[field] == evidence[field] for field in EVIDENCE_FIELDS[1:])]
    if len(matches) != 1:
        raise SourceAuthorityError('Existing evidence is missing, ambiguous or invalid; HOLD for manual resolution')
    return 'aaie_existing'


def load_for_contract(connection, student_id, contract_id):
    """Read the exact persisted context, without reclassifying historical facts."""
    _require_schema(connection)
    contract_id = _identifier(contract_id, 'Transfer contract')
    rows = _rows(connection, '''SELECT * FROM transfer_source_authorities
        WHERE student_id=? AND transfer_contract_id=?''', (student_id, contract_id))
    return rows[0] if rows else None


def list_source_options(connection, student_id):
    """Return evidence choices; displaying choices never associates a source."""
    _require_schema(connection)
    intakes = _rows(connection, '''SELECT token,intake_type,created_at,submitted_at
        FROM parent_form_tokens WHERE student_id=? AND intake_type='new_student_transfer'
          AND invalidated_at IS NULL AND length(trim(COALESCE(submitted_at,'')))>0
        ORDER BY created_at,token''', (student_id,))
    existing = _existing(connection, student_id)
    # Optional presentation fields do not participate in the authority decision.
    def display(table, identity, field):
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        if field not in columns:
            return None
        row = connection.execute(f'SELECT {field} FROM {table} WHERE id=?', (identity,)).fetchone()
        return row[0] if row else None
    for row in existing:
        row['current_school_name'] = display('student_school_enrolments', row['current_enrolment_id'], 'school_name_snapshot')
        row['relationship_opened_at'] = display('application_cycles', row['ordinary_relationship_cycle_id'], 'opened_at')
        if row['ordinary_relationship_contract_id'] is not None:
            row['proof_label'] = '普通申请合同'
            row['proof_date'] = display('contracts', row['ordinary_relationship_contract_id'], 'fully_executed_at')
        elif row['private_school_application_id'] is not None:
            row['proof_label'] = '已递交私校申请'
            row['proof_date'] = display('private_school_applications', row['private_school_application_id'], 'submitted_at')
        else:
            row['proof_label'] = '已递交公校申请'
            row['proof_date'] = display('public_application_groups', row['public_application_group_id'], 'submitted_at')
    return {'intakes': intakes, 'existing': existing}


def contract_source_options(connection, student_id):
    """Read-only form context; incomplete records never become authority choices."""
    options = list_source_options(connection, student_id)
    options['student_id'] = str(student_id)
    issues = options['issues'] = []
    intakes = _rows(connection, '''SELECT intake_type,submitted_at,invalidated_at
        FROM parent_form_tokens WHERE student_id=?''', (student_id,))
    if not options['intakes'] and not options['existing']:
        if not intakes:
            issues.append(('parent', '没有关联到这位学生的家长表单。新学生转学需提交对应的家长表单。'))
        else:
            if any(row['invalidated_at'] is not None for row in intakes):
                issues.append(('parent', '有关联家长表单已撤销，不能用于本次转学。'))
            active = [row for row in intakes if row['invalidated_at'] is None]
            if any(row['intake_type'] != 'new_student_transfer' for row in active):
                issues.append(('parent', '已有家长表单的申请类型不是“新学生转学”，不能作为新学生转学来源。'))
            if any(row['intake_type'] == 'new_student_transfer'
                   and not str(row['submitted_at'] or '').strip() for row in active):
                issues.append(('parent', '新学生转学家长表单尚未提交，提交后才可选择。'))
    if not options['existing'] and not options['intakes']:
        current = _rows(connection, '''SELECT id,confirmed_at FROM student_school_enrolments
            WHERE student_id=? AND enrolment_role='current' AND status='active' ''', (student_id,))
        if not current:
            issues.append(('enrolment', '没有有效的当前在读记录，请先确认当前学校。'))
        elif any(not str(row['confirmed_at'] or '').strip() for row in current):
            issues.append(('enrolment', '当前在读记录尚未确认，请先确认当前学校。'))
        # Include eligible historical proof even when its cycle link is missing,
        # for explanation only. Never infer or write a historical association.
        proofs = _rows(connection, '''
            SELECT id,application_cycle_id,'合同' AS label FROM contracts
            WHERE student_id=? AND contract_type IN
              ('NewInternationalStudentContract','LocalStudentApplicationContract')
              AND contract_status='FullyExecuted' AND deleted_at IS NULL AND voided_at IS NULL
            UNION ALL SELECT id,application_cycle_id,'私校申请' FROM private_school_applications
            WHERE student_id=? AND length(trim(COALESCE(submitted_at,'')))>0
              AND status NOT IN ('draft','not_submitted')
            UNION ALL SELECT id,application_cycle_id,'公校申请' FROM public_application_groups
            WHERE student_id=? AND length(trim(COALESCE(submitted_at,'')))>0 AND status<>'draft'
            ''', (student_id, student_id, student_id))
        if not proofs:
            issues.append(('history', '没有可用的已签普通申请合同或已递交学校申请记录。'))
        for row in proofs:
            cycle = connection.execute('''SELECT 1 FROM application_cycles
                WHERE id=? AND student_id=? AND cycle_type='initial'
                  AND education_stream='school_education' ''',
                (row['application_cycle_id'], student_id)).fetchone()
            if not cycle:
                issues.append(('history', f'{row["label"]} #{row["id"]} 已存在，但未关联到这位学生的普通中小学申请周期；需由管理员核对历史业务关联，修改学生资料或上传材料不会自动建立关联。'))
    elif options['existing']:
        count = connection.execute('''SELECT count(*) FROM student_school_enrolments
            WHERE student_id=? AND enrolment_role='current' AND status='active' ''', (student_id,)).fetchone()[0]
        if count != 1:
            issues.append(('enrolment', '存在多条当前在读记录，请先确认唯一的当前学校。'))
    if options['intakes'] and options['existing']:
        issues.append(('conflict', '新学生转学表单与已有学生申请记录冲突；请选择正确业务入口核对，不能仅靠选择其中一项消除冲突。'))
    return options


def save_authority(connection, student_id, transfer_contract_id, *, parent_form_token=None,
                   current_enrolment_id=None, ordinary_relationship_cycle_id=None,
                   ordinary_relationship_contract_id=None, private_school_application_id=None,
                   public_application_group_id=None, actor_user_id, occurred_at):
    """Persist one explicitly verified authority for the exact contract."""
    _require_schema(connection)
    contract_id = _identifier(transfer_contract_id, 'Transfer contract')
    actor = _audit(connection, actor_user_id, occurred_at)
    evidence = dict(parent_form_token=parent_form_token, current_enrolment_id=current_enrolment_id,
                    ordinary_relationship_cycle_id=ordinary_relationship_cycle_id,
                    ordinary_relationship_contract_id=ordinary_relationship_contract_id,
                    private_school_application_id=private_school_application_id,
                    public_application_group_id=public_application_group_id)
    if parent_form_token is not None and (not isinstance(parent_form_token, str) or not parent_form_token.strip()):
        raise SourceAuthorityError('Parent intake must identify an exact record')
    for field in EVIDENCE_FIELDS[1:]:
        if evidence[field] is not None:
            evidence[field] = _identifier(evidence[field], field)
    existing = load_for_contract(connection, student_id, contract_id)
    if existing:
        if any(existing[field] != evidence[field] for field in EVIDENCE_FIELDS):
            raise SourceAuthorityError('Source context already exists with different evidence; HOLD for manual resolution')
        if existing['lifecycle_state'] == 'pending':
            _contract(connection, student_id, contract_id)
            classification = _classify(connection, student_id, evidence)
            try:
                connection.execute('''UPDATE transfer_source_authorities
                    SET lifecycle_state='verified',source_classification=?,verified_by=?,verified_at=?
                    WHERE id=? AND lifecycle_state='pending' ''',
                    (classification, actor, occurred_at, existing['id']))
            except sqlite3.IntegrityError as exc:
                raise SourceAuthorityError(f'Source verification rejected: {exc}') from exc
        elif existing['lifecycle_state'] == 'verified':
            classification = _classify(connection, student_id, evidence)
            if existing['source_classification'] != classification:
                raise SourceAuthorityError('Persisted source classification conflicts with current evidence; HOLD for manual resolution')
        return load_for_contract(connection, student_id, contract_id)
    contract = _contract(connection, student_id, contract_id)
    if contract['application_cycle_id'] is not None:
        raise SourceAuthorityError('Already bound contract cannot acquire a new source context')
    classification = _classify(connection, student_id, evidence)
    fields = ('student_id', 'transfer_contract_id', *EVIDENCE_FIELDS, 'rule_version', 'created_by', 'created_at')
    values = (student_id, contract_id, *(evidence[field] for field in EVIDENCE_FIELDS), RULE_VERSION, actor, occurred_at)
    try:
        connection.execute(f"INSERT INTO transfer_source_authorities({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
    except sqlite3.IntegrityError as exc:
        raise SourceAuthorityError(f'Source evidence rejected: {exc}') from exc
    row = load_for_contract(connection, student_id, contract_id)
    try:
        connection.execute('''UPDATE transfer_source_authorities
            SET lifecycle_state='verified',source_classification=?,verified_by=?,verified_at=?
            WHERE id=? AND lifecycle_state='pending' ''',
            (classification, actor, occurred_at, row['id']))
    except sqlite3.IntegrityError as exc:
        raise SourceAuthorityError(f'Source verification rejected: {exc}') from exc
    return load_for_contract(connection, student_id, contract_id)


def resolve_for_creation(connection, student_id, transfer_contract_id):
    """Revalidate persisted verified evidence before Case/Cycle creation."""
    row = load_for_contract(connection, student_id, transfer_contract_id)
    if row is None or row['lifecycle_state'] != 'verified':
        raise SourceAuthorityError('SOURCE VERIFICATION HOLD: persisted verified Transfer authority is required')
    contract = _contract(connection, student_id, transfer_contract_id)
    if contract['application_cycle_id'] is not None:
        raise SourceAuthorityError('Transfer contract is already bound')
    classification = _classify(connection, student_id, row)
    if row['source_classification'] != classification:
        raise SourceAuthorityError('Persisted source classification conflicts with current evidence; HOLD for manual resolution')
    return row


def consume_authority(connection, authority_id, application_cycle_id, *, actor_user_id, occurred_at):
    """Consume after exact contract/Cycle/Case/origin bindings, in one transaction."""
    _require_schema(connection)
    authority_id = _identifier(authority_id, 'Source authority')
    application_cycle_id = _identifier(application_cycle_id, 'Transfer Cycle')
    actor = _audit(connection, actor_user_id, occurred_at)
    rows = _rows(connection, 'SELECT * FROM transfer_source_authorities WHERE id=?', (authority_id,))
    if not rows:
        raise SourceAuthorityError('Source authority not found')
    row = rows[0]
    if row['lifecycle_state'] == 'consumed':
        if row['application_cycle_id'] != application_cycle_id:
            raise SourceAuthorityError('Source authority already belongs to another Cycle')
        return row
    if row['lifecycle_state'] != 'verified':
        raise SourceAuthorityError('SOURCE VERIFICATION HOLD: persisted verified Transfer authority is required')
    _contract(connection, row['student_id'], row['transfer_contract_id'])
    classification = _classify(connection, row['student_id'], row)
    if row['source_classification'] != classification:
        raise SourceAuthorityError('Persisted source classification conflicts with current evidence; HOLD for manual resolution')
    try:
        connection.execute('''UPDATE transfer_source_authorities SET lifecycle_state='consumed',
            application_cycle_id=? WHERE id=? AND lifecycle_state='verified' ''',
            (application_cycle_id, authority_id))
    except sqlite3.IntegrityError as exc:
        raise SourceAuthorityError(f'Source consumption rejected: {exc}') from exc
    return load_for_contract(connection, row['student_id'], row['transfer_contract_id'])


def resolve_for_cycle(connection, student_id, application_cycle_id):
    """Read immutable consumed authority with its exact lifecycle bindings.

    Current enrolment is intentionally not reclassified after activation: it may
    legitimately become historical when this Transfer completes.
    """
    _require_schema(connection)
    cycle_id = _identifier(application_cycle_id, 'Transfer Cycle')
    rows = _rows(connection, """SELECT source.* FROM transfer_source_authorities source
        JOIN application_cycles cycle ON cycle.id=source.application_cycle_id
          AND cycle.student_id=source.student_id AND cycle.cycle_type='transfer'
        JOIN contracts contract ON contract.id=source.transfer_contract_id
          AND contract.student_id=source.student_id AND contract.application_cycle_id=cycle.id
          AND contract.contract_type='TransferApplicationContract'
        JOIN transfer_cases transfer ON transfer.case_id=cycle.source_student_case_id
          AND transfer.source_contract_id=contract.id
          AND transfer.transfer_source_label=source.source_classification
        WHERE source.student_id=? AND source.application_cycle_id=?
          AND source.lifecycle_state='consumed'
          AND source.source_classification IN ('new_transfer','aaie_existing')""", (student_id, cycle_id))
    if len(rows) != 1:
        raise SourceAuthorityError('SOURCE VERIFICATION HOLD: exact consumed Transfer authority is required')
    return rows[0]


def resolve_for_contract(connection, student_id, transfer_contract_id):
    """Resolve exact source for pricing/signing; never use Revenue or UI labels."""
    row = load_for_contract(connection, student_id, transfer_contract_id)
    if row is None:
        raise SourceAuthorityError('SOURCE VERIFICATION HOLD: verify Transfer source before generating a signable contract')
    if row['lifecycle_state'] == 'pending':
        raise SourceAuthorityError('SOURCE VERIFICATION HOLD: persisted verified Transfer authority is required')
    if row['lifecycle_state'] == 'verified':
        return resolve_for_creation(connection, student_id, transfer_contract_id)
    return resolve_for_cycle(connection, student_id, row['application_cycle_id'])


def identify_for_submission(connection, student_id, contract_id=None, *, form=None):
    """Read reliable source for the normal form, without reserving or saving rows."""
    _require_schema(connection)
    if contract_id is not None:
        saved = load_for_contract(connection, student_id, contract_id)
        if saved is not None:
            return resolve_for_contract(connection, student_id, contract_id)
    options = list_source_options(connection, student_id)
    submitted = form or {}
    token = str(submitted.get('source_parent_form_token') or '').strip()
    selected = str(submitted.get('source_existing_evidence') or '').strip()
    evidence = dict.fromkeys(EVIDENCE_FIELDS)
    if token and selected:
        raise SourceAuthorityError('转学来源冲突，请在正常业务资料中确认后再生成合同。')
    if token:
        evidence['parent_form_token'] = token
    elif selected:
        parts = selected.split(':')
        if len(parts) != 5 or any(not part.isascii() or not part.isdigit() for part in parts):
            raise SourceAuthorityError('在读与历史申请资料无效，请确认正常业务资料。')
        evidence.update(zip(EVIDENCE_FIELDS[1:], (int(part) or None for part in parts)))
    elif len(options['intakes']) == 1 and not options['existing']:
        evidence['parent_form_token'] = options['intakes'][0]['token']
    elif len(options['existing']) == 1 and not options['intakes']:
        evidence.update({key: options['existing'][0][key] for key in EVIDENCE_FIELDS[1:]})
    else:
        raise SourceAuthorityError('转学来源缺失或不唯一，请在正常业务资料中确认后再生成合同。')
    classification = _classify(connection, student_id, evidence)
    return {**evidence, 'student_id': str(student_id),
            'transfer_contract_id': contract_id, 'source_classification': classification,
            'rule_version': RULE_VERSION}


def ensure_for_submission(connection, student_id, contract_id, *, form=None,
                          actor_user_id, occurred_at):
    """Associate a reliable source within the normal unsigned-Draft submission."""
    contract = _contract(connection, student_id, contract_id)
    if contract['contract_status'] != 'Draft' or contract['application_cycle_id'] is not None:
        raise SourceAuthorityError('历史或已签署合同不能自动补来源关联。')
    for column in ('signed_at', 'fully_executed_at', 'signed_file_path', 'final_signed_file_path'):
        if contract.get(column):
            raise SourceAuthorityError('已签署合同不能自动补来源关联。')
    for table in ('contract_signatures',):
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() and connection.execute(
                f'SELECT 1 FROM {table} WHERE contract_id=? LIMIT 1', (contract_id,)).fetchone():
            raise SourceAuthorityError('已签署合同不能自动补来源关联。')
    identified = identify_for_submission(connection, student_id, contract_id, form=form)
    if identified.get('id'):
        return identified
    return save_authority(connection, student_id, contract_id,
        **{key: identified[key] for key in EVIDENCE_FIELDS},
        actor_user_id=actor_user_id, occurred_at=occurred_at)

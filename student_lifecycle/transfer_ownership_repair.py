"""Explicit frozen-scope ownership repair, never called by normal create or GET.

Caller owns the database transaction. The staging-only tool supplies the private,
reviewed scope; no student identities or business artifacts belong in source.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from business_changes.commands import Command, Mutation, Outcome, execute_command
from business_changes.contracts import Subject
from business_changes.registry import Registry
from business_changes.versions import version_of
from revenue.cycle_integration import ensure_revenue_case_for_cycle
from student_lifecycle import transfer_source_authority as authority
from student_lifecycle import transfer_v2_repository as repository


class RepairError(ValueError):
    pass


PARENT_INTAKE_TERMS = {'02': 'Term 1', '04': 'Term 2', '07': 'Term 3', '10': 'Term 4'}


def frozen_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def verify_scope(scope, approved_digest):
    if frozen_digest(scope) != approved_digest:
        raise RepairError('approved scope digest mismatch')
    targets = scope.get('targets', [])
    if not scope.get('batch_uid') or not targets:
        raise RepairError('empty repair scope')
    for field in ('student_id', 'cycle_id', 'contract_id', 'authority_id'):
        values = [t.get(field) for t in targets]
        if None in values or len(set(values)) != len(values):
            raise RepairError('duplicate or missing scope identity')
    return scope


def _rows(conn, table, where='1', parameters=()):
    cursor = conn.execute(f'SELECT * FROM "{table}" WHERE {where}', parameters)
    names = [c[0] for c in cursor.description]
    rows = [dict(zip(names, tuple(row))) for row in cursor]
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def _columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _require(condition, message):
    if not condition:
        raise RepairError(message)


def _actor(conn, actor, stamp):
    # The same persisted active-user, role permission and feature-flag gate used
    # for source consumption. This also prevents a forged arbitrary Admin id.
    authority._audit(conn, actor, stamp)
    row = conn.execute('SELECT r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?', (actor,)).fetchone()
    _require(row is not None and row[0] == 'Admin', 'repair requires active Admin')


def financial_fingerprint(conn):
    """Every existing financial fact; only container/audit/receipt inserts excluded."""
    excluded = {'revenue_commercial_cases', 'revenue_audit_events', 'revenue_outbox_events',
                'revenue_idempotency_receipts', 'revenue_number_sequences'}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'revenue_%' OR name IN ('payments','invoices','refunds','commissions')) ORDER BY name")]
    return {t: frozen_digest(_rows(conn, t)) for t in tables if t not in excluded}


def snapshot(conn, target):
    sid, cid = str(target['student_id']), int(target['cycle_id'])
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    result = {}
    for table in tables:
        cols = _columns(conn, table)
        # Freeze all student/cycle-owned facts, including offers, visa and money.
        if 'student_id' in cols:
            rows = _rows(conn, table, 'student_id=?', (sid,))
        elif 'application_cycle_id' in cols:
            rows = _rows(conn, table, 'application_cycle_id=?', (cid,))
        else:
            continue
        result[table] = frozen_digest(rows)
    for table in ('public_application_preferences', 'school_plan_report_confirmations',
                  'school_plan_school_submission_events', 'schools'):
        if _columns(conn, table):
            # These indirect source rows are frozen as well. Scope is private.
            result[table] = frozen_digest(_rows(conn, table))
    result['cycle_version'] = version_of(conn, Subject('application_cycle', str(cid)))
    return result


def _validate(conn, target, artifact_paths):
    sid, cid, contract_id = str(target['student_id']), int(target['cycle_id']), int(target['contract_id'])
    cycles = _rows(conn, 'application_cycles', 'student_id=? AND status=\'active\' AND education_stream=\'school_education\'', (sid,))
    _require(len(cycles) == 1 and cycles[0]['id'] == cid, 'active cycle ownership changed')
    cycle = cycles[0]
    _require(cycle['cycle_type'] == 'initial' and cycle['source_student_case_id'] is None, 'cycle is not an unowned initial cycle')
    _require(not _rows(conn, 'student_cases', 'student_id=? AND case_type=\'transfer\' AND active_slot=1', (sid,)), 'active transfer already exists')
    source = authority.resolve_for_creation(conn, sid, contract_id)
    _require(source['id'] == target['authority_id'] and source['source_classification'] == 'new_transfer', 'source ownership mismatch')
    _require(source['parent_form_token'] == cycle['parent_form_token'], 'parent token ownership mismatch')
    contract = _rows(conn, 'contracts', 'id=?', (contract_id,))[0]
    _require(contract['contract_status'] == 'FullyExecuted' and contract['application_cycle_id'] is None, 'contract is not signed and unbound')
    artifact = target['artifact']
    _require(contract['final_signed_file_path'] == artifact['original_path'], 'final contract path changed')
    docs = _rows(conn, 'student_documents', 'student_id=? AND file_path=? AND is_active=1', (sid, artifact['original_path']))
    _require(len(docs) == 1 and docs[0]['id'] == artifact['document_id'] and not docs[0].get('voided_at'), 'unique active final document required')
    path = Path(artifact_paths[str(contract_id)])
    _require(hashlib.sha256(path.read_bytes()).hexdigest() == artifact['sha256'], 'contract artifact hash changed')
    _require(path.read_bytes().startswith(b'%PDF-'), 'final contract is not PDF')
    # A later result requires a new reviewed repair plan, never silent takeover.
    for table in ('offer_received_events','cycle_final_acceptances','coe_oshc','visa_cases'):
        cols = _columns(conn, table)
        if 'application_cycle_id' in cols:
            _require(not _rows(conn, table, 'application_cycle_id=?', (cid,)), 'later business result requires review')
    confirmed_ids = []
    sources = []
    for branch in target['branches']:
        confirmed_id = int(branch['confirmed_school_id'])
        confirmed_ids.append(confirmed_id)
        confirmed = _rows(conn, 'school_plan_confirmed_schools', 'id=?', (confirmed_id,))
        _require(len(confirmed) == 1, 'confirmed school missing')
        row = confirmed[0]
        _require(str(row['student_id']) == sid and row['school_id'] == branch['school_id'] and row['school_name_snapshot'] == branch['school_name'], 'confirmed school ownership mismatch')
        _require(row['is_selected'] == 1 and row['submission_status'] == 'submitted' and row['submitted_at'] == branch['submitted_at'] and row['result_status'] in (None, '', 'pending'), 'confirmed submission changed')
        _require(row['report_id'] == target['report_id'] and row['confirmation_id'] == target['confirmation_id'], 'plan ownership mismatch')
        if branch['source_kind'] == 'public':
            group = _rows(conn, 'public_application_groups', 'id=?', (branch['source_id'],))
            _require(len(group) == 1, 'public group missing')
            app = group[0]
            preferences = _rows(conn, 'public_application_preferences', 'public_application_group_id=? AND confirmed_school_id=?', (app['id'], confirmed_id))
            _require(len(preferences) == 1 and preferences[0]['school_id'] == branch['school_id'] and preferences[0]['application_status'] == 'submitted', 'public preference ownership mismatch')
        elif branch['source_kind'] == 'private':
            apps = _rows(conn, 'private_school_applications', 'id=? AND confirmed_school_id=?', (branch['source_id'], confirmed_id))
            _require(len(apps) == 1, 'private application missing')
            app = apps[0]
            _require(app['school_id'] == branch['school_id'], 'private school changed')
        else:
            raise RepairError('unsupported source kind')
        _require(app['application_cycle_id'] == cid and str(app['student_id']) == sid and app['status'] == 'submitted' and app['submitted_at'] == branch['submitted_at'], 'application submission changed')
        school = repository.school_snapshot(conn, branch['school_id'])
        _require(school is not None and school['school_name'] == branch['school_name'], 'school master mismatch')
        sources.append(dict(school))
    _require(confirmed_ids and len(set(confirmed_ids)) == len(confirmed_ids), 'duplicate or empty source branches')
    expected = _rows(conn, 'school_plan_confirmed_schools', 'student_id=? AND confirmation_id=? AND is_selected=1', (sid, target['confirmation_id']))
    _require({r['id'] for r in expected} == set(confirmed_ids), 'selected schools changed')
    reports = _rows(conn, 'school_plan_reports', 'id=? AND student_id=?', (target['report_id'], sid))
    confirmations = _rows(conn, 'school_plan_report_confirmations', 'id=? AND report_id=? AND student_id=?', (target['confirmation_id'], target['report_id'], sid))
    _require(len(reports) == len(confirmations) == 1 and reports[0]['status'] == confirmations[0]['status'] == 'confirmed', 'confirmed plan missing')
    student = _rows(conn, 'students', 'student_id=?', (sid,))[0]
    form = json.loads(student['form_data_json'])
    _require(all(form.get(k) for k in ('current_school_name','current_year_level','applying_year_level','preferred_intake_year','preferred_intake_term')), 'parent school snapshot incomplete')
    # Parent form stores intake MONTH codes (fields.py), not term numbers.
    _require(str(form['preferred_intake_term']) in PARENT_INTAKE_TERMS, 'unknown intake month')
    return cycle, form, sources


def preview(conn, scope, approved_digest, actor, stamp, artifact_paths):
    verify_scope(scope, approved_digest)
    _actor(conn, actor, stamp)
    targets = []
    next_case = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM student_cases').fetchone()[0]
    for index, target in enumerate(scope['targets']):
        _validate(conn, target, artifact_paths)
        targets.append({'cycle_id': target['cycle_id'], 'contract_id': target['contract_id'],
                        'student_id': target['student_id'], 'case_id': next_case + index,
                        'branches': len(target['branches']), 'snapshot': snapshot(conn, target)})
    result = {'scope_digest': approved_digest, 'actor_user_id': actor, 'targets': targets,
              'financial_fingerprint': financial_fingerprint(conn)}
    return dict(result, preview_digest=frozen_digest(result))


def _insert(conn, table, values):
    names = ','.join(values)
    marks = ','.join('?' for _ in values)
    return conn.execute(f'INSERT INTO {table}({names}) VALUES({marks})', tuple(values.values())).lastrowid


def apply(conn, scope, approved_digest, frozen_preview, actor, stamp, artifact_paths):
    """Atomic batch. Replay uses the protected command receipt, not guessed data."""
    _require(conn.in_transaction, 'outer transaction required')
    verify_scope(scope, approved_digest)
    _actor(conn, actor, stamp)
    before = dict(frozen_preview)
    preview_digest = before.pop('preview_digest')
    _require(frozen_digest(before) == preview_digest and before['scope_digest'] == approved_digest and before['actor_user_id'] == actor, 'invalid frozen preview')
    expected = {Subject('application_cycle', str(t['cycle_id'])): t['snapshot']['cycle_version'] for t in before['targets']}
    command = Command('transfer_ownership_repair', 'user', str(actor), scope['batch_uid'],
                      {'scope': approved_digest, 'preview': preview_digest})

    def authorize(view):
        _actor(view, actor, stamp)

    def operation(view):
        current = preview(view, scope, approved_digest, actor, stamp, artifact_paths)
        _require(current == frozen_preview, 'preview stale; re-review current business facts')
        changes = []
        for target, frozen in zip(scope['targets'], before['targets']):
            cycle, form, schools = _validate(view, target, artifact_paths)
            cid, sid = int(target['cycle_id']), str(target['student_id'])
            case_id = _insert(view, 'student_cases', dict(id=frozen['case_id'], student_id=sid,
                case_type='transfer', sequence_no=repository.next_case_sequence(view, sid),
                status='in_progress', active_slot=1, started_at=stamp, created_by=actor,
                created_at=stamp, updated_by=actor, updated_at=stamp))
            matches = view.execute('SELECT id FROM schools WHERE school_name=? COLLATE NOCASE AND deleted_at IS NULL', (form['current_school_name'],)).fetchall()
            current_school = repository.school_snapshot(view, matches[0][0]) if len(matches) == 1 else None
            _insert(view, 'transfer_cases', dict(case_id=case_id, transfer_origin='external',
                source_contract_id=target['contract_id'], workflow_state='preparing',
                current_school_name_snapshot=form['current_school_name'],
                current_school_id=current_school['id'] if current_school else None,
                current_school_sector=current_school['sector_key'] if current_school else None,
                rule_status='pending_confirmation', required_completion_document='pending_confirmation',
                tuition_requirement='pending_confirmation', rule_version='transfer-v2',
                created_at=stamp, updated_at=stamp, relationship_status='needs_repair', v2_version=2,
                transfer_source_label='new_transfer', current_year_level_snapshot=form['current_year_level'],
                application_year_level=form['applying_year_level'], transfer_year=int(form['preferred_intake_year']),
                transfer_term=PARENT_INTAKE_TERMS[str(form['preferred_intake_term'])]))
            view.execute("UPDATE application_cycles SET cycle_type='transfer',source_student_case_id=?,updated_at=?,updated_by_user_id=? WHERE id=?", (case_id, stamp, actor, cid))
            mappings = []
            for index, (source, school) in enumerate(zip(target['branches'], schools), 1):
                branch = _insert(view, 'transfer_school_applications', dict(case_id=case_id,
                    school_id=school['id'], school_name_snapshot=source['school_name'], school_sector=school['sector_key'],
                    preference_order=index, application_status='submitted', created_by=actor, created_at=stamp,
                    updated_at=stamp, branch_uid='ownership-branch-'+uuid4().hex, application_cycle_id=cid,
                    original_school_id=school['id'], school_state_snapshot=school['state'],
                    school_region_snapshot=school['region'], school_type_snapshot=school['school_type'], updated_by=actor,
                    notes=json.dumps({'ownership_repair':scope['batch_uid'], 'source':source}, ensure_ascii=False, sort_keys=True)))
                _insert(view, 'transfer_target_state_events', dict(event_uid='ownership-state-'+uuid4().hex,
                    application_cycle_id=cid, branch_id=branch, previous_state=None,
                    new_state='application_submitted', event_type='ownership_repair_imported_submission',
                    actor_user_id=actor, occurred_at=source['submitted_at'], created_at=stamp,
                    note='Imported original submission; repair audit time is created_at.'))
                mappings.append(dict(source, branch_id=branch))
            _insert(view, 'transfer_v2_origin_events', dict(event_uid='ownership-origin-'+uuid4().hex,
                application_cycle_id=cid, previous_source_label=None,new_source_label='new_transfer',
                actor_user_id=actor,occurred_at=stamp,created_at=stamp))
            view.execute('UPDATE contracts SET application_cycle_id=?,updated_at=? WHERE id=? AND application_cycle_id IS NULL', (cid, stamp, target['contract_id']))
            authority.consume_authority(view, target['authority_id'], cid, actor_user_id=actor, occurred_at=stamp)
            revenue = ensure_revenue_case_for_cycle(view, cid, actor, occurred_at=stamp, integration_event='attached')
            _require(revenue is not None, 'Revenue container must be created or reused')
            _insert(view, 'case_events', dict(case_id=case_id, event_type='transfer_ownership_repaired',
                entity_type='application_cycle', entity_id=str(cid), old_value_json=json.dumps(cycle, sort_keys=True),
                new_value_json=json.dumps({'cycle_type':'transfer','case_id':case_id,'contract_id':target['contract_id']}, sort_keys=True),
                metadata_json=json.dumps({'batch_uid':scope['batch_uid'],'scope_digest':approved_digest,
                    'preview_digest':preview_digest,'source_mappings':mappings,'current_school_evidence':'parent_declared_unverified',
                    'parent_school_snapshot': {k:form[k] for k in ('current_school_name',
                        'current_year_level','applying_year_level','preferred_intake_year','preferred_intake_term')},
                    'artifact':target['artifact'],'no_charge_creation':True}, ensure_ascii=False, sort_keys=True),
                actor_type='user', actor_user_id=actor, request_id=scope['batch_uid'], created_at=stamp))
            affected = tuple(Subject(kind, str(identifier)) for kind, identifier in (
                ('student',sid),('student_case',case_id),('module','student_cases'),('module','transfer_cases'),
                ('module','revenue'),('dashboard','workflow'),('dashboard','overview')))
            changes.append(Mutation('transfer.ownership_repaired', Subject('application_cycle',str(cid)), affected))
        _require(financial_fingerprint(view) == before['financial_fingerprint'], 'financial facts changed; batch rolled back')
        return Outcome({'uid':scope['batch_uid'], 'changed':True, 'created':True}, tuple(changes))

    return execute_command(conn, command, registry=Registry([]), authorize=authorize,
                           operation=operation, expected_versions=expected, occurred_at=stamp)

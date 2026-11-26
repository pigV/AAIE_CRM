"""Create the independent 590 receivable from a submitted, identified parent choice."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import sqlite3

from revenue.operational_integration import _active_after_cutover, process_revenue_visa_change
from revenue.repository import RevenueRepository
from revenue.visa_fee_policy import visa_subclass


_LOG = logging.getLogger(__name__)
_RELATIONSHIPS = {'父亲': 'father', '母亲': 'mother'}
_VISA_TYPE = '590 Guardian Visa'


class _GuardianFeeUnavailable(ValueError):
    pass


def _key(token):
    return 'parent-guardian-intake:' + hashlib.sha256(token.encode()).hexdigest()


def _fee_key(cycle):
    return f'parent-guardian-fee:{cycle}'


def _name(value):
    return ' '.join(str(value or '').casefold().split())


@contextmanager
def _intake_transaction(connection, student_id):
    # A financial integration error cannot undo a submitted form or a signed
    # Transfer. Roll back all writes from this optional operation together.
    try:
        with RevenueRepository(connection).transaction():
            yield
    except (sqlite3.DatabaseError, ValueError, LookupError) as exc:
        _LOG.warning('Guardian intake finance unavailable for student %s (%s)',
                     student_id, type(exc).__name__)


def _intent(connection, student_id, token):
    link = connection.execute("""
        SELECT created_by, submitted_at, intake_type FROM parent_form_tokens
        WHERE token=? AND student_id=? AND invalidated_at IS NULL
    """, (token, student_id)).fetchone()
    if not link or not link[0] or not link[1]:
        return None
    if not connection.execute("SELECT 1 FROM users WHERE id=?", (link[0],)).fetchone():
        return None
    student = connection.execute("""
        SELECT form_data_json, passport_first_name, passport_last_name
        FROM students WHERE student_id=?
    """, (student_id,)).fetchone()
    if not student:
        return None
    form = json.loads(student[0] or '{}')
    relationship = form.get('accompanying_person_relationship')
    role = _RELATIONSHIPS.get(relationship)
    if form.get('requires_parent_accompaniment') != '是' or not role:
        return None
    parents = connection.execute("""
        SELECT passport_first_name, passport_last_name FROM parents
        WHERE student_id=? AND parent_type=?
    """, (student_id, role)).fetchall()
    if len(parents) != 1:
        return None
    first, last = (' '.join(str(part or '').split()) for part in parents[0])
    if not first or not last or any(
        _name(part) != _name(form.get(f'{role}_passport_{field}_name'))
        for part, field in ((first, 'first'), (last, 'last'))
    ):
        return None
    name, reversed_name = f'{first} {last}', f'{last} {first}'
    child = {_name(f'{student[1]} {student[2]}'), _name(f'{student[2]} {student[1]}')}
    if _name(name) in child:
        return None
    return {
        'actor': int(link[0]), 'submitted_at': str(link[1]), 'intake_type': link[2],
        'relationship': relationship, 'name': name,
        'aliases': {_name(name), _name(reversed_name)},
        'name_digest': hashlib.sha256(_name(name).encode()).hexdigest(),
    }


def _eligible(connection, occurred_at):
    columns = {row[1] for row in connection.execute('PRAGMA table_info(parent_form_tokens)')}
    return (
        {'token', 'student_id', 'created_by', 'submitted_at', 'intake_type', 'invalidated_at'} <= columns
        and _active_after_cutover(connection, occurred_at)
    )


def record_parent_guardian_intake(connection, student_id, token, *, occurred_at):
    """Run only at the successful form submission boundary, inside its transaction."""
    if not _eligible(connection, occurred_at):
        return
    with _intake_transaction(connection, student_id):
        return _record_submission(connection, student_id, token, occurred_at)


def _record_submission(connection, student_id, token, occurred_at):
    intent = _intent(connection, student_id, token)
    if not intent or intent['submitted_at'] != occurred_at:
        return
    cycle = connection.execute("""
        SELECT id, cycle_type FROM application_cycles
        WHERE student_id=? AND parent_form_token=? AND status='active'
    """, (student_id, token)).fetchone()
    if not cycle:
        return
    if intent['intake_type'] == 'new_student_transfer' and cycle[1] != 'transfer':
        # A fee on the temporary initial cycle would block its transfer takeover.
        repo = RevenueRepository(connection, now=lambda: occurred_at)
        payload = {
            'student_id': student_id, 'initial_cycle_id': cycle[0],
            'relationship': intent['relationship'], 'name_digest': intent['name_digest'],
        }
        repo.command(
            idempotency_key=_key(token), command_type='parent_guardian_intake_deferred',
            aggregate_type='application_cycle', payload=payload, actor_user_id=intent['actor'],
            operation=lambda: {'aggregate_uid': f'cycle:{cycle[0]}', **payload},
        )
        return
    return _ensure_fee(connection, student_id, cycle[0], intent, occurred_at)


def complete_deferred_guardian_fee(connection, student_id, cycle_id, token, *, occurred_at):
    if not token or not _eligible(connection, occurred_at):
        return
    with _intake_transaction(connection, student_id):
        return _complete_deferred(connection, student_id, cycle_id, token, occurred_at)


def _complete_deferred(connection, student_id, cycle_id, token, occurred_at):
    queued = connection.execute("""
        SELECT result_json FROM revenue_idempotency_receipts
        WHERE idempotency_key=? AND command_type='parent_guardian_intake_deferred'
    """, (_key(token),)).fetchone()
    if not queued:
        return
    evidence = json.loads(queued[0])
    intent = _intent(connection, student_id, token)
    if (not intent or evidence['student_id'] != student_id
            or intent['intake_type'] != 'new_student_transfer'
            or any(evidence[field] != intent[field] for field in ('relationship', 'name_digest'))):
        return
    scope = connection.execute("""
        SELECT 1 FROM application_cycles c
        JOIN transfer_source_authorities a ON a.application_cycle_id=c.id
        WHERE c.id=? AND c.student_id=? AND c.cycle_type='transfer' AND c.status='active'
          AND a.student_id=c.student_id AND a.parent_form_token=? AND a.lifecycle_state='consumed'
    """, (cycle_id, student_id, token)).fetchone()
    if scope:
        return _ensure_fee(connection, student_id, cycle_id, intent, occurred_at)


def _ensure_fee(connection, student_id, cycle, intent, occurred_at):
    repo = RevenueRepository(connection, now=lambda: occurred_at)
    key = _fee_key(cycle)

    def create():
        guardians = [
            dict(row) for row in connection.execute("""
                SELECT * FROM visa_cases
                WHERE student_id=? AND application_cycle_id=? AND archived_at IS NULL
            """, (student_id, cycle)) if visa_subclass(dict(row)) == '590'
        ]
        if guardians:
            if len(guardians) != 1 or _name(guardians[0]['applicant_name']) not in intent['aliases']:
                raise _GuardianFeeUnavailable('existing guardian case has different or ambiguous applicant')
            visa_id = guardians[0]['id']
        else:
            visa_id = connection.execute("""
                INSERT INTO visa_cases (
                    student_id, application_cycle_id, visa_case_name, visa_type, case_type,
                    visa_subclass, applicant_name, main_applicant, relationship_to_student,
                    application_type, applicant_count, status, result_status, decision_status,
                    created_by, created_at, updated_at
                ) VALUES (?,?,?,?,?,'590',?,?,?,'New Application',1,'Draft','Pending','Pending',?,?,?)
            """, (
                student_id, cycle, f'{_VISA_TYPE} - {student_id}', _VISA_TYPE, _VISA_TYPE,
                intent['name'], intent['name'], intent['relationship'],
                intent['actor'], occurred_at, occurred_at,
            )).lastrowid
        result = process_revenue_visa_change(connection, visa_id, intent['actor'], occurred_at) or {}
        items = result.get('fee_items', [])
        if (len(items) != 1 or items[0]['net_cents'] != 90000
                or items[0]['currency_code'] != 'AUD'):
            raise _GuardianFeeUnavailable('authoritative A$900 fee unavailable')
        return {
            'aggregate_uid': items[0]['uid'], 'student_id': student_id, 'visa_case_id': visa_id,
            'created_draft': not guardians,
            'applicant_digests': sorted(hashlib.sha256(v.encode()).hexdigest() for v in intent['aliases']),
        }

    try:
        with repo.fee_write_transaction():
            # A cycle can have just one automatic guardian intake. Replays use
            # the recorded outcome even if the profile was subsequently edited.
            previous = connection.execute("""
                SELECT result_json FROM revenue_idempotency_receipts
                WHERE idempotency_key=? AND command_type='parent_guardian_fee_created'
            """, (key,)).fetchone()
            if previous:
                return json.loads(previous[0])
            return repo.command(
                idempotency_key=key, command_type='parent_guardian_fee_created',
                aggregate_type='fee_item', payload={'student_id': student_id, 'cycle_id': cycle},
                actor_user_id=intent['actor'], operation=create,
            )
    except _GuardianFeeUnavailable as exc:
        _LOG.warning('Parent guardian fee deferred for cycle %s: %s', cycle, exc)


def automatic_guardian_draft(connection, student_id, cycle_id, applicant_name):
    """Reuse the intake draft when staff starts work on the same 590 applicant."""
    if cycle_id is None or not connection.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_idempotency_receipts'
    """).fetchone():
        return None
    row = connection.execute("""
        SELECT v.id, v.applicant_name, json_extract(r.result_json,'$.applicant_digests')
        FROM revenue_idempotency_receipts r
        JOIN visa_cases v ON v.id=json_extract(r.result_json,'$.visa_case_id')
        WHERE r.idempotency_key=? AND r.command_type='parent_guardian_fee_created'
          AND json_extract(r.result_json,'$.created_draft')=1
          AND v.student_id=? AND v.application_cycle_id=? AND v.archived_at IS NULL
          AND v.status='Draft' AND v.application_type='New Application'
    """, (_fee_key(cycle_id), student_id, cycle_id)).fetchone()
    if row:
        recorded = set(json.loads(row[2] or '[]'))
        current = hashlib.sha256(_name(row[1]).encode()).hexdigest()
        entered = hashlib.sha256(_name(applicant_name).encode()).hexdigest()
        if entered == current or (current in recorded and entered in recorded):
            return row[0]
    return None

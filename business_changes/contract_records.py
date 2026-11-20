"""Explicit contract invalidation adapter for reviewed legacy persistence."""
from .commands import Mutation
from .contracts import Subject
from .domain import run_domain_write
from .offers import _rows


def publish_generated_contract(conn, *, contract_id, document_id, audit_id, event_type,
                               actor_user_id, occurred_at):
    """Publish a retained generation audit before its existing owner commits.

    The original generation request fingerprint owns retry validation. This
    publisher does not create a contract, promote files or repeat generation.
    """
    from .commands import Command, Outcome, execute_command, _domain_owner
    from .registry import Registry
    from .runtime import enabled
    from .versions import version_of
    conn = _domain_owner(conn)
    if not enabled(conn):
        return
    actions = {'contract.generated': 'published_v2_contract_generated',
               'contract.draft_regenerated': 'published_v2_contract_draft_regenerated'}
    if event_type not in actions:
        raise ValueError('unsupported_contract_generation_event')
    row = conn.execute('SELECT student_id,application_cycle_id FROM contracts WHERE id=?',
                       (contract_id,)).fetchone() if any(
        r[1] == 'application_cycle_id' for r in conn.execute('PRAGMA table_info(contracts)')) else conn.execute(
            'SELECT student_id,NULL FROM contracts WHERE id=?', (contract_id,)).fetchone()
    document = conn.execute('SELECT student_id FROM student_documents WHERE id=?', (document_id,)).fetchone()
    audit = conn.execute('SELECT contract_id,action_type,user_id FROM audit_logs WHERE id=?', (audit_id,)).fetchone()
    if (row is None or document is None or str(row[0]) != str(document[0]) or audit is None
            or str(audit[0]) != str(contract_id) or audit[1] != actions[event_type]
            or str(audit[2]) != str(actor_user_id)):
        raise ValueError('contract_generation_owner_mismatch')
    affected = [Subject('student', str(row[0])), Subject('document', str(document_id)),
                Subject('module', 'contracts'), Subject('module', 'revenue'),
                Subject('dashboard', 'workflow'), Subject('dashboard', 'overview')]
    if row[1] is not None:
        cursor = conn.execute('SELECT * FROM application_cycles WHERE id=?', (row[1],))
        cycle_row = cursor.fetchone()
        cycle = dict(zip((c[0] for c in cursor.description), cycle_row)) if cycle_row else {}
        if str(cycle.get('student_id') or '') != str(row[0]):
            raise ValueError('contract_cycle_owner_mismatch')
        affected.append(Subject('application_cycle', str(row[1])))
        if cycle.get('source_student_case_id') is not None:
            affected.append(Subject('student_case', str(cycle['source_student_case_id'])))
    source = Subject('contract', str(contract_id))
    return execute_command(conn, Command(event_type.replace('.', '_'), 'user', str(actor_user_id),
        str(audit_id), {'contract_id': str(contract_id), 'document_id': str(document_id), 'audit_id': str(audit_id)}),
        registry=Registry([]), authorize=lambda view: None,
        expected_versions={source: version_of(conn, source)},
        operation=lambda view: Outcome({'record_id': str(contract_id)},
            (Mutation(event_type, source, tuple(affected)),)), occurred_at=occurred_at)


def contract_snapshot(conn,contract_id):
    cursor=conn.execute('SELECT * FROM contracts WHERE id=?',(contract_id,));row=cursor.fetchone()
    if row is None:raise ValueError('合同不存在')
    contract=dict(zip((c[0] for c in cursor.description),row))
    paths=tuple(sorted({str(contract.get(k) or '') for k in ('signed_file_path','final_signed_file_path')}-{''}))
    documents=_rows(conn,'student_documents','file_path IN ('+','.join('?' for _ in paths)+')',paths) if paths else ()
    # Keep named document records for exact changed-owner dependencies.
    docs={}
    if paths:
        cursor=conn.execute('SELECT * FROM student_documents WHERE file_path IN ('+','.join('?' for _ in paths)+')',paths)
        keys=[c[0] for c in cursor.description]
        docs={int(r[keys.index('id')]):dict(zip(keys,r)) for r in cursor.fetchall()}
    return contract,_rows(conn,'contract_signing_links','contract_id=?',(contract_id,)),documents,docs


def invalidate_contract(conn,*,database,contract_id,actor_user_id,operation):
    contract_id=int(contract_id)
    def perform(view):
        current=contract_snapshot(view,contract_id)[0]
        if current.get('contract_status') in {'Voided','Deleted'}:return current['contract_status']
        with database.business_scope(view):return operation(current)
    def describe(view,result,before,after):
        affected=[Subject('module','contracts'),Subject('module','revenue'),Subject('dashboard','workflow'),Subject('dashboard','overview')]
        owners={str(row['student_id']) for snapshot in (before,after) for row in (snapshot[0],) if row.get('student_id')}
        changed={i for i in before[3].keys()|after[3].keys() if before[3].get(i)!=after[3].get(i)}
        for identifier in changed:
            affected.append(Subject('document',str(identifier)))
            for snapshot in (before,after):
                document=snapshot[3].get(identifier,{})
                if document.get('student_id'):owners.add(str(document['student_id']))
        affected.extend(Subject('student',owner) for owner in sorted(owners))
        for snapshot in (before,after):
            record=snapshot[0];cycle_id=record.get('application_cycle_id');owner=record.get('student_id')
            if cycle_id is None or not owner:continue
            cursor=view.execute('SELECT * FROM application_cycles WHERE id=?',(cycle_id,));row=cursor.fetchone()
            cycle=dict(zip((c[0] for c in cursor.description),row)) if row else {}
            if str(cycle.get('student_id') or '')==str(owner):
                affected.append(Subject('application_cycle',str(cycle_id)))
                if cycle.get('source_student_case_id') is not None:affected.append(Subject('student_case',str(cycle['source_student_case_id'])))
        return Mutation('contract.invalidated',Subject('contract',str(contract_id)),tuple(affected))
    return run_domain_write(conn,event_type='contract.invalidated',actor_kind='user',actor_ref=str(actor_user_id),
        capture=lambda view:contract_snapshot(view,contract_id),operation=perform,describe=describe)


def update_signing_link(conn, *, database, contract_id, actor_user_id, operation):
    """Only the local persistence step; PDF preparation stays outside the lock."""
    def perform(view):
        current = contract_snapshot(view, contract_id)[0]
        with database.business_scope(view):
            return operation(current)
    def describe(view, result, before, after):
        affected = [Subject('module', 'contracts'), Subject('dashboard', 'workflow'),
                    Subject('dashboard', 'overview')]
        owner = after[0].get('student_id')
        if owner:
            affected.append(Subject('student', str(owner)))
        cycle_id = after[0].get('application_cycle_id')
        if cycle_id is not None:
            cursor = view.execute('SELECT * FROM application_cycles WHERE id=? AND student_id=?', (cycle_id, owner))
            row = cursor.fetchone()
            if row is None:
                raise ValueError('contract_cycle_owner_mismatch')
            cycle = dict(zip((c[0] for c in cursor.description), row))
            affected.append(Subject('application_cycle', str(cycle_id)))
            if cycle.get('source_student_case_id') is not None:
                affected.append(Subject('student_case', str(cycle['source_student_case_id'])))
        return Mutation('contract.signing_link_changed', Subject('contract', str(contract_id)), tuple(affected))
    return run_domain_write(conn, event_type='contract.signing_link_changed',
        actor_kind='user' if actor_user_id is not None else 'system',
        actor_ref=str(actor_user_id) if actor_user_id is not None else 'contract_signing_service',
        capture=lambda view: contract_snapshot(view, contract_id), operation=perform, describe=describe)

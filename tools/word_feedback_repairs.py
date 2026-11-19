#!/usr/bin/env python3
"""Narrow reviewed Word-feedback repairs. Defaults to a strictly read-only plan.

Apply requires an explicit --apply, active authorized actor, reason and timestamp.
Only database records are changed; original files and receipt history are retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGETS = ((106, '20260066', 48, 309), (156, '20260071', 53, 63),
           (98, '20260105', 80, 262))
ACTION = 'word_feedback_record_repaired'


class RepairConflict(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RepairConflict(message)


def rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur]


def one(conn, sql, args=()):
    found = rows(conn, sql, args)
    require(len(found) == 1, f'Expected exactly one record: {sql}')
    return found[0]


def actor(conn, actor_id):
    user = one(conn, '''SELECT u.id,u.username,u.display_name,u.is_active,u.deleted_at,r.permissions_json
                        FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?''', (actor_id,))
    require(user['is_active'] and not user['deleted_at'], 'Actor is inactive')
    permissions = set(json.loads(user.pop('permissions_json')))
    require('*' in permissions or {'offers.manage', 'coe.manage', 'documents.manage', 'students.edit'} <= permissions,
            'Actor lacks repair permissions')
    return {**user, 'can_manage_offer': True}


def state(conn, offer_id):
    offer = one(conn, 'SELECT * FROM offers WHERE id=?', (offer_id,))
    student = offer['student_id']
    result = {'offer': offer,
              'legacy_acceptance': rows(conn, 'SELECT * FROM student_offer_acceptances WHERE student_id=?', (student,)),
              'canonical_acceptance': rows(conn, 'SELECT * FROM cycle_final_acceptances WHERE application_cycle_id=?', (offer['application_cycle_id'],)),
              'offer_state': rows(conn, '''SELECT change.* FROM offer_received_event_state_events change
                                          JOIN offer_received_events event ON event.id=change.offer_received_event_id
                                          WHERE event.legacy_offer_id=? ORDER BY change.id''', (offer_id,))}
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'revenue_commercial_cases','revenue_enrolment_components','revenue_tuition_facts','revenue_exceptions'} <= present:
        components = rows(conn, '''SELECT component.* FROM revenue_enrolment_components component
            JOIN revenue_commercial_cases commercial ON commercial.id=component.commercial_case_id
            WHERE commercial.application_cycle_id=?''',(offer['application_cycle_id'],))
        result['revenue_components'] = components
        result['revenue_tuition_facts'] = []
        result['revenue_exceptions'] = []
        for component in components:
            result['revenue_tuition_facts'].extend(rows(conn,
                'SELECT * FROM revenue_tuition_facts WHERE enrolment_component_id=?',(component['id'],)))
            result['revenue_exceptions'].extend(rows(conn,
                "SELECT * FROM revenue_exceptions WHERE aggregate_type='enrolment_component' AND aggregate_uid=?",
                (component['uid'],)))
    if offer_id == 156:
        result['coe_confirmation'] = rows(conn, '''SELECT confirmation.* FROM final_school_coe_confirmations confirmation
            JOIN cycle_final_acceptances acceptance ON acceptance.id=confirmation.final_acceptance_id
            WHERE acceptance.application_cycle_id=53 AND confirmation.coe_id=284''')
    if offer_id == 98:
        result['confirmed_school'] = rows(conn, 'SELECT * FROM school_plan_confirmed_schools WHERE id=37')
        result['preference'] = rows(conn, 'SELECT * FROM public_application_preferences WHERE id=30')
        result['documents'] = rows(conn, 'SELECT * FROM student_documents WHERE id=1669')
    return result


def protected(conn):
    return {
        'asquith': rows(conn, 'SELECT * FROM school_plan_confirmed_schools WHERE id=38'),
        'asquith_preference': rows(conn, 'SELECT * FROM public_application_preferences WHERE id=31'),
        'public_group': rows(conn, 'SELECT * FROM public_application_groups WHERE id=7'),
        'ybi': rows(conn, 'SELECT * FROM offers WHERE id=99'),
        'ybi_document': rows(conn, 'SELECT * FROM student_documents WHERE id=1670'),
    }


def plan(conn, actor_id):
    actor(conn, actor_id)
    plans = []
    for offer_id, student, cycle_id, school_id in TARGETS:
        before = state(conn, offer_id)
        offer = before['offer']
        require((str(offer['student_id']),offer['application_cycle_id'],offer['school_id']) ==
                (student,cycle_id,school_id), f'Offer {offer_id} identity changed')
        cycle = one(conn, 'SELECT student_id,status FROM application_cycles WHERE id=?', (cycle_id,))
        require(str(cycle['student_id']) == student and cycle['status'] == 'active', f'Cycle {cycle_id} changed')
        event = one(conn, 'SELECT id,student_id,application_cycle_id,school_id FROM offer_received_events WHERE legacy_offer_id=?', (offer_id,))
        require((str(event['student_id']),event['application_cycle_id'],event['school_id']) ==
                (student,cycle_id,school_id), 'Canonical Offer ownership changed')
        done = bool(rows(conn, '''SELECT id FROM audit_logs WHERE action_type=? AND module_name='offers'
                           AND record_id=? AND student_id=? AND status='success' ''',
                         (ACTION,str(offer_id),student)))
        if offer_id in (106,156):
            require(all(r['offer_id'] == offer_id for r in before['legacy_acceptance']),
                    f'Offer {offer_id}: another legacy acceptance is active; refusing to switch')
            active = [r for r in before['canonical_acceptance'] if r['acceptance_status']=='active']
            require(all(r['offer_received_event_id']==event['id'] for r in active),
                    f'Offer {offer_id}: another canonical acceptance is active; refusing to switch')
            require(len(active) <= 1, 'Multiple active acceptances')
            require((bool(active) == bool(before['legacy_acceptance'])) or not before['legacy_acceptance'],
                    'Legacy accepted pointer lacks matching canonical acceptance')
            operations = ['set_accepted_offer without changing signature, payment or submission dates',
                          'existing Revenue hook may add component, official tuition, audit/outbox and missing-entity exception records']
            if offer_id == 156:
                require(bool(offer['signed_offer_file_path']), 'Yanqiu signed Offer attachment is missing')
                one(conn, '''SELECT id FROM student_documents WHERE id=3153 AND student_id=?
                           AND document_type='SignedOffer' AND file_path=? AND is_active=1
                           AND document_status='active' AND voided_at IS NULL''',
                    (student,offer['signed_offer_file_path']))
                coe = one(conn, 'SELECT student_id,application_cycle_id,file_path FROM coe_oshc WHERE id=284')
                require((str(coe['student_id']),coe['application_cycle_id'])==(student,53),'CoE ownership changed')
                one(conn, '''SELECT id FROM student_documents WHERE id=3155 AND student_id=? AND file_path=?
                           AND document_type='COE' AND is_active=1 AND document_status='active'
                           AND voided_at IS NULL''',(student,coe['file_path']))
                operations.append('confirm_school_coe 284 for exact cycle 53; no historical-date inference')
            if done:
                require(bool(before['legacy_acceptance']) and bool(active), 'Repaired acceptance was changed')
                require(offer['offer_status'] == 'accepted', 'Repaired accepted status changed')
                latest = rows(conn, '''SELECT state FROM offer_received_event_state_events
                    WHERE offer_received_event_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1''', (event['id'],))
                require(not latest or latest[0]['state'] == 'ACTIVE', 'Repaired Offer validity state changed')
                if offer_id == 156:
                    require(bool(before['coe_confirmation']), 'Repaired CoE confirmation is missing')
        else:
            require(not before['legacy_acceptance'] and not any(r['acceptance_status']=='active'
                     for r in before['canonical_acceptance']),
                    'Invalid Offer is still actively accepted')
            school = one(conn, 'SELECT * FROM school_plan_confirmed_schools WHERE id=37')
            pref = one(conn, 'SELECT * FROM public_application_preferences WHERE id=30')
            require((str(school['student_id']),school['school_id'],school['confirmation_id'],school['report_id']) ==
                    (student,262,34,131), 'Dulwich confirmed-school identity changed')
            require((pref['confirmed_school_id'],pref['public_application_group_id'],pref['school_id']) ==
                    (37,7,262), 'Dulwich preference identity changed')
            require(offer['offer_file_path'] == offer['signed_offer_file_path'] and bool(offer['offer_file_path']),
                    'Invalid Offer attachment pointers changed')
            path = offer['offer_file_path']
            require(not rows(conn, '''SELECT id FROM offers WHERE id<>98 AND
                           (offer_file_path=? OR signed_offer_file_path=? OR payment_receipt_file_path=?)''',(path,path,path)),
                    'Attachment is shared with another Offer')
            docs = rows(conn, 'SELECT * FROM student_documents WHERE file_path=?',(path,))
            require(len(docs)==1 and docs[0]['id']==1669 and str(docs[0]['student_id'])==student,
                    'Unexpected/shared attachment registration')
            operations = ['record_offer_state INVALID_RECORD, retain receipt history',
                          'Offer98 invalid_record; document1669 inactive/voided; retain physical file',
                          'set_confirmed_school_result37 voided; preference30 closed; school37 is_selected=0']
            if done:
                require(offer['offer_status']=='invalid_record' and school['is_selected']==0
                        and school['result_status']=='voided' and pref['application_status']=='closed'
                        and docs[0]['is_active']==0 and docs[0]['document_status']=='voided'
                        and before['offer_state'] and before['offer_state'][-1]['state']=='INVALID_RECORD',
                        'Previously repaired invalid record was changed')
        plans.append({'offer_id':offer_id,'student_id':student,'cycle_id':cycle_id,
                      'event_id':event['id'],'unchanged':done,'operations':operations,'before':before})
    return plans


def apply_batch(conn, actor_id, reason, stamp):
    require(not conn.in_transaction, 'Caller must not already hold a transaction')
    require(bool(reason.strip()), 'An explicit reason is required')
    require(datetime.fromisoformat(stamp).tzinfo is not None, 'Timestamp must include timezone')
    conn.execute('BEGIN IMMEDIATE')
    try:
        plans = plan(conn, actor_id)
        staff = actor(conn, actor_id)
        untouched = protected(conn)
        from offer_acceptance import set_accepted_offer
        from final_school_coe import confirm_school_coe
        from school_application_tracking import set_confirmed_school_result
        from workflow_offer_events import record_offer_state
        for entry in plans:
            if entry['unchanged']:
                continue
            offer_id, student = entry['offer_id'],entry['student_id']
            if offer_id in (106,156):
                set_accepted_offer(conn, student, offer_id, staff, occurred_at=stamp)
                if offer_id == 156:
                    acceptance = one(conn, '''SELECT id FROM cycle_final_acceptances
                                  WHERE application_cycle_id=53 AND student_id=? AND acceptance_status='active' ''',(student,))
                    confirm_school_coe(conn, student, acceptance['id'],284,actor_id,stamp)
            else:
                record_offer_state(conn,entry['event_id'],state='INVALID_RECORD',occurred_at=stamp,
                                   actor_user_id=actor_id,actor_name=staff['display_name'],reason=reason)
                conn.execute("UPDATE offers SET offer_status='invalid_record' WHERE id=98")
                conn.execute('''UPDATE student_documents SET is_active=0,document_status='voided',
                             voided_at=?,voided_by=?,void_reason=? WHERE id=1669''',(stamp,actor_id,reason))
                set_confirmed_school_result(conn,37,'voided',staff,stamp,note=reason)
                conn.execute('UPDATE school_plan_confirmed_schools SET is_selected=0,updated_at=? WHERE id=37',(stamp,))
            after = state(conn,offer_id)
            conn.execute('''INSERT INTO audit_logs(user_id,username,action_type,module_name,module_key,
                          record_id,student_id,old_value,new_value,status,created_at)
                          VALUES(?,?,?,'offers','offers',?,?,?,?, 'success',?)''',
                         (actor_id,staff['username'],ACTION,str(offer_id),student,
                          json.dumps(entry['before'],ensure_ascii=False),
                          json.dumps({'reason':reason,'state':after},ensure_ascii=False),stamp))
            entry['after'] = after
        require(protected(conn)==untouched,'Unrelated Asquith/group/YBI records changed; whole batch rolled back')
        plan(conn,actor_id)  # Validate all final pointers before committing.
        conn.commit()
        return plans
    except Exception:
        conn.rollback()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True,type=Path)
    parser.add_argument('--actor-id',required=True,type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true',help='Explicitly commit all reviewed repairs atomically')
    mode.add_argument('--dry-run','--dryrun',action='store_true',help='Read-only plan (the default)')
    parser.add_argument('--reason',default='')
    parser.add_argument('--occurred-at')
    args = parser.parse_args(argv)
    uri = args.database.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro')
    with sqlite3.connect(uri,uri=True) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if args.apply:
            require(bool(args.occurred_at),'--occurred-at is required with --apply')
            result=apply_batch(conn,args.actor_id,args.reason,args.occurred_at)
        else:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            result=plan(conn,args.actor_id)
            conn.rollback()
    print(json.dumps({'mode':'apply' if args.apply else 'dryrun','records':result},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

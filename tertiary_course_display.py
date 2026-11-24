"""Course-specific, evidence-bound labels for legacy tertiary Offer cards."""
from datetime import datetime
from html import escape
import json
import sqlite3
from reviewed_tertiary_course import _row, _hash, load_course_links

ACTION='conditional_offer_original_reviewed'


def _conditional_evidence(c,student_id,offer_id):
    offer=_row(c,'SELECT * FROM offers WHERE id=? AND student_id=?',(offer_id,str(student_id)))
    if not offer or offer['offer_status'] not in ('received','sent_to_parent') or offer['document_type']!='Offer' or offer.get('signed_offer_file_path'):
        raise ValueError('需仍未接受的原有Offer。')
    cycle=_row(c,'SELECT * FROM application_cycles WHERE id=? AND student_id=?',(offer['application_cycle_id'],str(student_id)))
    if not cycle or cycle['status']!='active' or cycle['cycle_type']!='initial' or cycle['education_stream']!='higher_education':raise ValueError('周期归属不符。')
    cur=c.execute("SELECT * FROM student_documents WHERE student_id=? AND file_path=? AND document_type='Offer' AND is_active=1 AND document_status='active' AND voided_at IS NULL",(str(student_id),offer['offer_file_path']))
    keys=[x[0] for x in cur.description];docs=[dict(zip(keys,r)) for r in cur]
    if len(docs)!=1:raise ValueError('需唯一有效原Offer。')
    return {'offer':offer,'document':docs[0],'sha256':_hash(offer['offer_file_path'])}


def confirm_conditional_offer(c,*,student_id,offer_id,actor_id,course_name,course_cricos,condition,reason,stamp):
    if not c.in_transaction:raise ValueError('需调用方事务。')
    actor=c.execute('SELECT u.is_active,r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=?',(actor_id,)).fetchone()
    if not actor or actor[0]!=1 or actor[1] not in ('Admin','Manager'):raise PermissionError('需有效核对人员。')
    if not all(str(s or '').strip() for s in (course_name,course_cricos,condition,reason)) or datetime.fromisoformat(stamp).tzinfo is None:raise ValueError('需原件课程、条件及核对理由。')
    evidence=_conditional_evidence(c,student_id,offer_id)
    payload={'version':1,'student_id':str(student_id),'offer_id':int(offer_id),'evidence':evidence,
        'course_name':course_name,'course_cricos':course_cricos,'condition':condition,'reason':reason}
    previous=c.execute("SELECT new_value FROM audit_logs WHERE action_type=? AND module_name='offers' AND student_id=? AND record_id=? ORDER BY id DESC LIMIT 1",(ACTION,str(student_id),str(offer_id))).fetchone()
    if previous and json.loads(previous[0])==payload:return False
    c.execute("INSERT INTO audit_logs(user_id,action_type,module_name,module_key,record_id,student_id,old_value,new_value,created_at) VALUES(?,?,'offers','students',?,?,?,?,?)",
        (actor_id,ACTION,str(offer_id),str(student_id),previous[0] if previous else None,json.dumps(payload,ensure_ascii=False,sort_keys=True),stamp))
    return True


def offer_course_annotations(c,student_id,offer_ids):
    ids={int(x) for x in offer_ids};result={}
    if not ids:return result
    cycles=[r[0] for r in c.execute('SELECT id FROM application_cycles WHERE student_id=?',(str(student_id),))]
    for links in load_course_links(c,cycles).values():
        for link in links:
            oid=int(link['offer_id'])
            if oid not in ids or str(link['student_id'])!=str(student_id):continue
            result[oid]=(f'<div class="notice" data-tertiary-course-evidence="accepted">'
                f'<strong>已接受课程：{escape(link["course_name"])}</strong>'
                f'<p>CRICOS {escape(link["course_cricos"])} · {escape(link["course_start_date"])} 至 {escape(link["course_end_date"])}'
                f' · 原签署日 {escape(link["signed_date"])}</p>'
                '<p>本接受记录仅适用于此课程；后续课程须分别确认。</p></div>')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_logs'").fetchone():return result
    seen=set()
    for oid,encoded in c.execute("SELECT record_id,new_value FROM audit_logs WHERE action_type=? AND module_name='offers' AND student_id=? ORDER BY id DESC",(ACTION,str(student_id))):
        try:oid=int(oid)
        except (ValueError,TypeError):continue
        if oid not in ids or oid in seen:continue
        seen.add(oid)
        try:
            payload=json.loads(encoded)
            if payload['version']!=1 or payload['offer_id']!=oid or payload['student_id']!=str(student_id) or payload['evidence']!=_conditional_evidence(c,student_id,oid):continue
            result[oid]=(f'<div class="notice warn" data-tertiary-course-evidence="conditional"><strong>有条件录取 · 尚未接受：{escape(payload["course_name"])}</strong>'
                f'<p>CRICOS {escape(payload["course_cricos"])} · {escape(payload["condition"])}</p></div>')
        except (ValueError,TypeError,KeyError,OSError,sqlite3.DatabaseError):continue
    return result

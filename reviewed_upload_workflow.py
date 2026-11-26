"""Workflow facts established by a confirmed upload, without financial inference."""
import hashlib
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def render_offer_cycle_selection(connection,student_id,*,selected=None):
    from coe_cycle_selection import render_coe_cycle_selection
    return render_coe_cycle_selection(connection,student_id,selected=selected,initial_only=True).replace(
        'name="coe_application_cycle_id"','name="offer_application_cycle_id"')


def bind_reviewed_offer(connection, student_id, offer_id, stored_form):
    """Ordinary intake uploads use one verified owner; case pickers own their routing."""
    from record_picker_and_uploader import upload_context_from_form
    from student_lifecycle.fact_binding import resolve_coe_application_cycle
    if upload_context_from_form(stored_form) is not None:
        return None
    cycle_id=resolve_coe_application_cycle(connection,student_id,stored_form.get('application_cycle_id'))
    if cycle_id is None:return None
    cycle=connection.execute('SELECT cycle_type FROM application_cycles WHERE id=?',(cycle_id,)).fetchone()
    if not cycle or cycle[0]!='initial':
        raise ValueError('转学或升学 Offer 请从对应业务上传入口提交，不能自动改绑其他申请。')
    row=connection.execute('SELECT student_id,application_cycle_id FROM offers WHERE id=?',(offer_id,)).fetchone()
    if not row or str(row[0])!=str(student_id) or row[1] not in (None,cycle_id):
        raise ValueError('Offer与当前学生或申请周期不匹配。')
    connection.execute('UPDATE offers SET application_cycle_id=? WHERE id=? AND application_cycle_id IS NULL',(cycle_id,offer_id))
    return cycle_id


def prepare_coe_identity(data):
    """Read only the CoE student section before opening the writer transaction."""
    from pypdf import PdfReader
    try:
        text='\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages)
    except Exception:return None
    section=re.search(r'C\.\s*STUDENT DETAILS\s*(.*?)\s*D\.\s*NOTES',text,re.S|re.I)
    if not section:return None
    family=re.search(r'Family Name:\s*([^\r\n]+)',section[1],re.I)
    given=re.search(r'Given Names:\s*([^\r\n]+)',section[1],re.I)
    if not family or not given:return None
    return {'family':family[1].strip(),'given':given[1].strip(),'sha256':hashlib.sha256(data).hexdigest()}


def confirm_matching_school_coe(connection,student_id,coe_id,identity,actor,stamp,*,can_select_offer=False):
    """A unique signed Offer plus matching reviewed CoE establishes enrolment only.

    Existing withdrawals/selections and ambiguous evidence are never replaced.
    Unreadable identity cannot independently establish a new final acceptance.
    """
    from current_coe_evidence import read_course_evidence
    from final_school_coe import eligibility_reason,current_school_coe_rows,confirm_school_coe
    from offer_acceptance import ensure_canonical_offer_event
    from workflow_offer_events import select_final_acceptance
    def row(sql,args):
        cur=connection.execute(sql,args);value=cur.fetchone()
        return dict(zip((x[0] for x in cur.description),value)) if value else None
    student=row('SELECT passport_first_name,passport_last_name FROM students WHERE student_id=?',(student_id,))
    key=lambda text:' '.join(str(text or '').split()).casefold()
    if not identity or not student or (key(identity['given']),key(identity['family']))!=(key(student['passport_first_name']),key(student['passport_last_name'])):
        return False
    coe=row('SELECT * FROM coe_oshc WHERE id=? AND student_id=?',(coe_id,student_id))
    if not coe or str(coe['document_kind']).upper() not in {'COE','COE_CAAW'}:return False
    cycle=row('SELECT * FROM application_cycles WHERE id=? AND student_id=?',(coe['application_cycle_id'],student_id))
    if not cycle or (cycle['status'],cycle['cycle_type'],cycle['education_stream'])!=('active','initial','school_education'):return False
    documents=connection.execute("SELECT id FROM student_documents WHERE student_id=? AND file_path=? AND document_type IN ('COE','COE_CAAW') AND is_active=1 AND document_status='active' AND voided_at IS NULL",(student_id,coe['file_path'])).fetchall()
    if len(documents)!=1 or coe_id not in read_course_evidence(connection, student_id=student_id).current:
        return False
    existing=connection.execute('SELECT id,acceptance_status FROM cycle_final_acceptances WHERE application_cycle_id=?',(cycle['id'],)).fetchall()
    if existing:
        candidates=[r for r in current_school_coe_rows(connection,(cycle['id'],)) if r['coe_id']==coe_id]
        if len(existing)!=1 or existing[0][1]!='active' or len(candidates)!=1:return False
        confirm_school_coe(connection,student_id,existing[0][0],coe_id,actor['id'],stamp)
        return True
    if not can_select_offer:return False
    today=datetime.now(ZoneInfo('Australia/Melbourne')).date().isoformat()
    cur=connection.execute("""SELECT offer.*,school.sector_key AS actual_sector,school.state AS actual_state
        FROM offers offer JOIN schools school ON school.id=offer.school_id
        WHERE offer.student_id=? AND offer.application_cycle_id=?
          AND offer.document_type IN ('SignedOffer','OfferAcceptance')
          AND offer.offer_status IN ('signed','signed_received','accepted')
          AND offer.classification_status='resolved'
          AND (coalesce(offer.offer_expiry_date,'')='' OR offer.offer_expiry_date>=?)
          AND EXISTS (SELECT 1 FROM student_documents d WHERE d.student_id=offer.student_id
               AND d.file_path=offer.signed_offer_file_path AND d.is_active=1
               AND d.document_status='active' AND d.voided_at IS NULL)""",(student_id,cycle['id'],today))
    offers=[dict(zip((x[0] for x in cur.description),r)) for r in cur]
    candidates=[]
    for offer in offers:
        test={**coe,'document_id':documents[0][0],'school_name_snapshot':offer['school_name'],
              'sector_key':offer['actual_sector'],'state':offer['actual_state']}
        if not eligibility_reason(test):candidates.append(offer)
    if len(candidates)!=1:return False
    event=ensure_canonical_offer_event(connection,candidates[0]['id'],actor)
    if event is None:return False
    state=connection.execute('SELECT state FROM offer_received_event_state_events WHERE offer_received_event_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1',(event,)).fetchone()
    if state and str(state[0]).lower()!='active':return False
    acceptance=select_final_acceptance(connection,cycle['id'],event,selected_at=stamp,
        actor_user_id=actor['id'],actor_name=actor.get('username') or actor.get('display_name'),signed_offer_completed_at=stamp)
    confirm_school_coe(connection,student_id,acceptance.id,coe_id,actor['id'],stamp)
    return True

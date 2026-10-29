"""Case-scoped editable labels/classification. No files, network or schema writes.
Original upload name and physical identity never change. Manual category is the
single override authority; classification always uses the immutable upload name.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import re
import unicodedata

CATEGORIES = {'other':'Other','deposit':'Deposit','salary':'Salary','financial_asset':'Financial Asset','grant_letter':'IMMI Grant Letter','refusal_letter':'IMMI Refusal Letter','s56_request':'S56 Request','s56_response':'S56 Response','lodgement_receipt':'Lodgement Receipt','immi_acknowledgment':'IMMI Acknowledgment Letter','bridging_visa':'Bridging Visa','health_examination':'Health Examination','hap_id':'HAP ID','withdrawal_confirmation':'Withdrawal Confirmation'}
LEGACY = {'VisaOther':'other','VisaGrantLetter':'grant_letter','VisaRefusalLetter':'refusal_letter','VisaS56Request':'s56_request','VisaS56Response':'s56_response','VisaLodgementReceipt':'lodgement_receipt','VisaImmiAcknowledgmentLetter':'immi_acknowledgment','VisaBridgingVisa':'bridging_visa','VisaHealthExamination':'health_examination','VisaHapId':'hap_id','VisaWithdrawalConfirmation':'withdrawal_confirmation'}

def _row(conn,sql,args=()):
    cur=conn.execute(sql,args);value=cur.fetchone()
    return dict(zip([d[0] for d in cur.description],value)) if value is not None else None

def _json(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)
@contextmanager
def _atomic(conn):
    conn.execute('SAVEPOINT visa_document_metadata_action')
    try:yield;conn.execute('RELEASE visa_document_metadata_action')
    except Exception:
        conn.execute('ROLLBACK TO visa_document_metadata_action');conn.execute('RELEASE visa_document_metadata_action');raise

def _actor(conn,actor,stamp):
    if isinstance(actor,bool) or not _row(conn,'SELECT id FROM users WHERE id=?',(actor,)):raise ValueError('Valid staff identity required')
    try:datetime.fromisoformat(stamp)
    except (ValueError,TypeError):raise ValueError('Valid audit time required') from None

def _document(conn,case_id,document_id,active=False):
    case=_row(conn,'SELECT id,archived_at FROM visa_cases WHERE id=?',(case_id,))
    doc=_row(conn,'SELECT * FROM visa_case_documents WHERE id=? AND visa_case_id=?',(document_id,case_id))
    if not case or not doc or (active and (case['archived_at'] or doc['archived_at'])):raise ValueError('Document not available in this active case')
    return doc

def normalize_keyword(value):return re.sub(r'[\W_]+',' ',unicodedata.normalize('NFKC',str(value)).casefold()).strip()

def _category(value):
    if value not in CATEGORIES:raise ValueError('Invalid Visa document category')
    return value

def effective_metadata(conn,case_id,document_id):
    doc=_document(conn,case_id,document_id);meta=_row(conn,'SELECT * FROM visa_document_metadata WHERE document_id=?',(document_id,)) or {}
    source=_row(conn,'SELECT document_type FROM student_documents WHERE id=?',(doc['student_document_id'],)) if doc['student_document_id'] else None
    fallback=LEGACY.get(source['document_type'],'other') if source else 'other'
    manual=meta.get('manual_category');auto=meta.get('auto_category')
    return {'document_id':document_id,'display_name':meta.get('display_name') or doc['original_name'],'auto_category':auto,'manual_category':manual,'manual_override':manual is not None,'effective_category':manual or auto or fallback,'classification_evidence':json.loads(meta.get('classification_evidence_json') or '{}')}

def _audit(conn,case_id,document_id,action,before,after,actor,stamp):
    conn.execute('INSERT INTO visa_document_audit(visa_case_id,document_id,action,before_json,after_json,actor_user_id,occurred_at) VALUES(?,?,?,?,?,?,?)',(case_id,document_id,action,_json(before),_json(after),actor,stamp))

def list_rules(conn):
    cur=conn.execute('SELECT * FROM visa_document_keyword_rules ORDER BY priority,length(normalized_keyword) DESC,id');names=[d[0] for d in cur.description]
    return [dict(zip(names,r)) for r in cur.fetchall()]

def classify_document(conn,case_id,document_id,*,actor_user_id,occurred_at):
    _actor(conn,actor_user_id,occurred_at)
    with _atomic(conn):
        doc=_document(conn,case_id,document_id,True);before=effective_metadata(conn,case_id,document_id)
        normalized=normalize_keyword(doc['original_name']);matches=[rule for rule in list_rules(conn) if rule['active'] and rule['normalized_keyword'] in normalized]
        winner=matches[0] if matches else None;category=winner['category'] if winner else None
        evidence={'input_name':doc['original_name'],'normalized_input':normalized,'normalizer':'nfkc_casefold_separators_v1','matching_rule_ids':[r['id'] for r in matches],'winner':winner,'no_match':not matches}
        if before['auto_category']==category and before['classification_evidence']==evidence:return before
        selected=_row(conn,"SELECT after_json FROM visa_document_audit WHERE visa_case_id=? AND document_id=? AND action='document_category_selected' ORDER BY id DESC LIMIT 1",(case_id,document_id))
        effective=before['manual_category'] if before['manual_override'] else json.loads(selected['after_json'])['category'] if selected else category or before['effective_category']
        if effective!=before['effective_category']:
            from visa_document_lifecycle import check_upload
            check_upload(conn,case_id,None,effective,exclude_document_id=document_id)
        conn.execute('''INSERT INTO visa_document_metadata(document_id,auto_category,classification_evidence_json,updated_by,updated_at) VALUES(?,?,?,?,?)
          ON CONFLICT(document_id) DO UPDATE SET auto_category=excluded.auto_category,classification_evidence_json=excluded.classification_evidence_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',(document_id,category,_json(evidence),actor_user_id,occurred_at))
        result=effective_metadata(conn,case_id,document_id);_audit(conn,case_id,document_id,'document_classified',before,result,actor_user_id,occurred_at)
        return result

def edit_document_metadata(conn,case_id,document_id,*,display_name=None,category=None,actor_user_id,occurred_at):
    _actor(conn,actor_user_id,occurred_at)
    if display_name is not None:
        display_name=str(display_name).strip()
        if not display_name or len(display_name)>255 or any(c in display_name for c in '/\\') or any(unicodedata.category(c).startswith('C') for c in display_name):raise ValueError('Display name must be plain text, 1–255 characters')
    if category is not None:_category(category)
    with _atomic(conn):
        _document(conn,case_id,document_id,True);before=effective_metadata(conn,case_id,document_id)
        if display_name is None and category is None:return before
        if category is not None:
            from visa_document_lifecycle import check_upload
            check_upload(conn,case_id,'',category,exclude_document_id=document_id)
        conn.execute('''INSERT INTO visa_document_metadata(document_id,display_name,manual_category,updated_by,updated_at) VALUES(?,?,?,?,?)
          ON CONFLICT(document_id) DO UPDATE SET display_name=COALESCE(excluded.display_name,visa_document_metadata.display_name),manual_category=COALESCE(excluded.manual_category,visa_document_metadata.manual_category),updated_by=excluded.updated_by,updated_at=excluded.updated_at''',(document_id,display_name,category,actor_user_id,occurred_at))
        result=effective_metadata(conn,case_id,document_id);_audit(conn,case_id,document_id,'document_metadata_edited',before,result,actor_user_id,occurred_at)
        return result

def save_rule(conn,*,keyword,category,priority,active,actor_user_id,occurred_at,rule_id=None):
    _actor(conn,actor_user_id,occurred_at);_category(category);keyword=str(keyword).strip();normalized=normalize_keyword(keyword)
    if not normalized or len(keyword)>200:raise ValueError('A core keyword of 1–200 characters is required')
    if type(priority) is not int or not 0<=priority<=10000 or type(active) is not bool:raise ValueError('Priority must be 0–10000; active must be boolean')
    with _atomic(conn):
        before=_row(conn,'SELECT * FROM visa_document_keyword_rules WHERE id=?',(rule_id,)) if rule_id is not None else None
        if rule_id is not None and not before:raise ValueError('Keyword rule not found')
        conflict=_row(conn,'SELECT id FROM visa_document_keyword_rules WHERE normalized_keyword=?',(normalized,))
        if conflict and conflict['id']!=rule_id:raise ValueError('A rule already owns this normalized keyword; edit it instead')
        if before:
            conn.execute('UPDATE visa_document_keyword_rules SET keyword=?,normalized_keyword=?,category=?,priority=?,active=?,updated_by=?,updated_at=? WHERE id=?',(keyword,normalized,category,priority,int(active),actor_user_id,occurred_at,rule_id))
        else:
            rule_id=conn.execute('INSERT INTO visa_document_keyword_rules(keyword,normalized_keyword,category,priority,active,created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(keyword,normalized,category,priority,int(active),actor_user_id,occurred_at,actor_user_id,occurred_at)).lastrowid
        result=_row(conn,'SELECT * FROM visa_document_keyword_rules WHERE id=?',(rule_id,))
        conn.execute('INSERT INTO audit_logs(user_id,action_type,module_name,record_id,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?)',(actor_user_id,'visa_keyword_rule_updated' if before else 'visa_keyword_rule_created','visa_document_keyword_rules',str(rule_id),_json(before or {}),_json(result),occurred_at))
        return result

def deactivate_rule(conn,rule_id,*,actor_user_id,occurred_at):
    rule=_row(conn,'SELECT * FROM visa_document_keyword_rules WHERE id=?',(rule_id,))
    if not rule:raise ValueError('Keyword rule not found')
    return save_rule(conn,rule_id=rule_id,keyword=rule['keyword'],category=rule['category'],priority=rule['priority'],active=False,actor_user_id=actor_user_id,occurred_at=occurred_at)

def delete_rule(conn,rule_id,*,actor_user_id,occurred_at):
    """Logical deletion; preserve rule identity and audits."""
    return deactivate_rule(conn,rule_id,actor_user_id=actor_user_id,occurred_at=occurred_at)

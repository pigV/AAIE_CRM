"""Case-scoped non-AI dates; never modify official Visa decision authority."""
import json
import re
from datetime import date
from visa_documents_candidate import _text_date, _DATE_TOKEN

FIELDS=('acknowledge_sent_on','health_exam_date','grant_sent_on','medical_completed_date')
def _today(value):return date.fromisoformat(str(value)) if value else date.today()
def _field(value='',status='not_detected',source='',document_id=None):
 return dict(value=value,status=status,source=source,document_id=document_id)
def _valid(value,today):
 if not value:return ''
 try:d=date.fromisoformat(value)
 except (TypeError,ValueError):raise ValueError('Enter a valid ISO calendar date.') from None
 if d>_today(today):raise ValueError('Business date cannot be in the future.')
 return d.isoformat()
def parse_processing_dates(text,*,today=None):
 text=str(text or '');lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines() if x.strip()]
 candidates={key:[] for key in ('sent_on','generated','completed')}
 for i,line in enumerate(lines):
  for key,pattern in [('sent_on',r'\bsent\s+on\b|^date(?:\s+of\s+(?:letter|notification))?\s*[:：]'),('generated',r'\bgenerated\b'),('completed',r'\b(?:health\s+|medical\s+)?exam(?:ination)?s?\s+(?:actually\s+)?completed(?:\s+(?:on|date))?\b|\b(?:date\s+(?:of\s+)?(?:health\s+|medical\s+)?exam(?:ination)?\s+completion)\b')]:
   for match in re.finditer(pattern,line,re.I):
    # Stop at the next labelled field; never borrow its date. A wrapped
    # value is accepted only when the next line starts with a date token.
    tail=line[match.end():]
    boundary=re.search(r'\b(?:sent\s+on|generated|date\s+of\s+(?:grant|birth|decision)|length\s+of\s+stay|expiry\s+date)\b',tail,re.I)
    raw=(tail[:boundary.start()] if boundary else tail).strip(' :：-;|')
    if not raw and not boundary and i+1<len(lines) and _DATE_TOKEN.match(lines[i+1]):
     raw=lines[i+1]
     next_label=re.search(r'\b(?:sent\s+on|generated|date\s+of\s+(?:grant|birth|decision)|length\s+of\s+stay|expiry\s+date)\b',raw,re.I)
     if next_label:raw=raw[:next_label.start()].rstrip(' :：-;|')
    tokens=_DATE_TOKEN.findall(raw) or ['']
    for token in tokens:
     value=_text_date(token)
     try:value=_valid(value,today)
     except ValueError:value=''
     candidates[key].append(dict(raw=raw,value=value))
 def pick(key):
  entries=candidates[key]
  if not entries:return _field()
  if any(not e['value'] for e in entries):return _field(status='invalid',source=key)
  values={e['value'] for e in entries}
  return _field(next(iter(values)),'detected',key) if len(values)==1 else _field(status='ambiguous',source=key)
 fields={key:_field() for key in FIELDS};low=text.lower()
 kinds=[]
 if re.search(r'grant notification|date of grant|visa (?:has been )?granted',low):kinds.append('grant_sent_on')
 if any(re.match(r'(?:health\s+(?:exam(?:ination)?|assessment)|medical\s+(?:exam(?:ination)?|letter|request)|examination\s+request)\b',line,re.I) for line in lines):kinds.append('health_exam_date')
 if re.search(r'acknowledg(?:e)?ment|acknowledge letter|lodgement receipt|application received',low):kinds.append('acknowledge_sent_on')
 if len(kinds)==1:
  key=kinds[0];fields[key]=pick('completed' if key=='health_exam_date' else 'sent_on')
 elif kinds:
  for key in kinds:fields[key]=_field(status='ambiguous')
 return dict(version=2,fields=fields,candidates=candidates)
def _rows(conn,sql,args=()):
 cur=conn.execute(sql,args);return [dict(zip([c[0] for c in cur.description],row)) for row in cur.fetchall()]
def _authorize(conn,case_id,actor,stamp,document_id=None):
 if not stamp or not conn.execute('SELECT 1 FROM users WHERE id=?',(actor,)).fetchone():raise ValueError('Valid Staff and timestamp required.')
 if not conn.execute('SELECT 1 FROM visa_cases WHERE id=?',(case_id,)).fetchone():raise ValueError('Case not found.')
 if document_id is not None and not conn.execute('SELECT 1 FROM visa_case_documents WHERE id=? AND visa_case_id=? AND archived_at IS NULL',(document_id,case_id)).fetchone():raise ValueError('Active document must belong to this Case.')
def save_processing_extraction(conn,case_id,document_id,text,*,actor_user_id,occurred_at,today=None):
 _authorize(conn,case_id,actor_user_id,occurred_at,document_id)
 result=parse_processing_dates(text,today=today)
 conn.execute('INSERT INTO visa_processing_date_extractions(visa_case_id,document_id,proposed_json,actor_user_id,occurred_at) VALUES(?,?,?,?,?)',(case_id,document_id,json.dumps(result),actor_user_id,occurred_at))
 return result
def _metrics(fields):
 validation=[];metrics={}
 for name,start in [('total_processing_days','acknowledge_sent_on'),('medical_completed_to_grant_days','medical_completed_date')]:
  a,b=fields[start]['value'],fields['grant_sent_on']['value'];delta=None
  if a and b:
   delta=(date.fromisoformat(b)-date.fromisoformat(a)).days
   if delta<0:validation.append('Grant sent on precedes '+start);delta=None
  metrics[name]=delta
 return metrics,validation
def processing_summary(conn,case_id,*,today=None):
 fields={key:_field() for key in FIELDS}
 docs={d['id']:d for d in _rows(conn,'SELECT id,archived_at FROM visa_case_documents WHERE visa_case_id=?',(case_id,))}
 latest={}
 for p in _rows(conn,'SELECT * FROM visa_processing_date_extractions WHERE visa_case_id=? ORDER BY id',(case_id,)):latest[p['document_id']]=p
 for key in FIELDS:
  choices=[]
  for did,p in latest.items():
   if did not in docs or docs[did]['archived_at']:continue
   item=json.loads(p['proposed_json'])['fields'][key]
   if key=='health_exam_date' and item.get('source')!='completed':continue
   if item['status']!='not_detected':choices.append(dict(item,document_id=did))
  if len(choices)==1:fields[key]=choices[0]
  elif choices:fields[key]=_field(status='ambiguous')
 for rev in _rows(conn,'SELECT * FROM visa_processing_date_revisions WHERE visa_case_id=? ORDER BY id',(case_id,)):
  did=rev['document_id']
  if rev['value']:fields[rev['field']]=_field(rev['value'],'manual','staff',did)
  else:
   # Clearing the latest manual revision releases the auto value.
   fields[rev['field']]=_field()
   choices=[]
   for source_id,p in latest.items():
    if source_id not in docs or docs[source_id]['archived_at']:continue
    item=json.loads(p['proposed_json'])['fields'][rev['field']]
    if rev['field']=='health_exam_date' and item.get('source')!='completed':continue
    if item['status']!='not_detected':choices.append(dict(item,document_id=source_id))
   if len(choices)==1:fields[rev['field']]=choices[0]
   elif choices:fields[rev['field']]=_field(status='ambiguous')
 for item in fields.values():
  try:_valid(item['value'],today)
  except ValueError:item.update(value='',status='invalid')
 metrics,validation=_metrics(fields)
 return dict(fields=fields,validation=validation,**metrics)
def correct_processing_dates(conn,case_id,values,*,actor_user_id,occurred_at,source_document_ids=None,today=None):
 _authorize(conn,case_id,actor_user_id,occurred_at)
 if not isinstance(values,dict) or not values or set(values)-set(FIELDS):raise ValueError('Unsupported processing date fields.')
 sources=source_document_ids or {}
 if set(sources)-set(values):raise ValueError('Source supplied without a date field.')
 before=processing_summary(conn,case_id,today=today);fields={k:dict(v) for k,v in before['fields'].items()};updates=[]
 for key,value in values.items():
  value=_valid(str(value or '').strip(),today);did=sources.get(key)
  if did is not None:_authorize(conn,case_id,actor_user_id,occurred_at,did)
  if key=='medical_completed_date' and did is not None:raise ValueError('Medical completion is a Staff fact, not a letter date.')
  fields[key]=_field(value,'manual','staff',did);updates.append((case_id,key,value,did,json.dumps(before['fields'][key]),actor_user_id,occurred_at))
 _,errors=_metrics(fields)
 if errors:raise ValueError('; '.join(errors))
 conn.execute('SAVEPOINT processing_correction')
 try:
  conn.executemany('INSERT INTO visa_processing_date_revisions(visa_case_id,field,value,document_id,before_json,actor_user_id,occurred_at) VALUES(?,?,?,?,?,?,?)',updates)
  conn.execute('RELEASE processing_correction')
 except Exception:
  conn.execute('ROLLBACK TO processing_correction');conn.execute('RELEASE processing_correction');raise
 return processing_summary(conn,case_id,today=today)

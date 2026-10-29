"""Immutable, evidence-bound Visa AI proposals; never confirms a decision."""
import json
from visa_documents_candidate import _actor, _document, _atomic, _audit, _row, _json, propose_letter, submitted_documents

def _manifest(rows):
 return sorted((int(r['document_id']),int(r['version']),r['sha256']) for r in rows)

def save_ai_letter(conn,case_id,document_id,envelope,*,actor_user_id,occurred_at):
 _actor(conn,actor_user_id,occurred_at);doc=_document(conn,case_id,document_id)
 expected=[{'document_id':doc['id'],'version':doc['version'],'sha256':doc['sha256']}]
 if _manifest(envelope['metadata']['source_evidence'])!=_manifest(expected):raise ValueError('AI source evidence changed; run detection again.')
 if doc['archived_at']:raise ValueError('Archived evidence cannot produce a new proposal.')
 result=envelope['result']
 if set(result)!=set(propose_letter('')) or result['medical_completed_date']:raise ValueError('Invalid AI letter proposal.')
 with _atomic(conn):
  cursor=conn.execute('INSERT INTO visa_letter_extractions(visa_case_id,document_id,proposed_json,extraction_method,created_by,created_at) VALUES(?,?,?,?,?,?)',(case_id,document_id,_json(result),envelope['metadata']['provider']+':'+envelope['metadata']['model'],actor_user_id,occurred_at))
  row=_row(conn,'SELECT * FROM visa_letter_extractions WHERE id=?',(cursor.lastrowid,))
  _audit(conn,case_id,document_id,'ai_letter_extracted',{},dict(envelope,extraction_id=row['id']),actor_user_id,occurred_at)
 return row

def save_ai_analysis(conn,case_id,envelope,*,actor_user_id,occurred_at):
 _actor(conn,actor_user_id,occurred_at)
 current=submitted_documents(conn,case_id)
 if not current or _manifest(current)!=_manifest(envelope['metadata']['source_evidence']):raise ValueError('Submitted evidence changed; run analysis again.')
 _audit(conn,case_id,None,'ai_analysis_completed',{},envelope,actor_user_id,occurred_at)

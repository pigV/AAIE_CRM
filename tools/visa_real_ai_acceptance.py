#!/usr/bin/env python3
"""Real HTTPS model acceptance using only fixtures embedded in this source.

No provider injection, arbitrary input files, database access or client documents.
Default mode is DEFERRED by release design: no configuration lookup or requests.
The future real-model path requires explicit --live and approved configuration.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from visa_ai_candidate import VisaAIProvider, VisaAIError, provider_from_environment

BASE=ROOT/'SAFEBOX/visa-only-release-20260907/real-ai-acceptance'
PREPARED_MARKER='SYNTHETIC_PREPARED_ONLY_ZEBRA_74391'


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8')
def digest(data):return hashlib.sha256(data).hexdigest()

def scenarios():
 empty={'document_kind':'unknown','decision_type':'','decision_date':'','official_date':'','length_of_stay':'','expiry_date':'','medical_completed_date':''}
 specs=[
 ('A-grant','A','SYNTHETIC VISA GRANT NOTIFICATION\nDate of grant: 5 September 2026\nLength of stay: Until 15 March 2029\nThis is a fictional test document. No real person is described.',dict(document_kind='grant',decision_type='grant',decision_date='2026-09-05',official_date='2026-09-05',length_of_stay='Until 15 March 2029',expiry_date='2029-03-15')),
 ('B-refusal','B','SYNTHETIC VISA REFUSAL NOTIFICATION\nDate of decision: 4 September 2026\nThis fictional request was refused. No visa was granted.',dict(document_kind='refusal',decision_type='refusal',decision_date='2026-09-04',official_date='2026-09-04')),
 ('C-medical','C','SYNTHETIC HEALTH EXAMINATION REQUEST\nDate of letter: 3 September 2026\nAn examination is requested. Completion has not been attested. Fictional only.',dict(document_kind='medical',official_date='2026-09-03')),
 ('D-lodgement','D','SYNTHETIC APPLICATION LODGEMENT RECEIPT\nDate of lodgement: 2 September 2026\nApplication received. No decision has been made. Fictional only.',dict(document_kind='lodgement_receipt',official_date='2026-09-02'))]
 cases=[]
 for sid,group,text,expected in specs:
  evidence={'document_id':1,'version':1,'sha256':digest(text.encode())}
  cases.append(dict(id=sid,group=group,synthetic=True,task='letter',text=text,evidence=evidence,expected={**empty,**expected}))
 def analysis(sid,group,text,ownership,**extra):
  row={'document_id':1,'submission_id':1,'version':1,'sha256':digest(text.encode()),'text':text}
  cases.append(dict(id=sid,group=group,synthetic=True,task='analysis',context={'scope':'IMMI_SUBMITTED_ONLY','documents':[row],'ownership':ownership,'prepared_documents':[{'text':PREPARED_MARKER}]},**extra))
 analysis('E-submitted-scope','E','SYNTHETIC SUBMITTED_ONLY_EVIDENCE_BLUE_46281: The application receipt is present.',{},required_marker='SUBMITTED_ONLY_EVIDENCE_BLUE_46281')
 for key,owner in [('applicant','Applicant'),('father','Father'),('mother','Mother'),('parents-jointly','Parents jointly')]:
  asset='SYNTHETIC_ASSET_'+key.upper().replace('-','_')
  text=f'SYNTHETIC ownership evidence. {owner} owns {asset}. Asset type: property. '+('The Applicant does not own this asset.' if owner!='Applicant' else 'No other person owns this asset.')
  analysis('F-'+key,'F',text,{'assets':[{'asset_name':asset,'owner':owner,'asset_type':'Property'}]},asset=asset,owner=owner)
 funding=[('Savings',10000),('Employment Income',40000),('Investment',5000)]
 text='SYNTHETIC MULTIPLE FUNDING EVIDENCE. '+' '.join(f'Father provides {kind} of AUD {amount}.' for kind,amount in funding)
 analysis('G-multiple-funding','G',text,{'funds':[{'owner':'Father','source':kind,'amount':amount,'currency':'AUD'} for kind,amount in funding]},funding=funding)
 analysis('H-presentation','H','SYNTHETIC presentation evidence: an application receipt is available; medical completion has not been confirmed.',{})
 return cases


def _owner_assertion(text,asset,owner):
 a=re.escape(asset);o=re.escape(owner)
 patterns=[rf'\b{o}\s+(?:solely\s+)?owns?\s+(?:the\s+)?{a}\b',rf'\b{a}\b[^.!\n]{{0,50}}\b(?:owned by|owner\s*[:=]|belongs to)\s+(?:the\s+)?{o}\b',rf'\b{o}[’\']s\s+(?:property|asset)\s+{a}\b']
 return next((m.group(0) for pattern in patterns if (m:=re.search(pattern,text,re.I))),None)


def evaluate(scenario,result,metadata):
 incorrect=[];missing=[];hallucinated=[];assertions=[]
 if not metadata.get('evidence_ids') or any(i!=1 for i in metadata.get('evidence_ids',[])):missing.append('evidence_ids')
 if scenario['task']=='letter':
  for key,value in scenario['expected'].items():
   if key not in result:missing.append(key)
   elif result[key]!=value:incorrect.append(key)
  correct=len(scenario['expected'])-len(set(incorrect+missing)&set(scenario['expected']))
 else:
  for key in ('Summary','Risk','Missing','Action Required','full_analysis'):
   if not isinstance(result.get(key),str) or not result[key].strip():missing.append(key)
  text='\n'.join(str(v) for v in result.values())
  if PREPARED_MARKER in text:hallucinated.append('prepared_document_in_analysis')
  if scenario.get('required_marker') and scenario['required_marker'] not in text:missing.append('submitted_source_attribution')
  if scenario['group']=='F':
   match=_owner_assertion(text,scenario['asset'],scenario['owner'])
   if match:assertions.append(match)
   else:missing.append('explicit_asset_owner_relationship')
   for owner in ('Applicant','Father','Mother','Parents jointly'):
    if owner!=scenario['owner'] and _owner_assertion(text,scenario['asset'],owner):hallucinated.append('asset_owner:'+owner)
  if scenario['group']=='G':
   for source,amount in scenario['funding']:
    # The same assertion must state owner, source and amount; words alone do not pass.
    pattern=rf'\bFather\s+(?:provides|has|earns|holds)\s+{re.escape(source)}\s+(?:of\s+)?AUD\s*{amount:,}\b'
    alternative=pattern.replace(f'{amount:,}',str(amount))
    match=re.search(pattern,text,re.I) or re.search(alternative,text,re.I)
    if match:assertions.append(match.group(0))
    else:missing.append('funding_owner_source_amount:'+source)
  correct=None
 return {'status':'FAIL' if incorrect or missing or hallucinated else 'PASS','incorrect_fields':incorrect,'missing_fields':missing,'hallucinated_fields':hallucinated,'supported_assertions':assertions,'exact_letter_fields_correct':correct,'exact_letter_fields_total':len(scenario.get('expected',{})),'manual_review':'Review retained raw result; absence of an explicit supported assertion is FAIL, not inferred PASS.'}


def acceptance_status(evaluation):
 """Mechanical assertions cannot certify semantic correctness or completeness."""
 automated=evaluation['status']
 return {**evaluation,'automated_check_status':automated,'status':'REVIEW_PENDING' if automated=='PASS' else automated}


class RecordedRealProvider(VisaAIProvider):
 """Instrumentation always delegates to actual HTTPS implementation; no fake path."""
 def __init__(self,*,api_key,model,output):
  self.output=output;self.request_count=0;self.current_case=''
  super().__init__(api_key=api_key,model=model)
 def _send(self,payload,timeout):
  raw=encoded(payload)
  if PREPARED_MARKER.encode() in raw:raise VisaAIError('ACCEPTANCE_PREPARED_INPUT_LEAK')
  (self.output/(self.current_case+'-request.json')).write_bytes(raw)
  self.request_count+=1
  return super()._send(payload,timeout)


def run(run_id, enable_live=False):
 if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',run_id):raise ValueError('Use a single safe run ID')
 output=BASE/run_id;output.mkdir(parents=True,exist_ok=False)
 cases=scenarios();(output/'synthetic-fixtures.json').write_bytes(encoded(cases))
 if not enable_live:
  reason='AI PROVIDER NOT YET CONFIGURED BY BUSINESS DESIGN'
  report={'status':'DEFERRED','reason':reason,'runtime_status':'NOT CONFIGURED BY DESIGN','current_release_mode':'AI-READY / AI-LIVE = NO','synthetic_data_only':True,'request_count':0,'expected_request_count':0,'scenario_count':len(cases),'model':None,'provider':None,'source_sha256':digest(Path(__file__).read_bytes()),'provider_source_sha256':digest((ROOT/'visa_ai_candidate.py').read_bytes()),'fixture_sha256':digest(encoded(cases)),'results':[{'id':c['id'],'group':c['group'],'status':'DEFERRED','reason':reason} for c in cases],'future_gate':['AI Provider Configuration','Secret Configuration','Real Model Acceptance','Production AI Enablement']}
  (output/'report.json').write_bytes(encoded(report))
  print(json.dumps({'status':report['status'],'request_count':0,'expected_request_count':0,'report':str(output/'report.json')}))
  return 0
 missing=[key for key in ('VISA_AI_PROVIDER','VISA_AI_MODEL','OPENAI_API_KEY') if not os.environ.get(key)]
 report={'status':'NOT RUN','synthetic_data_only':True,'started_at':datetime.now(timezone.utc).isoformat(),'request_count':0,'scenario_count':len(cases),'provider':'openai_responses','model':None,'missing_configuration':missing,'source_sha256':digest(Path(__file__).read_bytes()),'provider_source_sha256':digest((ROOT/'visa_ai_candidate.py').read_bytes()),'fixture_sha256':digest(encoded(cases)),'results':[], 'staff_confirmation':'NOT RUN by this harness; attach separate synthetic application/browser confirmation and correction evidence.', 'failure_boundary_tests':'Separate injected failure tests are not real model/network observations.'}
 try:
  configured=provider_from_environment()
 except VisaAIError as exc:
  configured=None;report['configuration_error']=exc.code
 if configured is None:
  report['results']=[{'id':s['id'],'group':s['group'],'status':'NOT RUN','reason':'Explicit Visa provider/model configuration unavailable; no request attempted.'} for s in cases]
 else:
  provider=RecordedRealProvider(api_key=os.environ['OPENAI_API_KEY'],model=configured.model,output=output)
  report['model']=provider.model
  for scenario in cases:
   provider.current_case=scenario['id'];started=time.monotonic();before=provider.request_count
   record={'id':scenario['id'],'group':scenario['group'],'expected':scenario.get('expected') or {k:scenario[k] for k in ('asset','owner','funding','required_marker') if k in scenario}}
   try:
    envelope=provider.extract_letter(scenario['text'],scenario['evidence']) if scenario['task']=='letter' else provider.analyze(scenario['context'])
    record.update(acceptance_status(evaluate(scenario,envelope['result'],envelope['metadata'])));record['actual']=envelope
   except VisaAIError as exc:
    record.update(status='FAIL',error_code=exc.code,actual=None)
   record.update(request_attempts=provider.request_count-before,elapsed_seconds=round(time.monotonic()-started,3))
   (output/(scenario['id']+'-result.json')).write_bytes(encoded(record));report['results'].append(record)
   report['request_count']=provider.request_count
   (output/'report.json').write_bytes(encoded(report))
  report['status']='AUTOMATED_CHECKS_PASS_REVIEW_PENDING' if all(r['status']=='REVIEW_PENDING' for r in report['results']) else 'FAIL'
 (output/'report.json').write_bytes(encoded(report))
 print(json.dumps({'status':report['status'],'request_count':report['request_count'],'report':str(output/'report.json')}))
 return 0 if report['status']=='PASS' else 2


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-id',default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));parser.add_argument('--live',action='store_true',help='Future AI enablement gate only; explicitly permit configured model requests.');args=parser.parse_args();return run(args.run_id,enable_live=args.live)
if __name__=='__main__':raise SystemExit(main())

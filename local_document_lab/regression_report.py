"""Grade only existing references, and audit remaining candidates without inventing truth."""
from pathlib import Path
import json
import hashlib
import statistics
from local_document_lab.audit import audit_result
from local_document_lab.evidence import compact
from local_document_lab.sandbox import DATA


def prediction_is_current(prediction, entry, run, packet_sha):
    return (entry.get('inference',{}).get('returncode')==0
        and entry.get('projection',{}).get('returncode')==0
        and prediction.get('status')=='completed_requires_review'
        and prediction.get('projection_version')==1
        and prediction.get('packet_sha256')==packet_sha
        and prediction.get('prompt_sha256')==run['prompt_sha256']
        and prediction.get('model_revision')==run['model_revision']
        and (not run.get('implementation_sha256') or prediction.get('implementation_sha256')==run['implementation_sha256'])
        and (not run.get('model_inventory_sha256') or prediction.get('model_inventory_sha256')==run['model_inventory_sha256']))


def main():
    run=json.loads((DATA/'results/v2-regression-run.json').read_text())
    from local_document_lab.provenance import implementation_signature
    root=Path(__file__).resolve().parents[1]
    if not run.get('implementation_sha256') or not run.get('model_inventory_sha256'):
        raise ValueError('Run signatures have not been sealed')
    if implementation_signature(root)!=run['implementation_sha256']:
        raise ValueError('Inference implementation changed since run')
    receipt=DATA/'models'/run['model']/'download-receipt.json'
    if hashlib.sha256(receipt.read_bytes()).hexdigest()!=run['model_inventory_sha256']:
        raise ValueError('Model inventory changed since verification')
    corpus=json.loads((DATA/'corpus/manifest.json').read_text())
    reference=json.loads((DATA/'corpus/reference-fields.json').read_text())['fields']
    projection_file=Path(__file__).resolve().parent/'project.py'
    if hashlib.sha256(projection_file.read_bytes()).hexdigest()!=run['projection_sha256']:
        raise ValueError('Projection implementation changed since run')
    entries={item['id']:item for item in run['runs']}
    if len(entries)!=len(corpus) or set(entries)!={case['id'] for case in corpus}:
        raise ValueError('Run and corpus identities differ')
    model=run['model'];cases=[];totals={'expected':0,'correct':0,'wrong':0,'missing':0}
    durations=[];loads=[];peaks=[]
    for case in corpus:
        key=case['id'];path=DATA/'results'/(key+'.v2.projected.'+model+'.json')
        prediction=json.loads(path.read_text()) if path.exists() else {}
        packet=json.loads((DATA/'results'/(key+'.packet.json')).read_text())
        source_sha=hashlib.sha256((Path(__file__).resolve().parents[1]/case['path']).read_bytes()).hexdigest()
        identity_ok=(packet.get('id')==case['id'] and packet.get('kind')==case['kind']
            and packet.get('source_sha256')==source_sha and case['sha256']==source_sha)
        current=identity_ok and prediction_is_current(prediction,entries[key],run,hashlib.sha256((DATA/'results'/(key+'.packet.json')).read_bytes()).hexdigest())
        value=prediction.get('result',{}) if current else None
        audit=audit_result(case['kind'],value,packet['blocks'])
        if not current:audit['issues'].append('stale_or_failed_prediction')
        grade={}
        for field,want in reference.get(key,{}).items():
            got=(value or {}).get('fields',{}).get(field,{}).get('value')
            verdict='missing' if not got else 'correct' if compact(got)==compact(want) else 'wrong'
            totals['expected']+=1;totals[verdict]+=1;grade[field]=verdict
        if current:
            durations.append(prediction['inference_seconds']);loads.append(prediction['load_seconds']);peaks.append(prediction['rss_peak_bytes'])
        cases.append({'id':key,'kind':case['kind'],'model_status':prediction.get('status','missing') if current else 'stale_or_failed_prediction',
                      'source_identity_verified':identity_ok,'current_run_verified':bool(current),
                      'reference_grade':grade,'has_reference':bool(grade),'audit':audit,
                      'label_note':'exploratory_reference_not_blind' if grade else 'unlabelled_no_accuracy_claim'})
    fresh=[item['inference']['elapsed_seconds'] for item in run['runs'] if 'elapsed_seconds' in item['inference']]
    result={'evaluation':'existing_exploratory_corpus_not_blind','model':model,
            'model_revision':run['model_revision'],'prompt_sha256':run['prompt_sha256'],
            'projection_sha256':run['projection_sha256'],'implementation_sha256':run['implementation_sha256'],
            'model_inventory_sha256':run['model_inventory_sha256'],'case_count':len(cases),
            'new_inference_runs':len(fresh),'reused_hash_matched_inferences':len(cases)-len(fresh),
            'reference_totals':totals,'unlabelled_case_count':sum(not c['has_reference'] for c in cases),
            'complete_candidate_count':sum(c['audit']['candidate_complete'] for c in cases),
            'cases_with_issues':sum(bool(c['audit']['issues']) for c in cases),
            'cases_with_missing_fields':sum(bool(c['audit']['missing_fields']) for c in cases),
            'inference_median_seconds':statistics.median(durations) if durations else None,
            'load_median_seconds':statistics.median(loads) if loads else None,
            'fresh_worker_median_seconds':statistics.median(fresh) if fresh else None,
            'peak_rss_bytes':max(peaks) if peaks else None,'cases':cases,
            'semantic_verified':False,'production_ready':False,
            'limits':['Only existing references graded','Grounding does not establish semantic correctness',
                      'No 30-50 document grouped blind test','No CRM adapter or production integration']}
    (DATA/'results/v2-regression-evaluation.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('cases','limits')}),flush=True)

if __name__=='__main__':main()

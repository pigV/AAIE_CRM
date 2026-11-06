"""Evaluate explicitly labelled exploratory fields; never infer labels from predictions."""
import json
from pathlib import Path
import statistics
from local_document_lab.sandbox import DATA


def normalized(value):
    return ' '.join(str(value or '').split()).casefold()


def main():
    reference=json.loads((DATA/'corpus/reference-fields.json').read_text())['fields']
    comparisons=[]
    for model in ('Qwen3-4B-Instruct-2507-4bit','Qwen3-8B-4bit'):
        counts={'expected':0,'correct':0,'wrong':0,'missing':0}
        cases=[];latencies=[];memory=[]
        for key,truth in reference.items():
            path=DATA/'results'/(key+'.'+model+'.json')
            prediction=json.loads(path.read_text()) if path.exists() else {}
            result=prediction.get('result',{}).get('fields',{})
            scores={}
            for field,want in truth.items():
                value=result.get(field,{}).get('value')
                verdict='missing' if not value else 'correct' if normalized(value)==normalized(want) else 'wrong'
                counts['expected']+=1;counts[verdict]+=1;scores[field]=verdict
            cases.append({'id':key,'fields':scores,'status':prediction.get('status','not_run')})
        corpus=json.loads((DATA/'corpus/manifest.json').read_text())
        for case in corpus:
            path=DATA/'results'/(case['id']+'.'+model+'.json')
            if not path.exists():continue
            prediction=json.loads(path.read_text())
            latencies.append(prediction['inference_seconds']);memory.append(prediction['rss_peak_bytes'])
        filled=counts['correct']+counts['wrong']
        comparisons.append({'model':model,'labelled_fields':counts,
            'filled_accuracy':counts['correct']/filled if filled else None,
            'coverage':filled/counts['expected'] if counts['expected'] else None,
            'inference_median_seconds':statistics.median(latencies) if latencies else None,
            'peak_rss_bytes':max(memory) if memory else None,'cases':cases})
    report={'evaluation':'exploratory_not_blind','warning':'Only the listed reference fields are graded. Grounding and JSON validity do not prove semantic correctness. No production acceptance.',
            'models':comparisons}
    (DATA/'results/evaluation.json').write_text(json.dumps(report,indent=2))
    for item in comparisons:print(json.dumps({k:v for k,v in item.items() if k!='cases'}),flush=True)

if __name__=='__main__':main()

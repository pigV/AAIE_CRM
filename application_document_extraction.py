"""Offline application-document candidates, enabled only in this isolated development runtime.

This module never confirms files or writes CRM business records. All candidates need
human review. Source quotes are validated again at the adapter boundary.
"""
from copy import deepcopy
import hashlib
import fcntl
import os
import time
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from local_document_lab.evidence import compact, validate_result
from local_document_lab.project import project_program_fields
from local_document_lab.layout_dates import resolve_language_dates
from local_document_lab.welfare_context import welfare_context
from local_document_lab.table_roles import resolve_table_roles

ROOT = Path(__file__).resolve().parent
KINDS = {"Offer":"offer", "SignedOffer":"offer", "OfferAcceptance":"offer",
         "COE":"coe", "CAAW":"caaw", "LetterOfConfirmation":"confirmation"}
MAPS = {
 "offer":{"main_school":"school_name", "language_school":"language_school_name", "course_start_date":"course_start_date"},
 "coe":{"provider":"coe_provider", "course":"coe_course", "course_start_date":"coe_start_date", "course_end_date":"coe_end_date"},
 "caaw":{"provider":"coe_provider", "welfare_start_date":"caaw_welfare_start_date", "welfare_end_date":"caaw_welfare_end_date"},
 "confirmation":{"main_school":"letter_main_course_name", "language_school":"letter_language_course_name",
                 "language_start_date":"letter_language_start_date", "language_end_date":"letter_language_end_date", "year_level":"letter_year_level"},
}


def review_candidates(kind, raw, blocks):
    if kind not in MAPS:
        return {}
    raw = resolve_language_dates(raw, blocks) if kind in ('offer','confirmation') else raw
    raw, anchored_course_dates = resolve_table_roles(kind, raw, blocks)
    checked = project_program_fields(validate_result(raw, blocks))
    fields = checked['fields']
    candidates = {}

    def put(target, claim):
        if not claim or claim.get('status') != 'supported_needs_review' or not claim.get('value'):
            return
        candidates[target] = {**claim, 'status':'needs_review', 'message':'本机识别候选，请核对原文',
                              'snippet':((claim['context_quote']+'\n') if claim.get('context_quote') else '')+claim['quote'],
                              'requires_human_review':True}

    for source, target in MAPS[kind].items():
        claim = fields.get(source)
        if source == 'main_school' and 'multiple_main_schools' in checked['issues']:
            continue
        if source.startswith('language_') and 'multiple_language_programs' in checked['issues']:
            continue
        # A course date is not evidence of a welfare date even if it exists in text.
        if kind == 'caaw' and source.startswith('welfare_') and claim:
            text = compact(claim.get('quote'))
            if 'welfare' not in text:
                context = welfare_context(source, claim, blocks)
                if context is None:
                    continue
                claim = {**claim, 'context_block_id':context['id'], 'context_quote':context['text']}
        if kind == 'offer' and source == 'course_start_date' and claim:
            if source not in anchored_course_dates and not re.search(r'\b(?:course|program)\s+(?:start|commencement)\s+date\b', compact(claim.get('quote'))):
                continue  # A bare range can belong to a different program.
        put(target, claim)
    if kind == 'confirmation':
        main = [p['fields'] for p in checked['programs'] if p['kind'] == 'main']
        # Only collapse an entire, explicitly dated set; missing segments stay unknown.
        if main and all(p.get(k, {}).get('value') for p in main for k in ('course_start_date','course_end_date')):
            put('letter_main_start_date', min((p['course_start_date'] for p in main), key=lambda c:c['value']))
            put('letter_main_end_date', max((p['course_end_date'] for p in main), key=lambda c:c['value']))
        for sector in ('primary','secondary'):
            schools = {}
            for program in main:
                course = program.get('course', {})
                school = program.get('school', {})
                # Sector describes education, never ordering of courses.
                if re.search(r'\b'+sector+r'\b', compact(course.get('value'))) and school.get('value'):
                    schools[compact(school['value'])] = school
            if len(schools) == 1:
                put(sector+'_main_course_school', next(iter(schools.values())))
    return candidates


def merge_review(data, candidates):
    result = deepcopy(data)
    for key, claim in candidates.items():
        if key not in result['fields']:
            continue
        old = result['fields'][key]
        if str(old.get('value') or '').strip():
            if compact(old['value']) != compact(claim['value']):
                old['local_candidate'] = deepcopy(claim)
        else:
            result['fields'][key] = deepcopy(claim)
    return result


def atomic_json(path, data):
    path=Path(path)
    temp=path.with_name(path.name+'.'+uuid4().hex)
    temp.write_text(json.dumps(data,ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(path)


class DirectoryReviewQueue:
    """The sandboxed web app only writes requests; an independent local broker runs workers."""
    def __init__(self, root, signature, max_jobs=16):
        self.root=Path(root);self.signature=signature;self.max_jobs=max_jobs
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)

    def submit(self, source, kind):
        fallback={'status':'manual_review','fields':{}}
        if kind not in MAPS or not isinstance(source,bytes) or not source.startswith(b'%PDF-') or len(source)>30*1024**2:
            return fallback
        key=hashlib.sha256(source+b'\0'+kind.encode()+self.signature.encode()).hexdigest()
        source_sha=hashlib.sha256(source).hexdigest()
        identity={'source_sha256':source_sha,'kind':kind,'signature':self.signature,'job_id':key}
        folder=self.root/key
        try:
            with (self.root/'.queue.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                if folder.exists():
                    request=json.loads((folder/'request.json').read_text())
                    state=json.loads((folder/'state.json').read_text())
                    if request!=identity or hashlib.sha256((folder/'source.pdf').read_bytes()).hexdigest()!=source_sha:
                        return fallback
                    if state.get('status') in ('completed','manual_review'):
                        return state if all(state.get(k)==v for k,v in identity.items()) else fallback
                heartbeat=json.loads((self.root/'heartbeat.json').read_text())
                if not 0<=time.time()-heartbeat['updated_at']<5:
                    return fallback
                if not folder.exists():
                    pending=0
                    for path in self.root.glob('*/state.json'):
                        state=json.loads(path.read_text())
                        pending+=state.get('status') in ('queued','running') and state.get('signature')==self.signature
                    if pending>=self.max_jobs:return fallback
                    folder.mkdir(mode=0o700)
                    (folder/'source.pdf').write_bytes(source)
                    (folder/'source.pdf').chmod(0o600)
                    atomic_json(folder/'request.json',identity)
                    atomic_json(folder/'state.json',{**identity,'status':'queued','fields':{}})
                state=json.loads((folder/'state.json').read_text())
                return state if all(state.get(k)==v for k,v in identity.items()) else fallback
        except (OSError,ValueError,TypeError,KeyError):
            return fallback


def broker_once(root, signature, worker):
    root=Path(root)
    selected=None
    with (root/'.queue.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for path in sorted(root.glob('*/request.json')):
            try:
                identity=json.loads(path.read_text());folder=path.parent
                if folder.resolve().parent!=root.resolve() or folder.is_symlink():continue
                state=json.loads((folder/'state.json').read_text())
                if identity.get('signature')!=signature or state.get('status')!='queued':continue
                if identity.get('kind') not in MAPS or identity.get('job_id')!=folder.name:continue
                atomic_json(folder/'state.json',{**identity,'status':'running','fields':{}})
                selected=(folder,identity);break
            except (OSError,ValueError,TypeError):continue
    if selected is None:return False
    folder,identity=selected
    try:
        if hashlib.sha256((folder/'source.pdf').read_bytes()).hexdigest()!=identity['source_sha256']:
            raise ValueError('Source identity mismatch')
        result=worker(folder/'source.pdf',identity['kind'],folder)
        payload={**identity,'status':'completed','fields':result['fields']}
    except Exception:
        payload={**identity,'status':'manual_review','fields':{}}
    atomic_json(folder/'state.json',payload)
    return True


def extraction_signature():
    names = ('application_document_extraction.py','local_document_lab/infer_v3.py','local_document_lab/infer_v4.py',
             'local_document_lab/evidence.py','local_document_lab/project.py','local_document_lab/parse.py',
             'local_document_lab/table_roles.py','local_document_lab/run.py','local_document_lab/sandbox.py','local_document_lab/layout_dates.py','local_document_lab/import_cache.py','local_document_lab/welfare_context.py',
             'STAGING_DATA/models/software-import-cache.json',
             'STAGING_DATA/environment.lock','STAGING_DATA/models/docling-artifacts.json',
             'STAGING_DATA/models/Qwen3-8B-4bit/download-receipt.json')
    h = hashlib.sha256()
    for name in names:
        h.update(name.encode()); h.update((ROOT/name).read_bytes())
    return h.hexdigest()


def offline_worker(source, kind, folder):
    from local_document_lab.run import run_offline
    from local_document_lab.sandbox import DATA, task_profile
    from local_document_lab.provenance import verify_model_snapshot
    # Model inventories are verified locally before every real task.
    verify_model_snapshot(DATA/'models/Qwen3-8B-4bit')
    policy=folder/'offline.sb'
    policy.write_text(task_profile(folder))
    packet, prediction = folder/'packet.json', folder/'prediction.json'
    before = extraction_signature()
    for stage,args,timeout in (
        ('parse',['-m','local_document_lab.parse','--input',str(source),'--id',folder.name,
                  '--kind',kind,'--artifacts',str(DATA/'models/docling'),'--output',str(packet)],240),
        ('infer',['-m','local_document_lab.infer_v4','--model',str(DATA/'models/Qwen3-8B-4bit'),
                  '--packet',str(packet),'--output',str(prediction)],180)):
        result = run_offline(args,folder/(stage+'.log'),timeout,sandbox_path=policy)
        atomic_json(folder/(stage+'-status.json'),result)
        if result['returncode'] != 0:
            raise RuntimeError('Offline stage failed')
    p, pred = json.loads(packet.read_text()), json.loads(prediction.read_text())
    if (extraction_signature()!=before or p['source_sha256']!=hashlib.sha256(source.read_bytes()).hexdigest()
            or p['kind']!=kind or p['id']!=folder.name
            or pred.get('status')!='completed_requires_review'
            or pred.get('packet_sha256')!=hashlib.sha256(packet.read_bytes()).hexdigest()):
        raise ValueError('Untrusted task identity')
    text = pred['raw_response']
    raw = json.loads(text[text.index('{'):text.rindex('}')+1])
    blocks = [b for b in p['blocks'] if b['id'] in pred['selected_block_ids']]
    return {'fields':review_candidates(kind,raw,blocks)}



def review_enabled(data):
    try:
        value=json.loads((Path(data)/'local-document-review.json').read_text())
        return (isinstance(value,dict) and set(value)=={'enabled','scope'} and value['enabled'] is True
                and isinstance(value['scope'],str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value['scope']))
                and os.environ.get('AAIE_LOCAL_DOCUMENT_REVIEW_SCOPE')==value['scope'])
    except (OSError,ValueError,TypeError):return False


_QUEUE = None
_QUEUE_LOCK = threading.Lock()
def development_queue(runtime_root, staging):
    """Fail closed in production/managed runtimes, even if they load this module."""
    global _QUEUE
    data = ROOT/'STAGING_DATA'
    if not staging or Path(runtime_root).resolve()!=data.resolve():
        return None
    marker = data/'local-document-review.json'
    try:
        if not review_enabled(data):
            return None
        with _QUEUE_LOCK:
            if _QUEUE is not None and _QUEUE.signature != extraction_signature():
                return None  # Require a development-server restart after source changes.
            if _QUEUE is None:
                _QUEUE = DirectoryReviewQueue(data/'document_review_jobs',extraction_signature())
            return _QUEUE
    except (OSError,ValueError,TypeError):
        return None


def enhance_review(data, upload, runtime_root, staging):
    queue = development_queue(runtime_root, staging)
    kind = KINDS.get(data.get('document_type'))
    if queue is None or not str(upload.filename).lower().endswith('.pdf'):
        return data
    data=deepcopy(data)
    for field in ('primary_main_course_school','secondary_main_course_school'):
        if field in data['fields']:
            data['fields'][field]['informational']=True
    if kind is None:
        data['local_extraction']={'status':'idle'}
        return data
    try:
        state = queue.submit(upload.data, kind)
        enhanced = merge_review(data,state.get('fields',{}))
    except Exception:
        state = {'status':'manual_review','fields':{}}
        enhanced = deepcopy(data)
    data = enhanced
    for field in ('primary_main_course_school','secondary_main_course_school'):
        if field in data['fields']:
            data['fields'][field]['informational']=True
    data['local_extraction'] = {k:v for k,v in state.items() if k!='fields'}
    return data


def serve_broker():
    """Launch outside the web sandbox: python -B -m application_document_extraction --serve.

    Only this controller is outside the worker sandbox. It uses no network client,
    and every parse/inference subprocess receives the deny-network task policy.
    """
    import signal
    data=ROOT/'STAGING_DATA';root=data/'document_review_jobs'
    if not review_enabled(data):
        raise RuntimeError('Development enablement required')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    signature=extraction_signature();stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *a:stop.set())
    signal.signal(signal.SIGINT,lambda *a:stop.set())
    with (root/'.broker.lock').open('a') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Interrupted jobs never retain a successful state or block fresh queued work.
        for path in root.glob('*/state.json'):
            state=json.loads(path.read_text())
            if state.get('status')=='running':
                atomic_json(path,{**state,'status':'manual_review','fields':{}})
        def heartbeat():
            while not stop.is_set():
                atomic_json(root/'heartbeat.json',{'updated_at':time.time(),'pid':os.getpid()})
                stop.wait(1)
        pulse=threading.Thread(target=heartbeat,daemon=True);pulse.start()
        try:
            while not stop.is_set():
                if extraction_signature()!=signature:break
                if not broker_once(root,signature,offline_worker):stop.wait(.5)
        finally:
            stop.set();pulse.join(2)
            atomic_json(root/'heartbeat.json',{'updated_at':0,'pid':os.getpid()})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true',required=True)
    parser.parse_args();serve_broker()

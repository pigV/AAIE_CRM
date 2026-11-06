"""Generate an offline lab profile; no production path or service grants."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'STAGING_DATA'
BASE = Path('/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies')


def profile():
    q = lambda value: json.dumps(str(value))
    return '\n'.join(['(version 1)', '(allow default)', '(deny network*)', '(deny file-write*)',
        f'(allow file-write* (subpath {q(DATA)}) (literal "/dev/null"))',
        '(deny file-read* (subpath "/Users") (subpath "/Volumes"))',
        f'(allow file-read* (subpath {q(ROOT)}) (subpath {q(BASE)}))',
        *[f'(allow file-read-metadata (literal {q(p)}))' for p in sorted(set(ROOT.parents) | set(BASE.parents))],
        '(deny process-exec)',
        f'(allow process-exec (literal {q(DATA/"venv/bin/python")}) (literal {q((DATA/"venv/bin/python").resolve())}))',
        '(allow signal (target children))'])+'\n'


def environment():
    env = {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'HOME':str(DATA/'home'), 'TMPDIR':str(DATA/'tmp'),
           'TMP':str(DATA/'tmp'),'TEMP':str(DATA/'tmp'),'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1',
           'HF_HOME':str(DATA/'cache/huggingface'),'XDG_CACHE_HOME':str(DATA/'cache'),
           'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_DATASETS_OFFLINE':'1',
           'HF_HUB_DISABLE_TELEMETRY':'1','DO_NOT_TRACK':'1','TOKENIZERS_PARALLELISM':'false',
           'OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2','MKL_NUM_THREADS':'2',
           'VECLIB_MAXIMUM_THREADS':'2','MPLCONFIGDIR':str(DATA/'cache/matplotlib')}
    return env


def task_profile(folder):
    """Give a REVIEW worker its own writable task, not the entire lab corpus."""
    folder = Path(folder).resolve()
    if not folder.is_relative_to(DATA.resolve()):
        raise ValueError('Task must be inside local staging data')
    q = lambda value: json.dumps(str(value))
    policy = profile().replace(f'(allow file-write* (subpath {q(DATA)}) (literal "/dev/null"))',
                               f'(allow file-write* (subpath {q(folder)}) (literal "/dev/null"))')
    policy += f'(deny file-read* (subpath {q(DATA)}))\n'
    for allowed in (folder, DATA/'models', DATA/'venv'):
        policy += f'(allow file-read* (subpath {q(allowed)}))\n'
    policy += f'(allow file-read* (literal {q(DATA/"environment.lock")}))\n'
    return policy

"""Minimal public-source fixture for isolation probes; no installed models or live state."""
from pathlib import Path
import json,shutil
SOURCE=Path(__file__).resolve().parents[1]

def prepare_isolation_source(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    for name in ('app.py','staging_environment.py','runtime_configuration.py',
                 'tools/staging_control.py','tools/school_map_tile_control.py',
                 'tools/test_extraction_control.py','tools/test_copy_control.py','tools/test_copy_probe.py'):
        target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(SOURCE/name,target)
    (root/'tools/__init__.py').write_text('')
    data=root/'STAGING_DATA';data.mkdir()
    (data/'staging.json').write_text(json.dumps({'mode':'staging','root':str(data),'admin_port':19860,'parent_port':19861,'namespace':'aaie_staging_'}))
    for name in ('tmp','database','storage','logs','backups','home','cache','audit'):(data/name).mkdir()
    return root

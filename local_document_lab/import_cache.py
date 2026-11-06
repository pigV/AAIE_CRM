"""Cache public package/export metadata for the prepared, frozen local environment.

The cache contains strings only, never student data or executable pickle content.
Unknown packages use the original library loader; changed environments reject it.
"""
from pathlib import Path
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'STAGING_DATA'
CACHE=DATA/'models/software-import-cache.json'


def decode_structure(payload):
    return {frozenset(item['backends']):{module:set(names) for module,names in item['modules'].items()} for item in payload}


def cached_structure(structures,key,prefix):
    if key not in structures:return None
    result=decode_structure(structures[key])
    return {backend:{(prefix+'.'+module if prefix else module):names for module,names in exports.items()}
            for backend,exports in result.items()}


def fingerprint():
    site=DATA/'venv/lib/python3.12/site-packages'
    records=sorted(site.glob('transformers-*.dist-info/RECORD'))
    if len(records)!=1:raise ValueError('Prepared transformers inventory required')
    return {'environment':hashlib.sha256((DATA/'environment.lock').read_bytes()).hexdigest(),
            'transformers_inventory':hashlib.sha256(records[0].read_bytes()).hexdigest(),
            'python':list(sys.version_info[:2]),'format':1}


def install():
    try:
        payload=json.loads(CACHE.read_text())
        if payload['fingerprint']!=fingerprint():return False
        packages=payload['packages'];structures=payload['structures']
        # All entries are public distribution names from this exact frozen environment.
        importlib.metadata.packages_distributions=lambda:{k:list(v) for k,v in packages.items()}
        package_root=(DATA/'venv/lib/python3.12/site-packages/transformers').resolve()
        class Hook(importlib.abc.MetaPathFinder):
            def find_spec(self,fullname,path=None,target=None):
                if fullname!='transformers.utils.import_utils':return None
                spec=importlib.machinery.PathFinder.find_spec(fullname,path)
                if spec is None or spec.loader is None:return None
                original_loader=spec.loader
                class Loader:
                    def __getattr__(self,name):return getattr(original_loader,name)
                    def create_module(self,spec):
                        return original_loader.create_module(spec) if hasattr(original_loader,'create_module') else None
                    def exec_module(self,module):
                        original_loader.exec_module(module)
                        original=module.define_import_structure
                        def define(module_path,prefix=None):
                            path=Path(module_path).resolve()
                            if path.is_file():path=path.parent
                            try:key=path.relative_to(package_root).as_posix()
                            except ValueError:return original(module_path,prefix)
                            value=cached_structure(structures,key,prefix)
                            return original(module_path,prefix) if value is None else value
                        for name in ('cache_clear','cache_info','cache_parameters'):
                            if hasattr(original,name):setattr(define,name,getattr(original,name))
                        module.define_import_structure=define
                spec.loader=Loader();return spec
        sys.meta_path.insert(0,Hook())
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def build():
    # Build from installed public software only. No input document is opened.
    packages=importlib.metadata.packages_distributions()
    import transformers
    from transformers.utils.import_utils import define_import_structure
    base=Path(transformers.__file__).resolve().parent
    requested=['.','models','models/auto','models/rt_detr','models/rt_detr_v2','models/table_transformer',
               'models/timm_backbone','models/qwen3','models/clip','models/siglip','models/vit','models/layoutlmv3']
    structures={}
    for key in requested:
        path=base/key
        if not path.is_dir():continue
        value=define_import_structure(str(path))
        structures[key]=[{'backends':sorted(backend),'modules':{module:sorted(names) for module,names in exports.items()}}
                         for backend,exports in value.items()]
        assert decode_structure(structures[key])==value
    CACHE.write_text(json.dumps({'fingerprint':fingerprint(),'packages':packages,'structures':structures},sort_keys=True))
    print(json.dumps({'status':'public_software_metadata_cached','packages':len(packages),'export_maps':len(structures),'bytes':CACHE.stat().st_size}))

if __name__=='__main__':build()

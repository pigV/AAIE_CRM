"""A Popen-owned startup phase. Never imports app or starts CRM services."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--config-sha256',required=True)
    parser.add_argument('--allow-production-control',action='store_true')
    parser.add_argument('--phase',choices=('preflight','ocr','final_status'),required=True)
    args=parser.parse_args(argv)
    from runtime_configuration import RuntimeConfiguration,preflight
    from tools import runtime_control as control
    config=RuntimeConfiguration.load(args.config,source_root=ROOT)
    if config.config_sha256!=args.config_sha256:
        raise RuntimeError('Startup configuration changed')
    if args.phase=='preflight':
        control.require_gate(config)
        result=preflight(config,check_ports=True,owned_ports_only=True)
    elif args.phase=='ocr':
        control.require_control_authority(config,args.allow_production_control)
        path=control.build_document_ocr.build(data_root=config.root,source_root=config.source_root)
        result={'prepared':True,'path':str(path)}
    else:
        result=control.status(config)
    config.check_current()
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        # Controller reports phase; no key/parser excerpts or configuration values.
        print(json.dumps({'worker_error':type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)

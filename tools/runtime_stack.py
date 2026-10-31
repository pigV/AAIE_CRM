"""Default production stack. Tests must explicitly select a staging config."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from runtime_configuration import RuntimeConfiguration
from tools import runtime_control as crm, runtime_auxiliary_control as auxiliary
from tools.runtime_control import require_gate


def default_config(source=ROOT):
    return source.parent/'runtime/production/config/runtime.json'


def status(config):
    primary=crm.status(config);aux=auxiliary.status(config)
    return dict(ready=primary['ready'] and aux['ready'],mode=config.mode,
                source_root=str(config.source_root),data_root=str(config.root),
                services=primary['services']+aux['services'])


def start(config):
    require_gate(config)
    prior=status(config)
    if prior['ready']:return prior
    # Never clean unknown or partial state during ordinary start.
    if any(crm.state_path(config,r).exists() for r in (*crm.ROLES,'map',*auxiliary.managed_roles(config))):
        raise crm.RuntimeControlError('Partial saved stack state; inspect status or explicitly restart')
    checked = crm.preflight(config, check_ports=True, owned_ports_only=True)
    if not checked['ready']:
        raise crm.RuntimeControlError('Preflight failed: '+','.join(checked['errors']))
    auxiliary.start(config)
    try:
        crm.start(config)
        result = status(config)
        if not result['ready']:
            raise crm.RuntimeControlError('Final stack health failed')
        return result
    except BaseException:
        try:
            crm.stop(config)
        finally:
            auxiliary.stop(config)
        raise


def stop(config):
    roles=(*crm.ROLES,'map',*auxiliary.managed_roles(config))
    for role in roles:
        path=crm.state_path(config,role)
        if path.exists():crm.verify_process(config,role,json.loads(path.read_text()))
    crm.stop(config)
    auxiliary.stop(config)
    return dict(state='stopped',data_root=str(config.root),external_agent='untouched')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','stop','restart','status','preflight'))
    parser.add_argument('--config',default=str(default_config()))
    parser.add_argument('--allow-production-control',action='store_true')
    args=parser.parse_args()
    config=RuntimeConfiguration.load(args.config,source_root=ROOT)
    if args.action=='status':result=status(config)
    elif args.action=='preflight':
        require_gate(config)
        result=crm.preflight(config,check_ports=False)
        auxiliary.dependencies(config)
        crm.sandbox_profile(config)
        if config.mode=='production':result['external_agent']=auxiliary.external_agent_health(config)
    else:
        crm.require_control_authority(config,args.allow_production_control)
        with crm.lifecycle_lock(config):
            if args.action=='restart':stop(config)
            result=stop(config) if args.action=='stop' else start(config)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('ready',True) else 1


if __name__=='__main__':raise SystemExit(main())

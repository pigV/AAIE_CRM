"""Explicit single release session retaining the Drive lease through read-only smoke.

After READY/ROLLED_BACK, the owning operator supplies an exact-SHA smoke receipt
on stdin. Before disable, EOF/error keeps business admission closed. A failure
after verified disable reports the actual OPEN state and never restores the DB.
"""
import argparse
import json
import os
import hashlib
from datetime import datetime,timezone
from pathlib import Path
from business_write_gate import Gate
from .production_writer_lease import ExistingWriterLease
from .production_write_gate_operator import WriteGateOperatorAdapter
from .production_visa_release_operator import VisaReleaseOperator,safe_failure_code
from .production_auth_preservation import validate_auth_delta
from .production_release_transaction import database_evidence,file_hash
from .production_graceful_control import GracefulController
from . import production_launchd_control as legacy


def completion_record(adapter,event,**payload):
    """Separate journal preserves the formal controller ledger's FINAL-last seal."""
    path=adapter.active_plan.evidence_dir/'GATE_COMPLETION.jsonl'
    expected=getattr(adapter,'completion_sha256',None)
    flags=os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if expected is None else 0)
    fd=os.open(path,flags,0o600)
    try:
        data=(json.dumps({'event':event,'utc_timestamp':datetime.now(timezone.utc).isoformat(),**payload},sort_keys=True)+'\n').encode()
        with os.fdopen(fd,'r+b',closefd=False) as handle:
            before=handle.read()
            if expected is not None and hashlib.sha256(before).hexdigest()!=expected:
                raise RuntimeError('completion_journal_changed')
            handle.write(data);handle.flush();os.fsync(fd)
            adapter.completion_sha256=hashlib.sha256(before+data).hexdigest()
    finally:os.close(fd)


def finish_gate(adapter,result,smoke):
    if (adapter.phase!='visa' or result.get('status') not in ('READY','ROLLED_BACK')
            or smoke.get('status')!='PASS' or smoke.get('production_sha')!=result.get('sha')
            or smoke.get('read_only') is not True or smoke.get('required_checks_passed') is not True):
        raise RuntimeError('matching_readonly_smoke_required')
    state=adapter.assert_gate();plan=adapter.active_plan;head=result['sha']
    adapter._authority(head,adapter.snapshot['source_commit']);plan.verify(head)
    for role in ('parent','admin'):
        obs=adapter.backend.inspect(role);payload=adapter.backend.health(role)
        if (not GracefulController._owned(obs) or not adapter.backend.health_success(role,payload)
                or payload.get('business_write_gate')!=state or adapter.backend.inspect(role)!=obs):
            raise RuntimeError('final_gated_health_not_proven')
    extra=plan._extra() if head==plan.target.candidate_sha else {}
    auth=validate_auth_delta(plan.snapshot.backup,plan.snapshot.database,expected_extra_tables=extra)
    evidence=database_evidence(plan.snapshot.database)
    checked=Path(result['aggregate_evidence'])
    prior=json.loads(checked.read_text())
    if prior['evidence']['schema_sha256']!=evidence['schema_sha256']:
        raise RuntimeError('post_smoke_schema_drift')
    adapter.assert_gate();plan.verify(head)
    completion_record(adapter,'FINAL_GATED_SMOKE_VERIFIED',production_sha=head,smoke=smoke,
                   database=evidence,auth_validation=auth,business_mutations=0)
    opened=adapter.gate.disable(expected_generation=state['generation'],release_verified=True)
    completion_record(adapter,'GATE_DISABLED',gate=opened,production_sha=head)
    # A real admission lease proves OPEN; this does not create a business record.
    with adapter.gate.operation('business'):
        for role in ('parent','admin'):
            obs=adapter.backend.inspect(role);payload=adapter.backend.health(role)
            if (not GracefulController._owned(obs) or not adapter.backend.health_success(role,payload)
                    or payload.get('business_write_gate')!=opened or adapter.backend.inspect(role)!=obs):
                raise RuntimeError('post_open_health_not_proven')
    after=database_evidence(plan.snapshot.database)
    plan.delegate.verify(head);adapter.lease.assert_held()
    if adapter.gate.status()!=opened:raise RuntimeError('post_open_generation_changed')
    final={**result,'business_write_gate':'PASS','business_mutations_while_gated':0,
           'final_backup_after_gate_enable':True,'rollback_window_protected':True,
           'normal_business_operations_restored':True,'post_open_integrity':after['integrity']}
    completion_record(adapter,'NORMAL_BUSINESS_OPERATIONS_RESTORED',result=final)
    return final


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=('bootstrap','visa'),required=True)
    parser.add_argument('--migration-mode',choices=('code-only','visa-037-038','visa-037-038-039'))
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--maintenance-idle-confirmed',action='store_true')
    for name in ('previous-sha','candidate-sha','source-commit','candidate-root','baseline-dir','baseline-ledger','evidence-dir','backup-name'):
        parser.add_argument('--'+name,required=True)
    args=vars(parser.parse_args(argv));phase=args.pop('phase')
    supplied=args.pop('migration_mode')
    args['migration_mode']=supplied or ('code-only' if phase=='bootstrap' else 'visa-037-038')
    root=legacy.PRODUCTION_ROOT
    directory=Path.home()/'Library/Application Support/AAIE CRM/production-services/business-write-gate'
    gate=Gate(root/'database/aaie_crm.db',directory)
    adapter=None
    try:
        if not args['execute']:
            adapter=WriteGateOperatorAdapter(phase=phase,gate=gate,held_writer_lease=None)
            result=VisaReleaseOperator(adapter).run(**args)
        else:
            with ExistingWriterLease(root/'cache/google_drive_sync/writer.lock') as lease:
                adapter=WriteGateOperatorAdapter(phase=phase,gate=gate,held_writer_lease=lease)
                result=VisaReleaseOperator(adapter).run(**args)
                adapter.session_result=result
                print(json.dumps(result,sort_keys=True),flush=True)
                if phase=='visa' and result['status'] in ('READY','ROLLED_BACK'):
                    print('GATE_CLOSED_AWAITING_READONLY_SMOKE_RECEIPT',flush=True)
                    command=json.loads(input())
                    if set(command)!={'action','smoke_path','smoke_sha256'} or command['action']!='finish':
                        raise RuntimeError('fixed_finish_command_required')
                    path=adapter._safe_path(command['smoke_path'])
                    if not path.is_relative_to(root) or file_hash(path)!=command['smoke_sha256']:
                        raise RuntimeError('smoke_receipt_authority_invalid')
                    result=finish_gate(adapter,result,json.loads(path.read_text()))
    except BaseException as exc:
        result={'status':'HOLD','safe_failure_code':safe_failure_code(exc),'gate':gate.status(),'automatic_retry':False}
        if adapter is not None and adapter.log is not None:
            try:
                if getattr(adapter,'session_result',{}).get('status') in ('READY','ROLLED_BACK'):
                    completion_record(adapter,'SESSION_HOLD',result=result)
                else:adapter.record('SESSION_HOLD',result=result)
            except Exception:pass
    print(json.dumps(result,sort_keys=True),flush=True)
    return 0 if result['status'] in ('PREFLIGHT','READY','ROLLED_BACK','ORIGINAL_RECOVERED') else 1


if __name__=='__main__':raise SystemExit(main())

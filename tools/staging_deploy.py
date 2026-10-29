"""Code-only gated release adapter. Importing this file performs no runtime IO.

Production execution requires exact manifest plus an explicit execute flag and
all existing baseline/maintenance authority. Automatic failure rollback preserves
SQLite; this module never invokes GitReleasePlan.restore or snapshot.restore.
"""
import argparse
import json
from pathlib import Path


def require_code_manifest(repo, manifest, previous, candidate):
    from . import staging_release
    value = staging_release.validate(repo, manifest)
    if (value['previous_release_sha'], value['new_release_sha']) != (previous, candidate):
        raise ValueError('manifest_release_authority_mismatch')
    changed = staging_release.git(repo, 'diff', '--name-only', previous, candidate).decode().splitlines()
    if any(p.startswith(('migrations/', 'database_migrations/')) for p in changed):
        raise ValueError('code_only_migration_changes_forbidden')
    staging_release.git(repo, 'merge-base', '--is-ancestor', previous, candidate)
    return value


def adapter_type():
    # Lazy imports: read-only manifest inspection cannot initialize runtime state.
    from .production_write_gate_operator import WriteGateOperatorAdapter
    from .production_visa_release_operator import VisaOperatorAdapter
    from .production_code_only_plan import CodeOnlyPlan
    from .production_activation_control import ActivationController
    from .production_release_transaction import database_evidence
    from .production_auth_preservation import validate_auth_delta
    import os

    class GatedCodePlan(CodeOnlyPlan):
        def __init__(self, delegate, assert_gate):
            self.assert_gate = assert_gate
            super().__init__(delegate)

        def validate(self):
            self.assert_gate(); super().validate(); self.assert_gate()

        def cutover(self):
            self.assert_gate(); super().cutover(); self.assert_gate()

        def restore(self):
            self.assert_gate(); super().restore(); self.assert_gate()

        def verify(self, head):
            self.assert_gate(); super().verify(head); self.assert_gate()

        def _extra(self):
            return {}  # No schema additions; compatible with existing finish_gate.

    class CodeReleaseAdapter(WriteGateOperatorAdapter):
        def __init__(self, *, gate, held_writer_lease, manifest, rollback=False):
            super().__init__(phase='visa', gate=gate, held_writer_lease=held_writer_lease)
            self.manifest = Path(manifest)
            self.rollback = rollback

        def preflight(self, **kwargs):
            if kwargs.get('migration_mode') != 'code-only':
                raise RuntimeError('code_only_required')
            if self.rollback:
                # Reverse ONLY the sealed accepted pair; source remains released B.
                require_code_manifest(self.source, self.manifest, kwargs['candidate_sha'], kwargs['previous_sha'])
            else:
                require_code_manifest(kwargs['candidate_root'], self.manifest, kwargs['previous_sha'], kwargs['candidate_sha'])
            result = VisaOperatorAdapter.preflight(self, **kwargs)
            expected = self.home / 'Library/Application Support/AAIE CRM/production-services/business-write-gate'
            if self.gate.directory != expected or self.gate.database != self.runtime / 'database/aaie_crm.db':
                raise RuntimeError('gate_path_authority_mismatch')
            if self.gate.status()['state'] != 'OPEN':
                raise RuntimeError('existing_open_gate_required')
            return result

        def _require_candidate(self, path, sha):
            if not self.rollback:
                return VisaOperatorAdapter._require_candidate(self, path, sha)
            # Preserve existing candidate path, exact HEAD, clean tracked files and
            # required release assets. Reverse ancestry ONLY for the sealed pair.
            path = self._safe_path(path)
            current = self.backend.head()
            require_code_manifest(self.source, self.manifest, sha, current)
            if not path.is_relative_to(self.runtime) or self._git(path, 'rev-parse', 'HEAD') != sha:
                raise RuntimeError('candidate_source_authority')
            self._git(path, 'diff', '--quiet', 'HEAD', '--')
            self._git(path, 'diff', '--cached', '--quiet', '--')
            self._git(path, 'ls-files', '--error-unmatch', 'migration_runner.py',
                      'config/production-launchd/production-service-launcher.zsh.in')
            self._git(self.runtime, 'merge-base', '--is-ancestor', sha, current)
            return path

        def prepare_plan(self, snapshot, directory, backup_name, migration_mode):
            self.assert_gate()
            if migration_mode != 'code-only':
                raise RuntimeError('code_only_required')
            delegate = VisaOperatorAdapter.prepare_plan(self, snapshot, directory, backup_name, migration_mode)
            if self.rollback:
                original_git = delegate._git
                previous, candidate = delegate.target.previous_sha, delegate.target.candidate_sha
                def rollback_git(*args):
                    if args == ('merge-base', '--is-ancestor', previous, candidate):
                        # The same exact sealed pair was accepted A -> B. A controlled
                        # rollback B -> A validates A's ancestry, never arbitrary refs.
                        require_code_manifest(self.source, self.manifest, candidate, previous)
                        return original_git('merge-base', '--is-ancestor', candidate, previous)
                    return original_git(*args)
                delegate._git = rollback_git
            plan = GatedCodePlan(delegate, self.assert_gate)
            plan.seal = delegate.seal
            self.assert_gate()
            return plan

        def activate(self, plan):
            self.active_plan = plan
            self.activation_called = True
            def event(item):
                self.assert_gate()
                self.record('CONTROLLER', details=item)
                if item['state'] == 'VERIFY_RUNTIME_AUTHORITY':
                    head = plan.target.previous_sha if item['candidate_failed'] else plan.target.candidate_sha
                    plan.verify_runtime_database(head)
                    self.precommit_smoke(head, rollback_available=not item['candidate_failed'])
                    auth = validate_auth_delta(plan.snapshot.backup, plan.snapshot.database, expected_extra_tables={})
                    evidence = database_evidence(plan.snapshot.database)
                    path = plan.evidence_dir / (('PREVIOUS' if item['candidate_failed'] else 'CANDIDATE') + '_PRECOMMIT_DATABASE_AGGREGATE.json')
                    with path.open('x') as handle:
                        json.dump({'validation_code': 'pass', 'evidence': evidence, 'auth_validation': auth,
                                   'business_mutations': 0, 'database_restored': False}, handle, sort_keys=True)
                        handle.flush(); os.fsync(handle.fileno())
                    if not hasattr(self, 'validated_runtime'):
                        self.validated_runtime = {}
                    self.validated_runtime[head] = {'database_integrity': 'ok', 'foreign_key_violations': 0,
                                                    'aggregate_evidence': str(path)}
            return ActivationController(self.backend, event_sink=event).run(plan)

        def precommit_smoke(self, head, *, rollback_available):
            from .production_release_transaction import file_hash
            self.assert_gate()
            print(json.dumps({'state': 'PRECOMMIT_SMOKE_REQUIRED', 'exact_sha': head,
                              'rollback_command': {'action': 'rollback'} if rollback_available else None}), flush=True)
            command = json.loads(input())
            if command == {'action': 'rollback'} and rollback_available:
                raise RuntimeError('operator_requested_code_only_rollback')
            if set(command) != {'action', 'smoke_path', 'smoke_sha256'} or command['action'] != 'smoke':
                raise RuntimeError('exact_smoke_receipt_required')
            path = self._safe_path(command['smoke_path'])
            if not path.is_relative_to(self.runtime) or file_hash(path) != command['smoke_sha256']:
                raise RuntimeError('smoke_receipt_authority_invalid')
            smoke = json.loads(path.read_text())
            if (smoke.get('status') != 'PASS' or smoke.get('production_sha') != head
                    or smoke.get('read_only') is not True or smoke.get('required_checks_passed') is not True):
                raise RuntimeError('matching_readonly_smoke_required')
            self.assert_gate()
            self.record('PRECOMMIT_SMOKE_VERIFIED', head=head, receipt_sha256=command['smoke_sha256'])

        def verify_final(self, plan, result):
            self.assert_gate()
            VisaOperatorAdapter.verify_final(self, plan, result)
            result['business_write_gate'] = self.assert_gate()
            result['gate_reopening'] = 'PENDING_OPERATOR_READONLY_SMOKE'
    return CodeReleaseAdapter


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--rollback-of', action='store_true', help='Reverse only the manifest sealed previous/new pair; take a fresh backup')
    parser.add_argument('--authorize-production-sha', help='Explicit user-approved exact SHA; required to execute')
    parser.add_argument('--maintenance-idle-confirmed', action='store_true')
    for name in ('manifest', 'previous-sha', 'candidate-sha', 'source-commit', 'candidate-root',
                 'baseline-dir', 'baseline-ledger', 'evidence-dir', 'backup-name'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args(argv))
    authorization = args.pop('authorize_production_sha')
    manifest = args.pop('manifest')
    rollback = args.pop('rollback_of')
    if args['execute'] and authorization != args['candidate_sha']:
        raise SystemExit('HOLD: explicit Production exact-SHA authorization required')
    manifest_repo = Path(__file__).resolve().parents[1] if rollback else args['candidate_root']
    require_code_manifest(manifest_repo, manifest, args['candidate_sha'] if rollback else args['previous_sha'],
                          args['previous_sha'] if rollback else args['candidate_sha'])
    from business_write_gate import Gate
    from .production_writer_lease import ExistingWriterLease
    from .production_visa_release_operator import VisaReleaseOperator
    from .production_write_gate_session import finish_gate
    from .production_release_transaction import file_hash
    from .production_launchd_control import PRODUCTION_ROOT
    gate = Gate(PRODUCTION_ROOT / 'database/aaie_crm.db', Path.home() / 'Library/Application Support/AAIE CRM/production-services/business-write-gate')
    cls = adapter_type()
    args['migration_mode'] = 'code-only'
    if not args['execute']:
        result = VisaReleaseOperator(cls(gate=gate, held_writer_lease=None, manifest=manifest, rollback=rollback)).run(**args)
    else:
        with ExistingWriterLease(PRODUCTION_ROOT / 'cache/google_drive_sync/writer.lock') as lease:
            adapter = cls(gate=gate, held_writer_lease=lease, manifest=manifest, rollback=rollback)
            try:
                result = VisaReleaseOperator(adapter).run(**args)
                print(json.dumps(result, sort_keys=True), flush=True)
                if result['status'] in ('READY', 'ROLLED_BACK'):
                    print('GATE_CLOSED_AWAITING_READONLY_SMOKE_RECEIPT', flush=True)
                    command = json.loads(input())
                    if set(command) != {'action', 'smoke_path', 'smoke_sha256'} or command['action'] != 'finish':
                        raise RuntimeError('fixed_finish_command_required')
                    path = adapter._safe_path(command['smoke_path'])
                    if not path.is_relative_to(PRODUCTION_ROOT) or file_hash(path) != command['smoke_sha256']:
                        raise RuntimeError('smoke_receipt_authority_invalid')
                    result = finish_gate(adapter, result, json.loads(path.read_text()))
            except BaseException as exc:
                from .production_write_gate_session import completion_record
                result = {'status': 'HOLD', 'failure': type(exc).__name__,
                          'gate': gate.status(), 'automatic_retry': False,
                          'database_restored': False}
                try:
                    if getattr(adapter, 'active_plan', None) is not None:
                        completion_record(adapter, 'SESSION_HOLD', result=result)
                    elif adapter.log is not None:
                        adapter.record('SESSION_HOLD', result=result)
                except Exception:
                    result['hold_journal'] = 'FAILED'
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result['status'] in ('PREFLIGHT', 'READY', 'ROLLED_BACK') else 2




def main(argv=None):
    try:
        return _main(argv)
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({'status': 'HOLD', 'failure': type(exc).__name__,
                          'reason': str(exc), 'automatic_retry': False,
                          'gate_reopened_by_error_handler': False}), flush=True)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())

"""Explicit release backup/restore primitives. Never imports the CRM application.

Callers must prove service and writer quiescence before using either operation.
Only aggregate evidence is retained. This module does not decide release authority.
"""
from dataclasses import dataclass
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import importlib.util
import sys
from .production_activation_control import ReleasePlan
from . import production_sha_release as sha_release


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      default=lambda b: {'blob_sha256': hashlib.sha256(b).hexdigest()}).encode()


def database_evidence(path):
    """Read one consistent transaction; retain counts/digests, never business rows."""
    path = Path(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        integrity = [r[0] for r in conn.execute('PRAGMA integrity_check')]
        if integrity != ['ok']:
            raise RuntimeError('database_integrity_failed')
        fk_count = sum(1 for _ in conn.execute('PRAGMA foreign_key_check'))
        if fk_count:
            raise RuntimeError('database_fk_failed')
        schema = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        counts, hashes = {}, {}
        for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            columns = conn.execute('PRAGMA table_info(' + _quoted(name) + ')').fetchall()
            pk = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            order = ','.join(map(_quoted, pk)) if pk else 'rowid'
            digest, count = hashlib.sha256(), 0
            for row in conn.execute('SELECT * FROM ' + _quoted(name) + ' ORDER BY ' + order):
                payload = _json(row)
                digest.update(len(payload).to_bytes(8, 'big'))
                digest.update(payload)
                count += 1
            counts[name], hashes[name] = count, digest.hexdigest()
        conn.rollback()
    return {'integrity': 'ok', 'foreign_key_violations': 0,
            'schema_sha256': hashlib.sha256(_json(schema)).hexdigest(),
            'row_counts': counts, 'rows_sha256': hashes}


def _no_symlink(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink_not_allowed')
    return path


def _sync_file(path):
    with Path(path).open('rb') as handle:
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class ReleaseSnapshot:
    database: Path
    backup: Path
    manifest: Path
    manifest_sha256: str
    backup_sha256: str

    @classmethod
    def create(cls, database, destination, *, backup_root, assert_quiescent):
        database = _no_symlink(database)
        destination = _no_symlink(destination)
        backup_root = _no_symlink(backup_root)
        if not destination.is_relative_to(backup_root) or destination == backup_root:
            raise ValueError('backup_destination_outside_approved_root')
        if not database.is_file() or not backup_root.is_dir():
            raise ValueError('backup_source_or_root_missing')
        assert_quiescent()
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        backup, ready = destination / 'baseline.db', destination / 'restore-ready.db'
        # mode=ro prevents accidental source creation or writes, including journal pragmas.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as src:
            src.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(backup)) as dst:
                src.backup(dst)
                dst.execute('PRAGMA journal_mode=DELETE')
        _sync_file(backup)
        evidence = database_evidence(backup)
        assert_quiescent()
        if evidence != database_evidence(database):
            raise RuntimeError('backup_source_changed')
        # Actual separate-file restore rehearsal, inside the same approved backup area.
        shutil.copyfile(backup, ready)
        _sync_file(ready)
        if database_evidence(ready) != evidence or file_hash(backup) != file_hash(ready):
            raise RuntimeError('restore_rehearsal_failed')
        digest = file_hash(backup)
        manifest = destination / 'BACKUP_MANIFEST.json'
        data = {'database': str(database), 'backup': str(backup), 'backup_sha256': digest,
                'purpose': 'pre-deployment rollback only', 'evidence': evidence,
                'restore_rehearsal': 'PASS', 'business_rows_retained_in_report': False}
        with manifest.open('x') as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        for path in (backup, manifest):
            path.chmod(0o400)
        ready.chmod(database.stat().st_mode & 0o777)
        return cls(database, backup, manifest, file_hash(manifest), digest)

    def verify(self):
        for path in (self.database, self.backup, self.manifest):
            _no_symlink(path)
        if file_hash(self.manifest) != self.manifest_sha256:
            raise RuntimeError('manifest_hash_mismatch')
        if file_hash(self.backup) != self.backup_sha256:
            raise RuntimeError('backup_hash_mismatch')
        if database_evidence(self.backup) != json.loads(self.manifest.read_text())['evidence']:
            raise RuntimeError('backup_evidence_mismatch')
        return True

    def restore(self, *, assert_quiescent):
        assert_quiescent()
        self.verify()
        directory = self.manifest.parent
        marker = directory / 'RESTORE_ATTEMPT.json'
        try:
            with marker.open('x') as handle:
                handle.write('{"state":"consumed-before-action"}')
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise RuntimeError('restore_attempt_consumed') from None
        ready = directory / 'restore-ready.db'
        _no_symlink(ready)
        if file_hash(ready) != self.backup_sha256:
            raise RuntimeError('restore_ready_hash_mismatch')
        if ready.stat().st_dev != self.database.parent.stat().st_dev:
            raise RuntimeError('restore_requires_same_filesystem')
        failed = directory / 'failed-state'
        failed.mkdir(mode=0o700)
        paths = [Path(str(self.database) + suffix) for suffix in ('', '-wal', '-shm')]
        for path in paths:
            _no_symlink(path)
        assert_quiescent()
        # Preserve the complete failed family. No sidecar is discarded or replayed.
        for path in paths:
            if path.exists():
                os.replace(path, failed / path.name)
        os.replace(ready, self.database)
        _sync_file(self.database)
        if file_hash(self.database) != self.backup_sha256:
            raise RuntimeError('restored_database_hash_mismatch')
        if database_evidence(self.database) != json.loads(self.manifest.read_text())['evidence']:
            raise RuntimeError('restored_database_evidence_mismatch')
        with (directory / 'RESTORE_RESULT.json').open('x') as handle:
            json.dump({'status': 'PASS', 'baseline_sha256': self.backup_sha256,
                       'failed_family_preserved': True}, handle)


def migration_versions(database):
    with closing(sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=1)) as conn:
        conn.execute('PRAGMA query_only=ON')
        return tuple(row[0] for row in conn.execute('SELECT version FROM schema_migrations ORDER BY version'))


@dataclass(frozen=True)
class MigrationStep:
    """Exact allowlist; full registry checks still belong to the formal runner."""
    source_root: Path
    source_commit: str
    versions: tuple
    required_predecessors: tuple = ()
    allow_already_applied: bool = False

    def expected_registry(self, before):
        before=tuple(before);chain=self.required_predecessors+self.versions
        if self.required_predecessors:
            if self.allow_already_applied and before[-len(chain):]==chain:
                return before
            if any(version in before for version in self.versions):
                raise RuntimeError('partial_target_migration_state')
            if before[-len(self.required_predecessors):]!=self.required_predecessors:
                raise RuntimeError('required_migration_predecessor_missing')
        return before+self.versions

    def _runner(self):
        root = _no_symlink(self.source_root)
        result = subprocess.run(['/usr/bin/git','-C',str(root),'rev-parse','HEAD'],
                                check=True,capture_output=True,text=True,timeout=15)
        if result.stdout.strip() != self.source_commit:
            raise RuntimeError('migration_source_head_changed')
        for args in (['diff','--quiet','HEAD','--'], ['diff','--cached','--quiet','--']):
            subprocess.run(['/usr/bin/git','-C',str(root),*args],check=True,capture_output=True,timeout=15)
        name = '_sealed_release_migration_runner'
        spec = importlib.util.spec_from_file_location(name,root/'migration_runner.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def apply(self, database):
        if not self.versions:
            raise RuntimeError('migration_allowlist_empty')
        runner = self._runner()
        directory = Path(self.source_root) / 'migrations'
        plan = runner.plan_migrations(database,directory,through=self.versions[-1])
        before=tuple(x['version'] for x in plan['already_applied'])
        expected_registry=self.expected_registry(before)
        expected_apply=() if expected_registry==before else self.versions
        if plan.get('pending'):
            raise RuntimeError('unrelated_pending_migration')
        if tuple(x['version'] for x in plan['will_apply']) != expected_apply:
            raise RuntimeError('unexpected_migration_plan')
        if not expected_apply:
            verification=runner.verify_migrations(database,directory)
            if (tuple(verification['verified'])!=before or verification['pending']
                    or verification['foreign_key_violations']):
                raise RuntimeError('already_applied_migration_verification_failed')
            return {'database':str(database),'applied':[],'already_applied':list(before),
                    'pending':[],'through':self.versions[-1]}
        # Explicit actual formal path: never rely on a candidate worktree's default.
        result = runner.apply_migrations(database,directory,
            applied_by='controlled-visa-production-release',source_commit=self.source_commit,
            formal_database_path=Path(database),allow_formal_database=True,
            confirmation=runner.FORMAL_CONFIRMATION,expected_database_sha256=file_hash(database),
            through=self.versions[-1])
        if tuple(result['applied']) != expected_apply or migration_versions(database)!=expected_registry:
            raise RuntimeError('migration_apply_result_mismatch')
        return result


class GitReleasePlan(ReleasePlan):
    """Reviewed Git checkout + formal SHA publisher + sealed SQLite recovery.

    Source/controller executable may live in its committed isolated worktree;
    the immutable target specifies the only checkout and artifact paths mutated.
    No force checkout/reset, migration-history edits or application imports.
    """
    def __init__(self,target,*,snapshot,source_root,home,service_python,evidence_dir,
                 assert_quiescent,migration_step=None):
        super().__init__(target)
        self.snapshot=snapshot
        self.root=_no_symlink(target.runtime_path)
        self.source_root=_no_symlink(source_root)
        self.home=_no_symlink(home)
        self.python=Path(service_python)
        self.evidence_dir=_no_symlink(evidence_dir)
        self.evidence_dir.mkdir(mode=0o700,exist_ok=False)
        self.assert_quiescent=assert_quiescent
        self.migration_step=migration_step
        self.policy_manifest=None
        self.policy_manifest_hash=None
        self.cutover_used=False
        self.restore_used=False
        self.migration_started=False
        self.prepared=False
        self.previous_policy_heads=None
        self.other_policy_heads={}
        self.other_policy_artifacts=()
        self.other_policy_seal=None
        from dataclasses import asdict
        self.seal=self.evidence_dir/'ROLLBACK_TARGET.json'
        self.seal.write_bytes(_json(asdict(target)))
        self.seal.chmod(0o400)
        self.seal_hash=file_hash(self.seal)

    def _event(self,stage):
        with (self.evidence_dir/'transaction.jsonl').open('a') as handle:
            handle.write(json.dumps({'stage':stage})+'\n')
            handle.flush();os.fsync(handle.fileno())

    def _git(self,*args):
        result=subprocess.run(['/usr/bin/git','-C',str(self.root),*args],
                              capture_output=True,text=True,timeout=30)
        if result.returncode:
            raise RuntimeError('release_git_operation_failed')
        return result.stdout.strip()

    def _sealed(self):
        from dataclasses import asdict
        if file_hash(self.seal)!=self.seal_hash or self.seal.read_bytes()!=_json(asdict(self.target)):
            raise RuntimeError('rollback_target_changed')
        if self.snapshot.manifest_sha256!=self.target.backup_manifest_sha256:
            raise RuntimeError('backup_authority_mismatch')
        if not self.snapshot.database.is_relative_to(self.root):
            raise RuntimeError('database_outside_runtime_root')
        if self.other_policy_seal is not None:
            path,digest=self.other_policy_seal
            if file_hash(path)!=digest:raise RuntimeError('protected_policy_seal_changed')
            expected={'heads':self.other_policy_heads,'artifacts':self.other_policy_artifacts}
            if json.loads(path.read_text())!=json.loads(json.dumps(expected)):
                raise RuntimeError('protected_policy_state_changed')
            self._artifacts(self.other_policy_artifacts)

    def _clean(self):
        self._git('diff','--quiet','HEAD','--')
        self._git('diff','--cached','--quiet','--')

    @staticmethod
    def _artifacts(expected):
        for path,digest in expected:
            if file_hash(_no_symlink(path))!=digest:
                raise RuntimeError('release_artifact_changed')

    def validate(self):
        self._sealed();self.assert_quiescent();self._clean()
        if self._git('rev-parse','HEAD')!=self.target.previous_sha:
            raise RuntimeError('previous_head_mismatch')
        self._git('cat-file','-e',self.target.candidate_sha+'^{commit}')
        self._git('merge-base','--is-ancestor',self.target.previous_sha,self.target.candidate_sha)
        self._artifacts(self.target.previous_artifacts)
        from .production_launchd_control import support_dir
        self.previous_policy_heads=sha_release.read_allowed_heads(
            support_dir(self.home)/'production-service-launcher.zsh')
        # Preserve each unrelated service's own policy; do not invent common SHA
        # authority or rewrite deferred services just to publish Admin/Parent.
        extraction=sha_release._load_controller(self.source_root,'content_extraction_launchd_control')
        nas=sha_release._load_controller(self.source_root,'nas_recovery_launchd_control')
        drive=sha_release._load_controller(self.source_root,'google_drive_launchd_control')
        policies={
            'expected_content_extraction_heads':extraction.support_dir(self.home)/'content-extraction-service-launcher.zsh',
            'expected_nas_recovery_heads':nas.launcher_path(self.home),
        }
        if drive.launcher_path(self.home).exists():policies['expected_google_drive_scheduler_heads']=drive.launcher_path(self.home)
        captured={key:_no_symlink(path).read_bytes() for key,path in policies.items()}
        self.other_policy_heads={key:sha_release.parse_allowed_heads(payload.decode('utf-8')) for key,payload in captured.items()}
        self.other_policy_artifacts=tuple((str(policies[key]),hashlib.sha256(payload).hexdigest()) for key,payload in captured.items())
        extra=self.evidence_dir/'PROTECTED_POLICY_HEADS.json'
        with extra.open('x') as f:
            json.dump({'heads':self.other_policy_heads,'artifacts':self.other_policy_artifacts},f,sort_keys=True)
            f.flush();os.fsync(f.fileno())
        extra.chmod(0o400);self.other_policy_seal=(extra,file_hash(extra))
        if migration_versions(self.snapshot.database)!=self.target.previous_migrations:
            raise RuntimeError('previous_migration_state_changed')
        self.snapshot.verify()
        if database_evidence(self.snapshot.database)!=json.loads(self.snapshot.manifest.read_text())['evidence']:
            raise RuntimeError('baseline_database_changed')
        if self.target.migration_mode=='restore_backup_after_migration':
            if not isinstance(self.migration_step,MigrationStep):
                raise RuntimeError('explicit_migration_step_required')
            if self.migration_step.source_commit!=self.target.candidate_sha:
                raise RuntimeError('migration_candidate_mismatch')
        elif self.migration_step is not None:
            raise RuntimeError('code_only_release_cannot_migrate')
        self.prepared=True;self._event('PREPARED')

    def cutover(self):
        if not self.prepared or self.cutover_used:
            raise RuntimeError('cutover_not_prepared_or_consumed')
        self.cutover_used=True;self.assert_quiescent();self._sealed()
        self.verify(self.target.previous_sha)
        if database_evidence(self.snapshot.database)!=json.loads(self.snapshot.manifest.read_text())['evidence']:
            raise RuntimeError('baseline_database_changed')
        if self.migration_step is not None:
            self.migration_started=True;self._event('MIGRATION_STARTED')
            self.migration_step.apply(self.snapshot.database)
            self._event('MIGRATION_APPLIED')
            before=json.loads(self.snapshot.manifest.read_text())['evidence']
            after=database_evidence(self.snapshot.database)
            for table,digest in before['rows_sha256'].items():
                if table not in ('schema_migrations','sqlite_sequence') and after['rows_sha256'].get(table)!=digest:
                    raise RuntimeError('preexisting_business_rows_changed_during_migration')
        self.assert_quiescent();self._event('CODE_CHECKOUT_STARTED')
        self._git('checkout','--detach',self.target.candidate_sha)
        self._event('CODE_CHECKOUT_COMPLETED')
        from .production_launchd_control import support_dir
        installed=support_dir(self.home)/'production-service-launcher.zsh'
        # Prepare the single-SHA policy only after disk authority is the candidate.
        # Previous bytes/hashes were already sealed before any activation.
        self._artifacts(self.target.previous_artifacts)
        policy_manifest=sha_release.prepare_release(source_root=self.source_root,
            production_root=self.root,home=self.home,safebox_dir=self.evidence_dir/'policy',
            expected_installed_heads=self.previous_policy_heads,
            requested_heads=(self.target.candidate_sha,),service_python=self.python,target='admin-parent',
            **self.other_policy_heads)
        manifest=json.loads(policy_manifest.read_text())
        expected=dict(self.target.candidate_artifacts)
        previous=dict(self.target.previous_artifacts)
        if {entry['target_path'] for entry in manifest['artifacts']}!={str(installed)}:
            raise RuntimeError('unsealed_policy_artifact_set')
        for entry in manifest['artifacts']:
            if previous.get(entry['target_path'])!=entry['before_sha256']:
                raise RuntimeError('unsealed_previous_policy')
            if expected.get(entry['target_path'])!=entry['after_sha256']:
                raise RuntimeError('unsealed_candidate_policy')
        self.policy_manifest=policy_manifest
        self.policy_manifest_hash=file_hash(policy_manifest)
        self._event('POLICY_APPLY_STARTED')
        sha_release.apply_release(self.policy_manifest)
        self._event('POLICY_APPLIED');self.verify(self.target.candidate_sha)

    def verify(self,head):
        self._sealed();self._clean()
        if self._git('rev-parse','HEAD')!=head:
            raise RuntimeError('runtime_head_mismatch')
        if head==self.target.previous_sha:
            artifacts=self.target.previous_artifacts;versions=self.target.previous_migrations
        elif head==self.target.candidate_sha:
            artifacts=self.target.candidate_artifacts
            versions=self.target.previous_migrations+(self.migration_step.versions if self.migration_step else ())
        else:raise RuntimeError('unsealed_runtime_head')
        self._artifacts(artifacts)
        if migration_versions(self.snapshot.database)!=versions:
            raise RuntimeError('runtime_migration_authority_mismatch')

    def restore(self):
        if self.restore_used:raise RuntimeError('release_restore_attempt_consumed')
        self.restore_used=True;self.assert_quiescent();self._sealed();self._clean()
        if self._git('rev-parse','HEAD') not in (self.target.previous_sha,self.target.candidate_sha):
            raise RuntimeError('unknown_head_protected')
        if self.policy_manifest is not None:
            if file_hash(self.policy_manifest)!=self.policy_manifest_hash:
                raise RuntimeError('policy_manifest_changed')
            sha_release.rollback_release(self.policy_manifest)
        else:self._artifacts(self.target.previous_artifacts)
        self.assert_quiescent()
        # Covers migration-first partial failure as well as failed startup metadata.
        self.snapshot.restore(assert_quiescent=self.assert_quiescent)
        self._event('DATABASE_RESTORED')
        self._git('checkout','--detach',self.target.previous_sha)
        self._event('CODE_RESTORED');self.verify(self.target.previous_sha)

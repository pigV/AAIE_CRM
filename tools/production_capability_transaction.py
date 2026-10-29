"""Formal same-code capability publication. No DB or Git mutations exist here."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import os
import subprocess

from .production_capability_baseline import CapabilityPlan, HealthReceipt
from .production_activation_control import StartupHandshake
from .production_release_transaction import file_hash, _no_symlink
from . import production_sha_release as sha_release


def _persist(path,data):
    with path.open('x') as f:
        json.dump(data,f,sort_keys=True,indent=2)
        f.flush();os.fsync(f.fileno())
    path.chmod(0o400)


class CapabilityInstallPlan(CapabilityPlan):
    def __init__(self,target,*,manifest_path,runtime_root,evidence_dir):
        super().__init__(target)
        self.root=_no_symlink(runtime_root)
        self.manifest_path=_no_symlink(manifest_path)
        self.manifest_hash=file_hash(self.manifest_path)
        self.directory=_no_symlink(evidence_dir)
        self.directory.mkdir(mode=0o700,exist_ok=False)
        self.seal=self.directory/'CAPABILITY_TARGET.json'
        _persist(self.seal,asdict(target));self.seal_hash=file_hash(self.seal)
        self.install_used=False;self.restore_used=False
        self._handshake=None

    def _manifest(self):
        if file_hash(self.manifest_path)!=self.manifest_hash:
            raise RuntimeError('capability_manifest_changed')
        if file_hash(self.seal)!=self.seal_hash or json.loads(self.seal.read_text())!=json.loads(json.dumps(asdict(self.target))):
            raise RuntimeError('capability_target_changed')
        m=sha_release._load_manifest(self.manifest_path)
        if (m['target']!=sha_release.TARGET_CAPABILITY or m['production_root']!=str(self.root)
                or m['production_head_at_prepare']!=self.target.sha
                or m['requested_heads']!=[self.target.sha]):
            raise RuntimeError('capability_manifest_authority_mismatch')
        before=dict(self.target.original_artifacts);after=dict(self.target.compatible_artifacts)
        paths={e['target_path'] for e in m['artifacts']+m['protected_artifacts']}
        if paths!=set(before) or paths!=set(after):raise RuntimeError('unsealed_capability_artifact_set')
        for e in m['artifacts']:
            if before[e['target_path']]!=e['before_sha256'] or after[e['target_path']]!=e['after_sha256']:
                raise RuntimeError('capability_artifact_binding_mismatch')
        for e in m['protected_artifacts']:
            if before[e['target_path']]!=e['sha256'] or after[e['target_path']]!=e['sha256']:
                raise RuntimeError('capability_protected_binding_mismatch')
        return m

    def _head(self):
        for args in (('rev-parse','HEAD'),('diff','--quiet','HEAD','--'),('diff','--cached','--quiet','--')):
            result=subprocess.run(['/usr/bin/git','-C',str(self.root),*args],
                capture_output=True,text=True,timeout=20)
            if result.returncode or args[0]=='rev-parse' and result.stdout.strip()!=self.target.sha:
                raise RuntimeError('capability_runtime_authority_changed')

    def verify(self,phase):
        if phase not in ('original','compatible'):raise RuntimeError('unknown_capability_phase')
        m=self._manifest();self._head()
        expected=self.target.original_artifacts if phase=='original' else self.target.compatible_artifacts
        for path,digest in expected:
            if file_hash(_no_symlink(path))!=digest:raise RuntimeError('capability_artifact_changed')
        for e in m['protected_artifacts']:sha_release._validate_protected(e)
        for e in m['artifacts']:
            sha_release._validate_target(e,'before_sha256' if phase=='original' else 'after_sha256')

    def validate(self):
        self.verify('original');sha_release._assert_pair_unloaded()

    @property
    def handshake(self):
        if self._handshake is None:raise RuntimeError('capability_not_installed')
        return self._handshake

    def install_compatible(self):
        if self.install_used:raise RuntimeError('capability_install_attempt_consumed')
        self.install_used=True
        self.validate()
        _persist(self.directory/'INSTALL_ATTEMPT.json',{'state':'consumed-before-action'})
        sha_release.apply_release(self.manifest_path)
        self.verify('compatible')
        m=self._manifest();entries={e['role']:e for e in m['artifacts']}
        launcher=Path(entries['admin-parent-launcher']['target_path'])
        directory=launcher.parent/'activation-control'
        _no_symlink(directory)
        if not directory.exists():directory.mkdir(mode=0o700)
        st=directory.stat()
        if st.st_uid!=os.getuid() or st.st_mode & 0o077:
            raise RuntimeError('capability_control_directory_permissions')
        if any(directory.iterdir()):raise RuntimeError('capability_control_directory_not_empty')
        specs={role:(Path(entries[role+'-plist']['target_path']),entries[role+'-plist']['after_sha256'])
               for role in ('parent','admin')}
        digest=entries['admin-parent-launcher']['after_sha256']
        self._handshake=StartupHandshake(directory,launcher,digest,specs,
            release_specs={self.target.sha:(digest,specs)})
        self._handshake.validate_capability()

    def restore_original(self):
        if self.restore_used:raise RuntimeError('capability_restore_attempt_consumed')
        self.restore_used=True;self._manifest();self._head()
        _persist(self.directory/'RESTORE_ATTEMPT.json',{'state':'consumed-before-action'})
        sha_release.rollback_release(self.manifest_path)
        self.verify('original')

    def seal_healthy(self,receipts):
        self.verify('compatible')
        old=dict(self.target.original_pids)
        if (len(receipts)!=2 or {r.role for r in receipts if isinstance(r,HealthReceipt)}!=set(old)
                or any(not isinstance(r,HealthReceipt) or r.pid in old.values() or not r.started_at
                       or r.sha!=self.target.sha or r.healthy is not True for r in receipts)):
            raise RuntimeError('fresh_health_receipts_required')
        _persist(self.directory/'COMPATIBLE_BASELINE.json',{
            'status':'KNOWN_HEALTHY_COMPATIBLE_BASELINE','sha':self.target.sha,
            'controller_manifest_sha256':self.manifest_hash,
            'health_receipts':[asdict(r) for r in receipts],
            'original_artifacts':self.target.original_artifacts,
            'compatible_artifacts':self.target.compatible_artifacts,
            'git_mutations':0,'database_mutations_by_installer':0})

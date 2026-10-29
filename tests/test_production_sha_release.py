import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_PATH = ROOT / "tools/production_sha_release.py"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProductionShaReleaseTests(unittest.TestCase):
    def setUp(self):
        self.release = load_module(RELEASE_PATH, "production_sha_release")
        self.admin = load_module(ROOT / "tools/production_launchd_control.py", "release_admin")
        self.extraction = load_module(
            ROOT / "tools/content_extraction_launchd_control.py", "release_extraction"
        )
        self.nas = load_module(
            ROOT / "tools/nas_recovery_launchd_control.py", "release_nas"
        )
        self.drive = load_module(
            ROOT / "tools/google_drive_launchd_control.py", "release_google_drive"
        )
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.temp = Path(self.folder.name).resolve()
        self.home = self.temp / "home"
        self.production = self.temp / "production"
        self.safebox = self.temp / "safebox"
        self.volume = self.temp / "Volumes/Zhang"
        self.volume.mkdir(parents=True)
        self.production.mkdir()
        self._git("init")
        self._git("config", "user.email", "release-test@example.invalid")
        self._git("config", "user.name", "Release Test")
        (self.production / "database").mkdir()
        (self.production / "cache/content_extractions/runtime/venv/bin").mkdir(parents=True)
        (self.production / "cache/content_extractions/runtime").mkdir(parents=True, exist_ok=True)
        (self.production / "app.py").write_text("# test entry\n", encoding="utf-8")
        (self.production / ".env.production").write_text("TEST=1\n", encoding="utf-8")
        (self.production / "database/aaie_crm.db").write_bytes(b"test-db")
        (self.production / "content_extraction_service.py").write_text(
            "# test extraction entry\n", encoding="utf-8"
        )
        (self.production / "cache/content_extractions/runtime/production-runtime-manifest.json").write_text(
            "{}\n", encoding="utf-8"
        )
        fake_python = self.production / "cache/content_extractions/runtime/venv/bin/python"
        fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_python.chmod(0o755)
        self.stable = self._commit("stable")
        self.current = self._commit("current")
        self.next = self._commit("next")
        self._git("checkout", "--detach", self.current)
        self._install_legacy_policy()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.production, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def _commit(self, label):
        marker = self.production / "marker.txt"
        marker.write_text(label + "\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", label)
        return self._git("rev-parse", "HEAD")

    def _install_legacy_policy(self):
        self.admin.install_artifacts(
            source_root=ROOT, home=self.home, production_root=self.production,
            expected_head=self.stable, service_python=Path("/usr/bin/python3"),
        )
        self.extraction.install_artifacts(
            source_root=ROOT, home=self.home, production_root=self.production,
            expected_head=self.stable, volume_root=self.volume,
        )
        self.nas.install_artifacts(
            source_root=ROOT, home=self.home, production_root=self.production,
            expected_head=self.stable, service_python=Path("/usr/bin/python3"),
        )

    def _launchers(self):
        return (
            self.admin.support_dir(self.home) / "production-service-launcher.zsh",
            self.extraction.support_dir(self.home) / "content-extraction-service-launcher.zsh",
            self.nas.launcher_path(self.home),
        )

    def _prepare(
        self,
        *,
        requested_heads=None,
        target="full-stack",
        expected_content_extraction_heads=None,
        expected_nas_recovery_heads=None,
    ):
        return self.release.prepare_release(
            source_root=ROOT,
            production_root=self.production,
            home=self.home,
            safebox_dir=self.safebox,
            expected_installed_heads=(self.stable,),
            expected_content_extraction_heads=expected_content_extraction_heads,
            expected_nas_recovery_heads=expected_nas_recovery_heads,
            requested_heads=requested_heads or (self.current, self.next),
            service_python=Path("/usr/bin/python3"),
            volume_root=self.volume,
            target=target,
        )

    @staticmethod
    def _sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _rewrite_manifest(self, manifest_path, manifest):
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        manifest_path.write_bytes(payload)
        manifest_path.with_suffix(".sha256").write_text(
            f"{hashlib.sha256(payload).hexdigest()}  {manifest_path.name}\n",
            encoding="utf-8",
        )

    def test_prepare_captures_exact_backup_and_stages_without_installed_mutation(self):
        before = {str(path): (path.read_bytes(), path.stat().st_mode & 0o777) for path in self._launchers()}

        manifest_path = self._prepare()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["production_head_at_prepare"], self.current)
        self.assertEqual(manifest["requested_heads"], [self.current, self.next])
        self.assertEqual(len(manifest["artifacts"]), 3)
        for entry in manifest["artifacts"]:
            target = Path(entry["target_path"])
            backup = Path(entry["backup_path"])
            staged = Path(entry["staged_path"])
            self.assertEqual(target.read_bytes(), before[str(target)][0])
            self.assertEqual(entry["before_sha256"], self._sha(backup))
            self.assertEqual(entry["after_sha256"], self._sha(staged))
            self.assertEqual(entry["uid"], os.getuid())
            self.assertEqual(entry["gid"], os.getgid())
            self.assertEqual(entry["mode"], before[str(target)][1])
        digest = manifest_path.with_suffix(".sha256").read_text().split()[0]
        self.assertEqual(digest, self._sha(manifest_path))

    def test_admin_parent_prepare_stages_one_launcher_and_protects_8792(self):
        manifest_path = self._prepare(target="admin-parent")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        shared_launcher, extraction_launcher, nas_launcher = self._launchers()
        expected_protected = {
            str(extraction_launcher),
            str(self.extraction.support_dir(self.home) / "content_extraction_launchd_entry.py"),
            str(self.extraction.installed_plist(self.home)),
            str(self.production / ".env.production"),
            str(nas_launcher),
            str(self.nas.installed_plist(self.home)),
            str(
                self.production
                / "cache/content_extractions/runtime/production-runtime-manifest.json"
            ),
        }

        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["target"], "admin-parent")
        self.assertEqual(
            [entry["target_path"] for entry in manifest["artifacts"]],
            [str(shared_launcher)],
        )
        self.assertEqual(
            {entry["target_path"] for entry in manifest["protected_artifacts"]},
            expected_protected,
        )
        self.assertFalse(
            any("content-extraction" in entry["staged_path"] for entry in manifest["artifacts"])
        )
        staged_names = {
            path.name
            for path in (self.safebox / "LAUNCHER_CONFIG_AFTER").iterdir()
            if path.is_file()
        }
        self.assertEqual(staged_names, {"admin-parent-launcher.zsh"})

    def test_prepare_default_remains_full_stack(self):
        manifest_path = self._prepare()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["target"], "full-stack")
        self.assertEqual(len(manifest["artifacts"]), 3)
        self.assertEqual(manifest["protected_artifacts"], [])

    def test_full_stack_release_updates_installed_google_drive_scheduler_launcher(self):
        self.drive.install_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.stable,
            service_python=Path("/usr/bin/python3"),
        )
        drive_launcher = self.drive.launcher_path(self.home)

        manifest_path = self._prepare()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["expected_google_drive_scheduler_heads"], [self.stable])
        self.assertEqual(manifest["google_drive_scheduler_installed"], True)
        self.assertEqual(manifest["artifacts"][-1]["role"], "google-drive-scheduler-launcher")
        self.assertEqual(manifest["artifacts"][-1]["target_path"], str(drive_launcher))
        self.release.apply_release(manifest_path)
        self.release.verify_release(manifest_path)
        self.assertEqual(
            self.release.read_allowed_heads(drive_launcher),
            (self.current, self.next),
        )

    def test_full_stack_prepare_accepts_separately_verified_mixed_installed_policies(self):
        extraction_launcher = self._launchers()[1]
        rendered = self.extraction.render_artifacts(
            source_root=ROOT,
            home=self.home,
            production_root=self.production,
            expected_head=self.next,
            volume_root=self.volume,
        )
        extraction_launcher.write_bytes(rendered[extraction_launcher][0])
        extraction_launcher.chmod(0o700)

        manifest_path = self._prepare(
            expected_content_extraction_heads=(self.next,),
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["expected_admin_parent_heads"], [self.stable])
        self.assertEqual(manifest["expected_content_extraction_heads"], [self.next])
        self.assertEqual(manifest["expected_nas_recovery_heads"], [self.stable])
        self.release.apply_release(manifest_path)
        self.release.verify_release(manifest_path)
        for launcher in self._launchers():
            self.assertEqual(
                self.release.read_allowed_heads(launcher),
                (self.current, self.next),
            )

    def test_admin_parent_apply_never_publishes_8792(self):
        manifest_path = self._prepare(target="admin-parent")
        published = []

        def record_publish(path, payload, mode, uid, gid):
            published.append(Path(path))
            self.release.atomic_publish(path, payload, mode, uid, gid)

        self.release.apply_release(manifest_path, publisher=record_publish)

        self.assertEqual(published, [self._launchers()[0]])
        self.assertEqual(
            self.release.read_allowed_heads(self._launchers()[1]),
            (self.stable,),
        )

    def test_admin_parent_apply_rejects_protected_8792_drift_before_publish(self):
        manifest_path = self._prepare(target="admin-parent")
        protected_launcher = self._launchers()[1]
        protected_launcher.write_text("#!/bin/zsh\n# DRIFT\n", encoding="utf-8")
        published = []

        def record_publish(path, payload, mode, uid, gid):
            published.append(Path(path))
            self.release.atomic_publish(path, payload, mode, uid, gid)

        with self.assertRaisesRegex(RuntimeError, "protected artifact hash mismatch"):
            self.release.apply_release(manifest_path, publisher=record_publish)

        self.assertEqual(published, [])

    def test_admin_parent_publish_failure_restores_shared_and_preserves_8792(self):
        manifest_path = self._prepare(target="admin-parent")
        shared, *protected_launchers = self._launchers()
        shared_before = shared.read_bytes()
        protected_before = {path: path.read_bytes() for path in protected_launchers}

        def fail_publish(*_args):
            raise OSError("injected selected publish failure")

        with self.assertRaisesRegex(OSError, "injected selected publish failure"):
            self.release.apply_release(manifest_path, publisher=fail_publish)

        self.assertEqual(shared.read_bytes(), shared_before)
        for path in protected_launchers:
            self.assertEqual(path.read_bytes(), protected_before[path])

    def test_admin_parent_dual_then_single_policy_runs_both_shared_services_only(self):
        manifest_path = self._prepare(target="admin-parent")
        shared, *protected_launchers = self._launchers()
        protected_before = {
            path: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in protected_launchers
        }
        self.release.apply_release(manifest_path)
        self._git("checkout", "--detach", self.next)

        for service in ("admin", "parent"):
            result = subprocess.run(
                ["/bin/zsh", str(shared), service], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        single_manifest = self.release.prepare_release(
            source_root=ROOT,
            production_root=self.production,
            home=self.home,
            safebox_dir=self.temp / "single-slot-safebox",
            expected_installed_heads=(self.current, self.next),
            requested_heads=(self.next,),
            service_python=Path("/usr/bin/python3"),
            volume_root=self.volume,
            target="admin-parent",
        )
        self.release.apply_release(single_manifest)
        self.assertEqual(self.release.read_allowed_heads(shared), (self.next,))
        for path in protected_launchers:
            self.assertEqual(self.release.read_allowed_heads(path), (self.stable,))
            self.assertEqual(
                (path.read_bytes(), path.stat().st_mode & 0o777),
                protected_before[path],
            )

        self._git("checkout", "--detach", self.current)
        rejected = subprocess.run(
            ["/bin/zsh", str(shared), "parent"], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(rejected.returncode, 78)

    def test_admin_parent_rollback_restores_exact_shared_and_preserves_8792(self):
        shared, *protected_launchers = self._launchers()
        before = {
            path: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in (shared, *protected_launchers)
        }
        manifest_path = self._prepare(target="admin-parent")

        self.release.apply_release(manifest_path)
        self.release.rollback_release(manifest_path)

        for path in (shared, *protected_launchers):
            self.assertEqual(
                (path.read_bytes(), path.stat().st_mode & 0o777),
                before[path],
            )

    def test_schema_one_full_stack_manifest_remains_apply_verify_rollback_compatible(self):
        manifest_path = self._prepare()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 1
        manifest.pop("target")
        manifest.pop("protected_artifacts")
        for entry in manifest["artifacts"]:
            entry.pop("role")
        self._rewrite_manifest(manifest_path, manifest)

        self.release.apply_release(manifest_path)
        self.release.verify_release(manifest_path)
        self.release.rollback_release(manifest_path)
        for launcher in self._launchers():
            self.assertEqual(self.release.read_allowed_heads(launcher), (self.stable,))

    def test_schema_two_full_stack_manifest_remains_compatible(self):
        manifest_path = self._prepare()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 2
        manifest["artifacts"] = manifest["artifacts"][:2]
        manifest.pop("expected_nas_recovery_heads")
        self._rewrite_manifest(manifest_path, manifest)

        self.release.apply_release(manifest_path)
        self.release.verify_release(manifest_path)
        self.release.rollback_release(manifest_path)
        for launcher in self._launchers():
            self.assertEqual(self.release.read_allowed_heads(launcher), (self.stable,))

    def test_prepare_cli_target_defaults_and_explicit_scope(self):
        base = [
            "prepare",
            "--safebox-dir", str(self.safebox),
            "--expected-installed-head", self.stable,
            "--requested-head", self.current,
        ]
        self.assertEqual(self.release.parse_args(base).target, "full-stack")
        self.assertEqual(
            self.release.parse_args([*base, "--target", "admin-parent"]).target,
            "admin-parent",
        )
        mixed = self.release.parse_args([
            *base,
            "--expected-content-extraction-head", self.next,
            "--expected-nas-recovery-head", self.stable,
            "--expected-google-drive-scheduler-head", self.current,
        ])
        self.assertEqual(mixed.expected_content_extraction_head, [self.next])
        self.assertEqual(mixed.expected_nas_recovery_head, [self.stable])
        self.assertEqual(mixed.expected_google_drive_scheduler_head, [self.current])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.release.parse_args([*base, "--target", "content-extraction"])

    def test_prepare_rejects_symlink_or_unexpected_installed_content_before_writes(self):
        first, second, *_rest = self._launchers()
        first.unlink()
        first.symlink_to(second)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self._prepare()
        self.assertFalse(self.safebox.exists())

        first.unlink()
        first.write_text("unexpected parallel owner\n", encoding="utf-8")
        first.chmod(0o700)
        with self.assertRaisesRegex(RuntimeError, "installed policy"):
            self._prepare()
        self.assertFalse(self.safebox.exists())

    def test_prepare_rejects_duplicate_malformed_or_nonancestor_policy(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self._prepare(requested_heads=(self.current, self.current))
        with self.assertRaisesRegex(ValueError, "full lowercase commit hash"):
            self._prepare(requested_heads=("bad",))

        self._git("checkout", "--orphan", "unrelated")
        for path in self.production.iterdir():
            if path.name != ".git" and path.is_file():
                path.unlink()
        unrelated = self._commit("unrelated")
        self._git("checkout", "--detach", self.current)
        with self.assertRaisesRegex(RuntimeError, "ancestor"):
            self._prepare(requested_heads=(self.current, unrelated))

    def test_prepare_rejects_dirty_production_tree_before_evidence_or_writes(self):
        (self.production / "marker.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Production tracked tree is dirty"):
            self._prepare()
        self.assertFalse(self.safebox.exists())

    def test_zsh_validation_rejects_malformed_staged_launcher(self):
        with self.assertRaisesRegex(RuntimeError, "zsh syntax validation failed"):
            self.release.validate_zsh(b"if then\n")

    def test_apply_verify_and_idempotent_rollback_preserve_exact_metadata(self):
        manifest_path = self._prepare()
        originals = {
            str(path): (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in self._launchers()
        }

        self.release.apply_release(manifest_path)
        self.release.verify_release(manifest_path)
        for path in self._launchers():
            self.assertEqual(self.release.read_allowed_heads(path), (self.current, self.next))
            self.assertEqual(path.stat().st_mode & 0o777, originals[str(path)][1])

        self.release.rollback_release(manifest_path)
        self.release.rollback_release(manifest_path)
        for path in self._launchers():
            self.assertEqual(path.read_bytes(), originals[str(path)][0])
            self.assertEqual(path.stat().st_mode & 0o777, originals[str(path)][1])

    def test_partial_apply_failure_restores_every_target(self):
        manifest_path = self._prepare()
        originals = {str(path): path.read_bytes() for path in self._launchers()}
        calls = 0

        def fail_second(path, payload, mode, uid, gid):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second publish failure")
            self.release.atomic_publish(path, payload, mode, uid, gid)

        with self.assertRaisesRegex(OSError, "injected second publish failure"):
            self.release.apply_release(manifest_path, publisher=fail_second)
        for path in self._launchers():
            self.assertEqual(path.read_bytes(), originals[str(path)])

    def test_single_slot_convergence_rejects_old_head(self):
        self._git("checkout", "--detach", self.next)
        manifest_path = self._prepare(requested_heads=(self.next,))
        self.release.apply_release(manifest_path)
        for path in self._launchers():
            self.assertEqual(self.release.read_allowed_heads(path), (self.next,))
            self.assertTrue(self.release.head_is_allowed(path, self.next))
            self.assertFalse(self.release.head_is_allowed(path, self.current))
            self.assertFalse(self.release.head_is_allowed(path, "f" * 40))

    def test_rendered_launchers_execute_current_and_next_and_fail_closed_for_other_sha(self):
        policy_home = self.temp / "policy-home"
        self.admin.install_artifacts(
            source_root=ROOT, home=policy_home, production_root=self.production,
            expected_heads=(self.current, self.next), service_python=Path("/usr/bin/true"),
        )
        self.extraction.install_artifacts(
            source_root=ROOT, home=policy_home, production_root=self.production,
            expected_heads=(self.current, self.next), volume_root=Path("/Volumes/Zhang"),
        )
        admin_launcher = self.admin.support_dir(policy_home) / "production-service-launcher.zsh"
        extraction_launcher = (
            self.extraction.support_dir(policy_home) / "content-extraction-service-launcher.zsh"
        )
        admin_log = self.admin.logs_dir(policy_home) / "admin.launcher.log"
        extraction_log = self.extraction.logs_dir(policy_home) / "launcher.log"

        for allowed in (self.current, self.next):
            self._git("checkout", "--detach", allowed)
            admin_result = subprocess.run(
                ["/bin/zsh", str(admin_launcher), "admin"], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            extraction_result = subprocess.run(
                ["/bin/zsh", str(extraction_launcher)], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(admin_result.returncode, 0, admin_result.stderr)
            self.assertEqual(extraction_result.returncode, 0, extraction_result.stderr)
            self.assertIn(f"exec_approved head={allowed}", admin_log.read_text())
            self.assertIn(f"exec_approved head={allowed}", extraction_log.read_text())

        self._git("checkout", "--detach", self.stable)
        admin_result = subprocess.run(
            ["/bin/zsh", str(admin_launcher), "admin"], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        extraction_result = subprocess.run(
            ["/bin/zsh", str(extraction_launcher)], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(admin_result.returncode, 78)
        self.assertEqual(extraction_result.returncode, 0)
        self.assertIn("preflight_failed code=production_head_mismatch", admin_log.read_text())
        self.assertIn("preflight_permanent code=production_head_mismatch", extraction_log.read_text())


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from pathlib import Path
from unittest.mock import patch

import formal_service_restart_control as control


class ClassTwoDisabledStartupScriptTests(unittest.TestCase):
    def test_start_script_launches_admin_on_lan_and_parent_on_loopback_without_watchdog(self):
        source = Path("start.command").read_text(encoding="utf-8")
        self.assertIn('AAIE_CRM_DISABLE_WATCHDOG', source)
        self.assertIn('ADMIN_HOST="${AAIE_CRM_ADMIN_HOST:-0.0.0.0}"', source)
        self.assertIn('app.py --host "$ADMIN_HOST" --port "$ADMIN_PORT" --service admin', source)
        self.assertIn('app.py --host 127.0.0.1 --port "$PARENT_PORT" --service parent', source)
        self.assertIn('echo $! > logs/admin.pid', source)
        self.assertIn('echo $! > logs/parent.pid', source)
import watchdog


class FormalServiceRestartControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "logs").mkdir()
        (self.root / "state").mkdir()
        (self.root / ".gitignore").write_text("logs/\nstate/\n", encoding="utf-8")
        (self.root / "adjustment.md").write_text("protected\n", encoding="utf-8")
        (self.root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        (self.root / "adjustment.md").write_text("protected user change\n", encoding="utf-8")
        self.approved = control.ApprovedBaseline(
            head=self.git("rev-parse", "HEAD"),
            tree=self.git("rev-parse", "HEAD^{tree}"),
            source_manifest_sha256=control.source_manifest_sha256(
                self.root, ("source.py",)
            ),
            adjustment_md_sha256=self.sha(self.root / "adjustment.md"),
            workspace=str(self.root.resolve()),
            source_files=("source.py",),
        )
        self.paths = control.RestartPaths(self.root, self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_gate_accepts_exact_baseline_and_protected_adjustment(self):
        result = control.inspect_gate(self.approved)
        self.assertTrue(result.ok)
        self.assertEqual(result.observed_head, self.approved.head)
        self.assertEqual(
            result.git_status_summary,
            {
                "branch": self.git("branch", "--show-current"),
                "head": self.approved.head,
                "staged_count": 0,
                "tracked_modified_count": 1,
                "untracked_count": 0,
                "allowed_tracked_modifications": ["adjustment.md"],
                "unexpected_tracked_modifications": [],
                "adjustment_md_status": " M",
                "status_ok": True,
            },
        )

    def test_gate_rejects_head_tree_manifest_adjustment_staged_and_other_dirty(self):
        mutations = [
            ("head", "0" * 40),
            ("tree", "1" * 40),
            ("source_manifest_sha256", "2" * 64),
            ("adjustment_md_sha256", "3" * 64),
        ]
        for field, value in mutations:
            with self.subTest(field=field):
                baseline = control.ApprovedBaseline(
                    **{**self.approved.__dict__, field: value}
                )
                self.assertFalse(control.inspect_gate(baseline).ok)
        subprocess.run(["git", "add", "adjustment.md"], cwd=self.root, check=True)
        self.assertFalse(control.inspect_gate(self.approved).ok)
        subprocess.run(["git", "reset", "-q"], cwd=self.root, check=True)
        (self.root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertFalse(control.inspect_gate(self.approved).ok)

    def test_stash_is_recorded_without_becoming_a_gate_failure(self):
        subprocess.run(
            ["git", "stash", "push", "-qm", "protected", "--", "adjustment.md"],
            cwd=self.root, check=True,
        )
        (self.root / "adjustment.md").write_text(
            "protected user change\n", encoding="utf-8"
        )
        result = control.inspect_gate(self.approved)
        self.assertTrue(result.ok)
        self.assertTrue(result.stash_snapshot)

    def test_paths_are_unified_and_markers_are_atomic_json(self):
        self.assertEqual(self.paths.request, self.root / "logs/restart.request")
        self.assertEqual(self.paths.handled, self.root / "state/restart.handled.json")
        self.assertEqual(self.paths.failed, self.root / "state/restart.failed.json")
        self.assertEqual(self.paths.result, self.root / "state/restart.result.json")
        payload = {"request_id": "req-1", "stage": "accepted"}
        control.atomic_write_json(self.paths.result, payload)
        self.assertEqual(json.loads(self.paths.result.read_text()), payload)
        self.assertFalse(any(self.paths.result.parent.glob("*.tmp")))

    def test_unique_request_id_and_old_marker_cannot_complete_new_request(self):
        first = control.new_request_id()
        second = control.new_request_id()
        self.assertNotEqual(first, second)
        control.atomic_write_json(
            self.paths.handled, {"request_id": first, "stage": "completed"}
        )
        self.assertEqual(
            control.read_request_outcome(self.paths, second)["state"], "pending"
        )
        control.atomic_write_json(
            self.paths.failed, {"request_id": second, "stage": "failed"}
        )
        self.assertEqual(
            control.read_request_outcome(self.paths, second)["state"], "failed"
        )

    def test_dry_run_never_creates_request_or_changes_markers(self):
        before = {p: p.stat().st_mtime_ns for p in self.paths.marker_paths if p.exists()}
        result = control.request_restart(
            self.approved, self.paths, dry_run=True, timeout_seconds=1
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.paths.request.exists())
        self.assertEqual(
            before,
            {p: p.stat().st_mtime_ns for p in self.paths.marker_paths if p.exists()},
        )

    def test_gate_failure_writes_failure_evidence_without_stopping_service(self):
        bad = control.ApprovedBaseline(
            **{**self.approved.__dict__, "head": "f" * 40}
        )
        called = []
        result = control.prepare_service_start(
            bad, self.paths, "admin", 8780, "/python", stop_callback=called.append
        )
        self.assertFalse(result["ok"])
        self.assertEqual(called, [])
        marker = json.loads(self.paths.failed.read_text())
        self.assertEqual(marker["stage"], "gate_rejected")

    def test_start_evidence_contains_required_fields(self):
        evidence = control.service_start_evidence(
            self.approved, self.paths, "parent", 8781, 12345, "/python"
        )
        required = {
            "approved_head", "approved_tree", "observed_head", "observed_tree",
            "source_manifest_sha256", "workspace", "python_runtime",
            "service_name", "port", "pid", "started_at",
            "adjustment_md_sha256", "git_status_summary",
        }
        self.assertTrue(required.issubset(evidence))
        self.assertEqual(evidence["pid"], 12345)

    def test_watchdog_startup_evidence_records_real_observed_values(self):
        evidence = control.watchdog_startup_evidence(
            self.approved, "/frozen/python3", 4321
        )
        required = {
            "approved_head",
            "approved_tree",
            "observed_head",
            "observed_tree",
            "source_manifest_sha256",
            "approved_adjustment_md_sha256",
            "observed_adjustment_md_sha256",
            "git_status_summary",
            "workspace",
            "python_runtime",
            "watchdog_pid",
            "started_at",
        }
        self.assertTrue(required.issubset(evidence))
        self.assertEqual(evidence["observed_head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(
            evidence["observed_tree"], self.git("rev-parse", "HEAD^{tree}")
        )
        self.assertEqual(
            evidence["observed_adjustment_md_sha256"],
            self.sha(self.root / "adjustment.md"),
        )
        self.assertEqual(evidence["python_runtime"], "/frozen/python3")
        self.assertEqual(evidence["watchdog_pid"], 4321)
        self.assertTrue(evidence["status_ok"])

    def test_watchdog_startup_evidence_logs_mismatches_before_rejecting(self):
        cases = (
            ("head", "0" * 40, "head_mismatch"),
            ("tree", "1" * 40, "tree_mismatch"),
            (
                "adjustment_md_sha256",
                "2" * 64,
                "adjustment_sha256_mismatch",
            ),
        )
        for field, value, error in cases:
            with self.subTest(field=field):
                baseline = control.ApprovedBaseline(
                    **{**self.approved.__dict__, field: value}
                )
                log_path = self.root / f"{field}.log"
                result = control.record_watchdog_startup_evidence(
                    baseline, log_path, "/frozen/python3", 4321
                )
                self.assertFalse(result["status_ok"])
                self.assertIn(error, result["gate_errors"])
                logged = json.loads(log_path.read_text(encoding="utf-8"))
                self.assertEqual(logged["approved_head"], baseline.head)
                self.assertEqual(
                    logged["observed_head"], self.git("rev-parse", "HEAD")
                )
                self.assertEqual(logged["status_ok"], False)

    def test_git_status_summary_rejects_staged_unexpected_and_untracked(self):
        subprocess.run(
            ["git", "add", "adjustment.md"], cwd=self.root, check=True
        )
        staged = control.inspect_gate(self.approved).git_status_summary
        self.assertEqual(staged["staged_count"], 1)
        self.assertFalse(staged["status_ok"])
        subprocess.run(["git", "reset", "-q"], cwd=self.root, check=True)

        (self.root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        unexpected = control.inspect_gate(self.approved).git_status_summary
        self.assertEqual(unexpected["tracked_modified_count"], 2)
        self.assertEqual(
            unexpected["unexpected_tracked_modifications"], ["source.py"]
        )
        self.assertFalse(unexpected["status_ok"])

        (self.root / "untracked-token-secret.txt").write_text(
            "token=must-not-appear\n", encoding="utf-8"
        )
        untracked = control.inspect_gate(self.approved).git_status_summary
        self.assertEqual(untracked["untracked_count"], 1)
        self.assertNotIn("untracked-token-secret.txt", json.dumps(untracked))
        self.assertNotIn("must-not-appear", json.dumps(untracked))
        self.assertFalse(untracked["status_ok"])

    def test_watchdog_startup_log_is_json_and_excludes_file_contents(self):
        secret = "password=do-not-log"
        (self.root / "adjustment.md").write_text(
            f"protected user change\n{secret}\n", encoding="utf-8"
        )
        baseline = control.ApprovedBaseline(
            **{
                **self.approved.__dict__,
                "adjustment_md_sha256": self.sha(self.root / "adjustment.md"),
            }
        )
        log_path = self.root / "watchdog.log"
        evidence = control.record_watchdog_startup_evidence(
            baseline, log_path, "/frozen/python3", 4321
        )
        logged = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(logged, evidence)
        self.assertEqual(logged["event"], "watchdog_startup_evidence")
        self.assertNotIn(secret, log_path.read_text(encoding="utf-8"))

    def test_watchdog_gate_failure_refuses_before_locks_or_service_checks(self):
        bad = control.ApprovedBaseline(
            **{**self.approved.__dict__, "head": "f" * 40}
        )
        log_path = self.root / "watchdog-formal.log"
        with (
            patch.object(
                watchdog,
                "approved_from_environment_if_enabled",
                return_value=bad,
            ),
            patch.object(watchdog, "WATCHDOG_LOG", log_path),
            patch.object(
                watchdog, "acquire_watchdog_dir_lock", return_value=False
            ) as acquire,
            patch.object(watchdog, "ensure_service") as ensure,
        ):
            watchdog.watch_loop()
        acquire.assert_not_called()
        ensure.assert_not_called()
        logged = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertFalse(logged["status_ok"])
        self.assertIn("head_mismatch", logged["gate_errors"])


if __name__ == "__main__":
    unittest.main()

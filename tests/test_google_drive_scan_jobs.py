import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google_drive_scan_jobs import (
    GoogleDriveScanCoordinator,
    GoogleDriveScanJobStore,
    ScanJobAccessError,
    ScanJobBusyError,
)


def wait_for_terminal(coordinator, job_id, actor_user_id, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = coordinator.get_for_actor(job_id, actor_user_id)
        if job["state"] in {"completed", "failed", "interrupted"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"scan job {job_id} did not finish within {timeout} seconds")


class GoogleDriveScanJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drive-scan-jobs-")
        self.now_value = datetime(2026, 8, 10, 5, 30, tzinfo=timezone.utc)
        self.store = GoogleDriveScanJobStore(
            Path(self.temp.name),
            now=lambda: self.now_value,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_job_json_is_sanitized_and_progress_never_moves_backwards(self):
        job = self.store.create(5)
        self.store.update(
            job["job_id"],
            state="running",
            phase="reading",
            percent=38,
            processed=326,
            total=853,
            message="Bearer secret-token file body private-family-text",
            ignored_body="private-family-text",
        )
        saved = self.store.update(job["job_id"], percent=12, error_summary="api_key=hidden-key")

        self.assertEqual(saved["percent"], 38)
        self.assertEqual(saved["processed"], 326)
        self.assertEqual(saved["total"], 853)
        raw = (self.store.job_dir / f"{job['job_id']}.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-token", raw)
        self.assertNotIn("hidden-key", raw)
        self.assertNotIn("private-family-text", raw)
        self.assertEqual(
            set(json.loads(raw)),
            {
                "job_id", "actor_user_id", "state", "phase", "percent",
                "processed", "total", "message", "started_at", "updated_at",
                "finished_at", "scan_id", "error_summary",
            },
        )

    def test_job_details_are_owner_only(self):
        job = self.store.create(5)

        self.assertEqual(self.store.get_for_actor(job["job_id"], 5)["job_id"], job["job_id"])
        with self.assertRaises(ScanJobAccessError):
            self.store.get_for_actor(job["job_id"], 6)

    def test_stale_running_job_becomes_interrupted_without_restart(self):
        job = self.store.create(5)
        self.store.update(job["job_id"], state="running", phase="reading", percent=42)
        self.store.set_active(job["job_id"])
        self.now_value += timedelta(minutes=6)

        changed = self.store.recover_interrupted(timedelta(minutes=5))
        recovered = self.store.get_for_actor(job["job_id"], 5)

        self.assertEqual(changed, 1)
        self.assertEqual(recovered["state"], "interrupted")
        self.assertEqual(recovered["percent"], 42)
        self.assertIn("重新扫描", recovered["message"])
        self.assertIsNone(self.store.active())


class GoogleDriveScanCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drive-scan-coordinator-")
        self.store = GoogleDriveScanJobStore(
            Path(self.temp.name),
            now=lambda: datetime.now(timezone.utc),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_same_actor_attaches_while_other_actor_is_rejected(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def runner(actor_user_id, progress):
            calls.append(actor_user_id)
            progress({"phase": "reading", "percent": 20, "processed": 1, "total": 2})
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return {"scan_id": "scan-safe"}

        coordinator = GoogleDriveScanCoordinator(self.store, runner)
        first = coordinator.start_or_attach(5)
        self.assertTrue(started.wait(timeout=1))

        attached = coordinator.start_or_attach(5)
        with self.assertRaises(ScanJobBusyError):
            coordinator.start_or_attach(6)

        self.assertEqual(attached["job_id"], first["job_id"])
        self.assertEqual(calls, [5])
        release.set()
        completed = wait_for_terminal(coordinator, first["job_id"], 5)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["percent"], 100)
        self.assertEqual(completed["scan_id"], "scan-safe")

    def test_runner_failure_is_terminal_and_redacted(self):
        def runner(_actor_user_id, _progress):
            raise RuntimeError("Bearer secret-token refresh_token=hidden")

        coordinator = GoogleDriveScanCoordinator(self.store, runner)
        job = coordinator.start_or_attach(5)
        failed = wait_for_terminal(coordinator, job["job_id"], 5)

        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("secret-token", failed["error_summary"])
        self.assertNotIn("hidden", failed["error_summary"])


if __name__ == "__main__":
    unittest.main()

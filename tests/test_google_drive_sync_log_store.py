import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google_drive_sync_log_store import (
    GoogleDriveRuntimeStore,
    PreviewAuthorizationError,
    PreviewExpiredError,
    PreviewNotFoundError,
    PreviewStateError,
)


class GoogleDriveRuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="google-drive-runtime-store-")
        self.root = Path(self.temp.name)
        self.current = datetime(2026, 8, 10, 3, 30, tzinfo=timezone.utc)
        self.store = GoogleDriveRuntimeStore(
            self.root,
            now=lambda: self.current,
            preview_ttl=timedelta(minutes=30),
        )
        self.preview_payload = {
            "scan_id": "scan-1",
            "confirmation_nonce": "nonce-1",
            "actor_user_id": 7,
            "summary": {"total": 1},
            "items": [
                {
                    "drive_file_id": "drive-1",
                    "name": "Safe title.txt",
                    "body": "private body must not be persisted",
                    "content_fingerprint": "fingerprint-1",
                    "headers": {"Authorization": "Bearer secret"},
                }
            ],
            "access_token": "access-secret",
            "nested": {"refresh_token": "refresh-secret", "safe": "kept"},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_save_preview_is_atomic_expiring_and_removes_sensitive_values(self):
        saved = self.store.save_preview(self.preview_payload)

        self.assertEqual(saved["state"], "previewed")
        self.assertEqual(saved["scan_id"], "scan-1")
        self.assertEqual(saved["items"][0]["content_fingerprint"], "fingerprint-1")
        self.assertEqual(saved["nested"], {"safe": "kept"})
        encoded = json.dumps(saved, ensure_ascii=False)
        for secret in ("private body", "Bearer secret", "access-secret", "refresh-secret"):
            self.assertNotIn(secret, encoded)
        self.assertTrue((self.root / "previews" / "scan-1.json").is_file())
        self.assertEqual(list((self.root / "previews").glob("*.tmp")), [])

    def test_completed_confirmation_replay_returns_saved_report_without_rewrite(self):
        self.store.save_preview(self.preview_payload)
        claimed = self.store.claim_confirmation("scan-1", "nonce-1", 7)
        self.assertEqual(claimed["state"], "confirming")
        expected = {"status": "completed", "created": 1, "failed": 0}
        self.store.complete_confirmation("scan-1", expected)

        replay = self.store.claim_confirmation("scan-1", "nonce-1", 7)

        self.assertEqual(replay["state"], "completed")
        self.assertEqual(replay["replay_report"], expected)

    def test_wrong_actor_or_nonce_is_rejected_without_state_change(self):
        self.store.save_preview(self.preview_payload)

        with self.assertRaises(PreviewAuthorizationError):
            self.store.claim_confirmation("scan-1", "wrong", 7)
        with self.assertRaises(PreviewAuthorizationError):
            self.store.claim_confirmation("scan-1", "nonce-1", 8)

        self.assertEqual(self.store.load_preview("scan-1")["state"], "previewed")

    def test_expired_missing_corrupt_and_already_confirming_previews_fail_closed(self):
        self.store.save_preview(self.preview_payload)
        self.current += timedelta(minutes=31)
        with self.assertRaises(PreviewExpiredError):
            self.store.load_preview("scan-1")
        with self.assertRaises(PreviewNotFoundError):
            self.store.load_preview("missing")

        corrupt = self.root / "previews" / "corrupt.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(PreviewStateError):
            self.store.load_preview("corrupt")

        self.current -= timedelta(minutes=31)
        self.store.claim_confirmation("scan-1", "nonce-1", 7)
        with self.assertRaises(PreviewStateError):
            self.store.claim_confirmation("scan-1", "nonce-1", 7)

    def test_failed_confirmation_is_saved_as_replayable_report(self):
        self.store.save_preview(self.preview_payload)
        self.store.claim_confirmation("scan-1", "nonce-1", 7)
        expected = {"status": "failed", "failed": 1, "error_code": "writer_failed"}
        self.store.fail_confirmation("scan-1", expected)

        replay = self.store.claim_confirmation("scan-1", "nonce-1", 7)

        self.assertEqual(replay["state"], "failed")
        self.assertEqual(replay["replay_report"], expected)

    def test_policy_write_is_atomic_and_defaults_to_paused(self):
        self.assertEqual(
            self.store.read_policy(),
            {"is_enabled": False, "status": "PAUSED", "paused_at": "", "updated_at": ""},
        )

        saved = self.store.write_policy(
            {"is_enabled": True, "status": "RUNNING", "updated_by": 7, "api_key": "secret"}
        )

        self.assertTrue(saved["is_enabled"])
        self.assertNotIn("api_key", saved)
        self.assertEqual(self.store.read_policy(), saved)
        self.assertEqual(list(self.root.glob("service-policy.json.tmp")), [])

    def test_logs_are_sanitized_newest_first_and_limited(self):
        for index in range(55):
            self.current += timedelta(seconds=1)
            self.store.append_log(
                {
                    "trigger": "manual_scan",
                    "sequence": index,
                    "body": f"private-{index}",
                    "details": {"client_secret": "secret", "result": "safe"},
                }
            )

        rows = self.store.list_logs(limit=50)

        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["sequence"], 54)
        self.assertEqual(rows[-1]["sequence"], 5)
        self.assertEqual(rows[0]["details"], {"result": "safe"})
        self.assertNotIn("private-54", json.dumps(rows))

    def test_logs_ignore_appledouble_and_non_utf8_sidecar_files(self):
        self.store.append_log({"trigger": "manual_scan", "sequence": 1})
        (self.store.log_dir / "._2026-08.jsonl").write_bytes(b"\x00\x05\xb0appledouble")
        (self.store.log_dir / "broken.jsonl").write_bytes(b"\xff\xfeinvalid")

        rows = self.store.list_logs(limit=50)

        self.assertEqual([row["sequence"] for row in rows], [1])

    def test_log_trigger_filter_is_applied_before_limit(self):
        self.store.append_log({"trigger": "automatic", "sequence": 1})
        for sequence in range(2, 205):
            self.current += timedelta(seconds=1)
            self.store.append_log({"trigger": "manual_scan", "sequence": sequence})

        rows = self.store.list_logs(limit=1, trigger="automatic")

        self.assertEqual([row["sequence"] for row in rows], [1])

    def test_reports_are_sanitized_identified_newest_first_and_ignore_sidecars(self):
        first = self.store.report_dir / "first.json"
        first.write_text(
            json.dumps({"scan_id": "first", "trigger": "automatic", "body": "private"}),
            encoding="utf-8",
        )
        second = self.store.report_dir / "second.json"
        second.write_text(
            json.dumps({"scan_id": "second", "trigger": "automatic", "failed": 1}),
            encoding="utf-8",
        )
        os.utime(first, (1_700_000_000, 1_700_000_000))
        os.utime(second, (1_700_000_100, 1_700_000_100))
        (self.store.report_dir / "._second.json").write_bytes(b"\x00\x05appledouble")
        (self.store.report_dir / "broken.json").write_text("{not-json", encoding="utf-8")

        rows = self.store.list_reports(limit=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["report_id"], "second")
        self.assertEqual(rows[0]["scan_id"], "second")
        self.assertIn("_stored_at", rows[0])
        self.assertNotIn("body", rows[0])

    def test_report_trigger_filter_is_applied_before_limit(self):
        automatic = self.store.report_dir / "automatic.json"
        automatic.write_text(json.dumps({"trigger": "automatic"}), encoding="utf-8")
        manual = self.store.report_dir / "manual.json"
        manual.write_text(json.dumps({"trigger": "manual_confirm"}), encoding="utf-8")
        os.utime(automatic, (1_700_000_000, 1_700_000_000))
        os.utime(manual, (1_700_000_100, 1_700_000_100))

        rows = self.store.list_reports(limit=1, trigger="automatic")

        self.assertEqual([row["report_id"] for row in rows], ["automatic"])


if __name__ == "__main__":
    unittest.main()

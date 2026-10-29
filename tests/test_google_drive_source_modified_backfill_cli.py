import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from google_drive_source_modified_backfill import build_source_modified_plan
from scripts.backfill_google_drive_source_modified import (
    MetadataOnlyDriveClient,
    assert_launcher_runtime_binding,
    apply_manifest,
    create_sqlite_backup,
    fetch_metadata_map,
    revalidate_plan_metadata,
    verify_live_authority,
    verify_reconciled_authority,
    write_dry_run_evidence,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


class MetadataOnlyDriveClientTests(unittest.TestCase):
    def test_request_is_metadata_only_and_has_no_content_reader(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({
                "id": "drive-1",
                "name": "Drive One",
                "modifiedTime": "2026-08-22T00:00:00Z",
                "createdTime": "2026-06-01T00:00:00Z",
            })

        client = MetadataOnlyDriveClient(
            {"access_token": "ephemeral", "token_expiry": "2099-01-01T00:00:00+00:00"},
            opener=opener,
        )

        result = client.get_metadata("drive-1")

        parsed = urlparse(requests[0][0].full_url)
        self.assertEqual(parsed.path, "/drive/v3/files/drive-1")
        self.assertEqual(
            parse_qs(parsed.query),
            {"fields": ["id,modifiedTime,createdTime,name"], "supportsAllDrives": ["true"]},
        )
        self.assertEqual(requests[0][0].get_method(), "GET")
        self.assertEqual(result["modifiedTime"], "2026-08-22T00:00:00Z")
        self.assertFalse(hasattr(client, "read_content"))

    def test_concurrent_fetch_reports_inaccessible_ids_without_aborting(self):
        class Client:
            def get_metadata(self, drive_id):
                if drive_id == "missing":
                    raise RuntimeError("not found")
                return {
                    "id": drive_id,
                    "name": drive_id,
                    "modifiedTime": "2026-08-22T00:00:00Z",
                    "createdTime": "2026-06-01T00:00:00Z",
                }

        result = fetch_metadata_map(["one", "missing", "two"], Client(), max_workers=2)

        self.assertEqual(sorted(result["values"]), ["one", "two"])
        self.assertEqual(result["errors"], {"missing": "not found"})


class ReconciledAuthorityTests(unittest.TestCase):
    def test_launcher_runtime_binding_rejects_historical_unbound_exec_record(self):
        with self.assertRaisesRegex(RuntimeError, "launcher exec record is not bound"):
            assert_launcher_runtime_binding(
                service="admin",
                listener_pid=999,
                launchd_pid=999,
                process_started_at=datetime(2026, 8, 22, 8, 14, tzinfo=timezone.utc),
                exec_approved_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )

    def test_launcher_runtime_binding_requires_listener_to_equal_launchd_pid(self):
        stamp = datetime(2026, 8, 22, 8, 14, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError, "launchd PID mismatch"):
            assert_launcher_runtime_binding(
                service="admin",
                listener_pid=999,
                launchd_pid=1000,
                process_started_at=stamp,
                exec_approved_at=stamp,
            )

    @mock.patch("scripts.backfill_google_drive_source_modified.subprocess.run")
    @mock.patch("scripts.backfill_google_drive_source_modified.git_head")
    def test_explicit_production_and_main_descendant_are_both_required(self, git_head_mock, run_mock):
        git_head_mock.side_effect = ["production", "main"]
        run_mock.return_value = mock.Mock(returncode=0)

        result = verify_reconciled_authority(Path("/repo"), "production", "main")

        self.assertEqual(result, {"production_sha": "production", "main_sha": "main"})
        run_mock.assert_called_once()

    @mock.patch("scripts.backfill_google_drive_source_modified.git_head")
    def test_authority_mismatch_fails_closed(self, git_head_mock):
        git_head_mock.side_effect = ["unexpected", "main"]

        with self.assertRaisesRegex(RuntimeError, "authority drift"):
            verify_reconciled_authority(Path("/repo"), "production", "main")

    @mock.patch("scripts.backfill_google_drive_source_modified.verify_reconciled_authority")
    @mock.patch("scripts.backfill_google_drive_source_modified.read_allowed_heads")
    def test_live_authority_requires_launcher_and_both_runtime_shas(
        self, heads_mock, reconciled_mock
    ):
        reconciled_mock.return_value = {"production_sha": "production", "main_sha": "main"}
        heads_mock.return_value = ("production",)

        result = verify_live_authority(
            Path("/repo"),
            "production",
            "main",
            launcher_path=Path("/launcher"),
            launcher_log_dir=Path("/logs"),
            runtime_probe=lambda service, port, root, log_dir: {
                "service": service,
                "port": port,
                "pid": 100 + port,
                "cwd": str(root),
                "sha": "production",
                "healthy": True,
            },
        )

        self.assertEqual(result["launcher_sha"], "production")
        self.assertEqual(result["admin_runtime_sha"], "production")
        self.assertEqual(result["parent_runtime_sha"], "production")

    @mock.patch("scripts.backfill_google_drive_source_modified.verify_reconciled_authority")
    @mock.patch("scripts.backfill_google_drive_source_modified.read_allowed_heads")
    def test_live_authority_rejects_mixed_runtime(self, heads_mock, reconciled_mock):
        reconciled_mock.return_value = {"production_sha": "production", "main_sha": "main"}
        heads_mock.return_value = ("production",)

        with self.assertRaisesRegex(RuntimeError, "parent runtime authority mismatch"):
            verify_live_authority(
                Path("/repo"),
                "production",
                "main",
                launcher_path=Path("/launcher"),
                launcher_log_dir=Path("/logs"),
                runtime_probe=lambda service, port, root, log_dir: {
                    "service": service,
                    "port": port,
                    "pid": 100 + port,
                    "cwd": str(root),
                    "sha": "stale" if service == "parent" else "production",
                    "healthy": True,
                },
            )


class BackfillEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-modified-cli-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "production.sqlite3"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE content_scripts(
              id INTEGER PRIMARY KEY,title TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE content_materials(
              id INTEGER PRIMARY KEY,script_id INTEGER,source_path TEXT,title TEXT,
              created_at TEXT,updated_at TEXT,source_modified_at TEXT
            );
            INSERT INTO content_scripts VALUES(
              1,'One','2026-06-01T00:00:00.000+00:00','2026-08-20T10:00:00+10:00'
            );
            INSERT INTO content_materials VALUES(
              1,1,'gdrive://drive-one','One','2026-08-01T00:00:00Z','2026-08-20T00:00:00Z',NULL
            );
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return build_source_modified_plan(
                conn,
                lambda drive_id: {
                    "id": drive_id,
                    "name": "One",
                    "modifiedTime": "2026-08-22T00:00:00Z",
                    "createdTime": "2026-06-01T00:00:00Z",
                },
                local_metadata={"values": {}, "conflicts": {}},
                require_freshness=True,
            )
        finally:
            conn.close()

    def test_evidence_manifest_and_apply_are_exact_and_idempotent(self):
        evidence = self.root / "evidence"
        manifest_path = write_dry_run_evidence(
            self.plan(), evidence, production_sha="prod", candidate_sha="candidate", database_path=self.db_path
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rollback = json.loads((evidence / "rollback-mapping.json").read_text(encoding="utf-8"))
        backup_path = self.root / "backup" / "before.sqlite3"
        backup = create_sqlite_backup(self.db_path, backup_path)

        first = apply_manifest(
            self.db_path,
            manifest_path,
            backup_path=backup_path,
            expected_production_sha="prod",
            expected_candidate_sha="candidate",
            backup_already_created=True,
            fresh_metadata_lookup=lambda drive_id: {
                "id": drive_id,
                "name": "One",
                "modifiedTime": "2026-08-22T00:00:00Z",
                "createdTime": "2026-06-01T00:00:00Z",
            },
            authority_check=lambda: {"production_sha": "prod"},
        )
        second_manifest = write_dry_run_evidence(
            self.plan(), self.root / "second", production_sha="prod", candidate_sha="candidate", database_path=self.db_path
        )
        second = apply_manifest(
            self.db_path,
            second_manifest,
            backup_path=backup_path,
            expected_production_sha="prod",
            expected_candidate_sha="candidate",
            backup_already_created=True,
            fresh_metadata_lookup=lambda drive_id: {
                "id": drive_id,
                "name": "One",
                "modifiedTime": "2026-08-22T00:00:00Z",
                "createdTime": "2026-06-01T00:00:00Z",
            },
            authority_check=lambda: {"production_sha": "prod"},
        )

        self.assertEqual(manifest["plan_sha256"], manifest["computed_plan_sha256"])
        self.assertEqual(rollback[0]["restore_source_modified_at"], None)
        self.assertEqual(backup["quick_check"], "ok")
        self.assertEqual(first["actual_updated"], 1)
        self.assertEqual(second["actual_updated"], 0)
        self.assertEqual(second["unchanged"], 1)

    def test_apply_revalidates_fresh_drive_metadata_and_rejects_stale_manifest(self):
        plan = self.plan()

        with self.assertRaisesRegex(ValueError, "Drive modifiedTime changed after dry run"):
            revalidate_plan_metadata(
                plan,
                lambda drive_id: {
                    "id": drive_id,
                    "name": "One",
                    "modifiedTime": "2026-08-22T01:00:00Z",
                    "createdTime": "2026-06-01T00:00:00Z",
                },
            )


if __name__ == "__main__":
    unittest.main()

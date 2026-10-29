import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_drive_created_time_backfill import build_backfill_plan
from scripts.backfill_google_drive_created_time import (
    MetadataOnlyDriveClient,
    apply_manifest,
    create_sqlite_backup,
    fetch_metadata_map,
    open_readonly_database,
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


class MetadataOnlyClientTests(unittest.TestCase):
    def test_metadata_request_asks_only_for_id_and_created_time(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"id": "drive-1", "createdTime": "2026-07-01T00:00:00Z"})

        client = MetadataOnlyDriveClient(
            {"access_token": "ephemeral-token", "token_expiry": "2099-01-01T00:00:00+00:00"},
            opener=opener,
        )

        result = client.get_created_time("drive-1")

        parsed = urlparse(requests[0][0].full_url)
        self.assertEqual(parsed.path, "/drive/v3/files/drive-1")
        self.assertEqual(parse_qs(parsed.query), {"fields": ["id,createdTime"], "supportsAllDrives": ["true"]})
        self.assertEqual(requests[0][0].get_method(), "GET")
        self.assertEqual(requests[0][0].headers["Authorization"], "Bearer ephemeral-token")
        self.assertEqual(result, {"id": "drive-1", "createdTime": "2026-07-01T00:00:00Z"})
        self.assertFalse(hasattr(client, "read_content"))

    def test_concurrent_fetch_keeps_per_file_errors_without_aborting_other_metadata(self):
        class Client:
            def get_created_time(self, drive_id):
                if drive_id == "missing":
                    raise RuntimeError("not found")
                return {"id": drive_id, "createdTime": "2026-07-01T00:00:00Z"}

        result = fetch_metadata_map(["one", "missing", "two"], Client(), max_workers=2)

        self.assertEqual(sorted(result["values"]), ["one", "two"])
        self.assertEqual(result["errors"], {"missing": "not found"})


class BackfillCliEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drive-created-time-cli-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "production.db"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE content_scripts(
              id INTEGER PRIMARY KEY, title TEXT, body TEXT, status TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE content_materials(
              id INTEGER PRIMARY KEY, script_id INTEGER, source_path TEXT,
              created_at TEXT, updated_at TEXT
            );
            INSERT INTO content_scripts VALUES(1,'One','Body','Draft','2026-08-01T10:00:00+10:00','2026-08-20T10:00:00+10:00');
            INSERT INTO content_materials VALUES(1,1,'gdrive://drive-one','2026-08-01T10:00:00+10:00','2026-08-01T10:00:00+10:00');
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
            return build_backfill_plan(
                conn,
                lambda drive_id: {"id": drive_id, "createdTime": "2026-06-01T00:00:00Z"},
                local_metadata={"values": {}, "conflicts": {}},
            )
        finally:
            conn.close()

    def test_readonly_connection_rejects_database_writes(self):
        conn = open_readonly_database(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE content_scripts SET title='changed' WHERE id=1")
        finally:
            conn.close()

    def test_dry_run_evidence_contains_full_snapshot_and_rollback_mapping(self):
        evidence = self.root / "evidence"

        manifest_path = write_dry_run_evidence(
            self.plan(),
            evidence,
            production_sha="production-sha",
            candidate_sha="candidate-sha",
            database_path=self.db_path,
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot = json.loads((evidence / "pre-backfill-rows.json").read_text(encoding="utf-8"))
        rollback = json.loads((evidence / "rollback-mapping.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["production_sha"], "production-sha")
        self.assertEqual(manifest["candidate_sha"], "candidate-sha")
        self.assertEqual(manifest["plan_sha256"], manifest["computed_plan_sha256"])
        self.assertEqual(snapshot[0]["record_id"], 1)
        self.assertEqual(snapshot[0]["source_key"], "gdrive://drive-one")
        self.assertEqual(snapshot[0]["current_created_at"], "2026-08-01T10:00:00+10:00")
        self.assertEqual(snapshot[0]["current_updated_at"], "2026-08-20T10:00:00+10:00")
        self.assertEqual(rollback[0]["restore_created_at"], "2026-08-01T10:00:00+10:00")
        self.assertTrue((evidence / "pre-backfill-rows.csv").is_file())
        self.assertTrue((evidence / "rollback-created-at.sql").is_file())

    def test_backup_is_valid_and_apply_requires_exact_manifest(self):
        evidence = self.root / "evidence"
        manifest_path = write_dry_run_evidence(
            self.plan(), evidence, production_sha="prod", candidate_sha="candidate", database_path=self.db_path
        )
        backup_path = self.root / "backup" / "production-before.sqlite3"

        backup = create_sqlite_backup(self.db_path, backup_path)
        result = apply_manifest(
            self.db_path,
            manifest_path,
            backup_path=backup_path,
            expected_production_sha="prod",
            expected_candidate_sha="candidate",
            backup_already_created=True,
        )

        self.assertEqual(backup["quick_check"], "ok")
        self.assertEqual(result["actual_updated"], 1)
        source = sqlite3.connect(self.db_path)
        backup_conn = sqlite3.connect(backup_path)
        try:
            self.assertEqual(source.execute("SELECT created_at FROM content_scripts WHERE id=1").fetchone()[0], "2026-06-01T00:00:00.000+00:00")
            self.assertEqual(backup_conn.execute("SELECT created_at FROM content_scripts WHERE id=1").fetchone()[0], "2026-08-01T10:00:00+10:00")
        finally:
            source.close()
            backup_conn.close()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["plan"]["proposals"][0]["canonical_created_at"] = "2020-01-01T00:00:00.000+00:00"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest plan hash mismatch"):
            apply_manifest(
                self.db_path,
                manifest_path,
                backup_path=self.root / "unused.sqlite3",
                expected_production_sha="prod",
                expected_candidate_sha="candidate",
            )


if __name__ == "__main__":
    unittest.main()

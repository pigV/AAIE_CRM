import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from google_drive_source_modified_backfill import (
    SourceModifiedBackfillConflict,
    apply_source_modified_plan,
    build_source_modified_plan,
    discover_source_modified_targets,
    load_local_modified_times,
    normalize_source_modified_time,
)


class GoogleDriveSourceModifiedBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drive-source-modified-")
        self.db_path = Path(self.temp.name) / "content.sqlite3"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE content_scripts(
              id INTEGER PRIMARY KEY,title TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE content_materials(
              id INTEGER PRIMARY KEY,script_id INTEGER REFERENCES content_scripts(id),
              source_path TEXT,source_url TEXT,file_hash TEXT,title TEXT,
              created_at TEXT,updated_at TEXT,source_modified_at TEXT
            );
            """
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def add_target(
        self,
        material_id,
        drive_id,
        *,
        script_id=None,
        created_at="2026-06-01T00:00:00.000+00:00",
        source_modified_at=None,
    ):
        script_id = material_id if script_id is None else script_id
        self.conn.execute(
            "INSERT OR IGNORE INTO content_scripts VALUES(?,?,?,?)",
            (script_id, f"Script {script_id}", created_at, "2026-08-20T10:00:00+10:00"),
        )
        self.conn.execute(
            """INSERT INTO content_materials(
                   id,script_id,source_path,source_url,file_hash,title,created_at,updated_at,source_modified_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                material_id,
                script_id,
                f"gdrive://{drive_id}",
                f"https://drive.invalid/{drive_id}",
                f"hash-{material_id}",
                f"Material {material_id}",
                "2026-08-01T10:00:00+10:00",
                "2026-08-20T10:00:00+10:00",
                source_modified_at,
            ),
        )
        self.conn.commit()

    def test_normalization_requires_real_timezone_aware_modified_time(self):
        self.assertEqual(
            normalize_source_modified_time("2026-08-21T23:45:01.123Z"),
            "2026-08-21T23:45:01.123+00:00",
        )
        for invalid in (None, "", "2026-08-21T23:45:01"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_source_modified_time(invalid)

    def test_discovery_uses_only_unique_exact_gdrive_source_paths(self):
        self.add_target(1, "drive-one")
        self.add_target(2, "drive-two")
        self.conn.execute(
            "INSERT INTO content_scripts VALUES(3,'Duplicate','2026-06-01T00:00:00.000+00:00','2026-08-20T10:00:00+10:00')"
        )
        self.conn.execute(
            """INSERT INTO content_materials(
                   id,script_id,source_path,source_url,file_hash,title,created_at,updated_at
               ) VALUES(99,3,'gdrive://drive-one','x','x','duplicate','x','x')"""
        )
        self.conn.commit()

        inventory = discover_source_modified_targets(self.conn)

        self.assertEqual(inventory["total_material_rows"], 3)
        self.assertEqual(inventory["target_total"], 1)
        self.assertEqual(inventory["targets"][0]["drive_file_id"], "drive-two")
        self.assertEqual(inventory["ambiguous"][0]["reason"], "duplicate_source_key")

    def test_local_preview_extracts_modified_time_without_created_time_fallback(self):
        path = Path(self.temp.name) / "preview.json"
        path.write_text(
            json.dumps({
                "items": [
                    {"drive_file_id": "one", "modified_time": "2026-08-20T01:02:03Z"},
                    {"id": "two", "modifiedTime": "2026-08-21T01:02:03.456Z"},
                    {"drive_file_id": "created-only", "createdTime": "2020-01-01T00:00:00Z"},
                ]
            }),
            encoding="utf-8",
        )

        local = load_local_modified_times([path])

        self.assertEqual(
            local["values"],
            {
                "one": "2026-08-20T01:02:03.000+00:00",
                "two": "2026-08-21T01:02:03.456+00:00",
            },
        )
        self.assertNotIn("created-only", local["values"])

    def test_local_preview_uses_latest_snapshot_when_modified_time_evolves(self):
        older = Path(self.temp.name) / "older.json"
        newer = Path(self.temp.name) / "newer.json"
        older.write_text(
            json.dumps({
                "created_at": "2026-08-10T00:00:00Z",
                "items": [{"drive_file_id": "one", "modified_time": "2026-08-09T05:05:16.119Z"}],
            }),
            encoding="utf-8",
        )
        newer.write_text(
            json.dumps({
                "created_at": "2026-08-21T00:00:00Z",
                "items": [{"drive_file_id": "one", "modified_time": "2026-08-09T23:24:08.264Z"}],
            }),
            encoding="utf-8",
        )

        local = load_local_modified_times([newer, older])

        self.assertEqual(local["conflicts"], {})
        self.assertEqual(local["values"]["one"], "2026-08-09T23:24:08.264+00:00")

    def test_freshness_api_supersedes_same_snapshot_local_conflict(self):
        self.add_target(1, "one")
        metadata = {
            "id": "one",
            "name": "One",
            "modifiedTime": "2026-08-21T00:00:00Z",
            "createdTime": "2026-06-01T00:00:00Z",
        }

        plan = build_source_modified_plan(
            self.conn,
            lambda _drive_id: metadata,
            local_metadata={"values": {}, "conflicts": {"one": ["old", "new"]}},
            require_freshness=True,
        )

        self.assertEqual(plan["ambiguous"], [])
        self.assertEqual(plan["accounting"]["freshness_revalidated"], 1)
        self.assertEqual(len(plan["proposals"]), 1)

    def test_freshness_plan_revalidates_every_exact_target_and_checks_created_time(self):
        self.add_target(1, "one")
        self.add_target(2, "two")
        calls = []

        def lookup(drive_id):
            calls.append(drive_id)
            return {
                "id": drive_id,
                "name": drive_id.title(),
                "modifiedTime": f"2026-08-2{drive_id == 'two' and 2 or 1}T00:00:00Z",
                "createdTime": "2026-06-01T00:00:00Z",
            }

        plan = build_source_modified_plan(
            self.conn,
            lookup,
            local_metadata={"values": {"one": "2020-01-01T00:00:00Z"}, "conflicts": {}},
            require_freshness=True,
        )

        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(plan["accounting"]["target_total"], 2)
        self.assertEqual(plan["accounting"]["exact_mappings"], 2)
        self.assertEqual(plan["accounting"]["freshness_revalidated"], 2)
        self.assertEqual(plan["accounting"]["changed_expected"], 2)
        self.assertEqual(plan["accounting"]["critical_errors"], 0)
        self.assertEqual(
            [item["canonical_source_modified_at"] for item in plan["proposals"]],
            ["2026-08-21T00:00:00.000+00:00", "2026-08-22T00:00:00.000+00:00"],
        )

    def test_missing_modified_time_and_created_time_mismatch_fail_closed(self):
        self.add_target(1, "missing")
        self.add_target(2, "created-mismatch")

        def lookup(drive_id):
            if drive_id == "missing":
                return {"id": drive_id, "modifiedTime": None, "createdTime": "2026-06-01T00:00:00Z"}
            return {
                "id": drive_id,
                "modifiedTime": "2026-08-22T00:00:00Z",
                "createdTime": "2025-01-01T00:00:00Z",
            }

        plan = build_source_modified_plan(
            self.conn, lookup, local_metadata={"values": {}, "conflicts": {}}, require_freshness=True
        )

        self.assertEqual(plan["proposals"], [])
        self.assertEqual(plan["accounting"]["missing_modified_time"], 1)
        self.assertEqual(plan["accounting"]["critical_errors"], 2)

    def test_apply_updates_only_source_modified_at_and_is_idempotent(self):
        self.add_target(1, "one")
        metadata = {
            "id": "one",
            "name": "One",
            "modifiedTime": "2026-08-22T01:02:03Z",
            "createdTime": "2026-06-01T00:00:00Z",
        }
        before = dict(self.conn.execute("SELECT * FROM content_materials WHERE id=1").fetchone())
        plan = build_source_modified_plan(
            self.conn, lambda _drive_id: metadata, local_metadata={"values": {}, "conflicts": {}}, require_freshness=True
        )

        first = apply_source_modified_plan(self.conn, plan)
        after = dict(self.conn.execute("SELECT * FROM content_materials WHERE id=1").fetchone())
        second_plan = build_source_modified_plan(
            self.conn, lambda _drive_id: metadata, local_metadata={"values": {}, "conflicts": {}}, require_freshness=True
        )
        second = apply_source_modified_plan(self.conn, second_plan)

        self.assertEqual(first["actual_updated"], 1)
        self.assertEqual(after["source_modified_at"], "2026-08-22T01:02:03.000+00:00")
        self.assertEqual(
            {key: value for key, value in after.items() if key != "source_modified_at"},
            {key: value for key, value in before.items() if key != "source_modified_at"},
        )
        self.assertEqual(second_plan["accounting"]["changed_expected"], 0)
        self.assertEqual(second_plan["accounting"]["unchanged_expected"], 1)
        self.assertEqual(second["actual_updated"], 0)

    def test_apply_rolls_back_all_rows_when_protected_preimage_changes(self):
        self.add_target(1, "one")
        self.add_target(2, "two")

        def lookup(drive_id):
            return {
                "id": drive_id,
                "modifiedTime": "2026-08-22T00:00:00Z",
                "createdTime": "2026-06-01T00:00:00Z",
                "name": drive_id,
            }

        plan = build_source_modified_plan(
            self.conn, lookup, local_metadata={"values": {}, "conflicts": {}}, require_freshness=True
        )
        self.conn.execute("UPDATE content_materials SET title='changed after dry run' WHERE id=2")
        self.conn.commit()

        with self.assertRaises(SourceModifiedBackfillConflict):
            apply_source_modified_plan(self.conn, plan)

        values = [row[0] for row in self.conn.execute("SELECT source_modified_at FROM content_materials ORDER BY id")]
        self.assertEqual(values, [None, None])

    def test_apply_rejects_manifest_missing_a_target_proposal(self):
        self.add_target(1, "one")
        self.add_target(2, "two")
        plan = build_source_modified_plan(
            self.conn,
            lambda drive_id: {
                "id": drive_id,
                "modifiedTime": "2026-08-22T00:00:00Z",
                "createdTime": "2026-06-01T00:00:00Z",
            },
            local_metadata={"values": {}, "conflicts": {}},
            require_freshness=True,
        )
        plan["proposals"].pop()

        with self.assertRaisesRegex(SourceModifiedBackfillConflict, "proposal coverage"):
            apply_source_modified_plan(self.conn, plan)

        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT source_modified_at FROM content_materials ORDER BY id")],
            [None, None],
        )


if __name__ == "__main__":
    unittest.main()

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from google_drive_created_time_backfill import (
    BackfillConflictError,
    apply_backfill_plan,
    build_backfill_plan,
    discover_drive_targets,
    load_local_created_times,
)


class GoogleDriveCreatedTimeBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drive-created-time-")
        self.db_path = Path(self.temp.name) / "content.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE content_scripts(
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT,
              status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE content_materials(
              id INTEGER PRIMARY KEY, script_id INTEGER REFERENCES content_scripts(id),
              source_path TEXT, source_url TEXT, created_at TEXT, updated_at TEXT
            );
            """
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def add_target(self, script_id, drive_id, *, created_at="2026-08-01T10:00:00+10:00"):
        self.conn.execute(
            "INSERT INTO content_scripts(id,title,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (script_id, f"Script {script_id}", "Body", "Draft", created_at, "2026-08-20T12:00:00+10:00"),
        )
        self.conn.execute(
            "INSERT INTO content_materials(id,script_id,source_path,source_url,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (script_id, script_id, f"gdrive://{drive_id}", f"https://drive.invalid/{drive_id}", created_at, created_at),
        )
        self.conn.commit()

    def test_discovery_requires_one_to_one_existing_drive_mapping(self):
        self.add_target(1, "drive-one")
        self.add_target(2, "drive-two")
        self.conn.execute(
            "INSERT INTO content_materials(id,script_id,source_path) VALUES(?,?,?)",
            (99, 2, "gdrive://drive-one"),
        )
        self.conn.commit()

        inventory = discover_drive_targets(self.conn)

        self.assertEqual(inventory["total_material_rows"], 3)
        self.assertEqual(inventory["target_total"], 0)
        self.assertEqual(
            {item["reason"] for item in inventory["ambiguous"]},
            {"drive_id_maps_to_multiple_scripts", "script_maps_to_multiple_drive_ids"},
        )

    def test_local_reports_extract_only_exact_id_created_time_and_flag_conflicts(self):
        first = Path(self.temp.name) / "first.json"
        first.write_text(
            json.dumps({
                "processed_files": [
                    {"id": "local-one", "created_time": "2026-07-01T01:02:03.456Z"},
                    {"drive_file_id": "local-two", "createdTime": "2026-07-02T01:02:03Z"},
                    {"name": "no-id", "created_time": "2026-07-03T01:02:03Z"},
                ]
            }),
            encoding="utf-8",
        )
        second = Path(self.temp.name) / "second.json"
        second.write_text(
            json.dumps({"items": [{"id": "local-two", "created_time": "2026-07-04T01:02:03Z"}]}),
            encoding="utf-8",
        )

        local = load_local_created_times([first, second])

        self.assertEqual(local["values"], {"local-one": "2026-07-01T01:02:03.456+00:00"})
        self.assertEqual(list(local["conflicts"]), ["local-two"])

    def test_plan_uses_local_then_api_and_accounts_for_unrecoverable_without_guessing(self):
        self.add_target(1, "local")
        self.add_target(2, "api")
        self.add_target(3, "missing")
        calls = []

        def lookup(drive_id):
            calls.append(drive_id)
            if drive_id == "api":
                return {"id": "api", "createdTime": "2026-07-02T03:04:05.678Z"}
            raise RuntimeError("not found")

        plan = build_backfill_plan(
            self.conn,
            lookup,
            local_metadata={"values": {"local": "2026-07-01T01:02:03+00:00"}, "conflicts": {}},
        )

        self.assertEqual(calls, ["api", "missing"])
        self.assertEqual(plan["accounting"]["target_total"], 3)
        self.assertEqual(plan["accounting"]["local_recoverable"], 1)
        self.assertEqual(plan["accounting"]["api_recoverable"], 1)
        self.assertEqual(plan["accounting"]["unrecoverable"], 1)
        self.assertEqual(plan["accounting"]["changed_expected"], 2)
        self.assertEqual(plan["accounting"]["unchanged_expected"], 0)
        self.assertEqual(plan["unrecoverable"][0]["drive_file_id"], "missing")
        self.assertEqual(
            {proposal["canonical_created_at"] for proposal in plan["proposals"]},
            {"2026-07-01T01:02:03.000+00:00", "2026-07-02T03:04:05.678+00:00"},
        )

    def test_mismatched_or_missing_created_time_is_unrecoverable(self):
        self.add_target(1, "mismatch")
        self.add_target(2, "missing-time")

        def lookup(drive_id):
            if drive_id == "mismatch":
                return {"id": "different", "createdTime": "2026-01-01T00:00:00Z"}
            return {"id": drive_id, "createdTime": None}

        plan = build_backfill_plan(self.conn, lookup, local_metadata={"values": {}, "conflicts": {}})

        self.assertEqual(plan["accounting"]["unrecoverable"], 2)
        self.assertEqual(plan["proposals"], [])

    def test_apply_updates_only_created_at_and_second_run_is_noop(self):
        self.add_target(1, "one")
        before = dict(self.conn.execute("SELECT * FROM content_scripts WHERE id=1").fetchone())
        plan = build_backfill_plan(
            self.conn,
            lambda drive_id: {"id": drive_id, "createdTime": "2026-06-01T00:00:00Z"},
            local_metadata={"values": {}, "conflicts": {}},
        )

        applied = apply_backfill_plan(self.conn, plan)
        after = dict(self.conn.execute("SELECT * FROM content_scripts WHERE id=1").fetchone())
        second = build_backfill_plan(
            self.conn,
            lambda drive_id: {"id": drive_id, "createdTime": "2026-06-01T00:00:00Z"},
            local_metadata={"values": {}, "conflicts": {}},
        )
        second_applied = apply_backfill_plan(self.conn, second)

        self.assertEqual(applied["actual_updated"], 1)
        self.assertEqual(after["created_at"], "2026-06-01T00:00:00.000+00:00")
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual({key: value for key, value in after.items() if key != "created_at"}, {key: value for key, value in before.items() if key != "created_at"})
        self.assertEqual(second["accounting"]["changed_expected"], 0)
        self.assertEqual(second["accounting"]["unchanged_expected"], 1)
        self.assertEqual(second_applied["actual_updated"], 0)

    def test_apply_fails_closed_and_rolls_back_when_preimage_changed(self):
        self.add_target(1, "one")
        self.add_target(2, "two")
        plan = build_backfill_plan(
            self.conn,
            lambda drive_id: {"id": drive_id, "createdTime": f"2026-06-0{drive_id == 'two' and 2 or 1}T00:00:00Z"},
            local_metadata={"values": {}, "conflicts": {}},
        )
        self.conn.execute("UPDATE content_scripts SET updated_at='changed-after-preview' WHERE id=2")
        self.conn.commit()

        with self.assertRaises(BackfillConflictError):
            apply_backfill_plan(self.conn, plan)

        rows = self.conn.execute("SELECT id,created_at FROM content_scripts ORDER BY id").fetchall()
        self.assertEqual([row["created_at"] for row in rows], ["2026-08-01T10:00:00+10:00"] * 2)


if __name__ == "__main__":
    unittest.main()

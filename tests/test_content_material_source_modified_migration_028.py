import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_runner import discover_migrations


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
MIGRATION = MIGRATIONS / "20260822_028_content_material_source_modified_at.sql"
FORWARD_CHECK = (
    MIGRATIONS
    / "checks"
    / "20260822_028_content_material_source_modified_at_forward_check.sql"
)


class ContentMaterialSourceModifiedMigration028Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-modified-028-")
        self.database_path = Path(self.temp.name) / "fixture.sqlite3"
        self.conn = sqlite3.connect(self.database_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE content_scripts(id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE content_materials(
              id INTEGER PRIMARY KEY,
              script_id INTEGER REFERENCES content_scripts(id),
              source_path TEXT,
              updated_at TEXT
            );
            INSERT INTO content_scripts(id,title) VALUES(1,'Existing');
            INSERT INTO content_materials(id,script_id,source_path,updated_at)
            VALUES(1,1,'gdrive://drive-one','2026-08-20T10:00:00+10:00');
            """
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_migration_adds_nullable_source_modified_timestamp_and_query_indexes(self):
        before = dict(self.conn.execute("SELECT * FROM content_materials WHERE id=1").fetchone())

        self.conn.executescript(MIGRATION.read_text(encoding="utf-8"))

        columns = {row["name"]: dict(row) for row in self.conn.execute("PRAGMA table_info(content_materials)")}
        self.assertEqual(columns["source_modified_at"]["type"].upper(), "TEXT")
        self.assertEqual(columns["source_modified_at"]["notnull"], 0)
        self.assertIsNone(columns["source_modified_at"]["dflt_value"])
        after = dict(self.conn.execute("SELECT * FROM content_materials WHERE id=1").fetchone())
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertIsNone(after["source_modified_at"])

        index_rows = [
            dict(row)
            for row in self.conn.execute("PRAGMA index_xinfo(idx_content_materials_source_modified_at)")
            if row["key"]
        ]
        self.assertEqual(
            [(row["name"], row["desc"]) for row in index_rows],
            [("source_modified_at", 1), ("id", 1)],
        )
        projection_index = [
            row["name"]
            for row in self.conn.execute("PRAGMA index_xinfo(idx_content_materials_script_id_id)")
            if row["key"]
        ]
        self.assertEqual(projection_index, ["script_id", "id"])

    def test_forward_check_and_discovery_contract(self):
        self.conn.executescript(MIGRATION.read_text(encoding="utf-8"))

        result = self.conn.execute(FORWARD_CHECK.read_text(encoding="utf-8")).fetchone()[0]
        discovered = [item for item in discover_migrations(MIGRATIONS) if item.version == "20260822_028"]

        self.assertEqual(result, 1)
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].name, "content_material_source_modified_at")
        self.assertEqual(len(discovered[0].checksum_sha256), 64)


if __name__ == "__main__":
    unittest.main()

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.revenue_migration_rehearsal import ROOT, schema_objects


class RevenueMigrationRehearsalTests(unittest.TestCase):
    def test_core_fingerprint_excludes_indexes_owned_by_revenue_tables(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "database") as directory:
            path = Path(directory) / "fingerprint.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE legacy_fact(id INTEGER PRIMARY KEY);
                    CREATE TABLE revenue_fact(id INTEGER PRIMARY KEY, code TEXT);
                    CREATE UNIQUE INDEX uq_revenue_fact_code
                    ON revenue_fact(code);
                    """
                )
                connection.commit()
                objects = schema_objects(connection, include_revenue=False)

        self.assertEqual(
            [("table", "legacy_fact", "legacy_fact", "CREATE TABLE legacy_fact(id INTEGER PRIMARY KEY)")],
            objects,
        )


if __name__ == "__main__":
    unittest.main()

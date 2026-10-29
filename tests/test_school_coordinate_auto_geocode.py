import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import auto_geocode_school_coordinates as auto_geocode


class SchoolCoordinateAutoGeocodeTests(unittest.TestCase):
    def make_db(self):
        folder = tempfile.TemporaryDirectory()
        path = Path(folder.name) / "aaie_crm.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE schools (
              id INTEGER PRIMARY KEY,
              school_name TEXT NOT NULL,
              state TEXT,
              suburb TEXT,
              postcode TEXT,
              address TEXT,
              latitude REAL,
              longitude REAL,
              geocode_status TEXT,
              geocoded_at TEXT,
              created_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE school_geocode_cache (
              id INTEGER PRIMARY KEY,
              raw_input_text TEXT,
              normalized_query TEXT,
              input_type TEXT,
              state TEXT,
              formatted_address TEXT,
              suburb TEXT,
              postcode TEXT,
              latitude REAL,
              longitude REAL,
              geocode_source TEXT,
              geocoded_at TEXT,
              geocode_status TEXT,
              candidates_json TEXT,
              warning_message TEXT,
              created_at TEXT,
              updated_at TEXT,
              UNIQUE(normalized_query,state,input_type)
            );
            """
        )
        stamp = "2026-06-28T23:58:00+10:00"
        conn.execute(
            "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (1, "Failed School", "NSW", "Burwood", "2134", "Unknown Address", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (2, "Next School", "NSW", "Burwood", "2134", "Rowley Street, Burwood NSW 2134", stamp, stamp),
        )
        conn.commit()
        conn.close()
        return folder, path

    def test_auto_run_records_failures_and_keeps_progressing(self):
        folder, db_path = self.make_db()
        old_fetch = getattr(auto_geocode.geocoder, "fetch_nominatim_geocode", None)
        try:
            root = Path(folder.name)
            state_path = root / "state.json"
            log_path = root / "run.log"
            error_path = root / "errors.jsonl"
            backup_dir = root / "backups"
            calls = []

            def fake_fetch(conn_arg, row_arg):
                calls.append(row_arg["id"])
                if row_arg["id"] == 1:
                    return None, "NO_RESULTS"
                stamp = "2026-06-28T23:59:00+10:00"
                conn_arg.execute(
                    """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                       latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("Rowley Street, Burwood NSW 2134", auto_geocode.geocoder.normalize_query("Rowley Street, Burwood NSW 2134"), "address", "NSW", "Rowley Street, Burwood NSW 2134", "Burwood", "2134", -33.8742, 151.1048, "OpenStreetMap Nominatim", stamp, "Resolved", "[]", stamp, stamp),
                )
                return auto_geocode.geocoder.cache_lookup(conn_arg, row_arg), ""

            auto_geocode.geocoder.fetch_nominatim_geocode = fake_fetch
            summary = auto_geocode.run_auto(
                db_path=db_path,
                states=("NSW",),
                batch_size=2,
                delay=0,
                provider="osm_nominatim",
                apply=True,
                state_path=state_path,
                log_path=log_path,
                error_path=error_path,
                backup_dir=backup_dir,
            )
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            updated = conn.execute("SELECT latitude,longitude FROM schools WHERE id=2").fetchone()
            conn.close()
            saved_state = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(calls, [1, 2])
            self.assertEqual(summary["total_updated"], 1)
            self.assertEqual(updated["latitude"], -33.8742)
            self.assertEqual(saved_state["failed_school_ids"], [1])
            self.assertTrue(any(backup_dir.glob("aaie_crm_before_school_geocode_auto_*.db")))
            self.assertIn('"school_id": 1', error_path.read_text(encoding="utf-8"))
        finally:
            folder.cleanup()
            if old_fetch is None:
                delattr(auto_geocode.geocoder, "fetch_nominatim_geocode")
            else:
                auto_geocode.geocoder.fetch_nominatim_geocode = old_fetch


if __name__ == "__main__":
    unittest.main()

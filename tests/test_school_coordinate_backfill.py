import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import geocode_school_coordinates as geocoder


class SchoolCoordinateBackfillTests(unittest.TestCase):
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
        return folder, conn

    def test_dry_run_reports_cache_matches_without_writing_school_coordinates(self):
        folder, conn = self.make_db()
        try:
            stamp = "2026-06-28T23:20:00+10:00"
            conn.execute(
                "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (1, "MLC School", "NSW", "Burwood", "2134", "Rowley Street, Burwood NSW 2134", stamp, stamp),
            )
            conn.execute(
                """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                   latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Rowley Street, Burwood NSW 2134", geocoder.normalize_query("Rowley Street, Burwood NSW 2134"), "address", "NSW", "Rowley Street, Burwood NSW 2134", "Burwood", "2134", -33.8742, 151.1048, "test", stamp, "Resolved", "[]", stamp, stamp),
            )
            conn.commit()
            result = geocoder.run_backfill(conn, apply=False, state="NSW", limit=0)
            row = conn.execute("SELECT latitude,longitude,geocode_status FROM schools WHERE id=1").fetchone()

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["cache_hits"], 1)
            self.assertEqual(result["updated"], 0)
            self.assertIsNone(row["latitude"])
            self.assertIsNone(row["longitude"])
            self.assertIsNone(row["geocode_status"])
        finally:
            conn.close()
            folder.cleanup()

    def test_apply_does_not_write_suburb_fallback_as_exact_school_coordinates(self):
        folder, conn = self.make_db()
        try:
            stamp = "2026-06-28T23:25:00+10:00"
            conn.execute(
                "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (1, "MLC School", "NSW", "Burwood", "2134", "Rowley Street, Burwood NSW 2134", stamp, stamp),
            )
            conn.execute(
                """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                   latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Burwood", geocoder.normalize_query("Burwood"), "suburb", "NSW", "Burwood suburb centre NSW", "Burwood", "2134", -33.8774, 151.1030, "test", stamp, "Resolved", "[]", stamp, stamp),
            )
            conn.commit()
            result = geocoder.run_backfill(conn, apply=True, state="NSW", limit=0)
            row = conn.execute("SELECT latitude,longitude,geocode_status FROM schools WHERE id=1").fetchone()

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["cache_hits"], 0)
            self.assertEqual(result["suburb_fallback_hits"], 1)
            self.assertEqual(result["updated"], 0)
            self.assertIsNone(row["latitude"])
            self.assertIsNone(row["longitude"])
            self.assertIsNone(row["geocode_status"])
        finally:
            conn.close()
            folder.cleanup()

    def test_backfill_uses_nominatim_provider_without_google_key(self):
        folder, conn = self.make_db()
        old_fetch = getattr(geocoder, "fetch_nominatim_geocode", None)
        try:
            stamp = "2026-06-28T23:40:00+10:00"
            conn.execute(
                "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (1, "MLC School", "NSW", "Burwood", "2134", "Rowley Street, Burwood NSW 2134", stamp, stamp),
            )
            conn.commit()
            calls = []

            def fake_fetch(conn_arg, row_arg):
                calls.append(row_arg["school_name"])
                conn_arg.execute(
                    """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                       latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("Rowley Street, Burwood NSW 2134", geocoder.normalize_query("Rowley Street, Burwood NSW 2134"), "address", "NSW", "Rowley Street, Burwood NSW 2134", "Burwood", "2134", -33.8742, 151.1048, "OpenStreetMap Nominatim", stamp, "Resolved", "[]", stamp, stamp),
                )
                return geocoder.cache_lookup(conn_arg, row_arg), ""

            geocoder.fetch_nominatim_geocode = fake_fetch
            result = geocoder.run_backfill(conn, apply=False, state="NSW", limit=0, geocode=True, provider="osm_nominatim", delay=0)

            self.assertEqual(calls, ["MLC School"])
            self.assertEqual(result["api_attempts"], 1)
            self.assertEqual(result["api_hits"], 1)
            self.assertEqual(result["skipped_no_api_key"], 0)
        finally:
            conn.close()
            folder.cleanup()
            if old_fetch is None:
                delattr(geocoder, "fetch_nominatim_geocode")
            else:
                geocoder.fetch_nominatim_geocode = old_fetch

    def test_backfill_geocodes_exact_address_even_when_suburb_fallback_exists(self):
        folder, conn = self.make_db()
        old_fetch = getattr(geocoder, "fetch_nominatim_geocode", None)
        try:
            stamp = "2026-06-28T23:50:00+10:00"
            conn.execute(
                "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (1, "MLC School", "NSW", "Burwood", "2134", "Rowley Street, Burwood NSW 2134", stamp, stamp),
            )
            conn.execute(
                """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                   latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Burwood", geocoder.normalize_query("Burwood"), "suburb", "NSW", "Burwood suburb centre NSW", "Burwood", "2134", -33.8774, 151.1030, "test", stamp, "Resolved", "[]", stamp, stamp),
            )
            conn.commit()
            calls = []

            def fake_fetch(conn_arg, row_arg):
                calls.append(row_arg["school_name"])
                conn_arg.execute(
                    """INSERT INTO school_geocode_cache(raw_input_text,normalized_query,input_type,state,formatted_address,suburb,postcode,
                       latitude,longitude,geocode_source,geocoded_at,geocode_status,candidates_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("Rowley Street, Burwood NSW 2134", geocoder.normalize_query("Rowley Street, Burwood NSW 2134"), "address", "NSW", "Rowley Street, Burwood NSW 2134", "Burwood", "2134", -33.8742, 151.1048, "OpenStreetMap Nominatim", stamp, "Resolved", "[]", stamp, stamp),
                )
                return geocoder.cache_lookup(conn_arg, row_arg), ""

            geocoder.fetch_nominatim_geocode = fake_fetch
            result = geocoder.run_backfill(conn, apply=False, state="NSW", limit=0, geocode=True, provider="osm_nominatim", delay=0)

            self.assertEqual(calls, ["MLC School"])
            self.assertEqual(result["suburb_fallback_hits"], 1)
            self.assertEqual(result["api_attempts"], 1)
            self.assertEqual(result["api_hits"], 1)
        finally:
            conn.close()
            folder.cleanup()
            if old_fetch is None:
                delattr(geocoder, "fetch_nominatim_geocode")
            else:
                geocoder.fetch_nominatim_geocode = old_fetch

    def test_backfill_can_skip_failed_school_ids(self):
        folder, conn = self.make_db()
        try:
            stamp = "2026-06-28T23:55:00+10:00"
            conn.execute(
                "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (1, "Failed School", "NSW", "Burwood", "2134", "Unknown Address", stamp, stamp),
            )
            conn.execute(
                "INSERT INTO schools(id,school_name,state,suburb,postcode,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (2, "Next School", "NSW", "Burwood", "2134", "Rowley Street, Burwood NSW 2134", stamp, stamp),
            )
            conn.commit()

            rows = geocoder.missing_school_rows(conn, state="NSW", limit=0, exclude_school_ids={1})
            result = geocoder.run_backfill(conn, apply=False, state="NSW", limit=0, exclude_school_ids={1})

            self.assertEqual([row["id"] for row in rows], [2])
            self.assertEqual(result["checked"], 1)
        finally:
            conn.close()
            folder.cleanup()


if __name__ == "__main__":
    unittest.main()

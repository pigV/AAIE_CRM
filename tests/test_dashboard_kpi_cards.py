import sqlite3
import unittest
from datetime import date

from dashboard_kpi_cards import load_dashboard_kpis


class DashboardKpiCardsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students (
              student_id TEXT PRIMARY KEY,
              deleted_at TEXT
            );
            CREATE TABLE workflow_status (
              student_id TEXT PRIMARY KEY,
              current_stage TEXT
            );
            CREATE TABLE contracts (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              contract_status TEXT,
              fully_executed_at TEXT,
              deleted_at TEXT,
              voided_at TEXT
            );
            CREATE TABLE visas (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              visa_status TEXT,
              visa_grant_date TEXT
            );
            CREATE TABLE offers (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              document_type TEXT,
              offer_date TEXT,
              uploaded_at TEXT
            );
            """
        )

    def tearDown(self):
        self.connection.close()

    def load(self, today=date(2026, 8, 7)):
        return load_dashboard_kpis(
            self.connection,
            today,
            final_workflow_stage="签证下签或拒签",
            workflow_stage_aliases={"Visa granted": "签证下签或拒签"},
        )

    def test_counts_confirmed_metrics_with_week_and_month_boundaries(self):
        self.connection.executemany(
            "INSERT INTO students(student_id,deleted_at) VALUES(?,?)",
            [("A", None), ("B", None), ("C", None), ("D", "2026-08-06")],
        )
        self.connection.executemany(
            "INSERT INTO workflow_status(student_id,current_stage) VALUES(?,?)",
            [("A", "建档"), ("B", "Visa granted"), ("D", "建档")],
        )
        self.connection.executemany(
            """INSERT INTO contracts(
                 student_id,contract_status,fully_executed_at,deleted_at,voided_at
               ) VALUES(?,?,?,?,?)""",
            [
                ("A", "FullyExecuted", "2026-08-03T09:00:00+10:00", None, None),
                ("A", "FullyExecuted", "2026-08-04T09:00:00+10:00", None, None),
                ("B", "FullyExecuted", "2026-08-07", None, None),
                ("C", "FullyExecuted", "2026-08-01", None, None),
                ("C", "ParentSigned", "2026-08-05", None, None),
                ("D", "FullyExecuted", "2026-08-05", "2026-08-06", None),
                ("D", "FullyExecuted", "2026-08-05", None, "2026-08-06"),
            ],
        )
        self.connection.executemany(
            "INSERT INTO visas(student_id,visa_status,visa_grant_date) VALUES(?,?,?)",
            [
                ("A", "granted", "2026-08-03"),
                ("A", "Granted", "2026-08-04"),
                ("B", "granted", "2026-08-07"),
                ("C", "granted", "2026-08-01"),
                ("D", "active", "2026-08-05"),
            ],
        )
        self.connection.executemany(
            """INSERT INTO offers(
                 student_id,document_type,offer_date,uploaded_at
               ) VALUES(?,?,?,?)""",
            [
                ("A", "Offer", "2026-07-20", "2026-08-03T09:00:00+10:00"),
                ("A", "Offer", "2026-07-21", "2026-08-07T15:00:00+10:00"),
                ("A", "SignedOffer", "2026-08-05", "2026-08-05T10:00:00+10:00"),
                ("B", "OfferAcceptance", "2026-08-05", "2026-08-05T11:00:00+10:00"),
                ("C", "Offer", "2026-08-05", "2026-08-02T23:59:59+10:00"),
                ("C", "Offer", "2026-08-05", "2026-08-10T00:00:00+10:00"),
            ],
        )

        result = self.load()

        self.assertEqual(result.in_progress_students, 2)
        self.assertEqual(result.signed_this_week, 2)
        self.assertEqual(result.signed_this_month, 3)
        self.assertEqual(result.visa_granted_this_week, 2)
        self.assertEqual(result.visa_granted_this_month, 3)
        self.assertEqual(result.offers_uploaded_this_week, 2)

    def test_uses_melbourne_calendar_boundaries_with_exclusive_ends(self):
        result = self.load(date(2026, 12, 31))

        self.assertEqual(result.week_start, date(2026, 12, 28))
        self.assertEqual(result.week_end, date(2027, 1, 4))
        self.assertEqual(result.month_start, date(2026, 12, 1))
        self.assertEqual(result.next_month, date(2027, 1, 1))

    def test_keeps_offset_timestamps_on_their_recorded_melbourne_date(self):
        self.connection.execute(
            "INSERT INTO students(student_id,deleted_at) VALUES(?,?)",
            ("A", None),
        )
        self.connection.execute(
            """INSERT INTO contracts(
                 student_id,contract_status,fully_executed_at,deleted_at,voided_at
               ) VALUES(?,?,?,?,?)""",
            ("A", "FullyExecuted", "2026-08-03T00:30:00+10:00", None, None),
        )
        self.connection.execute(
            "INSERT INTO visas(student_id,visa_status,visa_grant_date) VALUES(?,?,?)",
            ("A", "granted", "2026-08-03T00:30:00+10:00"),
        )
        self.connection.execute(
            """INSERT INTO offers(
                 student_id,document_type,offer_date,uploaded_at
               ) VALUES(?,?,?,?)""",
            ("A", "Offer", "2026-08-02", "2026-08-03T00:30:00+10:00"),
        )

        result = self.load()

        self.assertEqual(result.signed_this_week, 1)
        self.assertEqual(result.visa_granted_this_week, 1)
        self.assertEqual(result.offers_uploaded_this_week, 1)


if __name__ == "__main__":
    unittest.main()

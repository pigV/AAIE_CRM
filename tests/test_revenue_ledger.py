from __future__ import annotations

import sqlite3
import unittest

from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueLedgerTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_ledger_is_append_only_and_each_source_event_balances(self):
        with self.connect() as connection:
            service = RevenueService(RevenueRepository(connection))
            service.append_balanced_ledger_event(
                "adjustment", "manual_adjustment", "adjustment-1", 5_000,
                "Expense", "Payable", 1, commercial_case_id=None,
            )
            rows = connection.execute(
                "SELECT uid,debit_cents,credit_cents FROM revenue_ledger_entries WHERE source_uid='adjustment-1'"
            ).fetchall()
            self.assertEqual(2, len(rows))
            self.assertEqual(sum(row[1] for row in rows), sum(row[2] for row in rows))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE revenue_ledger_entries SET debit_cents=1 WHERE uid=?", (rows[0][0],)
                )


if __name__ == "__main__":
    unittest.main()

from datetime import date
from types import SimpleNamespace
import unittest

from revenue.repository import RevenueRepository
from tests.revenue_test_support import RevenueDatabaseFixture


class AuthoritySettlementTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_partial_wrong_state_duplicate_and_reconciliation(self):
        from revenue.authority_settlement import AuthoritySettlement

        repo = RevenueRepository(self.connect())
        invoice = {
            "uid": "vic-original", "target": ["authority", "VIC"], "simulation": True,
            "terms_revision": "original-rule", "gross_cents": 176000,
            "lines": [{"uid": "mel", "gross_cents": 80000}, {"uid": "gee", "gross_cents": 96000}],
        }
        service = AuthoritySettlement(repo, invoice, 1, rehearsal=True)
        proof = SimpleNamespace(original_filename="TEST-bank.pdf", sha256="a" * 64,
                                size_bytes=128, path=self.database_path.with_name("TEST-bank.pdf"))
        day = date.today().isoformat()
        with self.assertRaises(PermissionError):
            AuthoritySettlement(repo, invoice, 1, rehearsal=False)
        with self.assertRaises(PermissionError):
            AuthoritySettlement(repo, {**invoice, "target": ["school", 16]}, 1, rehearsal=True)
        with self.assertRaisesRegex(ValueError, "付款方"):
            service.record("NSW", "test-nsw", day, 100000, {"mel": 80000, "gee": 20000}, proof)
        first = service.record("VIC", "test-vic-a", day, 100000, {"mel": 80000, "gee": 20000}, proof)
        self.assertEqual(first, service.record("VIC", "TEST-VIC-A", day, 100000,
                                               {"mel": 80000, "gee": 20000}, proof))
        self.assertEqual(76000, service.summary()["outstanding_cents"])
        with self.assertRaisesRegex(ValueError, "收齐"):
            service.reconcile(True)
        with self.assertRaises(ValueError):
            service.record("VIC", "TEST-VIC-A", day, 76000, {"gee": 76000}, proof)
        service.record("VIC", "TEST-VIC-B", day, 76000, {"gee": 76000}, proof)
        record = service.reconcile(True)
        self.assertEqual(record, service.reconcile(True))
        self.assertEqual("reconciled", service.summary()["status"])
        self.assertEqual(176000, invoice["gross_cents"])
        self.assertEqual(2, len(service.summary()["payments"]))

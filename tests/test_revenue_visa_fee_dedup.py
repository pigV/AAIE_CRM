from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

from tests import test_revenue_controlled_onboarding as onboarding_fixture
from revenue.controlled_onboarding import ControlledOnboardingService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository
from revenue.services import RevenueService


class VisaFeeDedupTests(onboarding_fixture.RevenueControlledOnboardingTests):
    def prepare_visa(self, db):
        db.executescript("""
            ALTER TABLE visa_cases ADD COLUMN student_id TEXT;
            ALTER TABLE visa_cases ADD COLUMN status TEXT;
            ALTER TABLE visa_cases ADD COLUMN updated_at TEXT;
            UPDATE visa_cases SET student_id='S-REVENUE',status='Lodged',
                updated_at='2026-11-16T09:00:00+11:00' WHERE id=230;
        """)

    def import_fees(self, db):
        return ControlledOnboardingService(RevenueRepository(db)).apply(
            self._preview(db), actor_user_id=1, actor_role="Admin", idempotency_key="visa-fee-import"
        )

    def daily(self, db):
        return RevenueOperationalOrchestrator(db, 1, "Admin").visa_case_changed(230)

    def records(self, db):
        return [dict(row) for row in db.execute(
            "SELECT * FROM revenue_fee_items WHERE milestone_code='guardian-590' ORDER BY id"
        )]

    def test_import_then_daily_reuses_original_fee(self):
        with self.connect() as db:
            self.prepare_visa(db)
            self.import_fees(db)
            first, = self.records(db)
            self.assertEqual("EXISTING", self.daily(db)["fee_status"])
            self.assertEqual("EXISTING", self.daily(db)["fee_status"])
            self.assertEqual([first], self.records(db))

    def test_daily_then_import_reuses_original_fee_and_creates_base(self):
        with self.connect() as db:
            self.prepare_visa(db)
            self.daily(db)
            first, = self.records(db)
            self.import_fees(db)
            self.assertEqual([first], self.records(db))
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM revenue_fee_items WHERE commercial_case_id=? AND milestone_code='k12-new'",
                (first["commercial_case_id"],)
            ).fetchone()[0])

    def test_conflicting_amount_is_held(self):
        with self.connect() as db:
            self.prepare_visa(db)
            self.import_fees(db)
            db.execute("UPDATE revenue_fee_items SET net_cents=80000,gross_cents=80000 WHERE milestone_code='guardian-590'")
            self.assertEqual("NEEDS_ATTENTION", self.daily(db)["fee_status"])
            self.assertEqual([80000], [r["net_cents"] for r in self.records(db)])

    def test_cancellation_keeps_earlier_fee_and_is_audited(self):
        with self.connect() as db:
            self.prepare_visa(db)
            self.import_fees(db)
            first, = self.records(db)
            cursor = db.execute("""
                INSERT INTO revenue_fee_items(uid,commercial_case_id,billing_account_id,student_id,
                  rule_snapshot_id,milestone_code,description,net_cents,gst_cents,gross_cents,created_at)
                SELECT 'duplicate',commercial_case_id,billing_account_id,student_id,rule_snapshot_id,
                  milestone_code,description,net_cents,gst_cents,gross_cents,created_at
                FROM revenue_fee_items WHERE id=?
            """, (first["id"],))
            service = RevenueService(RevenueRepository(db))
            self.assertEqual("NEEDS_ATTENTION", self.daily(db)["fee_status"])
            result = service.cancel_duplicate_visa_fee(
                first["id"], cursor.lastrowid, 230, 1, "Admin", "rehearse-cancel-230"
            )
            self.assertEqual("cancelled", result["status"])
            self.assertEqual(result, service.cancel_duplicate_visa_fee(
                first["id"], cursor.lastrowid, 230, 1, "Admin", "rehearse-cancel-230"
            ))
            self.assertEqual(["unbilled", "cancelled"], [r["status"] for r in self.records(db)])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM revenue_audit_events WHERE action='duplicate_visa_fee_cancelled'"
            ).fetchone()[0])

    def test_parallel_writers_and_rule_version_change_reuse_the_same_fee(self):
        with self.connect() as db:
            self.prepare_visa(db)
            service = RevenueService(RevenueRepository(db))
            case = service.open_case(100, 1, "parallel-case")
            account = service.ensure_student_billing_account("S-REVENUE", 1, "parallel-account", "2026-08-01")
        barrier = Barrier(2)
        item = {"visa_case_id": 230, "application_cycle_id": 100,
                "milestone_code": "guardian-590", "description": "Visa service fee",
                "net_cents": 90000, "gst_cents": 0}

        def writer(version):
            db = sqlite3.connect(self.database_path, timeout=15)
            try:
                db.execute("PRAGMA foreign_keys=ON")
                barrier.wait(timeout=10)
                return RevenueService(RevenueRepository(db)).ensure_visa_fee(
                    case["uid"], account["uid"], item, 1, version, version
                )
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ("old-rule", "new-rule")))
        self.assertCountEqual(["CREATED", "EXISTING"], [status for status, _ in results])
        self.assertEqual(results[0][1][0]["uid"], results[1][1][0]["uid"])

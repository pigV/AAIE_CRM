from revenue.controlled_onboarding import ControlledOnboardingService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.read_models import RevenueReadModels
from revenue.repository import RevenueRepository
from revenue.views import render_revenue_workspace
from tests import test_revenue_controlled_onboarding as fixture


class SignedVisaPackageTests(fixture.RevenueControlledOnboardingTests):
    def prepare_package(self, db, *, status="FullyExecuted", scope='["student_visa_lodgement"]'):
        db.executescript("""
            ALTER TABLE contracts ADD COLUMN consulting_and_visa_service_fee TEXT;
            ALTER TABLE contracts ADD COLUMN discount_amount TEXT;
            ALTER TABLE contracts ADD COLUMN final_fee_amount TEXT;
            ALTER TABLE contracts ADD COLUMN service_scope_json TEXT;
            ALTER TABLE visa_cases ADD COLUMN student_id TEXT;
            ALTER TABLE visa_cases ADD COLUMN status TEXT;
            ALTER TABLE visa_cases ADD COLUMN archived_at TEXT;
        """)
        db.execute("""UPDATE contracts SET contract_status=?, service_fee_currency='澳洲公户',
            consulting_and_visa_service_fee='2400.00',discount_amount='0.00',final_fee_amount='2640.00',
            service_scope_json=? WHERE id=70""", (status, scope))
        db.execute("""INSERT INTO visa_cases(id,application_cycle_id,student_id,case_type,visa_subclass,application_type,status)
                VALUES(231,100,'S-REVENUE','500 Student Visa','500','New Application','Lodged')""")

    def preview_apply(self, db, key):
        proposal = self._preview(db)
        service = ControlledOnboardingService(RevenueRepository(db))
        service.apply(proposal, actor_user_id=1, actor_role="Admin", idempotency_key=key)
        cycle = next(c for c in proposal["cycles"] if c["application_cycle_id"] == 100)
        return {fee["visa_case_id"]: fee for fee in cycle["additional_fees"]}

    def render(self, db):
        model = RevenueReadModels(db)
        return [render_revenue_workspace(model.dashboard(), "fixture", mobile) for mobile in (False, True)]

    def case_fees(self, db):
        return db.execute("""SELECT f.milestone_code,f.net_cents FROM revenue_fee_items f
                  JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                 WHERE c.application_cycle_id=100 AND f.status<>'cancelled' ORDER BY f.milestone_code""").fetchall()

    def test_signed_unpaid_package_and_guardian_share_rules_and_display(self):
        with self.connect() as db:
            self.prepare_package(db)
            decisions = self.preview_apply(db, "import-first")
            self.assertEqual("signed_student_visa_package", decisions[231]["policy_code"])
            self.assertEqual(("included", "proposed"), (decisions[231]["status"], decisions[230]["status"]))
            orchestrator = RevenueOperationalOrchestrator(db, 1, "Admin")
            self.assertEqual("INCLUDED", orchestrator.visa_case_changed(231)["fee_status"])
            self.assertEqual("EXISTING", orchestrator.visa_case_changed(230)["fee_status"])
            self.assertEqual([("guardian-590",90000),("k12-new",240000)], [tuple(r) for r in self.case_fees(db)])
            for html in self.render(db):
                self.assertIn("首次学生签证已包含在 A$2,400 服务套餐中", html)
                self.assertIn("陪读签证 · 已收 A$0，还差 A$900", html)

    def test_package_is_recognized_before_payment_and_without_guardian(self):
        with self.connect() as db:
            self.prepare_package(db)
            db.execute("DELETE FROM visa_cases WHERE id=230")
            orchestrator = RevenueOperationalOrchestrator(db, 1, "Admin")
            result = orchestrator.visa_case_changed(231)
            self.assertEqual("signed_student_visa_package", result["visa_fee_policy"]["policy_code"])
            self.assertEqual([], result["fee_items"])
            orchestrator.contract_fully_executed(70)
            self.assertEqual([("k12-new",240000)], [tuple(r) for r in self.case_fees(db)])
            for html in self.render(db):
                self.assertIn("首次学生签证已包含在 A$2,400 服务套餐中", html)
                self.assertNotIn("还差 A$900", html)

    def test_formal_first_then_import_preserves_guardian_fee(self):
        with self.connect() as db:
            self.prepare_package(db)
            orchestrator = RevenueOperationalOrchestrator(db, 1, "Admin")
            self.assertEqual("INCLUDED", orchestrator.visa_case_changed(231)["fee_status"])
            first = orchestrator.visa_case_changed(230)
            self.assertEqual("CREATED", first["fee_status"])
            self.assertEqual(90000, first["visa_fee_cents"])
            decisions = self.preview_apply(db, "formal-first")
            self.assertEqual(("included", "existing"), (decisions[231]["status"], decisions[230]["status"]))
            self.assertEqual(first, orchestrator.visa_case_changed(230))
            self.assertEqual([("guardian-590",90000),("k12-new",240000)], [tuple(r) for r in self.case_fees(db)])

    def test_package_does_not_override_student_visa_renewal_rule(self):
        with self.connect() as db:
            self.prepare_package(db)
            db.execute("UPDATE visa_cases SET application_type='续签' WHERE id=231")
            result = RevenueOperationalOrchestrator(db, 1, "Admin").visa_case_changed(231)
            self.assertEqual("student-visa-renewal", result["visa_fee_policy"]["policy_code"])
            self.assertEqual(90000, result["visa_fee_cents"])

    def test_discounted_contract_is_not_identified_as_2400_package(self):
        with self.connect() as db:
            self.prepare_package(db)
            db.execute("UPDATE contracts SET discount_amount='1200',final_fee_amount='1200' WHERE id=70")
            result = RevenueOperationalOrchestrator(db, 1, "Admin").visa_case_changed(231)
            self.assertEqual("signed_visa_package_required", result["reason"])
            self.assertEqual([], result["fee_items"])

    def assert_package_unconfirmed(self, status, scope):
        with self.connect() as db:
            self.prepare_package(db, status=status, scope=scope)
            result = RevenueOperationalOrchestrator(db, 1, "Admin").visa_case_changed(231)
            self.assertEqual("NEEDS_ATTENTION", result["fee_status"])
            self.assertEqual([], result["fee_items"])

    def test_parent_only_signature_does_not_activate_package(self):
        self.assert_package_unconfirmed("ParentSigned", '["student_visa_lodgement"]')

    def test_consulting_only_contract_does_not_activate_package(self):
        self.assert_package_unconfirmed("FullyExecuted", '["study_planning_consulting"]')

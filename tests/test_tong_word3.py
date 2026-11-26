"""Approved Word feedback: customer refund grants and requested intake dates."""
import json
import unittest

from tests.test_revenue_client_refunds import RevenueClientRefundTests
from tests.test_student_management_summary import StudentManagementSummaryTests


class RefundGrantTests(RevenueClientRefundTests):
    def test_closed_application_retains_existing_financial_history(self):
        from revenue.read_models import RevenueReadModels
        from revenue.workbench import RevenueWorkbench
        payment = self._payment()
        self.connection.execute("UPDATE application_cycles SET status='closed' WHERE id=100")
        self.connection.execute("UPDATE revenue_commercial_cases SET status='closed' WHERE id=1")
        workspace = RevenueReadModels(self.connection).workspace('billing', {}, case_uid='case-1')
        self.assertEqual(payment['uid'], workspace['action']['payment_history'][0]['payment_uid'])
        self.assertTrue(RevenueWorkbench(self.connection).load('case-1')['payment_history'])

    def test_parent_cannot_refund_even_with_module_grant(self):
        payment = self._payment()
        with self.assertRaises(PermissionError):
            self.refunds.record(
                payment_uid=payment['uid'], actual_amount_cents=10000,
                refunded_at='2026-09-05T13:25', reason='Return',
                evidence_document_uid=self.document['uid'], actor_user_id=1,
                actor_role='Parent', actor_has_revenue_access=True, idempotency_key='parent-refund')

    def test_granted_staff_can_record_refund(self):
        payment = self._payment()
        result = self.refunds.record(
            payment_uid=payment['uid'], actual_amount_cents=10000,
            refunded_at='2026-09-05T13:25', reason='Approved return',
            evidence_document_uid=self.document['uid'], actor_user_id=1,
            actor_role='Staff', actor_has_revenue_access=True,
            idempotency_key='staff-refund',
        )
        self.assertEqual('paid', result['status'])

    def test_refund_route_matches_payment_grants(self):
        from revenue.access_policy import can_perform_revenue_action
        for role, grant in [('Admin', '[]'), ('Staff', '["revenue_commission"]'),
                            ('Staff', '[]'), ('Parent', '["revenue_commission"]')]:
            user = {'role_name': role, 'module_permissions_json': grant}
            self.assertEqual(can_perform_revenue_action(user, 'billing/payment'),
                             can_perform_revenue_action(user, 'billing/refund'))

    def test_staff_billing_keeps_history_and_refund_without_duplicate_list(self):
        from revenue.read_models import RevenueReadModels
        from revenue.views import render_revenue_workspace_page
        self._payment()
        data = RevenueReadModels(self.connection).workspace('billing', {}, case_uid='case-1')
        data.update(can_administer=False, can_record_client_refund=True)
        page = render_revenue_workspace_page(data, csrf='csrf', action_token='token')
        self.assertIn('客户收款与收据', page)
        self.assertIn('登记退款', page)
        self.assertNotIn('revenue-list-panel', page)


class IntakeMonthTests(StudentManagementSummaryTests):
    def test_canonical_student_fields_work_without_form_json(self):
        from student_management_summary import load_student_management_summary_map
        self.connection.execute('ALTER TABLE students ADD COLUMN preferred_intake_year TEXT')
        self.connection.execute('ALTER TABLE students ADD COLUMN preferred_intake_term TEXT')
        self.connection.execute("UPDATE students SET preferred_intake_year='2027',preferred_intake_term='10' WHERE student_id='S1'")
        self.assertEqual('2027年10月', load_student_management_summary_map(self.connection, ['S1'])['S1'].planned_intake_display)

    def test_planned_month_does_not_use_creation_or_offer_date(self):
        from student_management_summary import load_student_management_summary_map
        self.connection.execute('ALTER TABLE students ADD COLUMN form_data_json TEXT')
        self.connection.execute('UPDATE students SET form_data_json=? WHERE student_id=?',
            (json.dumps({'preferred_intake_year': '2028', 'preferred_intake_term': '07'}), 'S1'))
        summary = load_student_management_summary_map(self.connection, ['S1'])['S1']
        self.assertEqual('2028年7月', summary.planned_intake_display)

    def test_uncertain_intake_does_not_invent_a_month(self):
        from student_management_summary import load_student_management_summary_map
        self.connection.execute('ALTER TABLE students ADD COLUMN form_data_json TEXT')
        self.connection.execute('UPDATE students SET form_data_json=? WHERE student_id=?',
            (json.dumps({'preferred_intake_year': '2028', 'preferred_intake_term': 'unsure'}), 'S1'))
        summary = load_student_management_summary_map(self.connection, ['S1'])['S1']
        self.assertEqual('2028年 · 月份待定', summary.planned_intake_display)

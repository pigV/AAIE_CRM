from types import SimpleNamespace
import unittest


class TransferV2DashboardTests(unittest.TestCase):
    def test_counts_only_approved_new_transfer_projection_stages(self):
        from student_lifecycle.transfer_v2_dashboard import build_transfer_v2_dashboard

        rows = (
            SimpleNamespace(cycle_type="transfer", stage=SimpleNamespace(key="accepted_and_paid")),
            SimpleNamespace(cycle_type="transfer", stage=SimpleNamespace(key="coe_update")),
            SimpleNamespace(cycle_type="transfer", stage=SimpleNamespace(key="accepted_and_paid")),
            SimpleNamespace(cycle_type="initial", stage=SimpleNamespace(key="accepted_and_paid")),
        )
        result = build_transfer_v2_dashboard(rows)

        self.assertEqual(3, result.active_total)
        self.assertEqual(2, result.by_key["accepted_and_paid"].count)
        self.assertEqual(1, result.by_key["coe_update"].count)
        self.assertEqual(0, result.by_key["pending_completion"].count)

    def test_legacy_transfer_is_not_counted_as_an_active_v2_transfer(self):
        from student_lifecycle.transfer_v2_dashboard import build_transfer_v2_dashboard

        rows = (
            SimpleNamespace(
                cycle_type="transfer",
                stage=SimpleNamespace(key="legacy_unconfirmed"),
            ),
            SimpleNamespace(
                cycle_type="transfer",
                stage=SimpleNamespace(key="materials_preparation"),
            ),
        )

        result = build_transfer_v2_dashboard(rows)

        self.assertEqual(1, result.active_total)
        self.assertEqual(1, result.by_key["materials_preparation"].count)

    def test_dashboard_links_to_student_list_with_exact_type_and_stage(self):
        import app

        rows = (
            SimpleNamespace(cycle_type="transfer", stage=SimpleNamespace(key="accepted_and_paid")),
        )
        html = app.dashboard_transfer_v2_stage_cards_html(rows)

        self.assertIn("转学中总数", html)
        self.assertIn('href="/students?lifecycle_type=transfer&amp;stage=accepted_and_paid"', html)
        self.assertIn('data-transfer-v2-stage="coe_update"', html)
        self.assertIn("已接受并付款", html)
        self.assertIn("更新 COE", html)


if __name__ == "__main__":
    unittest.main()

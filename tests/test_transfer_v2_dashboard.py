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

    def test_other_active_transfer_is_counted_in_its_own_bucket(self):
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

        self.assertEqual(2, result.active_total)
        self.assertEqual(1, result.by_key["other"].count)
        self.assertEqual(1, result.by_key["materials_preparation"].count)



if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from tests.revenue_operational_browser_harness import IDENTITIES, TOKENS


class RevenueOperationalBrowserHarnessTests(unittest.TestCase):
    def test_non_admin_identity_labels_do_not_pollute_zero_leakage_assertions(self):
        sensitive = ("revenue", "commission", "收入与佣金", "佣金", "master agent")

        for _user_id, _username, display_name, _role_id, _token in IDENTITIES[1:]:
            lowered = display_name.lower()
            self.assertFalse(
                any(term.lower() in lowered for term in sensitive),
                display_name,
            )

    def test_staff_identity_uses_an_exact_staff_role_fixture(self):
        staff = next(identity for identity in IDENTITIES if identity[2] == "E2E Staff")

        self.assertEqual(900003, staff[3])

    def test_production_derived_tong_yu_has_a_dedicated_isolated_session(self):
        self.assertEqual("revenue-e2e-tong-yu", TOKENS["TongYu"])


if __name__ == "__main__":
    unittest.main()

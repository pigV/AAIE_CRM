import sqlite3
import unittest

from higher_education_views import render_higher_education_workspace
from revenue import access_policy
from revenue.access_policy import REVENUE_MODULE_KEY, is_revenue_admin, legacy_he_writes_closed


class RevenueAccessPolicyTests(unittest.TestCase):
    def test_admin_and_explicit_module_grant_can_access_revenue(self):
        can_access_revenue = getattr(access_policy, "can_access_revenue", lambda _user: False)
        self.assertEqual("revenue_commission", REVENUE_MODULE_KEY)
        self.assertTrue(is_revenue_admin({"role_name": "Admin"}))
        self.assertTrue(can_access_revenue({"role_name": "Admin"}))
        for role in ("Manager", "Manager + Student Cases", "Content Staff", "Staff"):
            with self.subTest(role=role):
                user = {
                    "role_name": role,
                    "module_permissions_json": '["revenue_commission"]',
                }
                self.assertFalse(is_revenue_admin(user), role)
                self.assertTrue(can_access_revenue(user), role)

    def test_revenue_access_fails_closed_without_authoritative_module_grant(self):
        can_access_revenue = getattr(access_policy, "can_access_revenue", lambda _user: False)
        denied = (
            None,
            {},
            {"role_name": "Consultant", "module_permissions_json": "[]"},
            {"role_name": "Finance", "module_permissions_json": '["payments"]'},
            {"role_name": "admin", "module_permissions_json": "[]"},
            {"role_name": "Manager", "module_permissions_json": "not-json"},
            {"role_name": "Manager", "module_permissions_json": '{"revenue_commission": true}'},
        )
        for user in denied:
            with self.subTest(user=user):
                self.assertFalse(can_access_revenue(user))
        self.assertFalse(is_revenue_admin(None))

    def test_legacy_writes_close_only_after_revenue_cutover(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.assertFalse(legacy_he_writes_closed(connection))
        connection.execute(
            "CREATE TABLE revenue_module_state(id INTEGER PRIMARY KEY,status TEXT,cutover_at TEXT)"
        )
        connection.execute(
            "INSERT INTO revenue_module_state VALUES(1,'disabled',NULL)"
        )
        self.assertFalse(legacy_he_writes_closed(connection))
        connection.execute(
            "UPDATE revenue_module_state SET status='active',cutover_at='2026-08-29' WHERE id=1"
        )
        self.assertTrue(legacy_he_writes_closed(connection))

    def test_non_admin_higher_education_html_has_no_legacy_commission_trace(self):
        application = {
            "id": 9,
            "status": "Submitted",
            "provider_name": "Example University",
            "course_type": "Bachelor",
            "course_name": "Commerce",
            "intake": "2027-02",
            "submission_channel": "Master Agent",
        }
        share = {
            "id": 6,
            "application_id": 9,
            "share_percent": 25,
            "status": "Pending",
        }
        html = render_higher_education_workspace(
            providers=[], relationships=[], plans=[], applications=[application],
            csrf="csrf", can_manage=True, offers=[], temporary_shares=[share],
            can_view_legacy_commission=False,
        )
        for leaked in (
            "temporary-shares", "commission-lines", "Temporary share",
            "Commission Line", "Master Agent ID",
        ):
            self.assertNotIn(leaked, html)

    def test_admin_can_read_legacy_history_but_cutover_removes_write_forms(self):
        application = {
            "id": 9,
            "status": "Submitted",
            "provider_name": "Example University",
            "course_type": "Bachelor",
            "course_name": "Commerce",
            "intake": "2027-02",
            "submission_channel": "Master Agent",
        }
        share = {
            "id": 6,
            "application_id": 9,
            "share_percent": 25,
            "status": "Approved",
        }
        html = render_higher_education_workspace(
            providers=[], relationships=[], plans=[], applications=[application],
            csrf="csrf", can_manage=True, offers=[], temporary_shares=[share],
            can_view_legacy_commission=True, legacy_commission_writes_closed=True,
        )
        self.assertIn("Legacy HE Commission Archive", html)
        self.assertIn("Temporary share 25%", html)
        self.assertNotIn('/temporary-shares"', html)
        self.assertNotIn('/commission-lines"', html)

    def test_admin_cutover_empty_state_still_explains_legacy_archive(self):
        html = render_higher_education_workspace(
            providers=[], relationships=[], plans=[], applications=[],
            csrf="csrf", can_manage=True, offers=[], temporary_shares=[],
            can_view_legacy_commission=True,
            legacy_commission_writes_closed=True,
        )
        self.assertIn("Legacy HE Commission Archive", html)
        self.assertIn("read-only", html)
        self.assertIn("No backfill", html)
        self.assertNotIn('/temporary-shares"', html)
        self.assertNotIn('/commission-lines"', html)


if __name__ == "__main__":
    unittest.main()

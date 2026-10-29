import json
import unittest

from ui import page


class RevenueNavigationTests(unittest.TestCase):
    def render(self, role_name, modules=()):
        return page(
            "Test",
            "<section>body</section>",
            {
                "username": f"revenue-{role_name.lower()}",
                "display_name": role_name,
                "role_name": role_name,
                "module_permissions_json": json.dumps(list(modules)),
            },
            "csrf",
            nas={"mode": "test", "ready": True},
        )

    def test_admin_has_revenue_sidebar_entry(self):
        html = self.render("Admin")
        self.assertIn('href="/revenue"', html)
        self.assertIn('data-nav-key="revenue"', html)
        self.assertIn("收入与佣金", html)

    def test_explicitly_authorized_non_admin_has_revenue_entry(self):
        for role_name in ("Manager", "Manager + Student Cases", "Content Staff"):
            with self.subTest(role_name=role_name):
                html = self.render(role_name, ("revenue_commission",))
                self.assertIn('href="/revenue"', html)
                self.assertIn('data-nav-key="revenue"', html)
                self.assertIn("收入与佣金", html)

    def test_non_admin_without_revenue_grant_has_zero_navigation_trace(self):
        for role_name in ("Manager", "Consultant", "Finance"):
            with self.subTest(role_name=role_name):
                html = self.render(role_name)
                self.assertNotIn('href="/revenue"', html)
                self.assertNotIn('data-nav-key="revenue"', html)
                self.assertNotIn("收入与佣金", html)


if __name__ == "__main__":
    unittest.main()

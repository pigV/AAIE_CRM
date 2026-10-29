import unittest
from pathlib import Path

import ui
from app import CRMHandler
from ui import page


ROOT = Path(__file__).resolve().parents[1]


class ParentFormPremiumUiTests(unittest.TestCase):
    def test_parent_form_marker_loads_dedicated_css_without_leaking_to_other_public_pages(self):
        parent_html = page(
            "家长表单",
            '<div data-public-parent-form-ui>家长表单</div>',
            asset_version="test",
        )
        unrelated_html = page(
            "普通页面",
            '<div class="public-result">普通公开页面</div>',
            asset_version="test",
        )

        self.assertIn('/assets/parent-form.css?v=test', parent_html)
        self.assertNotIn('/assets/parent-form.css', unrelated_html)

    def test_login_renderer_exposes_premium_wechat_flow_without_openid(self):
        renderer = getattr(ui, "parent_form_login", None)
        self.assertTrue(callable(renderer))

        html = renderer(
            "token-a",
            "12/08/2026 13:21",
            oauth_enabled=True,
        )

        self.assertIn('data-public-parent-form-ui', html)
        self.assertIn('data-parent-form-view="login"', html)
        self.assertIn('/parent-form/token-a/wechat-login', html)
        self.assertIn('微信验证并继续', html)
        self.assertIn('7 天有效', html)
        self.assertNotIn('OpenID', html)

    def test_result_renderer_distinguishes_success_and_locked_states(self):
        renderer = getattr(ui, "parent_form_result", None)
        self.assertTrue(callable(renderer))

        success = renderer(
            "资料已安全提交",
            "AAIE 已收到学生信息表。",
            tone="success",
            details=(("提交状态", "已完成"), ("资料保护", "已锁定")),
        )
        locked = renderer(
            "表单已锁定",
            "该表单已经正式递交。",
            tone="warning",
        )

        self.assertIn('data-parent-form-view="result"', success)
        self.assertIn('data-parent-form-tone="success"', success)
        self.assertIn('资料保护', success)
        self.assertIn('data-parent-form-tone="warning"', locked)

    def test_parent_form_uses_premium_shell_without_losing_save_and_submit_hooks(self):
        html = ui.parent_form(
            "token-a",
            "2026-08-12T13:21:00+10:00",
            revision=4,
            last_saved_at="2026-08-05T13:21:00+10:00",
        )

        self.assertIn('data-public-parent-form-ui', html)
        self.assertIn('data-parent-form-view="form"', html)
        self.assertIn('data-public-parent-form', html)
        self.assertIn('data-parent-form-revision="4"', html)
        self.assertIn('data-parent-form-save-status', html)
        self.assertIn('data-parent-form-save', html)
        self.assertIn('data-parent-form-submit', html)
        self.assertIn('parent-draft-dock', html)

    def test_parent_form_css_is_namespaced_mobile_safe_and_fixed_to_light_theme(self):
        css_path = ROOT / "assets" / "parent-form.css"
        self.assertTrue(css_path.exists())

        css = css_path.read_text(encoding="utf-8")
        self.assertIn(".public-parent-form-ui", css)
        self.assertIn("safe-area-inset-bottom", css)
        self.assertIn("@media (max-width: 430px)", css)
        self.assertIn("@media (max-width: 320px)", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("color-scheme: light", css)
        for forbidden in ("\nbody {", "\ninput {", "\nbutton {", "\n.public-result {"):
            self.assertNotIn(forbidden, css)

    def test_parent_form_login_route_uses_premium_renderer(self):
        handler = object.__new__(CRMHandler)
        handler.school_plan_wechat_oauth_enabled = lambda: True
        captured = {}
        handler.send_html = lambda html, status=200: captured.update(html=html, status=status)

        handler.parent_form_login_required_page(
            "token-a",
            {"expires_at": "2026-08-12T13:21:00+10:00"},
        )

        self.assertEqual(200, captured["status"])
        self.assertIn('data-public-parent-form-ui', captured["html"])
        self.assertIn('data-parent-form-view="login"', captured["html"])
        self.assertIn('/assets/parent-form.css', captured["html"])

    def test_parent_result_route_supports_premium_success_and_warning_states(self):
        handler = object.__new__(CRMHandler)
        captured = []
        handler.send_html = lambda html, status=200: captured.append((html, status))

        handler.parent_result_page(
            "资料已安全提交",
            "AAIE 已收到学生信息表。",
            tone="success",
            details=(("提交状态", "已完成"), ("资料保护", "已锁定")),
        )
        handler.parent_result_page(
            "表单已锁定",
            "该表单已经正式递交。",
            status=410,
        )

        success_html, success_status = captured[0]
        warning_html, warning_status = captured[1]
        self.assertEqual(200, success_status)
        self.assertIn('data-parent-form-tone="success"', success_html)
        self.assertIn("资料保护", success_html)
        self.assertEqual(410, warning_status)
        self.assertIn('data-parent-form-tone="warning"', warning_html)
        self.assertIn('/assets/parent-form.css', warning_html)


if __name__ == "__main__":
    unittest.main()

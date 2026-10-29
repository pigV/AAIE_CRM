"""Shared display localization must preserve inputs, destinations and role authority."""
import ast
import copy
import json
import re
from student_applications_display import application_error_display
from pathlib import Path
from types import SimpleNamespace
import unittest

import ui
from mobile.permissions.policy import account_actions


ROOT = Path(__file__).resolve().parents[1]


def handler_method(name):
    """Exercise the original renderer without importing application startup side effects."""
    tree = ast.parse((ROOT / "app.py").read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and any(isinstance(child, ast.FunctionDef) and child.name == name for child in node.body))
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = dict(vars(ui), re=re, application_error_display=application_error_display, nas=SimpleNamespace(status=lambda: {"ready": True, "mode": "test"}))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "app.py", "exec"), namespace)
    return namespace[name]


def user(role="Admin"):
    return {"id": 1, "username": "admin" if role == "Admin" else "fixture",
            "display_name": "User supplied English name", "role_name": role,
            "module_permissions_json": json.dumps(["dashboard", "students", "visa"])}


class SharedLocalizationTests(unittest.TestCase):
    def test_role_labels_do_not_change_user_values_or_permission_results(self):
        for role, label in (("Admin", "管理员"), ("Manager", "经理"),
                            ("Content Staff", "内容人员"), ("Consultant", "顾问"),
                            ("Visa Officer", "Visa 专员"), ("Finance", "财务"),
                            ("View Only", "只读")):
            with self.subTest(role=role):
                original = user(role)
                before = copy.deepcopy(original)
                modules = ui._user_modules(original)
                rendered = ui.page("首页", "<p>Original Offer / CoE / School Name</p>", original,
                                   nas={"ready": True, "mode": "test"})
                self.assertIn(f"<small>{label}</small>", rendered)
                self.assertEqual(original, before)
                self.assertEqual(ui._user_modules(original), modules)
                self.assertIn("User supplied English name", rendered)
                self.assertIn("Original Offer / CoE / School Name", rendered)

    def test_mode_mapping_is_display_only_and_unknown_values_are_preserved(self):
        for value, display in (("test", "测试模式"), ("production", "生产模式"),
                               ("staging", "预发布模式"), ("preprod", "预发布模式")):
            original = {"ready": True, "mode": value}
            rendered = ui.page("首页", "", user(), nas=original)
            self.assertIn(f'<div class="mode-badge">{display}</div>', rendered)
            self.assertEqual(original["mode"], value)
        self.assertIn("CUSTOM", ui.page("首页", "", user(), nas={"mode": "CUSTOM"}))

    def test_shell_labels_preserve_routes_and_allowed_product_names(self):
        rendered = ui.page("首页", "", user(), nas={"ready": True, "mode": "test"})
        for text in ("学生事务管理", "AAIE 学生管理 CRM", "移动版", "Visa 管理", "NAS 已连接"):
            self.assertIn(text, rendered)
        for target in ('href="/visa-management"', 'href="/students"', 'action="/logout"',
                       'href="/view-mode/mobile?return_to=%2F"', 'href="/account/password"'):
            self.assertIn(target, rendered)
        self.assertIn("AAIE CRM", rendered)
        self.assertIn("Agent Reach", rendered)

    def test_unknown_role_and_user_content_are_never_translated(self):
        rendered = ui.page("Original English Title", "<p>pending approved active</p>", user("Custom English Role"))
        for text in ("Original English Title", "Custom English Role", "pending approved active"):
            self.assertIn(text, rendered)

    def test_nas_failure_displays_keep_file_lock_and_distinct_failure_semantics(self):
        for code, label in (("mount_unavailable", "NAS 未连接"),
                            ("read_denied", "NAS 访问待恢复"), ("write_denied", "NAS 访问待恢复")):
            rendered = ui.page("首页", "", user(), nas={"ready": False, "mode": "test", "access_error_code": code})
            self.assertIn(label, rendered)
            self.assertIn("文件操作已锁定", rendered)
            if code == "mount_unavailable":
                self.assertIn("学生文件存储未连接", rendered)
            else:
                self.assertIn("NAS 已挂载", rendered)

    def test_account_display_preserves_desktop_destination_and_role(self):
        action = next(action for action in account_actions("/view-mode/desktop?return_to=%2Faccount") if action.key == "desktop")
        self.assertEqual(action.label, "切换桌面版")
        self.assertEqual(action.href, "/view-mode/desktop?return_to=%2Faccount")
        handler = SimpleNamespace(user=user("Manager"), csrf="fixture", mobile_shell=lambda title, body, active: body)
        result = []
        handler.send_html = result.append
        handler_method("mobile_account")(handler, "GET")
        self.assertIn("经理", result[0])
        self.assertIn("切换桌面版", result[0])
        self.assertEqual(handler.user["role_name"], "Manager")

    def test_generic_error_display_changes_no_status_or_arbitrary_error_content(self):
        responses = []
        handler = SimpleNamespace(user=user(), csrf="fixture", send_html=lambda body, status: responses.append((body, status)))
        method = handler_method("error_page")
        method(handler, "Method Not Allowed", 405)
        self.assertIn("不支持此请求方式", responses[-1][0])
        self.assertEqual(responses[-1][1], 405)
        method(handler, "Original School Name / Offer / CoE", 400)
        self.assertIn("Original School Name / Offer / CoE", responses[-1][0])


if __name__ == "__main__":
    unittest.main()

import sqlite3
import tempfile
import unittest
import re
import importlib.util
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlparse

from mobile.mode import resolve_view_mode, safe_return_path, view_mode_cookie
from mobile.home.data import (
    DocumentShortcut,
    MobileHomeData,
    ReminderSummary,
    StageSummary,
    load_mobile_home_data,
)
from mobile.home.views import _stage_tone, render_mobile_home
from mobile.adapters.filters import missing_school_plan_predicate, unsigned_contract_predicate
from mobile.permissions.policy import MobileAction, bottom_actions, plus_actions, quick_actions
from mobile.navigation.views import render_bottom_navigation, render_plus_sheet
from mobile.shell import mobile_page
import mobile.shell as mobile_shell_module
from mobile.components.cards import render_quick_action
from mobile.components.icon_assets import app_icon_path, outline_icon_path
from mobile.components.icons import icon_svg
from student_application_stage_distribution import APPLICATION_STAGE_DEFINITIONS


class MobileIconTests(unittest.TestCase):
    def test_local_svg_icons_are_decorative_and_have_safe_fallback(self):
        alert = icon_svg("alert", class_name="mobile-icon")
        fallback = icon_svg("unknown")
        self.assertIn('<svg class="mobile-icon"', alert)
        self.assertIn('viewBox="0 0 24 24"', alert)
        self.assertIn('aria-hidden="true"', alert)
        self.assertIn('focusable="false"', alert)
        self.assertEqual(fallback, icon_svg("stage_default"))
        self.assertNotIn("http", alert)


class MobileIconAssetTests(unittest.TestCase):
    APP_ICON_KEYS = (
        "parent-form-reminder",
        "contract-reminder",
        "school-plan-reminder",
        "parent-form-create",
        "contract-create",
        "school-plan-create",
        "commission-disabled",
        "file",
        "template",
        "content",
        "school-library",
        "generic-document",
    )
    OUTLINE_ICON_KEYS = (
        "search",
        "bell",
        "home",
        "student",
        "plus",
        "school",
        "visa",
        "chevron-right",
        "file",
    )

    def _icon_assets(self):
        spec = importlib.util.find_spec("mobile.components.icon_assets")
        self.assertIsNotNone(
            spec,
            "visible Mobile icons must come from a controlled local asset registry",
        )
        from mobile.components import icon_assets

        return icon_assets

    def test_icon_registry_returns_local_files_that_exist(self):
        icon_assets = self._icon_assets()
        for key in self.APP_ICON_KEYS:
            url = icon_assets.app_icon_path(key)
            self.assertTrue(url.startswith("/assets/mobile/icons/app/"), key)
            self.assertTrue((Path("assets") / url.removeprefix("/assets/")).is_file(), key)
        for key in self.OUTLINE_ICON_KEYS:
            url = icon_assets.outline_icon_path(key)
            self.assertTrue(url.startswith("/assets/mobile/icons/outline/"), key)
            self.assertTrue((Path("assets") / url.removeprefix("/assets/")).is_file(), key)

    def test_icon_registry_has_safe_local_fallbacks(self):
        icon_assets = self._icon_assets()
        self.assertEqual(
            icon_assets.app_icon_path("unknown"),
            "/assets/mobile/icons/app/generic-document.png",
        )
        self.assertEqual(
            icon_assets.outline_icon_path("unknown"),
            "/assets/mobile/icons/outline/file.svg",
        )


class MobileModePolicyTests(unittest.TestCase):
    def test_explicit_cookie_wins_over_device_default(self):
        self.assertEqual(resolve_view_mode("crm_view_mode=desktop", "iPhone"), "desktop")
        self.assertEqual(resolve_view_mode("crm_view_mode=mobile", "Macintosh"), "desktop")
        self.assertEqual(resolve_view_mode(view_mode_cookie("mobile"), "Macintosh", "/students"), "mobile")

    def test_phone_defaults_mobile_and_desktop_defaults_desktop(self):
        self.assertEqual(resolve_view_mode("", "Mozilla/5.0 (iPhone)"), "mobile")
        self.assertEqual(
            resolve_view_mode("", "Mozilla/5.0 (Linux; Android 15; Pixel 9) Mobile"),
            "mobile",
        )
        self.assertEqual(
            resolve_view_mode("", "Mozilla/5.0 (Macintosh; Intel Mac OS X)"),
            "desktop",
        )
        self.assertEqual(resolve_view_mode("", "Mozilla/5.0 (iPad)"), "desktop")

    def test_invalid_cookie_is_ignored(self):
        self.assertEqual(resolve_view_mode("crm_view_mode=broken", "iPhone"), "mobile")

    def test_return_path_rejects_external_targets(self):
        self.assertEqual(safe_return_path("/students?stage=offer"), "/students?stage=offer")
        self.assertEqual(safe_return_path("https://evil.example/path"), "/")
        self.assertEqual(safe_return_path("//evil.example/path"), "/")
        self.assertEqual(safe_return_path("students"), "/")

    def test_cookie_is_same_site_and_http_only(self):
        value = view_mode_cookie("mobile")
        self.assertIn("crm_view_mode=mobile", value)
        self.assertIn("Path=/", value)
        self.assertIn("SameSite=Lax", value)
        self.assertIn("HttpOnly", value)

    def test_cookie_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            view_mode_cookie("broken")


class MobilePermissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.sonny = {
            "id": 5,
            "username": "sonnyz",
            "display_name": "Sonny Zhang",
            "role_name": "Admin",
        }
        self.tongy = {
            "id": 2,
            "username": "tongy",
            "display_name": "Tong Yu",
            "role_name": "Manager + Student Cases",
        }

    @staticmethod
    def allow(_user, _module_key):
        return True

    def test_bottom_navigation_is_visually_consistent(self):
        expected = ["home", "students", "plus", "schools", "visa"]
        self.assertEqual([item.key for item in bottom_actions(self.sonny, self.allow)], expected)
        self.assertEqual([item.key for item in bottom_actions(self.tongy, self.allow)], expected)

    def test_sonny_plus_menu_uses_existing_content_routes(self):
        actions = plus_actions(self.sonny, self.allow)
        self.assertEqual(
            [item.href for item in actions],
            ["/content?tab=create", "/content?tab=extract"],
        )
        self.assertNotIn("/contracts?view=new", [item.href for item in actions])

    def test_tongy_plus_menu_uses_existing_crm_routes(self):
        actions = plus_actions(self.tongy, self.allow)
        self.assertEqual(
            [item.key for item in actions],
            ["followup", "parent_form", "contract", "school_plan", "student_search"],
        )
        self.assertNotIn("/content?tab=create", [item.href for item in actions])
        contract_action = next(item for item in actions if item.key == "contract")
        self.assertEqual(contract_action.label, "新建家长合同")
        self.assertEqual(contract_action.href, "/contracts?view=new")
        self.assertEqual(contract_action.state, "available")

    def test_quick_actions_show_revenue_from_central_module_authority(self):
        def deny_contract(user, module_key):
            return module_key != "contract_management_center"

        manager_actions = quick_actions(self.tongy, deny_contract)
        self.assertEqual(manager_actions[1].key, "contract")
        self.assertEqual(manager_actions[1].state, "forbidden")
        manager_revenue = next(item for item in manager_actions if item.key == "revenue")
        self.assertEqual("/mobile/revenue", manager_revenue.href)

        admin_actions = quick_actions(self.sonny, self.allow)
        revenue = next(item for item in admin_actions if item.key == "revenue")
        self.assertEqual("/mobile/revenue", revenue.href)
        self.assertEqual("available", revenue.state)
        self.assertEqual("收入与佣金", revenue.label)

    def test_quick_actions_hide_revenue_when_module_authority_denies(self):
        actions = quick_actions(self.tongy, lambda _user, module_key: module_key != "revenue_commission")
        self.assertNotIn("revenue", [item.key for item in actions])

    def test_non_sonny_admin_does_not_receive_content_actions(self):
        other_admin = dict(self.sonny, username="other-admin")
        self.assertEqual(plus_actions(other_admin, self.allow), ())


class SQLiteTestDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row

    def one(self, sql, params=()):
        return self.connection.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.connection.execute(sql, params).fetchall()

    def connect(self):
        return self.connection

    def release(self, _connection):
        return None


class MobileHomeDataTests(unittest.TestCase):
    def setUp(self):
        self.db = SQLiteTestDatabase()
        self.db.connection.executescript(
            """
            CREATE TABLE students (
              student_id TEXT PRIMARY KEY,
              passport_first_name TEXT,
              passport_last_name TEXT,
              deleted_at TEXT
            );
            CREATE TABLE parent_form_tokens (
              token TEXT PRIMARY KEY,
              submitted_at TEXT,
              invalidated_at TEXT,
              expires_at TEXT
            );
            CREATE TABLE contracts (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              contract_status TEXT,
              signed_at TEXT,
              deleted_at TEXT,
              voided_at TEXT
            );
            CREATE TABLE school_plan_reports (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              is_current INTEGER,
              invalidated_at TEXT
            );
            """
        )
        self.db.connection.executemany(
            "INSERT INTO students VALUES(?,?,?,NULL)",
            [
                ("S1", "Alice", "Able"),
                ("S2", "Bob", "Baker"),
                ("S3", "Cara", "Cole"),
                ("S4", "David", "Dunn"),
            ],
        )
        self.db.connection.executemany(
            "INSERT INTO parent_form_tokens VALUES(?,?,?,?)",
            [
                ("pending", None, None, "2999-01-01T00:00:00+11:00"),
                ("submitted", "2026-08-08", None, "2999-01-01T00:00:00+11:00"),
                ("invalid", None, "2026-08-08", "2999-01-01T00:00:00+11:00"),
            ],
        )
        self.db.connection.executemany(
            "INSERT INTO contracts VALUES(?,?,?,?,?,?)",
            [
                (1, "S1", "Sent", None, None, None),
                (2, "S2", "FullyExecuted", "2026-08-08", None, None),
                (3, "S3", "Voided", None, None, "2026-08-08"),
            ],
        )
        self.db.connection.executemany(
            "INSERT INTO school_plan_reports VALUES(?,?,?,?)",
            [(1, "S2", 1, None), (2, "S3", 0, None)],
        )
        self.db.connection.commit()
        self.user = {"username": "tongy"}

    def tearDown(self):
        self.db.connection.close()

    @staticmethod
    def allow(_user, _module_key):
        return True

    @staticmethod
    def load_stages(_connection):
        return tuple(
            SimpleNamespace(
                student_id=f"S{index}",
                student_name=name,
                stage=SimpleNamespace(key="school_plan", label="学校方案阶段", index=3),
            )
            for index, name in enumerate(
                ["Alice Able", "Bob Baker", "Cara Cole", "David Dunn"],
                start=1,
            )
        )

    def test_counts_existing_records(self):
        data = load_mobile_home_data(self.db, self.user, self.allow, self.load_stages)
        reminders = {item.key: item for item in data.reminders}
        self.assertEqual(reminders["parent_forms"].count, 1)
        self.assertEqual(reminders["contracts"].count, 1)
        self.assertEqual(reminders["school_plans"].count, 3)
        self.assertEqual(reminders["parent_forms"].href, "/parent-links?status=Pending")
        counts = {item.key: item.count for item in data.stages}
        self.assertEqual(counts["school_plan"], 4)

    def test_stage_summaries_include_all_canonical_stages_in_order(self):
        data = load_mobile_home_data(self.db, self.user, self.allow, self.load_stages)
        self.assertEqual(
            [(item.key, item.label) for item in data.stages],
            list(APPLICATION_STAGE_DEFINITIONS),
        )
        counts = {item.key: item.count for item in data.stages}
        self.assertEqual(counts["school_plan"], 4)
        self.assertEqual(counts["waiting_parent_form"], 0)
        self.assertEqual(counts["waiting_visa_result"], 0)
        self.assertEqual(
            [item.href for item in data.stages],
            [f"/students?stage={key}" for key, _label in APPLICATION_STAGE_DEFINITIONS],
        )

    def test_stage_summary_does_not_expose_student_names(self):
        data = load_mobile_home_data(self.db, self.user, self.allow, self.load_stages)
        self.assertFalse(hasattr(data.stages[0], "student_names"))

    def test_document_shortcuts_follow_existing_permissions(self):
        def deny_content(_user, module_key):
            return module_key != "content_center"

        data = load_mobile_home_data(self.db, self.user, deny_content, self.load_stages)
        self.assertNotIn("/content", [item.href for item in data.documents])
        self.assertIn("/file-manager", [item.href for item in data.documents])

    def test_one_failed_reminder_query_does_not_break_other_sections(self):
        self.db.connection.execute("DROP TABLE contracts")
        data = load_mobile_home_data(self.db, self.user, self.allow, self.load_stages)
        reminders = {item.key: item for item in data.reminders}
        self.assertEqual(reminders["contracts"].state, "unavailable")
        self.assertEqual(reminders["parent_forms"].count, 1)
        self.assertEqual(len(data.stages), len(APPLICATION_STAGE_DEFINITIONS))

    def test_loader_performs_no_database_writes(self):
        denied = []
        write_actions = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
        }

        def authorizer(action, _arg1, _arg2, _database, _trigger):
            if action in write_actions:
                denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.db.connection.set_authorizer(authorizer)
        try:
            data = load_mobile_home_data(self.db, self.user, self.allow, self.load_stages)
        finally:
            self.db.connection.set_authorizer(None)
        self.assertEqual(denied, [])
        self.assertEqual(len(data.reminders), 3)

    def test_home_loader_hides_cross_module_data_when_access_is_denied(self):
        stage_loader_calls = []
        database_connect_calls = []
        original_connect = self.db.connect
        self.db.connect = lambda: database_connect_calls.append(True) or original_connect()
        data = load_mobile_home_data(
            self.db,
            {"username": "restricted"},
            lambda _user, _module_key: False,
            lambda _connection: stage_loader_calls.append(True) or (),
        )
        self.assertEqual(data.reminders, ())
        self.assertEqual(data.stages, ())
        self.assertEqual(data.documents, ())
        self.assertEqual(stage_loader_calls, [])
        self.assertEqual(database_connect_calls, [])


class MobileRenderingTests(unittest.TestCase):
    def setUp(self):
        self.user = {
            "username": "tongy",
            "display_name": "Tong <Yu>",
            "role_name": "Manager + Student Cases",
        }

    @staticmethod
    def allow(_user, _module_key):
        return True

    def test_navigation_has_five_items_but_no_account_entry(self):
        html = render_bottom_navigation(
            bottom_actions(self.user, self.allow),
            active="home",
        )
        self.assertEqual(html.count('class="mobile-bottom-nav__item'), 5)
        self.assertEqual(html.count('<img class="mobile-nav-icon"'), 4)
        self.assertEqual(html.count('<svg class="mobile-nav-icon mobile-nav-icon--plus"'), 1)
        self.assertIn('aria-current="page"', html)
        self.assertIn('href="#mobile-plus-sheet"', html)
        self.assertNotIn("/account", html)
        self.assertNotIn("我的", html)
        self.assertNotIn("avatar", html.lower())

    def test_navigation_uses_the_required_local_icon_identities(self):
        html = render_bottom_navigation(bottom_actions(self.user, self.allow), active="home")
        for icon_name in ("home", "student", "school", "visa"):
            self.assertIn(
                f'<img class="mobile-nav-icon" src="{outline_icon_path(icon_name)}" alt="" aria-hidden="true">',
                html,
            )
        self.assertNotIn(outline_icon_path("plus"), html)
        self.assertIn(
            '<svg class="mobile-nav-icon mobile-nav-icon--plus" viewBox="0 0 24 24" '
            'aria-hidden="true" focusable="false">',
            html,
        )
        self.assertIn('<path d="M12 5v14M5 12h14"', html)

    def test_mobile_shell_versions_assets_and_separates_top_account_from_plus_sheet(self):
        html = mobile_page(
            "首页",
            "<main>Body</main>",
            self.user,
            "csrf-token",
            "home",
            "/view-mode/desktop?return_to=%2F",
            bottom_actions(self.user, self.allow),
            plus_actions(self.user, self.allow),
        )
        self.assertIn('class="mobile-ui"', html)
        self.assertIn('<body class="mobile-page-body">', html)
        self.assertIn('/assets/mobile.css?v=177-20260810-1', html)
        self.assertIn('/assets/mobile.js?v=177-20260810-1', html)
        self.assertIn('class="mobile-header-search" href="/students"', html)
        self.assertIn('class="mobile-header-reminders" href="/reminders"', html)
        self.assertIn('class="mobile-header-account" href="#mobile-account-sheet"', html)
        self.assertIn(outline_icon_path("search"), html)
        self.assertIn(outline_icon_path("bell"), html)
        self.assertNotIn('class="mobile-account-entry"', html)
        self.assertNotIn('class="mobile-mode-switch"', html)
        self.assertNotIn("Tong &lt;Yu&gt;", html)
        plus_sheet = html.split('id="mobile-plus-sheet"', 1)[1].split('id="mobile-account-sheet"', 1)[0]
        account_sheet = html.split('id="mobile-account-sheet"', 1)[1]
        for forbidden in ('href="/account"', 'href="/account/password"', '切换 Desktop', 'action="/logout"'):
            self.assertNotIn(forbidden, plus_sheet)
        self.assertIn('href="/account"', account_sheet)
        self.assertIn('href="/account/password"', account_sheet)
        self.assertIn('href="/view-mode/desktop?return_to=%2F"', account_sheet)
        self.assertIn('action="/logout"', account_sheet)
        self.assertIn('name="csrf" value="csrf-token"', html)
        self.assertIn('class="mobile-safe-area"', html)

    def test_account_actions_reuse_existing_routes(self):
        from mobile.permissions import policy

        self.assertTrue(
            hasattr(policy, "account_actions"),
            "approved header requires existing account routes in the auxiliary sheet",
        )
        self.assertEqual(
            [action.href for action in policy.account_actions("/view-mode/desktop?return_to=%2F")],
            ["/account", "/account/password", "/view-mode/desktop?return_to=%2F"],
        )


class MobileHomeRenderingTests(unittest.TestCase):
    def setUp(self):
        self.user = {"username": "tongy", "display_name": "Tong Yu"}

    @staticmethod
    def allow(_user, _module_key):
        return True

    @staticmethod
    def sample_data():
        return MobileHomeData(
            reminders=(
                ReminderSummary("parent_forms", "未填写家长表单", 2, "/parent-links?status=Pending"),
                ReminderSummary("contracts", "未签署家长合同", 1, "/contracts?view=list&status=unsigned"),
                ReminderSummary("school_plans", "未制作学校方案", 3, "/students?alert=missing_school_plan"),
            ),
            stages=tuple(
                StageSummary(
                    key,
                    label,
                    4 if key == "school_plan" else 0,
                    f"/students?stage={key}",
                )
                for key, label in APPLICATION_STAGE_DEFINITIONS
            ),
            documents=(
                DocumentShortcut("files", "文件管理", "/file-manager"),
                DocumentShortcut("templates", "模板管理", "/templates"),
                DocumentShortcut("content", "内容资料", "/content"),
                DocumentShortcut("schools", "学校资料库", "/schools/library"),
            ),
        )

    def test_home_renders_approved_sections_and_real_routes(self):
        data = MobileHomeData(
            reminders=(
                ReminderSummary("parent_forms", "未填写家长表单", 2, "/parent-links?status=Pending"),
                ReminderSummary("contracts", "未签署家长合同", 1, "/contracts?view=list&status=unsigned"),
                ReminderSummary("school_plans", "未制作学校方案", 3, "/students?alert=missing_school_plan"),
            ),
            stages=(
                StageSummary(
                    "school_plan",
                    "学校方案阶段",
                    4,
                    "/students?stage=school_plan",
                ),
            ),
            documents=(DocumentShortcut("files", "文件管理", "/file-manager"),),
        )
        user = {"username": "tongy", "display_name": "Tong Yu"}
        def allow_without_revenue(_user, module_key):
            return module_key != "revenue_commission"

        html = render_mobile_home(data, user, allow_without_revenue)
        for heading in ("重要提醒", "快捷操作", "阶段分布", "常用文档信息"):
            self.assertIn(heading, html)
        self.assertNotIn("<h2>快捷操作</h2>", html)
        self.assertIn('class="mobile-reminder-panel__icon"', html)
        self.assertIn(f'src="{outline_icon_path("bell")}"', html)
        self.assertIn('class="mobile-section__action" href="/students"', html)
        self.assertIn("查看全部", html)
        self.assertIn('href="/parent-links"', html)
        self.assertIn('href="/contracts?view=new"', html)
        self.assertIn('href="/school-plan-reports/create"', html)
        for forbidden in ("敬请期待", "Revenue", "Commission", "佣金", "/mobile/revenue"):
            self.assertNotIn(forbidden, html)
        self.assertNotIn("Alice Able", html)
        self.assertNotIn("<ul>", html)
        self.assertNotIn("<li>", html)
        self.assertNotRegex(html, r"等\s+\d+\s+人")
        self.assertNotIn("Ivy Zhang", html)
        self.assertNotIn(">12<", html)

    def test_home_renders_unavailable_and_forbidden_states_honestly(self):
        data = MobileHomeData(
            reminders=(ReminderSummary("contracts", "未签署家长合同", None, "/contracts", "unavailable"),),
            stages=(),
            documents=(),
        )

        def deny(_user, _module_key):
            return False

        html = render_mobile_home(data, {"username": "tongy"}, deny)
        self.assertIn("暂时无法读取", html)
        self.assertIn("无权限", html)
        self.assertIn("暂无阶段数据", html)
        self.assertIn("暂无可用文档入口", html)

    def test_home_sections_render_professional_icons_and_tones(self):
        html = render_mobile_home(self.sample_data(), self.user, self.allow)
        self.assertEqual(html.count('class="mobile-reminder-cell'), 3)
        self.assertEqual(html.count('class="mobile-action '), 4)
        self.assertIn('href="/mobile/revenue"', html)
        self.assertIn('class="mobile-stage-list"', html)
        self.assertEqual(html.count('class="mobile-stage-card '), 9)
        self.assertNotIn("Alice Able", html)
        self.assertNotIn("<ul>", html)
        self.assertNotIn("<li>", html)
        self.assertNotRegex(html, r"等\s+\d+\s+人")
        for key, label in APPLICATION_STAGE_DEFINITIONS:
            self.assertIn(label, html)
            self.assertIn(f'href="/students?stage={key}"', html)
        self.assertIn("mobile-stage-card--empty", html)
        self.assertGreaterEqual(html.count('aria-hidden="true"'), 8)
        self.assertNotIn("<svg", html)
        self.assertNotIn('class="mobile-card-icon', html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_cards_use_local_icons_for_each_view_model_key(self):
        for key, icon_key in (
            ("parent_form", "parent-form-create"),
            ("contract", "contract-create"),
            ("school_plan", "school-plan-create"),
            ("revenue", "generic-document"),
        ):
            html = render_quick_action(MobileAction(key, "操作", "/destination"))
            self.assertIn(
                f'<img class="mobile-action__icon" src="{app_icon_path(icon_key)}" width="40" height="40" alt="" aria-hidden="true">',
                html,
            )

        html = render_mobile_home(self.sample_data(), self.user, self.allow)
        for href, icon_key in (
            ("/parent-links?status=Pending", "parent-form-reminder"),
            ("/contracts?view=list&amp;status=unsigned", "contract-reminder"),
            ("/students?alert=missing_school_plan", "school-plan-reminder"),
            ("/file-manager", "file"),
            ("/templates", "template"),
            ("/content", "content"),
            ("/schools/library", "school-library"),
        ):
            self.assertIn(f'href="{href}"', html)
            self.assertIn(f'src="{app_icon_path(icon_key)}"', html)
        self.assertIn(f'src="{outline_icon_path("chevron-right")}"', html)

    def test_rendered_app_icons_have_intrinsic_size_guards(self):
        html = render_mobile_home(self.sample_data(), self.user, self.allow)
        self.assertEqual(html.count('class="mobile-reminder-icon"'), html.count('class="mobile-reminder-icon" width="32" height="32"'))
        self.assertEqual(html.count('class="mobile-action__icon"'), html.count('class="mobile-action__icon" src=') )
        self.assertEqual(html.count('class="mobile-action__icon"'), html.count('width="40" height="40" alt="" aria-hidden="true">'))
        self.assertEqual(html.count('class="mobile-document-icon"'), html.count('class="mobile-document-icon" width="22" height="22"'))

    def test_stage_visual_semantics_classify_known_and_unknown_keys(self):
        for key, tone in (
            ("waiting_parent_form", "blue"),
            ("waiting_contract_signature", "orange"),
            ("school_plan", "purple"),
            ("waiting_submission", "purple"),
            ("waiting_school_offer", "green"),
            ("waiting_final_school", "purple"),
            ("waiting_coe", "orange"),
            ("visa_preparation", "green"),
            ("waiting_visa_result", "green"),
            ("truly_unknown_stage", "blue"),
        ):
            self.assertEqual(_stage_tone(key), tone)


class MobileRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module

        cls.app = app_module

    def make_handler(self):
        handler = object.__new__(self.app.CRMHandler)
        handler.user = {
            "id": 2,
            "username": "tongy",
            "display_name": "Tong Yu",
            "role_name": "Manager + Student Cases",
            "permissions_json": '["students.view","students.edit","contracts.manage"]',
            "module_permissions_json": '["dashboard","students","parent_form","student_contracts","contract_management_center","school_plan_reports","school_library","visa"]',
        }
        handler.csrf = "csrf-token"
        handler.headers = {"User-Agent": "Mozilla/5.0 (iPhone)", "Cookie": ""}
        handler.query = {}
        handler.parsed = urlparse("/")
        return handler

    def test_dispatch_registers_mobile_foundation_routes(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn(r'(r"^/view-mode/(mobile|desktop)$", self.view_mode_switch)', source)
        self.assertIn(r'(r"^/account$", self.mobile_account)', source)
        self.assertNotIn(r'(r"^/mobile/commission$", self.mobile_commission)', source)
        self.assertIn(r'(r"^/mobile/revenue$", self.mobile_revenue_workspace)', source)

    def test_request_mode_uses_cookie_and_user_agent(self):
        handler = self.make_handler()
        self.assertEqual(handler.request_view_mode(), "mobile")
        handler.headers["Cookie"] = "crm_view_mode=desktop"
        self.assertEqual(handler.request_view_mode(), "desktop")

    def test_view_mode_switch_sets_cookie_and_rejects_external_return(self):
        handler = self.make_handler()
        captured = {}
        handler.query = {"return_to": ["https://evil.example/path"]}
        handler.redirect = lambda location, cookie=None: captured.update(location=location, cookie=cookie)
        handler.view_mode_switch("GET", "mobile")
        self.assertEqual(captured["location"], "/mobile")
        self.assertIn("crm_view_mode=mobile", captured["cookie"])
        self.assertIn("SameSite=Lax", captured["cookie"])

    def test_desktop_switch_from_explicit_mobile_route_returns_to_root(self):
        import app

        handler = object.__new__(app.CRMHandler)
        handler.query = {"return_to": ["/mobile"]}
        captured = {}
        handler.redirect = lambda location, cookie=None: captured.update(location=location, cookie=cookie)

        handler.view_mode_switch("GET", "desktop")

        self.assertEqual(captured["location"], "/")
        self.assertIn("crm_view_mode=desktop", captured["cookie"])

    def test_dashboard_selects_mobile_before_desktop_work(self):
        handler = self.make_handler()
        handler.request_view_mode = lambda: "mobile"
        handler.mobile_dashboard = lambda method: ("mobile-result", method)
        original = self.app.run_reminder_engine_if_due
        calls = []
        self.app.run_reminder_engine_if_due = lambda: calls.append(True)
        try:
            self.assertEqual(handler.dashboard("GET"), ("mobile-result", "GET"))
        finally:
            self.app.run_reminder_engine_if_due = original
        self.assertEqual(calls, [True])

    def test_active_navigation_is_derived_for_primary_and_nested_business_routes(self):
        resolver = getattr(mobile_shell_module, "active_navigation_for_path", None)
        self.assertTrue(callable(resolver), "the shared Mobile shell must own route-to-active navigation state")
        expectations = {
            "/": "home",
            "/students": "students",
            "/students/20260102": "students",
            "/students/20260102/followup-notes": "students",
            "/contracts": "students",
            "/parent-links": "students",
            "/schools": "schools",
            "/schools/917": "schools",
            "/school-plan-reports/create": "schools",
            "/visa-management": "visa",
            "/students/20260102/visa-cases/7": "visa",
            "/visas/7": "visa",
            "/content": "",
        }
        for path, expected in expectations.items():
            with self.subTest(path=path):
                self.assertEqual(resolver(path), expected)

    def test_mobile_presentation_wraps_authenticated_business_pages_once(self):
        from ui import page

        handler = self.make_handler()
        handler.parsed = urlparse("/students/20260102?tab=contracts")
        renderer = getattr(handler, "prepare_html_content", None)
        self.assertTrue(callable(renderer), "authenticated Mobile business pages need one presentation boundary")
        desktop_html = page(
            "学生详情",
            '<section id="student-business-content">Student business content</section>',
            handler.user,
            handler.csrf,
            "students",
            {"ready": True},
            asset_version="mobile-nav-test",
        )

        mobile_html = renderer(desktop_html)
        mobile_html_again = renderer(mobile_html)

        self.assertEqual(mobile_html.count('class="mobile-bottom-nav"'), 1)
        self.assertEqual(mobile_html_again.count('class="mobile-bottom-nav"'), 1)
        self.assertIn('class="mobile-business-page ui-premium-v2"', mobile_html)
        self.assertIn('content="width=device-width,initial-scale=1,viewport-fit=cover"', mobile_html)
        self.assertIn('/assets/mobile.css?v=', mobile_html)
        self.assertIn('/assets/mobile.js?v=', mobile_html)
        self.assertIn('href="/students" aria-current="page"', mobile_html)
        self.assertIn('id="student-business-content"', mobile_html)
        self.assertIn('id="mobile-plus-sheet"', mobile_html)
        self.assertIn('新增跟进记录', mobile_html)
        self.assertIn('新建家长表单', mobile_html)
        self.assertIn('新建家长合同', mobile_html)
        self.assertIn('制作学校方案', mobile_html)
        self.assertIn('搜索学生', mobile_html)

    def test_desktop_and_standalone_documents_bypass_mobile_navigation_decoration(self):
        from ui import page

        handler = self.make_handler()
        renderer = getattr(handler, "prepare_html_content", None)
        self.assertTrue(callable(renderer))
        internal_html = page(
            "学校详情",
            "<section>School</section>",
            handler.user,
            handler.csrf,
            "schools",
            {"ready": True},
            asset_version="desktop-nav-test",
        )
        handler.headers["Cookie"] = "crm_view_mode=desktop"
        self.assertEqual(renderer(internal_html), internal_html)

        handler.headers["Cookie"] = "crm_view_mode=mobile"
        standalone_preview = "<!doctype html><html><body><main>PDF preview</main></body></html>"
        self.assertEqual(renderer(standalone_preview), standalone_preview)

    def test_mobile_new_contract_route_renders_shared_student_selector(self):
        from database import Database

        old_db = self.app.db
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "mobile-selector.db")
            database.initialize()
            database.execute(
                """INSERT INTO students(
                     student_id,passport_first_name,passport_last_name,current_status,
                     created_date,updated_at,form_data_json,source_token
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "260951",
                    "Mobile",
                    "Student",
                    "Active",
                    "2026-08-09T10:00:00+10:00",
                    "2026-08-09T10:00:00+10:00",
                    "{}",
                    "parent-form-token",
                ),
            )
            handler = self.make_handler()
            handler.query = {"view": ["new"]}
            handler.parsed = urlparse("/contracts?view=new")
            captured = {}
            handler.message = lambda: ("", "")
            handler.send_html = lambda content, status=200, headers=None: captured.update(
                html=content, status=int(status)
            )
            handler.error_page = lambda message, status=400: captured.update(
                error=message, status=int(status)
            )
            self.app.db = database
            try:
                with mock.patch.object(self.app.nas, "status", return_value={"ready": True}):
                    handler.contract_management_center("GET")
            finally:
                self.app.db = old_db

        self.assertEqual(captured["status"], 200)
        self.assertIn("推荐新学生", captured["html"])
        self.assertIn("选择其他学生", captured["html"])
        self.assertIn("Mobile Student", captured["html"])
        self.assertIn('data-contract-student-search', captured["html"])
        self.assertNotIn("独立制作合同", captured["html"])
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")
        selector_css = css.split("Premium V2 Contract Student Selector Visual Map", 1)[1]
        self.assertIn("@media (max-width: 760px)", selector_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", selector_css)

    def test_account_page_and_non_admin_revenue_denial_are_presentation_only(self):
        handler = self.make_handler()
        captured = []
        handler.send_html = lambda html, *args, **kwargs: captured.append(html)
        handler.mobile_account("GET")
        account_html = captured.pop()
        self.assertIn("Tong Yu", account_html)
        self.assertIn("Manager + Student Cases", account_html)
        self.assertIn('/account/password', account_html)
        self.assertIn('/logout', account_html)
        denied = {}
        handler.error_page = lambda message, status=400: denied.update(
            message=message, status=int(status)
        )
        handler.mobile_revenue_workspace("GET")
        self.assertEqual(403, denied["status"])
        self.assertNotIn("Revenue", denied["message"])
        self.assertNotIn("Commission", denied["message"])


class MobileAssetContractTests(unittest.TestCase):
    def test_plus_actions_remain_reachable_without_javascript(self):
        actions = plus_actions({"username": "tongy"}, lambda *_: True)
        navigation = render_bottom_navigation(bottom_actions({}, lambda *_: True), "home")
        sheet = render_plus_sheet(actions)
        self.assertIn('href="#mobile-plus-sheet"', navigation)
        self.assertNotIn(" hidden", sheet)
        self.assertIn('href="/parent-links"', sheet)
        for forbidden in ("账户设置", "修改密码", "切换 Desktop", "退出登录"):
            self.assertNotIn(forbidden, sheet)

    def test_plus_sheet_markup_keeps_backdrop_out_of_focus_and_panel_focusable(self):
        sheet = render_plus_sheet(plus_actions({"username": "tongy"}, lambda *_: True))
        self.assertIn(
            'class="mobile-plus-sheet__backdrop" href="#" data-mobile-plus-close '
            'tabindex="-1" aria-hidden="true"',
            sheet,
        )
        self.assertIn('data-mobile-plus-panel tabindex="-1"', sheet)

    def test_mobile_css_is_scoped_and_phone_safe(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        self.assertIn(".mobile-ui", css)
        self.assertIn("env(safe-area-inset-bottom)", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("position: fixed", css)
        for forbidden in ("\nbody {", "\ntable {", "\nbutton {", "\na {"):
            self.assertNotIn(forbidden, css)

    def test_mobile_business_pages_reserve_dynamic_safe_area_clearance(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        self.assertIn("--mobile-bottom-nav-clearance", css)
        self.assertIn("min-height: 100dvh", css)
        self.assertIn(".mobile-business-page .app-shell > .main", css)
        business_rule = css.split(".mobile-business-page .app-shell > .main {", 1)[1].split("}", 1)[0]
        self.assertIn("padding-bottom:", business_rule)
        self.assertIn("env(safe-area-inset-bottom)", business_rule)

    def test_centre_plus_is_explicit_red_on_white_without_image_filter_dependency(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        plus_selector = ".mobile-ui .mobile-bottom-nav__item--plus .mobile-nav-icon--plus"
        self.assertIn(f"{plus_selector} {{", css)
        circle_rule = css.split(
            ".mobile-ui .mobile-bottom-nav__item--plus .mobile-bottom-nav__icon {", 1
        )[1].split("}", 1)[0]
        plus_rule = css.split(f"{plus_selector} {{", 1)[1].split("}", 1)[0]
        self.assertIn("background: #fff", circle_rule)
        self.assertIn("color: var(--mobile-plus-red)", circle_rule)
        self.assertIn("stroke: currentColor", plus_rule)
        self.assertIn("filter: none", plus_rule)
        self.assertNotIn("invert(", plus_rule)

    def test_stage_distribution_is_three_columns_without_horizontal_scroll(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        stage_rule = css.split(".mobile-ui .mobile-stage-list {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", stage_rule)
        self.assertIn("grid-auto-flow: row", stage_rule)
        self.assertIn("grid-auto-columns: auto", stage_rule)
        self.assertIn("overflow-x: visible", stage_rule)
        self.assertIn("scroll-snap-type: none", stage_rule)
        self.assertNotIn("repeat(4", stage_rule)

    def test_stage_cards_can_shrink_inside_three_column_tracks(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        stage_card_rule = css.split(".mobile-ui .mobile-stage-card {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 0", stage_card_rule)

    def test_empty_stage_heading_uses_muted_text_color(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        selector = ".mobile-ui .mobile-stage-card--empty .mobile-stage-card__heading"
        self.assertIn(f"{selector} {{", css)
        heading_rule = css.split(f"{selector} {{", 1)[1].split("}", 1)[0]
        self.assertIn("color: var(--mobile-muted)", heading_rule)

    def test_approved_icon_sizes_and_dense_grids_are_fixed(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        def rule(selector):
            marker = f"{selector} {{"
            self.assertIn(marker, css, f"missing explicit production rule for {selector}")
            return css.split(marker, 1)[1].split("}", 1)[0]

        reminder_grid = rule(".mobile-ui .mobile-reminder-grid")
        quick_grid = rule(".mobile-ui .mobile-quick-grid")
        document_grid = rule(".mobile-ui .mobile-document-grid")
        reminder_icon = rule(".mobile-ui .mobile-reminder-icon")
        quick_icon = rule(".mobile-ui .mobile-action__icon")
        document_icon = rule(".mobile-ui .mobile-document-icon")
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", reminder_grid)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", quick_grid)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", document_grid)
        self.assertIn("width: 32px", reminder_icon)
        self.assertIn("width: 40px", quick_icon)
        self.assertIn("width: 22px", document_icon)
        for rule in (reminder_icon, quick_icon, document_icon):
            self.assertIn("object-fit: contain", rule)

    def test_narrow_phone_keeps_reminders_and_stages_on_one_page(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        narrow = css.split("@media (max-width: 380px) {", 1)[1].split(
            "@media (orientation: landscape)", 1
        )[0]
        self.assertNotIn(".mobile-reminder-grid", narrow)
        self.assertNotIn(".mobile-stage-list", narrow)
        self.assertNotIn("overflow-x", narrow)
        self.assertNotRegex(
            narrow,
            r"\.mobile-stage-list\s*\{[^}]*grid-template-columns:\s*repeat\((?:1|2|4),",
        )

    def test_mobile_visual_tokens_and_tones_are_scoped(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        for token in ("--mobile-primary", "--mobile-orange", "--mobile-purple", "--mobile-green"):
            self.assertIn(token, css)
        for tone in ("blue", "orange", "purple", "green"):
            self.assertIn(f".mobile-ui .mobile-stage-card--{tone}", css)

    def test_inactive_bottom_navigation_text_meets_wcag_aa_contrast(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        nav_rule = css.rsplit(".mobile-ui .mobile-bottom-nav__item {", 1)[1].split("}", 1)[0]
        color_match = re.search(r"color:\s*(#[0-9a-fA-F]{6}|var\(--[a-z-]+\))", nav_rule)
        self.assertIsNotNone(color_match)
        color = color_match.group(1)
        if color.startswith("var("):
            token = color[4:-1]
            token_match = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})", css)
            self.assertIsNotNone(token_match)
            color = token_match.group(1)

        channels = [int(color[index:index + 2], 16) for index in (1, 3, 5)]

        def linearize(channel):
            value = channel / 255
            return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

        luminance = sum(weight * linearize(channel) for weight, channel in zip((0.2126, 0.7152, 0.0722), channels))
        contrast = 1.05 / (luminance + 0.05)
        self.assertGreaterEqual(contrast, 4.5)

    def test_inactive_bottom_navigation_icons_use_neutral_gray_filter(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        icon_rule = css.split(".mobile-ui .mobile-nav-icon {", 1)[1].split("}", 1)[0]
        self.assertIn("filter: invert(", icon_rule)

    def test_mobile_interaction_motion_honors_reduced_motion(self):
        css = Path("assets/mobile.css").read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce) {", css)
        reduced_motion = css.split("@media (prefers-reduced-motion: reduce) {", 1)[1]
        self.assertIn(".mobile-ui .mobile-reminder-cell", reduced_motion)
        self.assertIn("transition: none", reduced_motion)
        self.assertIn(".mobile-ui a.mobile-bottom-nav__item:active", reduced_motion)
        self.assertIn("transform: none", reduced_motion)

    def test_mobile_javascript_is_presentation_only(self):
        script = Path("assets/mobile.js").read_text(encoding="utf-8")
        self.assertIn("data-mobile-sheet-open", script)
        self.assertIn("data-mobile-sheet", script)
        self.assertIn("aria-expanded", script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("XMLHttpRequest", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn(".submit(", script)

    def test_mobile_plus_sheet_contains_focus_and_restores_background_state(self):
        script = Path("assets/mobile.js").read_text(encoding="utf-8")
        for contract in (
            'event.key === "Tab"',
            "event.shiftKey",
            "panel.focus()",
            "element.inert = true",
            "state.wasInert",
            "state.hadInertAttribute",
            'removeAttribute("inert")',
        ):
            self.assertIn(contract, script)
        self.assertNotIn('sheet.querySelector("[data-mobile-plus-close], a")?.focus()', script)


class DesktopIsolationTests(unittest.TestCase):
    def test_desktop_shell_keeps_existing_structure_and_only_adds_mode_link(self):
        from ui import page

        user = {
            "username": "admin",
            "display_name": "System Admin",
            "role_name": "Admin",
            "module_permissions_json": "[]",
        }
        html = page("工作概览", '<section class="dashboard-hero">Desktop</section>', user, "csrf")
        self.assertIn('class="sidebar"', html)
        self.assertIn('class="topbar"', html)
        self.assertIn('class="dashboard-hero"', html)
        self.assertIn('href="/view-mode/mobile?return_to=%2F"', html)
        self.assertNotIn('/assets/mobile.css', html)
        self.assertNotIn('class="mobile-bottom-nav"', html)


class MobileDestinationFilterTests(unittest.TestCase):
    def test_existing_students_list_applies_mobile_stage_query_filter(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn('stage_filter = (self.query.get("stage") or [""])[0]', source)
        self.assertIn('if summary_map[row["student_id"]].stage_key == stage_filter', source)
        self.assertIn('{"selected" if stage_filter == stage else ""}', source)

    def test_existing_list_handlers_wire_mobile_filter_predicates(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn('missing_school_plan_predicate("s")', source)
        self.assertIn('contract_status_filter == "unsigned"', source)
        self.assertIn('self.contract_center_table("未签署家长合同", "unsigned")', source)

    def test_unsigned_contract_predicate_uses_existing_contract_states(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE contracts (
              id INTEGER PRIMARY KEY,
              contract_status TEXT,
              signed_at TEXT,
              deleted_at TEXT,
              voided_at TEXT
            );
            INSERT INTO contracts VALUES(1,'Draft',NULL,NULL,NULL);
            INSERT INTO contracts VALUES(2,'Sent',NULL,NULL,NULL);
            INSERT INTO contracts VALUES(3,'ParentSigned','2026-08-08',NULL,NULL);
            INSERT INTO contracts VALUES(4,'FullyExecuted','2026-08-08',NULL,NULL);
            INSERT INTO contracts VALUES(5,'Voided',NULL,NULL,'2026-08-08');
            """
        )
        rows = connection.execute(
            f"SELECT id FROM contracts c WHERE {unsigned_contract_predicate('c')} ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual([row[0] for row in rows], [1, 2])

    def test_missing_school_plan_predicate_uses_current_valid_reports(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE students (student_id TEXT PRIMARY KEY, deleted_at TEXT);
            CREATE TABLE school_plan_reports (
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              is_current INTEGER,
              invalidated_at TEXT
            );
            INSERT INTO students VALUES('S1',NULL);
            INSERT INTO students VALUES('S2',NULL);
            INSERT INTO students VALUES('S3',NULL);
            INSERT INTO school_plan_reports VALUES(1,'S2',1,NULL);
            INSERT INTO school_plan_reports VALUES(2,'S3',0,NULL);
            """
        )
        rows = connection.execute(
            f"SELECT student_id FROM students s WHERE s.deleted_at IS NULL AND {missing_school_plan_predicate('s')} ORDER BY student_id"
        ).fetchall()
        connection.close()
        self.assertEqual([row[0] for row in rows], ["S1", "S3"])


if __name__ == "__main__":
    unittest.main()

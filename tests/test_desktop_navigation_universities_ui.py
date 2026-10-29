import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from higher_education_admin_views import (
    render_australian_universities_card,
    render_australian_universities_list,
    render_university_detail,
    render_university_edit,
)
from mobile.mode import resolve_view_mode, view_mode_cookie
from ui import _nav_items, _user_modules


ROOT = Path(__file__).resolve().parents[1]


def admin_user():
    return {
        "id": 1,
        "username": "admin",
        "display_name": "Admin",
        "role_name": "Admin",
        "permissions_json": '["*"]',
        "module_permissions_json": "[]",
    }


class DesktopNavigationContractTests(unittest.TestCase):
    def test_desktop_sidebar_hides_he_entry_but_permission_and_route_remain(self):
        user = admin_user()
        html = _nav_items("dashboard", user)

        self.assertNotIn('href="/higher-education"', html)
        self.assertNotIn("高等教育申请", html)
        self.assertIn("higher_education", _user_modules(user))

        import app

        handler = object.__new__(app.CRMHandler)
        handler.query = {}
        self.assertEqual(handler.route_module_key("/higher-education"), "higher_education")
        self.assertEqual(
            handler.route_module_key("/higher-education/applications/7/offers"),
            "higher_education",
        )


class DesktopModeSelectionContractTests(unittest.TestCase):
    desktop_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/140"
    windows_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140"
    phone_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) Mobile"

    def test_legacy_mobile_cookie_cannot_pin_computer_to_mobile(self):
        for user_agent in (self.desktop_ua, self.windows_ua):
            with self.subTest(user_agent=user_agent):
                self.assertEqual(resolve_view_mode("crm_view_mode=mobile", user_agent), "desktop")

    def test_phone_stays_mobile_with_or_without_legacy_cookie(self):
        self.assertEqual(resolve_view_mode("", self.phone_ua), "mobile")
        self.assertEqual(resolve_view_mode("crm_view_mode=mobile", self.phone_ua), "mobile")

    def test_explicit_mobile_cookie_cannot_pin_desktop_root_to_mobile(self):
        cookie = view_mode_cookie("mobile")
        self.assertIn("crm_view_mode=mobile-explicit-v2", cookie)
        self.assertEqual(resolve_view_mode(cookie, self.desktop_ua, "/"), "desktop")

    def test_explicit_mobile_route_remains_mobile_on_desktop(self):
        cookie = view_mode_cookie("mobile")
        self.assertEqual(resolve_view_mode(cookie, self.desktop_ua, "/mobile"), "mobile")

    def test_narrow_desktop_identity_stays_desktop(self):
        self.assertEqual(resolve_view_mode("", self.desktop_ua, "/"), "desktop")

    def test_desktop_override_and_invalid_cookie_are_stable(self):
        self.assertEqual(resolve_view_mode("crm_view_mode=desktop", self.phone_ua), "desktop")
        self.assertEqual(resolve_view_mode("crm_view_mode=broken", self.desktop_ua), "desktop")
        self.assertEqual(resolve_view_mode("crm_view_mode=broken", self.phone_ua), "mobile")


class UniversitiesCardDirectoryTests(unittest.TestCase):
    universities = [
        {
            "id": 17,
            "provider_name": "A Very Long University Name That Must Wrap Without Losing Its Identity",
            "short_name": "AVLU",
            "state": "VIC",
            "city": "Melbourne",
            "active_status": "Active",
            "pathway_count": 4,
        },
        {
            "id": 29,
            "provider_name": "Regional University",
            "short_name": "",
            "state": "",
            "city": "",
            "active_status": "Inactive",
            "pathway_count": 0,
        },
    ]

    def render(self, can_manage=True, rows=None, *, query="", state="", status="", page=1, total=2):
        return render_australian_universities_list(
            self.universities if rows is None else rows,
            can_manage=can_manage,
            csrf="csrf-value",
            filters={"q": query, "state": state, "status": status},
            pagination={"page": page, "page_size": 12, "total": total, "pages": max(1, (total + 11) // 12)},
        )

    def test_visible_copy_is_universities_while_internal_route_stays_unchanged(self):
        card = render_australian_universities_card(42)
        html = self.render()

        self.assertIn(">Universities<", card)
        self.assertNotIn("Australian Universities", card + html)
        self.assertIn('href="/schools/universities"', card)
        self.assertIn("<h1>Universities</h1>", html)

    def test_directory_is_semantic_card_grid_not_table_or_list(self):
        html = self.render()

        self.assertIn('class="universities-directory-grid"', html)
        self.assertEqual(html.count('class="university-directory-card"'), 2)
        self.assertNotIn("<table", html)
        self.assertNotIn("<tr", html)
        self.assertNotIn("<ul", html)

    def test_each_card_uses_its_exact_record_id_for_view_and_edit(self):
        html = self.render(can_manage=True)

        self.assertIn('data-university-id="17"', html)
        self.assertIn('href="/schools/universities/17"', html)
        self.assertIn('data-university-id="29"', html)
        self.assertIn('href="/schools/universities/29"', html)
        self.assertNotIn("latest", html.lower())
        self.assertEqual(html.count(">View Details<"), 2)
        self.assertNotIn("View / Edit", html)

    def test_long_and_missing_fields_render_safe_overview_fallbacks(self):
        html = self.render()

        self.assertIn("A Very Long University Name That Must Wrap", html)
        self.assertIn("AVLU", html)
        self.assertIn("Location not recorded", html)
        self.assertIn("No logo", html)

    def test_search_filters_pagination_and_admin_create_are_preserved(self):
        html = self.render(query="Monash", state="VIC", status="Active", page=2, total=25)

        self.assertIn('name="q" value="Monash"', html)
        self.assertIn('<option value="VIC" selected>', html)
        self.assertIn('<option value="Active" selected>', html)
        self.assertIn('action="/higher-education/providers/create"', html)
        self.assertIn('name="provider_type" value="University"', html)
        self.assertIn('href="/schools/universities?q=Monash&amp;state=VIC&amp;status=Active&amp;page=1"', html)
        self.assertIn('href="/schools/universities?q=Monash&amp;state=VIC&amp;status=Active&amp;page=3"', html)

    def test_view_only_role_has_view_but_no_create_or_edit_copy(self):
        html = self.render(can_manage=False)

        self.assertIn(">View Details<", html)
        self.assertNotIn("View / Edit", html)
        self.assertNotIn('action="/higher-education/providers/create"', html)

    def test_empty_and_search_empty_states_are_distinct(self):
        empty = self.render(rows=[], total=0)
        search_empty = self.render(rows=[], query="No Such University", total=0)

        self.assertIn("No universities configured.", empty)
        self.assertIn("No universities match the current search or filters.", search_empty)


class UniversitiesRouteQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app

        cls.app = app

    def make_handler(self):
        handler = object.__new__(self.app.CRMHandler)
        handler.user = admin_user()
        handler.csrf = "csrf-value"
        handler.query = {"q": ["Monash"], "state": ["VIC"], "status": ["Active"], "page": ["2"]}
        handler.parsed = urlparse("/schools/universities?q=Monash&state=VIC&status=Active&page=2")
        handler.headers = {"User-Agent": "Desktop Test", "Cookie": "crm_view_mode=mobile"}
        handler.message = lambda: ("", "success")
        captured = {}
        handler.send_html = lambda value: captured.setdefault("html", value)
        return handler, captured

    def test_route_applies_parameterized_search_filters_and_stable_page(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def one(self, sql, params=()):
                self.calls.append(("one", sql, tuple(params)))
                return {"total": 25}

            def all(self, sql, params=()):
                self.calls.append(("all", sql, tuple(params)))
                return [UniversitiesCardDirectoryTests.universities[0]]

        fake = FakeDB()
        handler, captured = self.make_handler()
        original_db = self.app.db
        original_nas_status = self.app.nas.status
        self.app.db = fake
        self.app.nas.status = lambda: {"mode": "test", "ready": True}
        try:
            handler.australian_universities("GET")
        finally:
            self.app.db = original_db
            self.app.nas.status = original_nas_status

        count_call, rows_call = fake.calls
        self.assertEqual(count_call[0], "one")
        self.assertEqual(rows_call[0], "all")
        self.assertIn("u.provider_name LIKE ?", rows_call[1])
        self.assertEqual(
            rows_call[2][0:5],
            ("%Monash%", "%Monash%", "%Monash%", "VIC", "Active"),
        )
        self.assertEqual(rows_call[2][-2:], (12, 12))
        self.assertIn("Page 2 of 3", captured["html"])
        self.assertIn("Universities", captured["html"])

    def test_edit_route_requires_school_library_module_access(self):
        handler, captured = self.make_handler()
        handler.user = {**admin_user(), "module_permissions_json": '[{"module_key":"school_library","can_access":false}]'}
        handler.school_action_denied = lambda action: captured.update(denied=action)
        with patch.object(self.app, "can_access_module", return_value=False):
            handler.australian_university_edit("GET", "9")
        self.assertEqual(captured.get("denied"), "edit")


class UniversitiesCreateReturnTests(unittest.TestCase):
    def test_university_directory_create_returns_to_directory_without_changing_he_default(self):
        import app

        class FakeDB:
            @contextmanager
            def transaction(self):
                yield object()

            def audit(self, *args, **kwargs):
                return None

        handler = object.__new__(app.CRMHandler)
        handler.user = admin_user()
        handler.form = {
            "provider_name": "Candidate University",
            "provider_type": "University",
            "source_url": "",
            "evidence": "",
            "return_to": "/schools/universities",
        }
        captured = {}
        handler.flash_redirect = lambda target, message: captured.update(target=target, message=message)
        original_db = app.db
        app.db = FakeDB()
        try:
            with patch.object(app, "HigherEducationRepository") as repository:
                repository.return_value.create_provider.return_value = 101
                handler.higher_education_provider_create("POST")
        finally:
            app.db = original_db

        self.assertEqual(captured["target"], "/schools/universities")


class UniversitiesResponsiveStyleTests(unittest.TestCase):
    def test_directory_styles_are_page_scoped_and_responsive(self):
        css = (ROOT / "assets" / "style.css").read_text(encoding="utf-8")

        self.assertIn(".universities-directory-grid", css)
        self.assertIn("repeat(auto-fit, minmax(min(100%, 260px), 1fr))", css)
        self.assertIn(".university-directory-card:focus-within", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("@media (max-width: 680px)", css)


class UniversityDetailViewEditTests(unittest.TestCase):
    university = {
        "id": 9,
        "provider_name": "CQ University",
        "short_name": "CQU",
        "state": "QLD",
        "city": "Rockhampton",
        "active_status": "Active",
        "source_url": "https://example.test/cqu",
    }
    context = "/schools/universities?q=CQ&state=QLD&status=Active&page=2"

    def detail(self, *, pathways=None, segments=None, courses=None, can_manage=True, course_filters=None, course_pagination=None):
        return render_university_detail(
            university=self.university,
            pathways=[] if pathways is None else pathways,
            segments=[] if segments is None else segments,
            courses=[] if courses is None else courses,
            providers=[],
            relationships=[],
            csrf="csrf-value",
            can_manage=can_manage,
            list_context=self.context,
            course_filters=course_filters,
            course_pagination=course_pagination,
        )

    def test_detail_is_read_only_by_default_with_consistent_copy(self):
        html = self.detail()
        self.assertIn("School Management / Universities / CQ University", html)
        self.assertIn("← Back to Universities", html)
        self.assertIn("Overview", html)
        self.assertIn("Pathways", html)
        self.assertIn("Courses / Programs", html)
        self.assertNotIn("AUSTRALIAN UNIVERSITIES", html)
        self.assertNotIn('action="/schools/universities/9/update"', html)

    def test_back_and_edit_preserve_allowlisted_directory_context(self):
        html = self.detail()
        self.assertIn('href="/schools/universities?q=CQ&amp;state=QLD&amp;status=Active&amp;page=2"', html)
        self.assertIn('href="/schools/universities/9/edit?return_to=%2Fschools%2Funiversities%3Fq%3DCQ%26state%3DQLD%26status%3DActive%26page%3D2"', html)

    def test_view_only_role_has_no_edit_or_management_actions(self):
        html = self.detail(can_manage=False)
        self.assertNotIn("Edit University", html)
        self.assertNotIn("Manage Pathways", html)
        self.assertNotIn("Add Course / Program", html)

    def test_pathway_and_course_empty_states_are_clear(self):
        html = self.detail()
        self.assertIn("No pathways have been added", html)
        self.assertIn("No courses or programs have been added", html)
        self.assertIn("Manage Pathways", html)
        self.assertIn("Add Course / Program", html)

    def test_pathway_provider_labels_and_populated_courses_render(self):
        pathways = [{"id": 4, "active_status": "Active", "display_name": "Diploma + Bachelor", "final_course_level": "Bachelor"}]
        segments = [{"pathway_combination_id": 4, "course_level": "Diploma", "provider_name": "Example College"}]
        courses = [{"id": 18, "course_name": "Bachelor of IT", "course_level": "Bachelor", "specialisation": "AI", "active_status": "Active"}]
        html = self.detail(pathways=pathways, segments=segments, courses=courses)
        self.assertIn("Diploma Provider", html)
        self.assertIn("Example College", html)
        self.assertIn("Bachelor of IT", html)
        self.assertIn("AI", html)

    def test_courses_support_search_and_pagination_at_scale(self):
        html = self.detail(
            courses=[{"id": 18, "course_name": "Bachelor of IT", "course_level": "Bachelor", "specialisation": "AI", "active_status": "Active"}],
            course_filters={"cq": "IT"},
            course_pagination={"page": 2, "pages": 4, "total": 75},
        )
        self.assertIn('name="cq" value="IT"', html)
        self.assertIn("Course page 2 of 4", html)
        self.assertIn('href="/schools/universities/9?return_to=', html)
        self.assertIn("cq=IT", html)
        self.assertIn("cp=1", html)
        self.assertIn("cp=3", html)

    def test_edit_is_independent_grouped_form_with_cancel_and_exact_id(self):
        html = render_university_edit(
            university=self.university,
            csrf="csrf-value",
            return_to="/schools/universities?q=CQ&state=QLD&status=Active&page=2",
        )
        self.assertIn('action="/schools/universities/9/update"', html)
        self.assertIn('data-unsaved-form', html)
        self.assertIn("University details", html)
        self.assertIn("Location and status", html)
        self.assertIn("Save Changes", html)
        self.assertIn('href="/schools/universities/9?return_to=%2Fschools%2Funiversities%3Fq%3DCQ%26state%3DQLD%26status%3DActive%26page%3D2"', html)
        self.assertIn('name="return_to" value="/schools/universities?q=CQ&amp;state=QLD&amp;status=Active&amp;page=2"', html)

    def test_detail_styles_prevent_horizontal_overflow(self):
        css = (ROOT / "assets" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".university-detail-page", css)
        self.assertIn("minmax(0, 1fr)", css)
        self.assertIn("overflow-x: auto", css)


if __name__ == "__main__":
    unittest.main()

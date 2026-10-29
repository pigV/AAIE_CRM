import unittest
import tempfile
import io
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


class ParentFormAuthenticationFlowTests(unittest.TestCase):
    def test_parent_portal_has_dedicated_wechat_login_route(self):
        self.assertIn(
            '(r"^/parent-form/([^/]+)/wechat-login$", self.parent_form_wechat_login)',
            APP_SOURCE,
        )

    def test_parent_form_oauth_state_and_callback_are_purpose_scoped(self):
        self.assertIn('"purpose": "parent_form"', APP_SOURCE)
        self.assertIn(
            'if state_payload.get("purpose") == "parent_form":',
            APP_SOURCE,
        )

    def test_parent_form_authentication_uses_only_parent_form_cookie_helpers(self):
        start = APP_SOURCE.index("def parent_form_login_required_page")
        end = APP_SOURCE.index("def parent_links", start)
        source = APP_SOURCE[start:end]
        self.assertIn("parent_form_cookie_name", source)
        self.assertIn("parent_form_cookie_value", source)
        self.assertNotIn("contract_signing_cookie", source)
        self.assertNotIn("school_plan_share_cookie", source)
        self.assertNotIn("visa_information_collection_cookie", source)

    def test_parent_portal_requires_dedicated_cookie_before_rendering_form(self):
        start = APP_SOURCE.index("def parent_portal")
        end = APP_SOURCE.index("def parent_form_status", start)
        source = APP_SOURCE[start:end]
        self.assertIn("parent_form_authenticated_openid", source)
        self.assertIn("parent_form_login_required_page", source)
        self.assertIn("load_draft", source)


class ParentFormDraftEndpointTests(unittest.TestCase):
    def test_parent_form_has_dedicated_draft_endpoint(self):
        self.assertIn(
            '(r"^/api/parent-form/([^/]+)/draft$", self.parent_form_draft)',
            APP_SOURCE,
        )

    def test_draft_endpoint_authenticates_and_delegates_to_service(self):
        start = APP_SOURCE.index("def parent_form_draft")
        end = APP_SOURCE.index("def parent_form_status", start)
        source = APP_SOURCE[start:end]
        self.assertIn("parent_form_authenticated_openid", source)
        self.assertIn("secure_parent_form_service().save_draft", source)
        self.assertIn("DraftRevisionConflict", source)
        self.assertNotIn("contract_signing", source)
        self.assertNotIn("school_plan", source)

    def test_parent_form_markup_has_revision_and_persistent_save_hooks(self):
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertIn("data-parent-form-draft-url", source)
        self.assertIn("data-parent-form-revision", source)
        self.assertIn("data-parent-form-save-status", source)
        self.assertIn("data-parent-form-save", source)
        self.assertIn("parent-draft-dock", source)

    def test_parent_form_page_loads_dedicated_browser_module(self):
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertIn("/assets/parent-form.js", source)

    def test_same_wechat_saves_encrypted_draft_and_other_cookie_is_rejected(self):
        import app as app_module
        from database import Database
        from parent_form_repository import ParentFormRepository
        from parent_form_service import parent_form_cookie_name, parent_form_cookie_value

        with tempfile.TemporaryDirectory() as folder:
            test_db = Database(Path(folder) / "draft-endpoint.db")
            test_db.initialize()
            conn = test_db.connect()
            try:
                conn.executescript(
                    (ROOT / "migrations" / "20260805_012_parent_form_secure_draft.sql").read_text(encoding="utf-8")
                )
                conn.executescript(
                    (ROOT / "migrations" / "20260805_013_parent_form_access_history.sql").read_text(encoding="utf-8")
                )
                now = datetime.now().astimezone()
                conn.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,created_by,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    ("draft-token", 1, "Draft", (now + timedelta(days=7)).isoformat(timespec="seconds"), 1, now.isoformat(timespec="seconds")),
                )
                conn.commit()
            finally:
                conn.close()

            old_db = app_module.db
            app_module.db = test_db
            try:
                service = app_module.secure_parent_form_service()
                service.bind_wechat("draft-token", {"openid": "openid-a"})
                secret = app_module.SECRET_PATH.read_bytes().strip()

                def request(cookie_openid, first_name):
                    body = urlencode({
                        "passport_first_name": first_name,
                        "account_password": "secret-123",
                        "revision": "0",
                        "save_source": "manual",
                    }).encode("utf-8")
                    handler = object.__new__(app_module.CRMHandler)
                    cookie_value = parent_form_cookie_value("draft-token", cookie_openid, secret)
                    handler.headers = {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Content-Length": str(len(body)),
                        "Cookie": f"{parent_form_cookie_name('draft-token')}={cookie_value}",
                    }
                    handler.rfile = io.BytesIO(body)
                    handler.client_address = ("127.0.0.1", 10000)
                    captured = {}
                    handler.json_response = lambda value, status=200: captured.update(status=status, json=value)
                    handler.parent_form_draft("POST", "draft-token")
                    return captured

                saved = request("openid-a", "Jesse")
                self.assertEqual(saved["status"], 200)
                self.assertEqual(saved["json"]["revision"], 1)
                ciphertext = ParentFormRepository(test_db).load_draft("draft-token").payload_encrypted
                self.assertNotIn("Jesse", ciphertext)
                self.assertNotIn("secret-123", ciphertext)
                self.assertEqual(service.load_draft("draft-token", "openid-a").values["passport_first_name"], "Jesse")

                blocked = request("openid-b", "Blocked")
                self.assertEqual(blocked["status"], 403)
                self.assertNotIn("Blocked", str(blocked["json"]))
                self.assertEqual(service.load_draft("draft-token", "openid-a").values["passport_first_name"], "Jesse")
            finally:
                app_module.db = old_db


class ParentFormSubmissionLockTests(unittest.TestCase):
    def test_submit_uses_authenticated_saved_draft_revision(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index("def parent_form_submit(self, method, token)")
        end = source.index("def template_management", start)
        handler = source[start:end]
        self.assertIn("parent_form_authenticated_openid", handler)
        self.assertIn("secure_parent_form_service().load_draft", handler)
        self.assertIn("draft_revision", handler)
        self.assertIn("HTTPStatus.CONFLICT", handler)

    def test_repeated_submission_rolls_back_second_student_and_deletes_draft(self):
        import app as app_module
        from database import Database
        from fields import FIELD_MAP
        from parent_form_repository import ParentFormRepository
        from parent_form_service import ParentFormExpired, ParentFormSubmitted

        class FailingNas:
            @staticmethod
            def create_student_tree(*_args, **_kwargs):
                raise RuntimeError("skip file creation in submission lock test")

        with tempfile.TemporaryDirectory() as folder:
            test_db = Database(Path(folder) / "submission-lock.db")
            test_db.initialize()
            conn = test_db.connect()
            try:
                for migration in (
                    "20260728_001_student_case_phase1.sql",
                    "20260805_012_parent_form_secure_draft.sql",
                    "20260805_013_parent_form_access_history.sql",
                    "20260811_017_workflow_application_cycles.sql",
                    "20260813_018_higher_education_application_module.sql",
                    "20260813_019_higher_education_module_v2.sql",
                ):
                    conn.executescript(
                        (ROOT / "migrations" / migration).read_text(encoding="utf-8")
                    )
                expires_at = (datetime.now().astimezone() + timedelta(days=7)).isoformat(timespec="seconds")
                conn.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,created_by,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    ("submit-token", 1, "Student", expires_at, 1, datetime.now().astimezone().isoformat(timespec="seconds")),
                )
                conn.commit()
            finally:
                conn.close()

            old_db, old_nas = app_module.db, app_module.nas
            app_module.db, app_module.nas = test_db, FailingNas()
            try:
                service = app_module.secure_parent_form_service()
                service.bind_wechat("submit-token", {"openid": "openid-parent"})
                values = {
                    key: ([] if meta["type"] == "checkbox" else "")
                    for key, meta in FIELD_MAP.items()
                }
                values.update({
                    "passport_last_name": "ZHANG",
                    "passport_first_name": "Jesse",
                    "preferred_cities": ["Melbourne"],
                    "application_stream": "school_education",
                })
                service.save_draft(
                    "submit-token",
                    "openid-parent",
                    values,
                    expected_revision=0,
                    source="manual",
                )
                token_row = ParentFormRepository(test_db).get_link("submit-token")

                first_student_id = app_module.create_student_from_form(values, token_row)
                with self.assertRaises(ParentFormSubmitted):
                    app_module.create_student_from_form(values, token_row)

                self.assertEqual(
                    test_db.one("SELECT count(*) count FROM students")["count"],
                    1,
                )
                self.assertEqual(
                    ParentFormRepository(test_db).get_link("submit-token")["student_id"],
                    first_student_id,
                )
                self.assertIsNone(ParentFormRepository(test_db).load_draft("submit-token"))

                expired_at = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat(timespec="seconds")
                test_db.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,created_by,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    ("expired-submit-token", 1, "Expired", expired_at, 1, datetime.now().astimezone().isoformat(timespec="seconds")),
                )
                expired_row = ParentFormRepository(test_db).get_link("expired-submit-token")
                with self.assertRaises(ParentFormExpired):
                    app_module.create_student_from_form(values, expired_row)
                self.assertEqual(
                    test_db.one("SELECT count(*) count FROM students")["count"],
                    1,
                )
            finally:
                app_module.db, app_module.nas = old_db, old_nas


class ParentFormAdminLinkTests(unittest.TestCase):
    def test_admin_link_generation_is_fixed_to_service_seven_days(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index("def parent_links(self, method)")
        end = source.index("def parent_link_revoke", start)
        handler = source[start:end]
        self.assertIn("secure_parent_form_service().create_link", handler)
        self.assertNotIn('self.form.get("days"', handler)
        self.assertNotIn('<option value="14">', handler)
        self.assertIn("固定 7 天", handler)

    def test_admin_list_masks_binding_and_keeps_draft_metadata(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index("def parent_links(self, method)")
        end = source.index("def parent_link_revoke", start)
        handler = source[start:end]
        self.assertIn("parent_form_access_bindings", handler)
        self.assertIn("parent_form_drafts", handler)
        self.assertIn("微信绑定", handler)
        self.assertIn("草稿状态", handler)
        self.assertIn("bound_wechat_openid", handler)
        self.assertIn("render_parent_form_access_summary", handler)


if __name__ == "__main__":
    unittest.main()

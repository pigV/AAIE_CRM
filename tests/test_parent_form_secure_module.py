import shutil
import sqlite3
import tempfile
import unittest
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from database import Database
from migration_runner import plan_migrations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "20260805_012_parent_form_secure_draft.sql"
ACCESS_HISTORY_MIGRATION_PATH = (
    ROOT / "migrations" / "20260805_013_parent_form_access_history.sql"
)
FORWARD_CHECK_PATH = (
    ROOT
    / "migrations"
    / "checks"
    / "20260805_012_parent_form_secure_draft_forward_check.sql"
)


class ParentFormSecureMigrationTests(unittest.TestCase):
    def test_migration_creates_only_parent_form_binding_and_draft_tables(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY)")
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("parent_form_access_bindings", tables)
            self.assertIn("parent_form_drafts", tables)
            self.assertNotIn("contract_signing_links_copy", tables)
            self.assertNotIn("school_plan_report_links_copy", tables)

            binding_fk = conn.execute(
                """SELECT [table],[from],[to],on_delete
                   FROM pragma_foreign_key_list('parent_form_access_bindings')"""
            ).fetchone()
            draft_fk = conn.execute(
                """SELECT [table],[from],[to],on_delete
                   FROM pragma_foreign_key_list('parent_form_drafts')"""
            ).fetchone()
            self.assertEqual(
                binding_fk,
                ("parent_form_tokens", "token", "token", "CASCADE"),
            )
            self.assertEqual(
                draft_fk,
                ("parent_form_tokens", "token", "token", "CASCADE"),
            )
        finally:
            conn.close()

    def test_migration_package_is_discoverable_without_writing_formal_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            migrations = root / "migrations"
            checks = migrations / "checks"
            checks.mkdir(parents=True)
            shutil.copy2(MIGRATION_PATH, migrations / MIGRATION_PATH.name)
            shutil.copy2(FORWARD_CHECK_PATH, checks / FORWARD_CHECK_PATH.name)
            database = root / "parent-form-test.db"
            sqlite3.connect(database).close()

            plan = plan_migrations(database, migrations)

            self.assertEqual(
                [item["version"] for item in plan["will_apply"]],
                ["20260805_012"],
            )


class ParentFormRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.folder.name) / "parent-form.db")
        conn = self.database.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE parent_form_tokens (
                  token TEXT PRIMARY KEY,
                  consultant_id INTEGER NOT NULL,
                  student_hint TEXT,
                  expires_at TEXT NOT NULL,
                  submitted_at TEXT,
                  invalidated_at TEXT,
                  student_id TEXT,
                  created_by INTEGER,
                  created_at TEXT NOT NULL,
                  intake_type TEXT
                );
                """
            )
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            conn.executescript(
                ACCESS_HISTORY_MIGRATION_PATH.read_text(encoding="utf-8")
            )
            conn.executemany(
                """INSERT INTO parent_form_tokens(
                     token,consultant_id,student_hint,expires_at,created_at
                   ) VALUES(?,?,?,?,?)""",
                [
                    ("form-a", 1, "A", "2026-08-12T00:00:00+10:00", "2026-08-05T00:00:00+10:00"),
                    ("form-b", 1, "B", "2026-08-12T00:00:00+10:00", "2026-08-05T00:00:00+10:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        from parent_form_repository import ParentFormRepository

        self.repository = ParentFormRepository(self.database)

    def tearDown(self):
        self.folder.cleanup()

    def test_wechat_binding_is_isolated_by_parent_form_token(self):
        stamp = "2026-08-05T10:00:00+10:00"
        self.repository.bind_identity(
            "form-a",
            {"openid": "openid-a", "unionid": "union-a", "nickname": "Parent A"},
            stamp,
        )
        self.repository.bind_identity(
            "form-b",
            {"openid": "openid-b", "unionid": "union-b", "nickname": "Parent B"},
            stamp,
        )

        self.assertEqual(self.repository.get_binding("form-a")["bound_wechat_openid"], "openid-a")
        self.assertEqual(self.repository.get_binding("form-b")["bound_wechat_openid"], "openid-b")

    def test_existing_binding_cannot_be_replaced_by_different_wechat(self):
        from parent_form_repository import BindingIdentityConflict

        stamp = "2026-08-05T10:00:00+10:00"
        self.repository.bind_identity("form-a", {"openid": "openid-a"}, stamp)

        with self.assertRaises(BindingIdentityConflict):
            self.repository.bind_identity("form-a", {"openid": "openid-other"}, stamp)

        self.assertEqual(self.repository.get_binding("form-a")["bound_wechat_openid"], "openid-a")

    def test_draft_save_uses_optimistic_revision_and_rejects_stale_write(self):
        from parent_form_repository import DraftRevisionConflict

        first = self.repository.save_draft(
            "form-a",
            "encrypted-one",
            expected_revision=0,
            source="auto",
            stamp="2026-08-05T10:00:00+10:00",
        )
        second = self.repository.save_draft(
            "form-a",
            "encrypted-two",
            expected_revision=first.revision,
            source="manual",
            stamp="2026-08-05T10:01:00+10:00",
        )

        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.save_source, "manual")

        with self.assertRaises(DraftRevisionConflict) as error:
            self.repository.save_draft(
                "form-a",
                "stale-value",
                expected_revision=1,
                source="auto",
                stamp="2026-08-05T10:02:00+10:00",
            )

        self.assertEqual(error.exception.current_revision, 2)
        self.assertEqual(self.repository.load_draft("form-a").payload_encrypted, "encrypted-two")

    def test_repository_does_not_reference_other_wechat_business_tables(self):
        source = (ROOT / "parent_form_repository.py").read_text(encoding="utf-8")
        for forbidden in (
            "contract_signing_links",
            "school_plan_report_links",
            "visa_information_collection",
        ):
            self.assertNotIn(forbidden, source)

    def test_repository_regenerates_link_in_one_transaction_without_copying_state(self):
        stamp = "2026-08-05T10:00:00+10:00"
        self.repository.bind_identity("form-a", {"openid": "openid-a"}, stamp)
        self.repository.save_draft("form-a", "cipher", 0, "manual", stamp)

        replacement = self.repository.regenerate_link(
            "form-a",
            "form-new",
            "2026-08-12T10:00:00+10:00",
            created_by=1,
            stamp=stamp,
        )

        self.assertEqual(replacement["token"], "form-new")
        self.assertIsNotNone(self.repository.get_link("form-a")["invalidated_at"])
        self.assertIsNone(self.repository.get_binding("form-new"))
        self.assertIsNone(self.repository.load_draft("form-new"))


class ParentFormServiceTests(unittest.TestCase):
    def setUp(self):
        from parent_form_repository import ParentFormRepository

        self.folder = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.folder.name) / "parent-form-service.db")
        conn = self.database.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE parent_form_tokens (
                  token TEXT PRIMARY KEY,
                  consultant_id INTEGER NOT NULL,
                  student_hint TEXT,
                  expires_at TEXT NOT NULL,
                  submitted_at TEXT,
                  invalidated_at TEXT,
                  student_id TEXT,
                  created_by INTEGER,
                  created_at TEXT NOT NULL,
                  intake_type TEXT
                );
                """
            )
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            conn.executescript(
                ACCESS_HISTORY_MIGRATION_PATH.read_text(encoding="utf-8")
            )
            conn.executemany(
                """INSERT INTO parent_form_tokens(
                     token,consultant_id,student_hint,expires_at,created_at,intake_type
                   ) VALUES(?,?,?,?,?,?)""",
                [
                    ("parent-token-a", 1, "A", "2026-08-12T23:59:59+10:00", "2026-08-05T00:00:00+10:00", "ordinary_new_application"),
                    ("parent-token-b", 1, "B", "2026-08-12T23:59:59+10:00", "2026-08-05T00:00:00+10:00", "ordinary_new_application"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.repository = ParentFormRepository(self.database)
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=ZoneInfo("Australia/Melbourne"))

    def tearDown(self):
        self.folder.cleanup()

    @staticmethod
    def _encrypt(value):
        return b"cipher:" + urlsafe_b64encode(value)

    @staticmethod
    def _decrypt(value):
        if not value.startswith(b"cipher:"):
            raise ValueError("invalid test ciphertext")
        return urlsafe_b64decode(value[len(b"cipher:") :])

    def make_service(self, tokens=None):
        from parent_form_service import ParentFormService

        token_values = tokens or iter(["generated-parent-token"])
        return ParentFormService(
            self.repository,
            clock=lambda: self.now,
            token_factory=lambda: next(token_values),
            encrypt=self._encrypt,
            decrypt=self._decrypt,
        )

    def test_service_creates_and_regenerates_exactly_seven_day_links(self):
        service = self.make_service(iter(["token-one", "token-two"]))

        created = service.create_link(
            consultant_id=1,
            student_hint="Jesse",
            created_by=1,
            intake_type="ordinary_new_application",
        )
        regenerated = service.regenerate_link(created.token, created_by=1)

        expected = (self.now + timedelta(days=7)).isoformat(timespec="seconds")
        self.assertEqual(created.expires_at, expected)
        self.assertEqual(regenerated.expires_at, expected)
        self.assertNotEqual(created.token, regenerated.token)
        self.assertIsNone(self.repository.get_binding(regenerated.token))
        self.assertIsNone(self.repository.load_draft(regenerated.token))
        self.assertIsNotNone(self.repository.get_link(created.token)["invalidated_at"])

    def test_parent_form_identity_is_local_to_its_own_token(self):
        from parent_form_service import ParentFormIdentityMismatch

        service = self.make_service()
        service.bind_wechat("parent-token-a", {"openid": "openid_a", "nickname": "家长甲"})
        service.bind_wechat("parent-token-b", {"openid": "openid_b", "nickname": "家长乙"})

        self.assertEqual(service.authorize("parent-token-a", "openid_a").token, "parent-token-a")
        with self.assertRaises(ParentFormIdentityMismatch):
            service.authorize("parent-token-a", "openid_b")

    def test_service_encrypts_full_draft_and_never_persists_declaration(self):
        service = self.make_service()
        service.bind_wechat("parent-token-a", {"openid": "openid_a", "nickname": "家长甲"})

        result = service.save_draft(
            "parent-token-a",
            "openid_a",
            {
                "passport_first_name": "Jesse",
                "account_password": "secret-123",
                "declaration": "yes",
                "unknown": "drop",
            },
            expected_revision=0,
            source="manual",
        )

        stored = self.repository.load_draft("parent-token-a").payload_encrypted
        self.assertNotIn("Jesse", stored)
        self.assertNotIn("secret-123", stored)
        loaded = service.load_draft("parent-token-a", "openid_a")
        self.assertEqual(loaded.values["passport_first_name"], "Jesse")
        self.assertEqual(loaded.values["account_password"], "secret-123")
        self.assertNotIn("declaration", loaded.values)
        self.assertNotIn("unknown", loaded.values)
        self.assertEqual(result.revision, 1)

    def test_expired_revoked_and_submitted_links_reject_draft_access(self):
        from parent_form_service import ParentFormExpired, ParentFormRevoked, ParentFormSubmitted

        service = self.make_service()
        service.bind_wechat("parent-token-a", {"openid": "openid_a"})

        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE parent_form_tokens SET expires_at=? WHERE token='parent-token-a'",
                ((self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),),
            )
        with self.assertRaises(ParentFormExpired):
            service.load_draft("parent-token-a", "openid_a")

        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE parent_form_tokens SET expires_at=?,invalidated_at=? WHERE token='parent-token-a'",
                ((self.now + timedelta(days=1)).isoformat(timespec="seconds"), self.now.isoformat(timespec="seconds")),
            )
        with self.assertRaises(ParentFormRevoked):
            service.load_draft("parent-token-a", "openid_a")

        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE parent_form_tokens SET invalidated_at=NULL,submitted_at=? WHERE token='parent-token-a'",
                (self.now.isoformat(timespec="seconds"),),
            )
        with self.assertRaises(ParentFormSubmitted):
            service.save_draft(
                "parent-token-a", "openid_a", {"passport_first_name": "Blocked"}, 0, "auto"
            )

    def test_parent_form_cookie_helpers_are_token_scoped(self):
        from parent_form_service import parent_form_cookie_name, parent_form_cookie_value

        self.assertNotEqual(parent_form_cookie_name("token-a"), parent_form_cookie_name("token-b"))
        self.assertNotEqual(
            parent_form_cookie_value("token-a", "openid", b"secret"),
            parent_form_cookie_value("token-b", "openid", b"secret"),
        )

    def test_service_regenerates_expired_link_without_copying_old_state(self):
        service = self.make_service(iter(["fresh-token"]))
        service.bind_wechat("parent-token-a", {"openid": "openid_a"})
        service.save_draft(
            "parent-token-a",
            "openid_a",
            {"passport_first_name": "Old draft"},
            expected_revision=0,
            source="manual",
        )
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE parent_form_tokens SET expires_at=? WHERE token='parent-token-a'",
                ((self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),),
            )

        regenerated = service.regenerate_link(
            "parent-token-a",
            created_by=1,
            intake_type="ordinary_new_application",
        )

        self.assertEqual(regenerated.token, "fresh-token")
        self.assertIsNone(self.repository.get_binding("fresh-token"))
        self.assertIsNone(self.repository.load_draft("fresh-token"))


if __name__ == "__main__":
    unittest.main()

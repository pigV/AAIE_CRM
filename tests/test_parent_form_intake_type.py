from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FakeRepository:
    def __init__(self):
        self.rows = {}
        self.events = []

    def create_link(
        self,
        token,
        consultant_id,
        student_hint,
        expires_at,
        created_by,
        created_at,
        student_id=None,
        intake_type=None,
    ):
        row = {
            "token": token,
            "consultant_id": consultant_id,
            "student_hint": student_hint,
            "expires_at": expires_at,
            "student_id": student_id,
            "intake_type": intake_type,
        }
        self.rows[token] = row
        return row

    def regenerate_link(
        self,
        old_token,
        new_token,
        expires_at,
        created_by,
        stamp,
        intake_type=None,
    ):
        old = self.rows[old_token]
        row = dict(old, token=new_token, expires_at=expires_at, intake_type=intake_type)
        self.rows[new_token] = row
        return row

    def correct_intake_type(self, token, previous_type, new_type, changed_by, changed_at):
        current = self.rows[token]
        if current["intake_type"] != previous_type:
            raise sqlite3.IntegrityError("Parent intake correction gate failed")
        self.events.append((token, previous_type, new_type, changed_by, changed_at))
        current["intake_type"] = new_type
        return current

    def get_link(self, token):
        return self.rows.get(token)


class ParentFormIntakeTypeTests(unittest.TestCase):
    def setUp(self):
        from parent_form_service import ParentFormService

        self.repository = FakeRepository()
        self.tokens = iter(["token-one", "token-two", "token-three"])
        self.now = datetime(2026, 9, 4, 10, 0, tzinfo=ZoneInfo("Australia/Melbourne"))
        self.service = ParentFormService(
            self.repository,
            clock=lambda: self.now,
            token_factory=lambda: next(self.tokens),
            encrypt=lambda value: value,
            decrypt=lambda value: value,
        )

    def test_new_link_requires_an_explicit_approved_intake_type(self):
        from parent_form_service import ParentFormIntakeTypeInvalid

        with self.assertRaises(ParentFormIntakeTypeInvalid):
            self.service.create_link(1, "New student", 1)
        with self.assertRaises(ParentFormIntakeTypeInvalid):
            self.service.create_link(1, "New student", 1, intake_type="guessed")

        link = self.service.create_link(
            1,
            "New student",
            1,
            intake_type="new_student_transfer",
        )
        self.assertEqual("new_student_transfer", link.intake_type)

    def test_non_null_type_correction_keeps_token_and_appends_audit(self):
        created = self.service.create_link(
            1,
            "Correction",
            1,
            intake_type="ordinary_new_application",
        )

        corrected = self.service.correct_intake_type(
            created.token,
            "new_student_transfer",
            changed_by=9,
        )

        self.assertEqual(created.token, corrected.token)
        self.assertEqual("new_student_transfer", corrected.intake_type)
        self.assertEqual(
            [(created.token, "ordinary_new_application", "new_student_transfer", 9, self.now.isoformat(timespec="seconds"))],
            self.repository.events,
        )

    def test_historical_null_requires_explicit_type_only_for_regeneration(self):
        from parent_form_service import ParentFormIntakeTypeInvalid

        self.repository.rows["legacy-null"] = {
            "token": "legacy-null",
            "consultant_id": 1,
            "student_hint": "Legacy",
            "expires_at": "2026-09-01T00:00:00+10:00",
            "student_id": None,
            "intake_type": None,
        }
        with self.assertRaises(ParentFormIntakeTypeInvalid):
            self.service.correct_intake_type("legacy-null", "new_student_transfer", changed_by=1)
        with self.assertRaises(ParentFormIntakeTypeInvalid):
            self.service.regenerate_link("legacy-null", created_by=1)

        regenerated = self.service.regenerate_link(
            "legacy-null",
            created_by=1,
            intake_type="ordinary_new_application",
        )
        self.assertEqual("ordinary_new_application", regenerated.intake_type)

    def test_non_null_regeneration_preserves_type_without_guessing(self):
        created = self.service.create_link(
            1,
            "Existing type",
            1,
            intake_type="new_student_transfer",
        )
        regenerated = self.service.regenerate_link(created.token, created_by=1)
        self.assertEqual("new_student_transfer", regenerated.intake_type)

    def test_parent_link_page_exposes_explicit_create_regenerate_and_correction_controls(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('name="intake_type"', source)
        self.assertIn("parent_link_intake_type_correct", source)
        self.assertIn("普通新申请", source)
        self.assertIn("新学生转学", source)


if __name__ == "__main__":
    unittest.main()

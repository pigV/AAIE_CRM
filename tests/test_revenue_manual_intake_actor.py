"""Authenticated intake routes retain their actual operator identity."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app


class ManualIntakeActorTests(unittest.TestCase):
    def handler(self):
        return SimpleNamespace(user={"id":17}, form={}, require=lambda _p:True,
            flash_redirect=lambda *args:args, uploaded_for=lambda _k:SimpleNamespace(filename="student.xlsx",data=b"fixture"))

    def test_manual_and_excel_intake_forward_authenticated_operator(self):
        for route in ("student_new", "student_import"):
            received = []
            def create(_data, **kwargs):
                received.append(kwargs.get("actor_user_id"))
                return "S-NEW"
            with self.subTest(route=route), patch("app.create_student_from_form", side_effect=create), \
                    patch("app.normalize_form", return_value={}), patch("app.missing_student_fields", return_value=[]), \
                    patch("app.data_from_template_bytes", return_value={}), patch("app.validate_student_data", return_value=[]), \
                    patch.object(app.db, "audit"):
                getattr(app.CRMHandler, route)(self.handler(), "POST")
                self.assertEqual([17], received)

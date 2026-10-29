import json
import unittest
from unittest.mock import patch

import student_capabilities
from student_case_feature_flags import StudentCaseFeatureFlags


class StudentCapabilitiesTests(unittest.TestCase):
    def flags(self, *, read=True, transfer=True, progression=True):
        return StudentCaseFeatureFlags(
            read_enabled=read,
            transfer_write_enabled=transfer,
            progression_write_enabled=progression,
            completion_enabled=False,
        )

    def user(self, *permissions):
        return {"permissions_json": json.dumps(list(permissions))}

    def allowed(self, function, permissions, flags):
        with patch.object(student_capabilities, "_feature_flags", return_value=flags):
            return function(self.user(*permissions), {"student_id": "20260091"})

    def test_transfer_view_create_and_edit_require_exact_permissions(self):
        flags = self.flags()
        self.assertTrue(self.allowed(student_capabilities.can_view_transfer_cases, ("student_cases.view",), flags))
        self.assertTrue(self.allowed(student_capabilities.can_create_transfer_case, ("student_cases.create",), flags))
        self.assertTrue(self.allowed(student_capabilities.can_edit_transfer_case, ("student_cases.edit",), flags))
        self.assertTrue(self.allowed(student_capabilities.can_assign_transfer_case, ("student_cases.assign",), flags))
        for function in (
            student_capabilities.can_view_transfer_cases,
            student_capabilities.can_create_transfer_case,
            student_capabilities.can_edit_transfer_case,
            student_capabilities.can_assign_transfer_case,
        ):
            self.assertFalse(self.allowed(function, ("student_cases.everything",), flags))
            self.assertFalse(self.allowed(function, ("*",), flags))

    def test_progression_view_create_and_edit_require_exact_permissions(self):
        flags = self.flags()
        self.assertTrue(self.allowed(student_capabilities.can_view_progression_cases, ("progression_cases.view",), flags))
        self.assertTrue(self.allowed(student_capabilities.can_create_progression_case, ("progression_cases.create",), flags))
        self.assertTrue(self.allowed(student_capabilities.can_edit_progression_case, ("progression_cases.update",), flags))
        self.assertFalse(self.allowed(student_capabilities.can_edit_progression_case, ("progression_cases.stage",), flags))

    def test_view_does_not_imply_create_and_student_has_no_new_rule(self):
        flags = self.flags()
        user = self.user("student_cases.view", "progression_cases.view")
        with patch.object(student_capabilities, "_feature_flags", return_value=flags):
            self.assertTrue(student_capabilities.can_view_transfer_cases(user, None))
            self.assertFalse(student_capabilities.can_create_transfer_case(user, None))
            self.assertTrue(student_capabilities.can_view_progression_cases(user, {"student_id": "new"}))
            self.assertFalse(student_capabilities.can_create_progression_case(user, {"student_id": "new"}))

    def test_feature_flags_gate_read_and_write(self):
        self.assertFalse(self.allowed(student_capabilities.can_view_transfer_cases, ("student_cases.view",), self.flags(read=False)))
        self.assertFalse(self.allowed(student_capabilities.can_create_transfer_case, ("student_cases.create",), self.flags(transfer=False)))
        self.assertFalse(self.allowed(student_capabilities.can_edit_transfer_case, ("student_cases.edit",), self.flags(transfer=False)))
        self.assertFalse(self.allowed(student_capabilities.can_assign_transfer_case, ("student_cases.assign",), self.flags(transfer=False)))
        self.assertFalse(self.allowed(student_capabilities.can_create_progression_case, ("progression_cases.create",), self.flags(progression=False)))
        self.assertFalse(self.allowed(student_capabilities.can_edit_progression_case, ("progression_cases.update",), self.flags(progression=False)))

    def test_malformed_permissions_and_completion_permissions_fail_closed(self):
        flags = self.flags()
        with patch.object(student_capabilities, "_feature_flags", return_value=flags):
            self.assertFalse(student_capabilities.can_view_transfer_cases({"permissions_json": "not-json"}, None))
            completion_user = self.user("student_cases.complete", "student_cases.undo_complete")
            self.assertFalse(student_capabilities.can_view_transfer_cases(completion_user, None))
            self.assertFalse(student_capabilities.can_edit_transfer_case(completion_user, None))


if __name__ == "__main__":
    unittest.main()

"""Shared Student Detail capabilities for Transfer and Progression cases."""

import database
from student_case_feature_flags import read_student_case_feature_flags
import student_case_permissions


def _feature_flags():
    return read_student_case_feature_flags(database.setting)


def _flags(explicit):
    return explicit if explicit is not None else _feature_flags()


def can_view_transfer_cases(user, student, *, flags=None):
    del student
    return student_case_permissions.can_view_student_cases(user, _flags(flags))


def can_create_transfer_case(user, student, *, flags=None):
    del student
    return student_case_permissions.can_create_transfer_case(user, _flags(flags))


def can_edit_transfer_case(user, student, *, flags=None):
    del student
    return student_case_permissions.can_edit_student_case(user, _flags(flags), "transfer")


def can_assign_transfer_case(user, student, *, flags=None):
    del student
    return student_case_permissions.can_assign_student_case(
        user, _flags(flags), "transfer"
    )


def can_complete_transfer_case(user, student, *, flags=None):
    del student
    return student_case_permissions.can_complete_student_case(
        user, _flags(flags), "transfer"
    )


def can_view_progression_cases(user, student, *, flags=None):
    del student
    return student_case_permissions.can_view_progression_cases(user, _flags(flags))


def can_create_progression_case(user, student, *, flags=None):
    del student
    return student_case_permissions.can_create_progression_case(user, _flags(flags))


def can_edit_progression_case(user, student, *, flags=None):
    del student
    return student_case_permissions.can_progression_action(user, _flags(flags), "update")

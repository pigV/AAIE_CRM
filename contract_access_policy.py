"""Side-effect-free contract access decisions.

Contract access follows explicit capabilities instead of fixed role names so
approved derivative roles retain the contract access of their base role.
"""

import json


CONTRACT_MANAGE_PERMISSION = "contracts.manage"
CONTRACT_MODULE_KEYS = frozenset(
    {"student_contracts", "contract_management_center"}
)


def _json_string_set(raw_value):
    try:
        values = json.loads(raw_value or "[]")
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        return frozenset()
    return frozenset(values)


def can_manage_contracts(user):
    if not user:
        return False
    permissions = _json_string_set(user.get("permissions_json"))
    return "*" in permissions or CONTRACT_MANAGE_PERMISSION in permissions


def can_access_contract_module(user, module_key):
    if module_key not in CONTRACT_MODULE_KEYS or not can_manage_contracts(user):
        return False
    permissions = _json_string_set(user.get("permissions_json"))
    if "*" in permissions:
        return True
    modules = _json_string_set(user.get("module_permissions_json"))
    return module_key in modules

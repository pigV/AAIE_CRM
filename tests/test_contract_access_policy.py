import json
import unittest

import app


class ContractAccessPolicyTests(unittest.TestCase):
    def test_manager_derivative_keeps_enabled_contract_modules(self):
        user = {
            "role_name": "Manager + Student Cases",
            "permissions_json": json.dumps(["students.view", "contracts.manage"]),
            "module_permissions_json": json.dumps(
                ["students", "student_contracts", "contract_management_center"]
            ),
        }

        self.assertTrue(app.can_view_contract_features(user))
        self.assertTrue(app.can_access_module(user, "student_contracts"))
        self.assertTrue(app.can_access_module(user, "contract_management_center"))

    def test_contract_access_requires_business_and_module_permissions(self):
        business_only = {
            "role_name": "Manager + Student Cases",
            "permissions_json": json.dumps(["contracts.manage"]),
            "module_permissions_json": json.dumps(["students"]),
        }
        module_only = {
            "role_name": "Consultant + Student Cases",
            "permissions_json": json.dumps(["students.view"]),
            "module_permissions_json": json.dumps(["student_contracts"]),
        }

        self.assertTrue(app.can_view_contract_features(business_only))
        self.assertFalse(app.can_access_module(business_only, "student_contracts"))
        self.assertFalse(app.can_view_contract_features(module_only))
        self.assertFalse(app.can_access_module(module_only, "student_contracts"))

    def test_admin_wildcard_keeps_contract_access(self):
        admin = {
            "role_name": "Admin",
            "permissions_json": json.dumps(["*"]),
            "module_permissions_json": json.dumps([]),
        }

        self.assertTrue(app.can_view_contract_features(admin))
        self.assertTrue(app.can_access_module(admin, "student_contracts"))
        self.assertTrue(app.can_access_module(admin, "contract_management_center"))

    def test_malformed_permissions_fail_closed(self):
        user = {
            "role_name": "Manager + Student Cases",
            "permissions_json": "not-json",
            "module_permissions_json": json.dumps(
                ["student_contracts", "contract_management_center"]
            ),
        }

        self.assertFalse(app.can_view_contract_features(user))
        self.assertFalse(app.can_access_module(user, "student_contracts"))


if __name__ == "__main__":
    unittest.main()

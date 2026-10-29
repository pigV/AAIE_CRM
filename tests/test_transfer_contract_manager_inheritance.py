"""Regression: established Manager derivative retains Transfer contract access."""
import json
import unittest

from contract_access_policy import can_access_contract_module
from tests.test_transfer_contract_route_gate import module_function


class TransferContractManagerInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.check = module_function(
            'can_manage_transfer_contract',
            {'can_generate_contract': lambda user: can_access_contract_module(user, 'student_contracts')},
        )

    def actor(self, role, permissions=None, modules=None):
        return {
            'role_name': role,
            'permissions_json': json.dumps(['contracts.manage'] if permissions is None else permissions),
            'module_permissions_json': json.dumps(['student_contracts'] if modules is None else modules),
        }

    def test_admin_and_manager_keep_access(self):
        self.assertTrue(self.check(self.actor('Admin', ['*'], [])))
        self.assertTrue(self.check(self.actor('Manager')))

    def test_established_manager_derivative_keeps_access(self):
        self.assertTrue(self.check(self.actor('Manager + Student Cases')))

    def test_unapproved_roles_are_denied_even_with_contract_capability(self):
        for role in ('Staff', 'Consultant', 'Consultant + Student Cases',
                     'Assistant Manager', 'Manager Custom', 'Manager + Student Cases Extra', ''):
            with self.subTest(role=role):
                self.assertFalse(self.check(self.actor(role)))

    def test_missing_contract_capability_or_module_is_denied(self):
        for role in ('Manager', 'Manager + Student Cases'):
            with self.subTest(role=role):
                self.assertFalse(self.check(self.actor(role, ['students.view'])))
                self.assertFalse(self.check(self.actor(role, modules=['contract_management_center'])))

    def test_malformed_authority_and_missing_actor_fail_closed(self):
        self.assertFalse(self.check(None))
        for field in ('permissions_json', 'module_permissions_json'):
            for invalid in ('not-json', '{}', 'null', '[1]'):
                with self.subTest(field=field, invalid=invalid):
                    user = self.actor('Manager + Student Cases')
                    user[field] = invalid
                    self.assertFalse(self.check(user))

    def test_manager_derivative_generation_enters_existing_transfer_flow(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from tests.test_transfer_source_routes import handler_method
        prepare = Mock(return_value='existing-transfer-flow')
        handler = SimpleNamespace(
            user=self.actor('Manager + Student Cases'),
            form={'contract_type': 'TransferApplicationContract'},
            transfer_contract_prepare=prepare,
            error_page=Mock(side_effect=AssertionError('unexpected denial')),
        )
        namespace = {
            'can_generate_contract': lambda u: can_access_contract_module(u, 'student_contracts'),
            'can_manage_transfer_contract': self.check,
            'normalize_contract_type': lambda value: value,
            'restrict_contract_creation_for_student_transfer_context': lambda *args: False,
            'db': SimpleNamespace(one=lambda *args: {'student_id': 'S1'},
                                  connect=lambda: object(), _release_connection=lambda c: None),
            'student_higher_education_experience': lambda *args: {'contract_applicable': True},
        }
        method = handler_method('contract_generate', namespace)
        self.assertEqual('existing-transfer-flow', method(handler, 'POST', 'S1'))
        prepare.assert_called_once_with('S1')

    def test_staff_direct_generation_request_is_denied_before_business_access(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from tests.test_transfer_source_routes import handler_method
        handler = SimpleNamespace(
            user=self.actor('Staff'),
            form={'contract_type': 'TransferApplicationContract'},
            error_page=Mock(return_value=('denied', 403)),
            transfer_contract_prepare=Mock(side_effect=AssertionError('unexpected creation')),
        )
        method = handler_method('contract_generate', {
            'can_generate_contract': lambda u: can_access_contract_module(u, 'student_contracts'),
            'can_manage_transfer_contract': self.check,
            'normalize_contract_type': lambda value: value,
        })
        self.assertEqual(('denied', 403), method(handler, 'POST', 'S1'))
        handler.transfer_contract_prepare.assert_not_called()

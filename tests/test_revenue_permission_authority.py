import json
import sqlite3
import unittest

import ui
from revenue.access_policy import can_access_revenue, can_perform_revenue_action
from revenue.services import RevenueService
from revenue.client_payments import ClientPaymentService
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.ui_actions import RevenueUIActions


class RevenuePermissionAuthorityTests(unittest.TestCase):
    def test_configured_staff_grant_controls_menu_and_operational_access(self):
        for role in ('Manager + Student Cases', 'Staff', 'Finance', 'Custom Role'):
            for enabled in (False, True):
                with self.subTest(role=role, enabled=enabled):
                    user = {'id': 42, 'role_name': role, 'module_permissions_json': json.dumps(['revenue_commission'] if enabled else [])}
                    self.assertEqual(enabled, can_access_revenue(user))
                    self.assertEqual(enabled, 'revenue_commission' in ui._user_modules(user))
                    self.assertEqual(enabled, can_perform_revenue_action(user, 'billing/payment'))
                    self.assertFalse(can_perform_revenue_action(user, 'contracts/activate'))
                    self.assertFalse(can_perform_revenue_action(user, 'financial/refund'))
                    self.assertFalse(can_perform_revenue_action(user, 'financial/clawback'))

    def test_parent_anonymous_and_malformed_grants_fail_closed(self):
        for user in (None, {}, {'role_name':'Parent','module_permissions_json':'["revenue_commission"]'},
                     *({'role_name':'Staff','module_permissions_json':raw} for raw in ('bad','null','{}','"revenue_commission"','[{}]', None))):
            with self.subTest(user=user):
                self.assertFalse(can_access_revenue(user))
        self.assertTrue(can_access_revenue({'role_name':'Admin'}))

    def test_operator_layers_honor_server_authorization_without_admin_escalation(self):
        with sqlite3.connect(':memory:') as connection:
            for role, grant, enabled in [('Admin',False,True),('Staff',True,True),('Staff',False,False),('Parent',True,False)]:
                for layer in ('service','payment','orchestrator','ui'):
                    with self.subTest(role=role, grant=grant, layer=layer):
                        def call():
                            if layer=='service': return RevenueService._require_operator(role,grant)
                            if layer=='payment': return ClientPaymentService._require_operator(role,grant)
                            if layer=='orchestrator':return RevenueOperationalOrchestrator(connection,42,role,actor_has_revenue_access=grant)._require_operator()
                            return RevenueUIActions(connection,None,42,role,actor_has_revenue_access=grant)
                        if enabled: call()
                        else:
                            with self.assertRaises(PermissionError):call()
            with self.assertRaises(PermissionError):RevenueService._require_admin('Staff')

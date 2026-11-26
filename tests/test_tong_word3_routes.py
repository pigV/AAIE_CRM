"""The scoped billing handler must pass the staff refund grant to its renderer."""
from types import SimpleNamespace
from unittest.mock import patch
import app
from tests.test_revenue_client_refunds import RevenueClientRefundTests


class ScopedRefundRouteTests(RevenueClientRefundTests):
    def test_scoped_billing_handler_exposes_refund_for_authorized_employee(self):
        self._payment()
        handler = SimpleNamespace(
            user={'id':1,'role_name':'Manager','module_permissions_json':'["revenue_commission"]'},
            query={},csrf='test-csrf',
            _transfer_intake_panels=lambda sid: '',
            send_html=lambda body, **kwargs: body,
            error_page=lambda message, status: (message, status),
        )
        database = SimpleNamespace(connect=lambda: self.connection, _release_connection=lambda c: None)
        with patch.object(app,'db',database), patch.object(app,'page',lambda title,body,*args:body), patch.object(app.nas,'status',return_value={}):
            body = app.CRMLegacyHandler.revenue_case_billing(handler, 'GET', 'case-1')
        self.assertIn('登记退款', body)
        self.assertIn('客户收款与收据', body)
        self.assertNotIn('revenue-list-panel', body)

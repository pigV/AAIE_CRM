"""Local-only preference contract, no business submission or real transport."""
import unittest
from unittest.mock import patch
import agent_reach_gateway as gateway
from tests.test_agent_reach_monitoring import body,plan,overview

class ContentRangeContract(unittest.TestCase):
 def test_all_keyword_preferences(self):
  for value in ['7','30','90','180','365','all']:
   with self.subTest(value=value):
    self.assertEqual(gateway.validate_monitoring_request('monitoring_keyword_create',body(content_range=value))['content_range'],value)
    data=overview();data['keywords']=[plan(content_range=value)]
    self.assertEqual(gateway._project('monitoring_read',data)['keywords'][0]['content_range'],value)
 def test_legacy_read_defaults_to_30(self):
  self.assertEqual(gateway._project('monitoring_read',overview())['keywords'][0]['content_range'],'30')
 def test_legacy_write_does_not_force_new_preference(self):
  self.assertNotIn('content_range',gateway.validate_monitoring_request('monitoring_keyword_update',body(id=1)))
 def test_invalid_preference_blocked_before_transport(self):
  for value in [0,30,True,None,{},'31','http://example.invalid','token=fixture']:
   with self.subTest(value=value),patch('agent_reach_gateway.http.client.HTTPConnection') as connection:
    with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('monitoring_keyword_create',body(content_range=value))
    connection.assert_not_called()
 def test_no_content_range_in_search_submit(self):
  with patch('agent_reach_gateway.http.client.HTTPConnection') as connection:
   with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('search_submit',{'keyword':'教育','content_range':'30'})
   connection.assert_not_called()
 def test_no_xhs_collector_or_trends_preference(self):
  with patch('agent_reach_gateway.http.client.HTTPConnection') as connection:
   with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('trends_submit',{'platforms':['xiaohongshu']})
   with self.assertRaises(gateway.GatewayError):gateway.validate_monitoring_request('monitoring_trends_update',{'enabled':True,'auto_enabled':False,'schedule':None,'content_range':'30'})
   connection.assert_not_called()
 def test_corrupt_backend_range_fails_closed(self):
  data=overview();data['keywords']=[plan(content_range='bogus')]
  with self.assertRaises(gateway.GatewayError):gateway._project('monitoring_read',data)

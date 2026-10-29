import unittest
from agent_reach_gateway import _monitor_plan,_monitor_task,GatewayError
class PhaseContract(unittest.TestCase):
 def plan(self,**kw):return dict(id=1,keyword='测试',enabled=True,auto_enabled=False,schedule=None,running=True,**kw)
 def test_plan_phase(self):self.assertEqual(_monitor_plan(self.plan(execution_phase='preparing'))['execution_phase'],'preparing')
 def test_closed_enum(self):
  with self.assertRaises(GatewayError):_monitor_plan(self.plan(execution_phase='token=private'))
 def test_task_phase(self):
  self.assertEqual(_monitor_task(dict(kind='keyword',trigger='manual',status='running',execution_phase='preparing'))['execution_phase'],'preparing')
 def test_legacy(self):self.assertIsNone(_monitor_plan(self.plan()).get('execution_phase'))
if __name__=='__main__':unittest.main()

class SearchTimeoutContract(unittest.TestCase):
 def test_only_search_operation_gets_120_seconds(self):
  from unittest.mock import patch
  from agent_reach_gateway import FoundationGateway,GatewayError
  for operation,payload,expected in [('search_submit',{'keyword':'测试'},120),('monitoring_keyword_run',{'id':1},120),('monitoring_read',None,95),('trends_submit',{},95)]:
   with self.subTest(operation=operation),patch('agent_reach_gateway.http.client.HTTPConnection',side_effect=OSError('synthetic')) as factory:
    with self.assertRaises(GatewayError):FoundationGateway().request(operation,payload)
    self.assertEqual(factory.call_args.kwargs['timeout'],expected)

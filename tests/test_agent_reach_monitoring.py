"""Closed monitoring contract; mocked transport only."""
import io,json,unittest
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock,patch
import agent_reach_gateway as gateway
from agent_reach_controller import handle_agent_reach,ROUTES
from agent_reach_views import render_workspace

PATHS=[('GET','/agent-reach/api/monitoring'),('POST','/agent-reach/api/monitoring/keywords'),('POST','/agent-reach/api/monitoring/keywords/update'),('POST','/agent-reach/api/monitoring/keywords/delete'),('POST','/agent-reach/api/monitoring/keywords/run'),('GET','/agent-reach/api/monitoring/trends'),('POST','/agent-reach/api/monitoring/trends')]
class MonitoringGateTests(unittest.TestCase):
 def invoke(self,user,method,path,payload=None,csrf='test'):
  b=json.dumps(payload or {}).encode();h=SimpleNamespace(user=user,parsed=urlparse(path),csrf='test',headers={'Content-Type':'application/json','Content-Length':str(len(b)),'X-CSRF-Token':csrf},rfile=io.BytesIO(b),json_response=lambda value,status=200:(status,value));factory=Mock();factory.return_value.request.return_value={}
  return handle_agent_reach(h,method,Mock(),factory),factory
 def test_finite_routes_exist(self):
  for route in PATHS:self.assertIn(route,ROUTES)
 def test_unauthorized_all_monitoring_routes_before_gateway(self):
  for user,status in [(None,401),({'role_name':'Staff','module_permissions_json':'["content_center"]'},403)]:
   for method,path in PATHS:
    result,factory=self.invoke(user,method,path);self.assertEqual(result[0],status);factory.assert_not_called()
 def test_all_mutations_require_csrf(self):
  for method,path in PATHS:
   if method!='POST':continue
   result,factory=self.invoke({'role_name':'Admin'},method,path,csrf='wrong');self.assertEqual(result[0],403);factory.assert_not_called()
 def test_chinese_schedule_workspace(self):
  html=render_workspace('test')
  for text in ['关键词管理','新增关键词','趋势自动采集','每天','工作日','每周','每隔','自定义星期','墨尔本时间','最近任务']:
   self.assertIn(text,html)
  self.assertNotIn('cron',html.lower())

SCHEDULE={'frequency':'daily','time':'09:00','weekdays':[],'interval_days':None,'anchor_date':None,'timezone':'Australia/Melbourne'}
def plan(trends=False,**updates):
 p={'enabled':True,'auto_enabled':False,'schedule':dict(SCHEDULE),'last_run_at':None,'next_run_at':None,'last_status':None,'last_new_count':None,'running':False}
 if not trends:p.update(id=1,keyword='教育')
 p.update(updates);return p

def overview():return {'timezone':'Australia/Melbourne','keywords':[plan()],'trends':plan(True),'tasks':[]}

def body(**updates):
 p={'keyword':'教育','enabled':True,'auto_enabled':False,'schedule':dict(SCHEDULE)};p.update(updates);return p

class MonitoringGatewayTests(unittest.TestCase):
 def setUp(self):
  patcher=patch('socket.create_connection',side_effect=AssertionError('external network forbidden'));self.network=patcher.start();self.addCleanup(patcher.stop)
 def reject(self,operation,payload):
  with patch('agent_reach_gateway.http.client.HTTPConnection') as connection:
   with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request(operation,payload)
   connection.assert_not_called()
 def test_schedule_variants_and_booleans(self):
  for frequency,days,interval,anchor in [('daily',[],None,None),('weekdays',[],None,None),('weekly',[7],None,None),('custom_weekdays',[1,3,6],None,None),('interval_days',[],3,'2026-09-08')]:
   schedule={**SCHEDULE,'frequency':frequency,'weekdays':days,'interval_days':interval,'anchor_date':anchor}
   result=gateway.validate_monitoring_request('monitoring_keyword_create',body(schedule=schedule))
   self.assertEqual(result['schedule'],schedule)
   self.assertTrue(result['enabled']);self.assertFalse(result['auto_enabled'])
 def test_invalid_schedules_rejected_before_network(self):
  for changes in [{'timezone':'UTC'},{'time':'25:01'},{'time':'9:00'},{'weekdays':[0]},{'weekdays':[1,1]},{'frequency':'cron'},{'frequency':'weekly','weekdays':[]},{'frequency':'interval_days','interval_days':0,'anchor_date':'2026-09-08'},{'frequency':'interval_days','interval_days':2,'anchor_date':'2026-02-30'},{'frequency':'custom_weekdays','weekdays':[]}]:
   self.reject('monitoring_keyword_create',body(schedule={**SCHEDULE,**changes}))
 def test_identifiers_and_extra_fields_rejected_before_network(self):
  for value in [True,False,0,-1,1.5,'1','../1',None]:self.reject('monitoring_keyword_delete',{'id':value})
  for payload in [body(url='https://example.invalid'),body(enabled=1),body(auto_enabled='yes'),body(keyword='token=synthetic'),body(keyword=''),body(schedule={**SCHEDULE,'url':'https://example.invalid'})]:self.reject('monitoring_keyword_create',payload)
 def test_seeded_manual_plan_can_have_no_schedule(self):
  value={**overview(),"keywords":[plan(schedule=None)],"trends":plan(True,schedule=None)}
  projected=gateway._project("monitoring_read",value)
  self.assertIsNone(projected["keywords"][0]["schedule"])
  self.assertIsNone(gateway.validate_monitoring_request("monitoring_keyword_update",body(id=1,schedule=None))["schedule"])
  self.reject("monitoring_keyword_create",body(auto_enabled=True,schedule=None))
 def test_trends_frequency_is_restricted(self):
  payload={k:v for k,v in body().items() if k!="keyword"}
  for freq in ["interval_days","custom_weekdays"]:
   payload["schedule"]={**SCHEDULE,"frequency":freq,"weekdays":[1] if freq=="custom_weekdays" else [],"interval_days":2 if freq=="interval_days" else None,"anchor_date":"2026-09-08" if freq=="interval_days" else None}
   self.reject("monitoring_trends_update",payload)
 def test_legacy_120_character_keyword_remains_manageable(self):
  keyword="教"*120
  self.assertEqual(gateway.validate_monitoring_request("monitoring_keyword_create",body(keyword=keyword))["keyword"],keyword)
  self.assertEqual(gateway._project("monitoring_read",{**overview(),"keywords":[plan(keyword=keyword)]})["keywords"][0]["keyword"],keyword)
  self.reject("monitoring_keyword_create",body(keyword="教"*121))
 def test_keyword_projection_is_business_only(self):
  value=overview();value['keywords'][0]['raw_exception']='SECRET NOT TO EXPORT';value['backend_url']='http://localhost:8790'
  projected=gateway._project('monitoring_read',value)
  self.assertNotIn('SECRET',json.dumps(projected));self.assertNotIn('backend_url',projected)
  self.assertEqual(projected['keywords'][0]['run_mode'],'manual')
 def test_invalid_response_fails_closed(self):
  for changes in [{'running':'false'},{'enabled':1},{'last_status':'token=foo'},{'keyword':'http://localhost:8790'},{'last_new_count':-1},{'schedule':{**SCHEDULE,'timezone':'UTC'}}]:
   with self.assertRaises(gateway.GatewayError):gateway._project('monitoring_read',{**overview(),'keywords':[plan(**changes)]})
 def test_fixed_paths_one_send_no_redirect_and_no_forwarded_auth(self):
  from tests.test_agent_reach_gateway import Connection,Response
  for operation,request_body,reply,status in [('monitoring_read',None,overview(),200),('monitoring_keyword_create',body(),{'item':plan()},201),('monitoring_keyword_update',body(id=1),{'item':plan()},200),('monitoring_keyword_delete',{'id':1},{'deleted':True},200),('monitoring_trends_read',None,{'item':plan(True)},200),('monitoring_trends_update',{k:v for k,v in body().items() if k!='keyword'},{'item':plan(True)},200)]:
   conn=Connection(Response(reply,status=status))
   with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn):result=gateway.FoundationGateway().request(operation,request_body)
   self.assertEqual(len(conn.requests),1);self.assertEqual(conn.requests[0][1],gateway._OPERATIONS[operation][1]);self.assertNotIn('Authorization',conn.requests[0][3]);self.assertTrue(conn.closed)
  conn=Connection(Response({},status=302))
  with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn):
   with self.assertRaises(gateway.GatewayError):gateway.FoundationGateway().request('monitoring_read')
  self.assertEqual(len(conn.requests),1)
 def test_uncertain_mutation_not_retried(self):
  from tests.test_agent_reach_gateway import Connection,Response
  conn=Connection(Response(),TimeoutError('sensitive raw error'))
  with patch('agent_reach_gateway.http.client.HTTPConnection',return_value=conn):
   with self.assertRaises(gateway.GatewayError) as error:gateway.FoundationGateway().request('monitoring_keyword_create',body())
  self.assertEqual(error.exception.code,'request_uncertain');self.assertNotIn('sensitive',str(error.exception));self.assertEqual(len(conn.requests),1)

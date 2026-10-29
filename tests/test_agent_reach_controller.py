import ast
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock
from agent_reach_controller import handle_agent_reach, ROUTES
from agent_reach_permissions import can_access_agent_reach
import ui

ADMIN={'role_name':'Admin'}
STAFF={'role_name':'Content Staff','username':'sonnyz','module_permissions_json':'["content_center"]'}
GRANTED={'role_name':'Consultant','module_permissions_json':'["agent_reach"]'}
class ControllerTests(unittest.TestCase):
    def invoke(self,user,path,method='GET',payload=None,csrf='test'):
        body=json.dumps(payload or {}).encode()
        h=SimpleNamespace(user=user,parsed=urlparse(path),csrf='test',headers={'Content-Type':'application/json','Content-Length':str(len(body)),'X-CSRF-Token':csrf},rfile=io.BytesIO(body),json_response=lambda value,status=200:(status,value))
        factory=Mock();factory.return_value.request.return_value={'items':[]}
        shell=Mock(return_value=(200,'html'))
        result=handle_agent_reach(h,method,shell,factory)
        return result,factory,shell
    def test_denied_matrix(self):
        for user,status in [(None,401),(STAFF,403)]:
            for method,path in [('GET','/agent-reach')]+list(ROUTES):
                with self.subTest(user=user,path=path):
                    result,factory,shell=self.invoke(user,path,method)
                    self.assertEqual(result[0],status);factory.assert_not_called();shell.assert_not_called()
    def test_allowed_matrix(self):
        for user in [ADMIN,GRANTED]:
            for method,path in ROUTES:
                payload={'keyword':'教育'} if path.endswith('/search') else {}
                if '/monitoring/' in path and method == 'POST':
                    payload={'id':1} if path.endswith(('/delete','/run')) else {'enabled':True,'auto_enabled':False,'schedule':{'frequency':'daily','time':'09:00','weekdays':[],'interval_days':None,'anchor_date':None,'timezone':'Australia/Melbourne'}}
                    if '/keywords' in path and not path.endswith(('/delete','/run')):payload['keyword']='教育'
                    if path.endswith('/update'):payload['id']=1
                result,factory,_=self.invoke(user,path,method,payload)
                self.assertEqual(result[0],200);factory.return_value.request.assert_called_once()
            result,factory,shell=self.invoke(user,'/agent-reach')
            self.assertEqual(result[0],200);factory.assert_not_called();shell.assert_called_once()
    def test_csrf_and_extra_payload_rejected(self):
        for path in ['/agent-reach/api/search','/agent-reach/api/trends']:
            result,factory,_=self.invoke(ADMIN,path,'POST',{'keyword':'x'},csrf='wrong')
            self.assertEqual(result[0],403);factory.assert_not_called()
        result,factory,_=self.invoke(ADMIN,'/agent-reach/api/search','POST',{'keyword':'x','url':'http://example.invalid'})
        self.assertEqual(result[0],400);factory.assert_not_called()
    def test_no_proxy_or_deep_surface(self):
        for path in ['/agent-reach/api/detail','/agent-reach/api/comments','/agent-reach/api/query_self','/agent-reach/api/session','/agent-reach/api/login','/agent-reach/api/proxy','/agent-reach/api/search/runs?url=http://example.invalid']:
            result,factory,_=self.invoke(ADMIN,path)
            self.assertIn(result[0],[400,404]);factory.assert_not_called()
    def test_permission_is_strict_and_does_not_grant_admin(self):
        for value in ['"agent_reach"','{"agent_reach":true}','null','["agent_reach",1]','broken','["content_center"]']:
            self.assertFalse(can_access_agent_reach({'role_name':'Staff','module_permissions_json':value}))
        self.assertTrue(can_access_agent_reach(GRANTED))
        nav=ui._nav_items('agent_reach',GRANTED)
        self.assertIn('href="/agent-reach"',nav)
        self.assertNotIn('href="/settings/users"',nav)
    def test_actual_dispatch_hook_precedes_side_effects(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name in ('CRMHandler','CRMLegacyHandler') and any(isinstance(f,ast.FunctionDef) and f.name=='dispatch' for f in n.body))
        dispatch=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='dispatch')
        source=ast.unparse(dispatch)
        self.assertLess(source.index('handle_agent_reach('),source.index('self.touch_current_session('))
        self.assertLess(source.index('handle_agent_reach('),source.index('maybe_run_auto_backup('))
        self.assertIn('self.parsed.path.startswith(\'/agent-reach/\')',source)
    def test_real_dispatch_executes_permission_guard(self):
        import re
        from urllib.parse import parse_qs
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name in ('CRMHandler','CRMLegacyHandler') and any(isinstance(f,ast.FunctionDef) and f.name=='dispatch' for f in n.body))
        dispatch=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='dispatch')
        factory=Mock()
        def hooked(handler,method,shell):
            return handle_agent_reach(handler,method,shell,factory)
        env={'urlparse':urlparse,'parse_qs':parse_qs,'re':re,'GOOGLE_DRIVE_OAUTH_CALLBACK_PATH':'/unused','handle_agent_reach':hooked}
        exec(compile(ast.Module(body=[dispatch],type_ignores=[]),'isolated-real-dispatch','exec'),env)
        for user,status in [(None,401),(STAFF,403)]:
            for method,path in [('GET','/agent-reach')]+list(ROUTES):
                h=SimpleNamespace(path=path,server=SimpleNamespace(service_mode='admin'),current_user=lambda u=user:(u,'test'),json_response=lambda v,status=200:(status,v))
                self.assertEqual(env['dispatch'](h,method)[0],status)
        factory.assert_not_called()
if __name__=='__main__': unittest.main()

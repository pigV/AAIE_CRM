import unittest
import ast
from pathlib import Path
from types import SimpleNamespace


class RequestClassificationTests(unittest.TestCase):
    def test_async_worker_is_admitted_before_request_can_finish(self):
        import tempfile,threading
        from business_write_gate import Gate, GateBusy
        from release_business_mode import start_business_thread
        root=Path(__file__).resolve().parents[1]/'SAFEBOX/current-gate-20260909/write-gate/marker-tests'
        root.mkdir(parents=True,exist_ok=True);directory=Path(tempfile.mkdtemp(dir=root))
        gate=Gate(directory/'db',directory/'gate');gate.initialize_open()
        release=threading.Event();entered=threading.Event()
        def work():entered.set();release.wait(3)
        thread=start_business_thread(work,lambda:gate,name='synthetic-business')
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(GateBusy):gate.enable(timeout=.03)
            self.assertEqual('OPEN',gate.status()['state'])
        finally:release.set();thread.join(3)
        self.assertEqual('CLOSED',gate.enable()['state'])

    def test_late_worker_is_cancelled_and_never_replayed(self):
        from unittest.mock import patch
        from business_write_gate import MaintenanceError
        from release_business_mode import start_business_thread
        calls=[];threads=[]
        class DelayedThread:
            def __init__(self,*,target,**kw):self.target=target;threads.append(self)
            def start(self):pass
        event=SimpleNamespace(wait=lambda timeout:False,set=lambda:None,is_set=lambda:False)
        with patch('release_business_mode.threading.Thread',DelayedThread), patch('release_business_mode.threading.Event',return_value=event):
            with self.assertRaises(MaintenanceError):
                start_business_thread(lambda:calls.append('write'),lambda:None,name='late-synthetic')
            threads[0].target()
        self.assertEqual([],calls)

    def test_bootstrap_marker_is_single_use_and_verified(self):
        import tempfile
        from release_business_mode import create_bootstrap_marker, remove_bootstrap_marker, startup_readonly
        root=Path(__file__).resolve().parents[1]/'SAFEBOX/current-gate-20260909/write-gate/marker-tests'
        root.mkdir(parents=True,exist_ok=True)
        directory=Path(tempfile.mkdtemp(dir=root))
        gate=SimpleNamespace(directory=directory,status=lambda:{'state':'OPEN','generation':'a'*32})
        receipt=create_bootstrap_marker(gate,'1'*40)
        self.assertTrue(startup_readonly(gate))
        with self.assertRaises(FileExistsError):create_bootstrap_marker(gate,'1'*40)
        with self.assertRaises(RuntimeError):remove_bootstrap_marker(gate,receipt,release_verified=False)
        remove_bootstrap_marker(gate,receipt,release_verified=True)
        self.assertFalse(startup_readonly(gate))

    def test_explicit_read_routes(self):
        from release_business_mode import request_kind
        for path in ('/', '/health', '/students', '/students/12', '/visa-management',
                     '/visa-management/cases/3/workspace', '/documents/4/download',
                     '/students?search=synthetic', '/login'):
            self.assertEqual('read', request_kind('GET', path), path)

    def test_mutation_and_unknown_routes_fail_closed(self):
        from release_business_mode import request_kind
        for method,path in [('POST','/students'),('POST','/api/parent-form/token/submit'),
                            ('POST','/visa-management/cases/3/confirm'),
                            ('POST','/file-manager/upload'),('GET','/documents/4/preview/regenerate'),
                            ('GET','/unknown-writer'),('GET','/backup')]:
            self.assertEqual('business',request_kind(method,path))

    def test_only_canonical_auth_posts(self):
        from release_business_mode import request_kind
        for path in ('/login','/logout'):
            self.assertEqual('auth',request_kind('POST',path))
        self.assertEqual('business',request_kind('POST','/account/password'))

    def test_actual_health_reports_gate_capability(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'basic_health_payload')
        values = {'STAGING': None, 'DB_PATH': SimpleNamespace(is_file=lambda:True),
                  'APP_DIR':SimpleNamespace(is_dir=lambda:True),
                  'os':SimpleNamespace(access=lambda *a:True,R_OK=1,W_OK=2),
                  'nas':SimpleNamespace(status=lambda:{'ready':True}),
                  'now_iso':lambda:'synthetic', 'VERSION':'synthetic',
                  'APP_WRITE_GATE':SimpleNamespace(status=lambda:{'state':'CLOSED','generation':3})}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'actual-health','exec'),values)
        self.assertEqual({'state':'CLOSED','generation':3},values['basic_health_payload']('parent',8781)['business_write_gate'])


if __name__ == '__main__':
    unittest.main()

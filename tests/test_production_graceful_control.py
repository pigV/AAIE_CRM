"""Offline contract tests: no real launchd, process signals, services or app import."""
import ast
import importlib.util
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tools import production_launchd_control as legacy
try:
    from tools import production_graceful_control as graceful
except ImportError:
    graceful=None
ROOT=Path(__file__).resolve().parents[1]
HEAD='a'*40
NEW_HEAD='b'*40
class Backend:
    def __init__(self):
        self.state='running';self.pid=321;self.checkout=HEAD;self.events=[];self.tick=0.;self.behavior='normal';self.health_ok=True;self.identity_ok=True
    def head(self):return self.checkout
    def validate_job_policy(self,service):self.events.append('policy')
    def inspect(self,service):
        return graceful.Observation(True,self.pid if self.state=='running' else None,(self.pid,) if self.state=='running' else (), 'birth-1' if self.pid==321 else 'birth-2',self.identity_ok,0 if self.state=='idle' else None)
    def pid_exists(self,pid):return self.state=='running' and self.pid==pid
    def signal_interrupt(self,service,pid,expected_started_at):
        self.events.append(('signal',service,pid))
        if self.behavior=='normal':self.state='idle'
        if self.behavior=='replacement':self.pid=999
        if self.behavior=='signal_error':raise OSError('private fixture exception')
    def kickstart(self,service):
        self.events.append(('kickstart',service))
        if self.behavior=='start_error':raise OSError('private fixture exception')
        self.state='running';self.pid=654
    def health(self,service):return {'status':'ok' if self.health_ok else 'failed','service':service,'port':legacy.SERVICE_PORTS[service]}
    def monotonic(self):return self.tick
    def sleep(self,seconds):self.tick+=seconds
class Existence(unittest.TestCase):
    def test_controller_exists(self):self.assertIsNotNone(graceful,'No owned stop/start controller contract')
    def test_legacy_restart_cannot_force_kill(self):
        with patch.object(legacy,'job_is_loaded',return_value=True),patch.object(legacy,'run_launchctl') as launch:
            with self.assertRaises(RuntimeError):legacy.restart('admin')
        launch.assert_not_called()
@unittest.skipIf(graceful is None,'RED missing implementation')
class ControllerTests(unittest.TestCase):
    def setUp(self):self.backend=Backend();self.control=graceful.GracefulController(self.backend,timeout_seconds=2,poll_seconds=.1)
    def stop(self):return self.control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
    def test_stop_and_explicit_new_head_start_are_separate(self):
        receipt=self.stop();self.assertFalse(any(e[0]=='kickstart' for e in self.backend.events if isinstance(e,tuple)))
        self.backend.checkout=NEW_HEAD
        result=self.control.start(receipt,expected_head=NEW_HEAD)
        self.assertEqual(result['status'],'healthy');self.assertEqual(result['checkout_head'],NEW_HEAD)
        self.assertEqual(result['runtime_memory_sha'],'not_measured')
        self.assertEqual([e for e in self.backend.events if isinstance(e,tuple)],[('signal','parent',321),('kickstart','parent')])
    def test_without_idle_confirmation_no_signal(self):
        with self.assertRaises(graceful.GracefulControlError):self.control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=False)
        self.assertFalse(self.backend.events)
    def test_wrong_head_or_process_identity_never_signals(self):
        for mode in ('head','identity'):
            with self.subTest(mode=mode):
                self.backend=Backend();self.control=graceful.GracefulController(self.backend,timeout_seconds=2)
                if mode=='head':self.backend.checkout=NEW_HEAD
                else:self.backend.identity_ok=False
                with self.assertRaises(graceful.GracefulControlError):self.stop()
                self.assertFalse(any(isinstance(e,tuple) for e in self.backend.events))
    def test_ignore_interrupt_times_out_without_second_signal(self):
        self.backend.behavior='ignore'
        with self.assertRaisesRegex(graceful.GracefulControlError,'stop_timeout'):self.stop()
        self.assertEqual([e[0] for e in self.backend.events if isinstance(e,tuple)],['signal'])
    def test_replacement_pid_is_hold_without_second_signal(self):
        self.backend.behavior='replacement'
        with self.assertRaises(graceful.GracefulControlError):self.stop()
        self.assertEqual([e[0] for e in self.backend.events if isinstance(e,tuple)],['signal'])
    def test_signal_exception_is_safe_and_never_retried(self):
        self.backend.behavior='signal_error'
        with self.assertRaises(graceful.GracefulControlError) as caught:self.stop()
        self.assertNotIn('private',str(caught.exception));self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(graceful.GracefulControlError):self.stop()
        self.assertEqual([e[0] for e in self.backend.events if isinstance(e,tuple)],['signal'])
    def test_foreign_listener_or_unloaded_job_blocks_stop(self):
        for obs in [graceful.Observation(True,321,(888,),'birth-1',True),graceful.Observation(False,None,(),None,False)]:
            with self.subTest(obs=obs),patch.object(self.backend,'inspect',return_value=obs):
                with self.assertRaises(graceful.GracefulControlError):self.stop()
        self.assertFalse(any(isinstance(e,tuple) for e in self.backend.events))
    def test_inspection_error_fail_closed(self):
        with patch.object(self.backend,'inspect',side_effect=OSError('private')):
            with self.assertRaises(graceful.GracefulControlError):self.stop()
        self.assertFalse(any(isinstance(e,tuple) for e in self.backend.events))
    def test_listener_remaining_after_exit_blocks_start(self):
        receipt=self.stop()
        with patch.object(self.backend,'inspect',return_value=graceful.Observation(True,None,(888,),None,False)):
            with self.assertRaises(graceful.GracefulControlError):self.control.start(receipt,expected_head=HEAD)
        self.assertFalse(any(e[0]=='kickstart' for e in self.backend.events if isinstance(e,tuple)))
    def test_foreign_receipt_is_never_accepted(self):
        receipt=self.stop();other=graceful.GracefulController(self.backend)
        with self.assertRaises(graceful.GracefulControlError):other.start(receipt,expected_head=HEAD)
    def test_start_attempt_consumed_before_error_and_no_auto_recovery(self):
        receipt=self.stop();self.backend.behavior='start_error'
        for _ in range(2):
            with self.assertRaises(graceful.GracefulControlError):self.control.start(receipt,expected_head=HEAD)
        self.assertEqual([e[0] for e in self.backend.events if isinstance(e,tuple)],['signal','kickstart'])
    def test_health_timeout_has_single_start_only(self):
        receipt=self.stop();self.backend.health_ok=False
        with self.assertRaisesRegex(graceful.GracefulControlError,'health_timeout'):self.control.start(receipt,expected_head=HEAD)
        self.assertEqual([e[0] for e in self.backend.events if isinstance(e,tuple)],['signal','kickstart'])
    def test_duplicate_start_after_success_is_rejected(self):
        receipt=self.stop();self.control.start(receipt,expected_head=HEAD)
        with self.assertRaises(graceful.GracefulControlError):self.control.start(receipt,expected_head=HEAD)
    def test_start_identity_mismatch_does_not_probe_health(self):
        receipt=self.stop();self.backend.identity_ok=False
        with patch.object(self.backend,'health',side_effect=AssertionError('health must not run')):
            with self.assertRaises(graceful.GracefulControlError):self.control.start(receipt,expected_head=HEAD)
class ExistingApplicationInterruptTests(unittest.TestCase):
    def test_actual_run_server_keyboard_interrupt_returns_normally_and_closes(self):
        source=ast.parse((ROOT/'app.py').read_text());node=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='run_server')
        events=[]
        class Server:
            server_port=19001
            def __init__(self,*a):pass
            def serve_forever(self):raise KeyboardInterrupt()
            def server_close(self):events.append('closed')
        noop=lambda *a,**k:None
        ns=dict(SAFE_PRODUCTION_ACTIVE=False,STAGING=False,APP_WRITE_GATE=None,startup_readonly=lambda gate:False,ensure_dirs=noop,db=SimpleNamespace(initialize=noop),ensure_template_management_seed=noop,CLASS_TWO_TASKS=(),append_service_log=noop,VERSION='fixture',os=SimpleNamespace(getpid=lambda:999),wechat_oauth_startup_messages=lambda:[],ReusableThreadingHTTPServer=Server,CRMHandler=object,print=noop,run_reminder_engine=noop,start_optional_startup_task=lambda service,name,task:task())
        exec(compile(ast.Module(body=[node],type_ignores=[]),'actual-run-server','exec'),ns)
        ns['run_server']('127.0.0.1',19001,'parent',startup_preflight_done=True)
        self.assertEqual(events,['closed']);self.assertFalse(ns['SAFE_PRODUCTION_ACTIVE'])
    def test_existing_daemon_semantics_and_sigterm_gap_are_not_hidden(self):
        source=(ROOT/'app.py').read_text()
        self.assertIn('daemon_threads = True',source)
        self.assertNotIn('signal.signal(signal.SIGTERM',source)

@unittest.skipIf(graceful is None,'implementation missing')
class RealAdapterTests(unittest.TestCase):
    def test_real_plist_program_arguments_are_accepted(self):
        fixture=Path('/Volumes/Zhang/AAIE CodeX/CRM/SAFEBOX/agent-reach-current-production-20260908')
        home=fixture/'mock-home';artifacts=legacy.render_artifacts(source_root=ROOT,home=home,production_root=fixture/'mock-production',expected_head=HEAD)
        payload=artifacts[legacy.installed_plist(home,'parent')][0]
        run=lambda *a,**k:SimpleNamespace(returncode=113,stdout='',stderr='Could not find service')
        backend=graceful.SystemBackend(run=run)
        with patch.object(Path,'home',return_value=home),patch.object(Path,'is_symlink',return_value=False),patch.object(Path,'stat',return_value=SimpleNamespace(st_size=len(payload))),patch.object(Path,'read_bytes',return_value=payload):
            try:backend.validate_job_policy('parent')
            except RuntimeError:self.fail('Actual reviewed launcher plist was rejected')
    def test_dirty_checkout_rejected_despite_matching_head(self):
        def run(command,**kwargs):
            self.assertEqual(kwargs['timeout'],5)
            return SimpleNamespace(returncode=1 if 'diff' in command else 0,stdout=HEAD+'\n',stderr='')
        backend=graceful.SystemBackend(run=run)
        with self.assertRaises(RuntimeError):backend.head()
    def test_start_waits_for_owned_pid_to_bind_listener(self):
        backend=Backend();control=graceful.GracefulController(backend,timeout_seconds=2,poll_seconds=.1)
        receipt=control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
        original=backend.inspect;delayed=[0]
        def inspect(service):
            obs=original(service)
            if obs.pid==654 and delayed[0]<2:
                delayed[0]+=1
                return graceful.Observation(True,654,(),'birth-2',True)
            return obs
        with patch.object(backend,'inspect',side_effect=inspect):
            result=control.start(receipt,expected_head=HEAD)
        self.assertEqual(result['status'],'healthy');self.assertEqual(delayed[0],2)

    def test_signal_boundary_revalidates_same_pid_birth(self):
        calls=[];backend=graceful.SystemBackend(armed=True,run=lambda *a,**k:calls.append(a))
        with patch.object(backend,'inspect',return_value=graceful.Observation(True,321,(321,),'new-birth',True)):
            with self.assertRaises(RuntimeError):backend.signal_interrupt('parent',321,'old-birth')
        self.assertEqual(calls,[])
    def test_exact_os_signal_and_start_are_bounded_and_never_force(self):
        calls=[]
        def run(command,**kwargs):
            calls.append(command);self.assertEqual(kwargs['timeout'],5)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        backend=graceful.SystemBackend(armed=True,run=run)
        with patch.object(backend,'inspect',return_value=graceful.Observation(True,321,(321,),'birth',True)):
            backend.signal_interrupt('parent',321,'birth')
        with patch.object(backend,'inspect',return_value=graceful.Observation(True,None,(),None,False,0)):
            backend.kickstart('parent')
        self.assertEqual(calls,[['/bin/launchctl','kill','SIGINT',backend.domain+'/com.aaie.crm.production.parent'],['/bin/launchctl','kickstart',backend.domain+'/com.aaie.crm.production.parent']])
    def test_os_adapter_is_unarmed_by_default(self):
        backend=graceful.SystemBackend(run=lambda *a,**k:self.fail('OS action reached'))
        with self.assertRaises(RuntimeError):backend.signal_interrupt('parent',321,'birth')
        with self.assertRaises(RuntimeError):backend.kickstart('parent')
    def test_health_rejects_redirect_and_closes_without_second_request(self):
        response=SimpleNamespace(status=302,read=lambda *a:self.fail('redirect body read'))
        class Connection:
            count=0;closed=False
            def request(this,*a,**k):this.count+=1
            def getresponse(this):return response
            def close(this):this.closed=True
        connection=Connection()
        with patch.object(graceful.http.client,'HTTPConnection',return_value=connection) as factory:
            with self.assertRaises(RuntimeError):graceful.SystemBackend().health('parent')
        factory.assert_called_once_with('127.0.0.1',8781,timeout=5)
        self.assertEqual(connection.count,1);self.assertTrue(connection.closed)
    def test_stop_timeout_cannot_create_start_receipt(self):
        backend=Backend();backend.behavior='ignore';control=graceful.GracefulController(backend,timeout_seconds=.2,poll_seconds=.1)
        with self.assertRaises(graceful.GracefulControlError):control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
        with self.assertRaises(graceful.GracefulControlError):control.start(None,expected_head=HEAD)
        self.assertFalse(any(e[0]=='kickstart' for e in backend.events if isinstance(e,tuple)))
    def test_nonzero_exit_never_mints_receipt(self):
        backend=Backend();original=backend.inspect
        def inspect(service):
            obs=original(service)
            return graceful.Observation(True,None,(),None,False,1) if obs.pid is None else obs
        with patch.object(backend,'inspect',side_effect=inspect):
            with self.assertRaisesRegex(graceful.GracefulControlError,'normal_exit_not_proven'):
                graceful.GracefulController(backend).stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)

class DisposableExistingInterrupt(unittest.TestCase):
    def test_owned_child_sigint_closes_actual_run_server_and_exits_zero(self):
        import os,select,subprocess,sys,textwrap
        code=textwrap.dedent("""
            import ast,os,signal,time
            from pathlib import Path
            from types import SimpleNamespace
            signal.signal(signal.SIGINT,signal.default_int_handler)
            signal.signal(signal.SIGALRM,lambda *a:os._exit(77))
            signal.alarm(15)
            source=ast.parse(Path('app.py').read_text())
            node=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='run_server')
            class Server:
                server_port=0
                def __init__(self,*a):pass
                def serve_forever(self):
                    print('OWNED_FIXTURE_READY',flush=True)
                    while True:signal.pause()
                def server_close(self):print('OWNED_FIXTURE_CLOSED',flush=True)
            noop=lambda *a,**k:None
            ns=dict(SAFE_PRODUCTION_ACTIVE=False,STAGING=False,APP_WRITE_GATE=None,startup_readonly=lambda gate:False,ensure_dirs=noop,db=SimpleNamespace(initialize=noop),ensure_template_management_seed=noop,CLASS_TWO_TASKS=(),append_service_log=noop,VERSION='fixture',os=os,wechat_oauth_startup_messages=lambda:[],ReusableThreadingHTTPServer=Server,CRMHandler=object,print=noop,run_reminder_engine=noop,start_optional_startup_task=lambda service,name,task:task())
            exec(compile(ast.Module(body=[node],type_ignores=[]),'actual-run-server','exec'),ns)
            ns['run_server']('127.0.0.1',0,'parent',startup_preflight_done=True)
            print('OWNED_FIXTURE_NORMAL_EXIT',flush=True)
        """)
        child=subprocess.Popen([sys.executable,'-B','-c',code],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        # No terminate/kill cleanup: this owned fixture self-expires after15s.
        ready,_,_=select.select([child.stdout],[],[],10)
        if not ready:
            child.wait(timeout=20);self.fail('owned fixture not ready')
        line=child.stdout.readline().strip()
        if line!='OWNED_FIXTURE_READY':
            child.wait(timeout=20);self.fail('unexpected owned fixture stage')
        os.kill(child.pid,signal.SIGINT)
        output,errors=child.communicate(timeout=20)
        self.assertEqual(child.returncode,0);self.assertEqual(errors,'')
        self.assertEqual(output.splitlines(),['OWNED_FIXTURE_CLOSED','OWNED_FIXTURE_NORMAL_EXIT'])

@unittest.skipIf(graceful is None,'implementation missing')
class LauncherTransitionTests(unittest.TestCase):
    def test_actual_metadata_parser_recognizes_only_approved_launcher_then_python(self):
        import sys
        fixture=Path('/Volumes/Zhang/AAIE CodeX/CRM/SAFEBOX/agent-reach-current-production-20260908')
        home=fixture/'mock-home';backend=graceful.SystemBackend();backend.root=fixture/'mock-production';backend.python=Path(sys.executable)
        stage=['launcher']
        def run(command,**kwargs):
            self.assertEqual(kwargs['timeout'],5)
            if command[0]=='/bin/launchctl':return SimpleNamespace(returncode=0,stdout='state = running\npid = 654\n',stderr='')
            if '-t' in command:return SimpleNamespace(returncode=0 if stage[0]=='bound' else 1,stdout='654\n' if stage[0]=='bound' else '',stderr='')
            if 'lstart=' in command:return SimpleNamespace(returncode=0,stdout='same-birth\n',stderr='')
            shell=stage[0] in ('launcher','handshake','unknown')
            if 'command=' in command:
                text=f'/bin/zsh {legacy.support_dir(home)}/production-service-launcher.zsh parent' if shell else f'{backend.python} {backend.root}/app.py --host 127.0.0.1 --port 8781 --service parent'
                if stage[0]=='handshake':text=text.replace('/bin/zsh ', '/bin/zsh -f ',1)
                if stage[0]=='unknown':text+=' extra-unapproved'
                return SimpleNamespace(returncode=0,stdout=text,stderr='')
            return SimpleNamespace(returncode=0,stdout=f'fcwd\nn{Path("/") if shell else backend.root}\nftxt\nn{Path("/bin/zsh") if shell else backend.python.resolve()}\n',stderr='')
        backend.run=run
        with patch.object(Path,'home',return_value=home):
            launcher=backend.inspect('parent');self.assertEqual(launcher.stage,'approved_launcher_pending');self.assertFalse(launcher.identity_ok)
            stage[0]='handshake';self.assertEqual(backend.inspect('parent').stage,'approved_launcher_pending')
            stage[0]='unknown';self.assertEqual(backend.inspect('parent').stage,'unknown')
            stage[0]='python';python=backend.inspect('parent');self.assertTrue(python.identity_ok);self.assertEqual(python.listeners,())
            stage[0]='bound';bound=backend.inspect('parent');self.assertTrue(bound.identity_ok);self.assertEqual(bound.listeners,(654,))
        self.assertEqual(launcher.started_at,bound.started_at)
    def test_start_waits_for_approved_launcher_same_pid_exec(self):
        backend=Backend();control=graceful.GracefulController(backend,timeout_seconds=2,poll_seconds=.1)
        receipt=control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True);original=backend.inspect;pending=[True]
        def inspect(service):
            obs=original(service)
            if obs.pid==654 and pending[0]:
                pending[0]=False
                return graceful.Observation(True,654,(),'birth-2',False,None,'approved_launcher_pending')
            return obs
        with patch.object(backend,'inspect',side_effect=inspect):self.assertEqual(control.start(receipt,expected_head=HEAD)['status'],'healthy')

class ExitInspectionRaceTests(unittest.TestCase):
    """Real inspect + stop, replacing only the external command transport."""
    def backend(self, failure_at='lstart=', mode='normal'):
        import sys
        backend=graceful.SystemBackend(armed=True)
        backend.root=ROOT;backend.python=Path(sys.executable)
        state={'signalled':False,'disappeared':False,'signals':0,'prints_after':0}
        def result(code=0,out='',err=''):
            return SimpleNamespace(returncode=code,stdout=out,stderr=err)
        def run(command,**kwargs):
            if command[:3]==['/bin/launchctl','kill','SIGINT']:
                state['signalled']=True;state['signals']+=1;return result()
            if command[:2]==['/bin/launchctl','print']:
                if state['disappeared']:
                    state['prints_after']+=1
                    if mode=='replacement':return result(out='state = running\npid = 999\nlast exit code = 0\n')
                    if mode=='stale':return result(out='state = running\npid = 321\nlast exit code = 0\n')
                    if mode=='unloaded':return result(113,err='Could not find service')
                    return result(out='state = waiting\n'+('' if mode=='unknown_exit' else 'last exit code = '+('1' if mode=='nonzero' else '0')+'\n'))
                return result(out='state = running\npid = 321\n')
            if '-iTCP:8781' in command:
                if state['signalled'] and not state['disappeared'] and mode=='transient_foreign':return result(out='999\n')
                if state['disappeared'] and mode=='foreign':return result(out='999\n')
                return result(1) if state['signalled'] else result(out='321\n')
            if 'pid=' in command:
                if mode=='alive':return result(out='321\n')
                if mode=='pid_permission':return result(1,err='private error')
                return result(1)
            selected=('lstart=' in command and failure_at=='lstart=') or ('command=' in command and failure_at=='command=') or ('cwd,txt' in command and failure_at=='files')
            if state['signalled'] and selected:
                state['disappeared']=True
                return result(2 if mode=='unknown_rc' else 1,'unexpected' if mode=='stdout' else '', 'private error' if mode=='stderr' else '')
            if 'lstart=' in command:return result(out='same-birth\n')
            if 'command=' in command:return result(out=f'{backend.python} {ROOT}/app.py --host 127.0.0.1 --port 8781 --service parent')
            if 'cwd,txt' in command:return result(out=f'fcwd\nn{ROOT}\nftxt\nn{backend.python.resolve()}\n')
            raise AssertionError('Unexpected command transport')
        backend.run=run
        backend.head=lambda:HEAD
        backend.validate_job_policy=lambda service:None
        backend.health=lambda service:{'status':'ok','service':service,'port':8781}
        return backend,state

    def test_normal_exit_at_each_process_probe_mints_original_single_use_receipt(self):
        for probe in ('lstart=','command=','files'):
            with self.subTest(probe=probe):
                backend,state=self.backend(probe)
                control=graceful.GracefulController(backend,timeout_seconds=.1,poll_seconds=.01)
                try:receipt=control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
                except graceful.GracefulControlError as exc:self.fail(f'Normal independently proven exit lost receipt: {exc}')
                self.assertEqual((receipt.original_pid,receipt.original_started_at),(321,'same-birth'))
                self.assertEqual(state['signals'],1)
                self.assertEqual(state['prints_after'],1)
                with self.assertRaisesRegex(graceful.GracefulControlError,'stop_attempt_consumed'):
                    control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
                with self.assertRaisesRegex(graceful.GracefulControlError,'invalid_or_consumed_receipt'):
                    graceful.GracefulController(backend).start(receipt,expected_head=HEAD)

    def test_unproven_exit_never_produces_receipt_or_resends_signal(self):
        for mode in ('replacement','stale','unloaded','nonzero','unknown_exit','foreign','transient_foreign','alive','pid_permission','stderr','stdout','unknown_rc'):
            with self.subTest(mode=mode):
                backend,state=self.backend(mode=mode)
                control=graceful.GracefulController(backend,timeout_seconds=.1,poll_seconds=.01)
                with self.assertRaises(graceful.GracefulControlError):
                    control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
                self.assertEqual(state['signals'],1)
                self.assertLessEqual(state['prints_after'],1)
                with self.assertRaises(graceful.GracefulControlError):control.start(None,expected_head=HEAD)

    def test_safe_inspection_stage_survives_controller_without_private_output(self):
        backend,state=self.backend(mode='stderr')
        control=graceful.GracefulController(backend,timeout_seconds=.1,poll_seconds=.01)
        with self.assertRaises(graceful.GracefulControlError) as caught:
            control.stop('parent',expected_head=HEAD,maintenance_idle_confirmed=True)
        self.assertEqual(getattr(caught.exception,'stage',None),'process_start')
        self.assertEqual(getattr(caught.exception,'safe_failure_code',None),'process_inspection_failed')
        self.assertNotIn('private',str(caught.exception));self.assertIsNone(caught.exception.__context__)

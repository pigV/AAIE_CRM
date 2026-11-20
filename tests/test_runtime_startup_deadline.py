"""Startup total budgets, real bounded worker processes, no CRM imports/services."""
from contextlib import ExitStack
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import MagicMock,patch

from tools import runtime_control as control

ROOT=Path(__file__).resolve().parents[1]


class RuntimeStartupDeadlineTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'reports/preproduction-readiness-20261120/source-compat/startup-unit';base.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();(self.root/'config').mkdir();(self.root/'map_tile_service').mkdir()
        self.cfg=SimpleNamespace(root=self.root,source_root=ROOT,path=self.root/'config/runtime.json',config_sha256='synthetic-sha',mode='staging',check_current=lambda:None)

    def test_sqlite_preflight_worker_is_interrupted_at_the_hard_budget(self):
        self.assertTrue(hasattr(control,'run_startup_worker'),'startup preflight has no bounded worker')
        code="import sqlite3; c=sqlite3.connect(':memory:'); c.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n').fetchone()"
        created=[];real=control.subprocess.Popen
        def spawn(*args,**kw):
            proc=real(*args,**kw)
            if kw.get("start_new_session"):created.append(proc)
            return proc
        start=time.monotonic()
        with patch.object(control,'startup_worker_command',return_value=[control.PYTHON,'-B','-c',code]),patch.object(control.subprocess,'Popen',side_effect=spawn):
            with self.assertRaisesRegex(control.RuntimeControlError,'preflight.*timeout'):
                control.run_startup_worker(self.cfg,'preflight',timeout=.25)
        self.assertLess(time.monotonic()-start,4)
        self.assertEqual(1,len(created));self.assertIsNotNone(created[0].poll())

    def test_ocr_worker_timeout_cleans_its_descendants_but_not_foreign_process(self):
        self.assertTrue(hasattr(control,'run_startup_worker'),'startup OCR has no bounded worker')
        pidfile=self.root/'descendant.pid'
        code="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',\"import signal,time,sys,os;signal.signal(signal.SIGTERM,signal.SIG_IGN);open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)\",sys.argv[1]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(60)"
        real=control.subprocess.Popen
        foreign=real([control.PYTHON,'-B','-c','import time;time.sleep(60)'],start_new_session=True)
        created=[]
        def spawn(*args,**kw):
            proc=real(*args,**kw)
            if kw.get("start_new_session"):created.append(proc)
            return proc
        try:
            with patch.object(control,'startup_worker_command',return_value=[control.PYTHON,'-B','-c',code,str(pidfile)]),patch.object(control.subprocess,'Popen',side_effect=spawn):
                with self.assertRaisesRegex(control.RuntimeControlError,'ocr.*timeout'):
                    control.run_startup_worker(self.cfg,'ocr',timeout=.6)
            self.assertTrue(pidfile.exists(),'synthetic compiler descendant never launched')
            self.assertFalse(control.process_group_alive(created[0].pid))
            self.assertIsNone(foreign.poll(),'a foreign process was signalled')
        finally:
            for proc in created:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            if foreign.poll() is None:foreign.terminate()
            foreign.wait(timeout=5)

    def test_worker_cannot_build_production_assets_without_explicit_control_authority(self):
        from tools import runtime_startup_worker as worker
        config=SimpleNamespace(mode='production',config_sha256='synthetic-sha',root=self.root,source_root=ROOT,check_current=lambda:None)
        with patch('runtime_configuration.RuntimeConfiguration.load',return_value=config),patch.object(control.build_document_ocr,'build',return_value=self.root/'synthetic-ocr') as build:
            with self.assertRaises(control.RuntimeControlError):
                worker.main(['--config',str(self.cfg.path),'--config-sha256','synthetic-sha','--phase','ocr'])
        build.assert_not_called()

    def harness(self,stack,clock,worker,wait):
        stack.enter_context(patch.object(control.time,'monotonic',side_effect=lambda:clock[0]))
        stack.enter_context(patch.object(control,'run_startup_worker',side_effect=worker))
        for name,value in (('sandbox_profile','profile'),('environment',{}),('observe','birth')):
            stack.enter_context(patch.object(control,name,return_value=value))
        stack.enter_context(patch.object(control.ManagedRuntime,'from_configuration'))
        stack.enter_context(patch.object(control.school_map_tile_control,'start'))
        stack.enter_context(patch.object(control,'runtime_file'))
        popen=stack.enter_context(patch.object(control.subprocess,'Popen',side_effect=[MagicMock(pid=900001),MagicMock(pid=900002)]))
        stack.enter_context(patch.object(control,'wait_for_service_health',side_effect=wait))
        cleanup=stack.enter_context(patch.object(control,'cleanup_started',return_value=[]))
        return popen,cleanup

    def test_children_consume_one_shared_budget_after_preflight_and_ocr(self):
        self.assertTrue(hasattr(control,'run_startup_worker'),'startup has no bounded phases')
        clock=[0.];timeouts=[]
        def worker(config,phase,*,timeout):
            clock[0]+=4 if phase=='preflight' else 3
            return {'ready':True}
        def wait(config,role,proc,*,timeout):
            timeouts.append((role,timeout))
            if role=='admin':clock[0]+=2;return {'healthy':True}
            clock[0]+=timeout;raise control.RuntimeControlError('parent total startup timeout')
        with ExitStack() as stack:
            popen,cleanup=self.harness(stack,clock,worker,wait)
            with self.assertRaisesRegex(control.RuntimeControlError,'timeout'):control.start(self.cfg,timeout=10)
        self.assertEqual([('admin',3),('parent',1)],timeouts)
        self.assertEqual(2,popen.call_count);cleanup.assert_called_once()
        self.assertEqual(['admin','parent'],[role for role,proc in cleanup.call_args.args[1]])

    def test_expired_preflight_cannot_start_any_listener(self):
        self.assertTrue(hasattr(control,'run_startup_worker'),'startup has no bounded phases')
        clock=[0.]
        def worker(config,phase,*,timeout):clock[0]+=11;return {'ready':True}
        with ExitStack() as stack:
            popen,cleanup=self.harness(stack,clock,worker,lambda *a,**kw:None)
            with self.assertRaisesRegex(control.RuntimeControlError,'timeout'):control.start(self.cfg,timeout=10)
        popen.assert_not_called()


if __name__=='__main__':unittest.main()

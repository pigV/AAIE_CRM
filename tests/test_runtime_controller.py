import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tools import runtime_control as control


class RuntimeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name).resolve()
        (root/"config").mkdir()
        self.cfg=SimpleNamespace(root=root,source_root=Path("/synthetic/code"),
                                 path=root/"config/runtime.json",config_sha256="digest",
                                 check_current=lambda:None,mode="staging")
        self.record=dict(pid=123456,started="birth",role="admin",
                         data_root=str(root),config_sha256="digest")
        self.path=root/"config/admin.pid.json"
        self.path.write_text(json.dumps(self.record))

    def test_exit_between_stop_observations_does_not_report_foreign_pid(self):
        with patch.object(control,"verify_process",side_effect=[True,control.RuntimeControlError("exited")]),patch.object(control.os,"kill") as kill, \
             patch.object(control,"observe",side_effect=["S",""]), \
             patch.object(control.time,"sleep"):
            control.stop_one(self.cfg,"admin")
        kill.assert_called_once_with(123456,control.signal.SIGTERM)
        self.assertFalse(self.path.exists())

    def test_process_inspection_denied_is_not_treated_as_exit(self):
        result=SimpleNamespace(stdout="",stderr="Operation not permitted",returncode=1)
        with patch.object(control.subprocess,"run",return_value=result):
            with self.assertRaises(control.RuntimeControlError):control.observe(123456,"stat")

    def test_correct_identity_is_accepted(self):
        control.validate_process_identity(self.cfg,"admin",self.record,
                                          " ".join(control.process_command(self.cfg,"admin")),"birth")

    def test_reused_pid_is_rejected(self):
        with self.assertRaises(control.RuntimeControlError):
            control.validate_process_identity(self.cfg,"admin",self.record,
                                              " ".join(control.process_command(self.cfg,"admin")),"new birth")

    def test_different_config_is_rejected(self):
        self.record["config_sha256"]="foreign"
        with self.assertRaises(control.RuntimeControlError):
            control.validate_process_identity(self.cfg,"admin",self.record,
                                              " ".join(control.process_command(self.cfg,"admin")),"birth")

    def test_lock_contention_refuses_second_operation(self):
        with control.lifecycle_lock(self.cfg):
            with self.assertRaises(control.RuntimeControlError):
                with control.lifecycle_lock(self.cfg):pass

    def test_stale_matching_record_does_not_signal_any_process(self):
        with patch.object(control,"observe",return_value=""),patch.object(control.os,"kill") as kill:
            control.stop_one(self.cfg,"admin")
        kill.assert_not_called()
        self.assertFalse(self.path.exists())



    def test_status_is_not_ready_when_map_helper_has_failed(self):
        with patch.object(control,"verify_process",return_value=True), \
             patch.object(control,"health",return_value={"healthy":True}), \
             patch.object(control,"state_path",return_value=self.path), \
             patch.object(control.ManagedRuntime,"from_configuration"), \
             patch.object(control.school_map_tile_control,"health",side_effect=OSError("offline")):
            self.assertFalse(control.status(self.cfg)["ready"])


    def test_failed_final_health_rolls_back_started_children(self):
        from contextlib import ExitStack
        from unittest.mock import MagicMock
        self.path.unlink()
        child=MagicMock(pid=123456);child.poll.return_value=None
        with ExitStack() as stack:
            for name,value in (("preflight",{"ready":True}),("sandbox_profile","profile"),
                               ("environment",{}),("observe","birth"),("health",{"healthy":True}),
                               ("status",{"ready":False})):
                stack.enter_context(patch.object(control,name,return_value=value))
            stack.enter_context(patch.object(control.ManagedRuntime,"from_configuration"))
            stack.enter_context(patch.object(control.school_map_tile_control,"start"))
            stack.enter_context(patch.object(control,"runtime_file"))
            stack.enter_context(patch.object(control.subprocess,"Popen",return_value=child))
            cleanup=stack.enter_context(patch.object(control,"cleanup_started",return_value=[]))
            with self.assertRaises(control.RuntimeControlError):
                control.start(self.cfg)
            cleanup.assert_called_once()


class ManagedMapReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name).resolve()
        (root/"config").mkdir()
        (root/"map_tile_service").mkdir()
        self.cfg=SimpleNamespace(root=root,source_root=control.ROOT,path=root/"config/runtime.json",
                                 config_sha256="digest",mode="staging",admin_port=29880,
                                 parent_port=29881,extraction_port=29892,map_port=29893,
                                 namespace="aaie_staging_unit_",check_current=lambda:None)
        self.adapter=control.ManagedRuntime.from_configuration(self.cfg)
        self.record=dict(pid=123456,started="birth",role="map",data_root=str(root),config_sha256="digest")

    def test_map_profile_symlink_is_rejected_without_overwriting_target(self):
        target=self.cfg.root/"sentinel";target.write_text("unchanged")
        (self.cfg.root/"map_tile_service/sandbox.sb").symlink_to(target)
        with self.assertRaises((control.IsolationError,control.RuntimeConfigurationError,OSError)):
            control.school_map_tile_control.prepare(self.adapter)
        self.assertEqual(target.read_text(),"unchanged")

    def test_map_log_symlink_is_rejected_before_process_creation(self):
        target=self.cfg.root/"sentinel";target.write_text("unchanged")
        (self.cfg.root/"map_tile_service/service.log").symlink_to(target)
        with patch.object(control.subprocess,"Popen",side_effect=AssertionError("Process must not start")) as popen:
            with self.assertRaises((control.IsolationError,control.RuntimeConfigurationError,OSError)):
                control.school_map_tile_control.start(self.adapter)
        popen.assert_not_called()
        self.assertEqual(target.read_text(),"unchanged")

    def test_map_rejects_echo_with_matching_script_substring(self):
        text="/bin/echo "+str(control.ROOT/"school_map_tiles.py")+" --root "+str(self.cfg.root/"map_tile_service")+" --port 29893"
        with patch.object(control.subprocess,"check_output",side_effect=[text,"birth"]), \
             patch.object(control,"observe",side_effect=[text,"birth"]):
            with self.assertRaises((control.IsolationError,control.RuntimeControlError)):
                control.school_map_tile_control.verify_process(self.adapter,self.record)

    def test_exited_map_record_does_not_block_other_services_stop(self):
        (self.cfg.root/"map_tile_service/pid.json").write_text(json.dumps(self.record))
        with patch.object(control,"observe",return_value=""), \
             patch.object(control.subprocess,"check_output",side_effect=control.subprocess.CalledProcessError(1,"ps")), \
             patch.object(control,"stop_one") as stop_one, \
             patch.object(control.school_map_tile_control,"stop"):
            control.stop(self.cfg)
        self.assertEqual(stop_one.call_count,2)

    def test_cleanup_continues_after_one_child_times_out(self):
        self.assertTrue(hasattr(control,"cleanup_started"),"Per-child cleanup aggregation is missing")
        from unittest.mock import MagicMock
        first=MagicMock(pid=123456);second=MagicMock(pid=123457)
        first.poll.return_value=None;second.poll.return_value=None
        second.wait.side_effect=control.subprocess.TimeoutExpired("child",5)
        with patch.object(control.school_map_tile_control,"stop") as stop_map:
            errors=control.cleanup_started(self.cfg,[("admin",first),("parent",second)],True,self.adapter)
        first.terminate.assert_called_once()
        stop_map.assert_called_once()
        self.assertTrue(errors)


if __name__=="__main__":unittest.main()

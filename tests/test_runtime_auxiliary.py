import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_reach_gateway as gateway
from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError


class AuxiliaryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'main'
        self.source.mkdir()
        self.root = self.base / 'runtime/staging/unit'
        (self.root / 'config').mkdir(parents=True)
        self.path = self.root / 'config/runtime.json'
        self.value = dict(version=1, mode='staging', root=str(self.root),
                          database='staging.db', admin_port=29880, parent_port=29881,
                          extraction_port=29892, namespace='aaie_staging_unit_')
        self.path.write_text(json.dumps(self.value))

    def config(self):
        return RuntimeConfiguration.load(self.path, source_root=self.source)

    def test_staging_has_separate_agent_port(self):
        self.assertEqual(getattr(self.config(), 'agent_reach_port', None), 29890)

    def test_agent_port_collision_is_rejected(self):
        self.value['extraction_port'] = 29890
        self.path.write_text(json.dumps(self.value))
        with self.assertRaises(RuntimeConfigurationError):
            self.config()

    def test_staging_gateway_never_falls_back_to_production(self):
        self.assertTrue(hasattr(gateway, 'backend_port'), 'Managed gateway routing missing')
        with patch.dict(os.environ, {'AAIE_CRM_ENV': 'staging'}, clear=True):
            with self.assertRaises(gateway.GatewayError):
                gateway.backend_port()

    def test_managed_gateway_uses_derived_port(self):
        self.assertTrue(hasattr(gateway, 'backend_port'), 'Managed gateway routing missing')
        with patch.dict(os.environ, {'AAIE_CRM_ENV': 'staging',
                                     'AAIE_CRM_RUNTIME_CONFIG': str(self.path)}, clear=True), \
             patch.object(RuntimeConfiguration, 'load', return_value=self.config()):
            self.assertEqual(gateway.backend_port(), 29890)

    def test_auxiliary_controller_is_present(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('tools.runtime_auxiliary_control'))

    def test_not_yet_listening_is_retried_before_process_identity_check(self):
        from tools import runtime_auxiliary_control as aux
        with patch.object(aux, 'read_json', side_effect=ConnectionRefusedError), \
             patch.object(aux.control, 'verify_process', side_effect=aux.control.RuntimeControlError('exec transition')):
            with self.assertRaises(ConnectionRefusedError):
                aux.health(self.config(), 'extraction', {'pid':123456})

    def test_auxiliary_stop_uses_verified_process_group(self):
        from tools import runtime_control as control
        record=dict(pid=123456,started='birth',role='extraction',data_root=str(self.root),
                    config_sha256=self.config().config_sha256)
        (self.root/'config/extraction.pid.json').write_text(json.dumps(record))
        with patch.object(control,'verify_process',return_value=True), \
             patch.object(control.os,'getpgid',return_value=123456), \
             patch.object(control.os,'killpg') as killpg, \
             patch.object(control,'process_group_alive',return_value=False), \
             patch.object(control,'observe',return_value=''):
            control.stop_one(self.config(),'extraction',process_group=True)
        killpg.assert_called_once_with(123456,control.signal.SIGTERM)

    def test_production_control_still_requires_explicit_authority(self):
        from tools.runtime_control import require_control_authority,RuntimeControlError
        from types import SimpleNamespace
        with self.assertRaises(RuntimeControlError):
            require_control_authority(SimpleNamespace(mode='production'),False)

    def test_sandbox_exposes_only_owned_listener_and_no_network_egress(self):
        from tools.runtime_auxiliary_control import sandbox_profile
        deps = dict(extraction_packages=Path('/dependency/ocr'), agent_packages=Path('/dependency/flask'),
                    ffmpeg=Path('/dependency/bin/ffmpeg'), agent_source=self.root/'agent-source')
        for role, expected in [('extraction',29892),('agent-reach',29890)]:
            profile = sandbox_profile(self.config(), role, deps)
            self.assertIn('localhost:' + str(expected), profile)
            self.assertIn('(deny network-outbound)', profile)
            self.assertNotIn('(allow network-outbound', profile)
            for forbidden in (8780,8781,8790,8792):
                self.assertNotIn('localhost:' + str(forbidden), profile)

    def test_changed_dependency_identity_prevents_signal(self):
        from tools import runtime_control as control
        from tools.runtime_auxiliary_control import process_command
        cfg = self.config()
        deps = {}
        for key in ('extraction_packages','agent_packages','agent_source'):
            folder=self.root / key; folder.mkdir(); deps[key]=str(folder)
        ffmpeg=self.root/'ffmpeg';ffmpeg.write_text('fixture');deps['ffmpeg']=str(ffmpeg)
        manifest=self.root/'config/auxiliary.json';manifest.write_text(json.dumps(deps))
        command=' '.join(process_command(cfg,'agent-reach'))
        record=dict(pid=123456,started='birth',role='agent-reach',data_root=str(cfg.root),
                    config_sha256=cfg.config_sha256)
        manifest.write_text(json.dumps(deps,indent=2))
        with self.assertRaises(control.RuntimeControlError):
            control.validate_process_identity(cfg,'agent-reach',record,command,'birth')


if __name__ == '__main__':
    unittest.main()

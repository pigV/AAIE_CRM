import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError
from tools import runtime_control as control


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.source=self.base/'main';self.source.mkdir()
        self.root=self.base/'runtime/production';(self.root/'config').mkdir(parents=True)
        self.path=self.root/'config/runtime.json'
        self.value=dict(version=1,mode='production',root=str(self.root),database='staging.db',
                        admin_port=8780,parent_port=8781,extraction_port=8792,namespace='aaie_production_')
        self.path.write_text(json.dumps(self.value))

    def config(self):return RuntimeConfiguration.load(self.path,source_root=self.source)

    def test_production_rejects_nonproduction_ports(self):
        self.value['admin_port']=30880;self.path.write_text(json.dumps(self.value))
        with self.assertRaises(RuntimeConfigurationError):self.config()

    def test_production_profile_preserves_explicit_lan_and_nas(self):
        (self.root/'admin-lan.json').write_text('{"address":"192.168.1.123"}')
        (self.root/'nas-readonly.json').write_text('{"enabled":true}')
        (self.root/'nas-write.json').write_text('{"enabled":true}')
        profile=control.sandbox_profile(self.config())
        self.assertIn('local ip "*:8780"',profile)
        self.assertIn('(allow file-write* (subpath "/Volumes/Server/Students/CRM_TEST"))',profile)
        self.assertIn('remote ip "localhost:8790"',profile)

    def test_external_agent_is_not_a_managed_role(self):
        from tools import runtime_auxiliary_control as aux
        self.assertTrue(hasattr(aux,'managed_roles'))
        with patch.object(aux,'dependencies',return_value={'agent_management':'external'}):
            self.assertEqual(aux.managed_roles(self.config()),('extraction',))

    def test_default_stack_selects_production_without_creating_it(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('tools.runtime_stack'))
        from tools.runtime_stack import default_config
        self.assertEqual(default_config(self.source),self.path)

    def test_new_production_write_gate_is_required(self):
        import release_business_mode as mode
        self.assertTrue(hasattr(mode,'managed_production_root'))
        with patch.object(mode,'managed_production_root',return_value=self.root), \
             patch.object(RuntimeConfiguration,'load',return_value=self.config()), \
             patch('business_write_gate.install_sqlite_guard'):
            gate=mode.configured_gate(self.root)
        self.assertIsNotNone(gate)
        self.assertEqual(gate.status()['state'],'UNKNOWN')

    def test_staging_cannot_reserve_production_map_port(self):
        from runtime_configuration import PRODUCTION_PORTS
        self.assertIn(8793, PRODUCTION_PORTS)

    def test_production_start_requires_known_gate(self):
        from tools.runtime_stack import require_gate
        with self.assertRaises(control.RuntimeControlError):
            require_gate(self.config())

    def test_failed_final_stack_health_cleans_owned_services(self):
        from tools import runtime_stack as stack
        with patch.object(stack,'require_gate'), \
             patch.object(stack,'status',side_effect=[{'ready':False},{'ready':False}]), \
             patch.object(stack.auxiliary,'managed_roles',return_value=('extraction',)), \
             patch.object(stack.crm,'preflight',return_value={'ready':True}), \
             patch.object(stack.auxiliary,'start'), patch.object(stack.crm,'start'), \
             patch.object(stack.crm,'stop') as stop_crm, patch.object(stack.auxiliary,'stop') as stop_aux:
            with self.assertRaises(control.RuntimeControlError):stack.start(self.config())
            stop_crm.assert_called_once()
            stop_aux.assert_called_once()


if __name__=='__main__':unittest.main()

import importlib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from tools import staging_control as staging

class ExtractionTestConfiguration(unittest.TestCase):
    def setUp(self):
        from tests.isolation_test_support import prepare_isolation_source
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=prepare_isolation_source(Path(self.temp.name)/"source")
        source_patch=patch('staging_environment.SOURCE',root)
        config_patch=patch.object(staging,'CONFIG',root/'STAGING_DATA/staging.json')
        source_patch.start();config_patch.start()
        self.addCleanup(source_patch.stop);self.addCleanup(config_patch.stop)
    def test_admin_uses_only_test_extraction_endpoint(self):
        c=staging.load()
        self.assertEqual(staging.environment(c).get("AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL"),"http://127.0.0.1:18892")
        self.assertIn('(remote ip "localhost:18892")',staging.sandbox_profile(c))
        self.assertNotIn('localhost:8792',staging.sandbox_profile(c))
    def test_extraction_has_no_outbound_network_or_production_paths(self):
        module=importlib.import_module("tools.test_extraction_control")
        c=staging.load();profile=module.sandbox_profile(c);env=module.environment(c)
        self.assertIn('(local ip "localhost:18892")',profile)
        self.assertNotIn('(allow network-outbound',profile)
        self.assertIn('(deny file-read* (subpath "/Volumes"))',profile)
        self.assertIn('(deny signal)',profile)
        self.assertTrue(env["AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH"].startswith(str(c.root)))
        self.assertTrue(env["PADDLE_PDX_CACHE_HOME"].startswith(str(c.root)))
        self.assertEqual(env["BILLING_MODE"],"FREE_ONLY")
        self.assertEqual(env["AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"],"false")

class ExtractionServerPortTests(unittest.TestCase):
    setUp = ExtractionTestConfiguration.setUp
    def test_actual_server_construction_uses_the_same_shared_port(self):
        import ast
        from types import SimpleNamespace
        from unittest.mock import Mock
        tree=ast.parse((Path(__file__).resolve().parents[1]/"tools/test_extraction_server.py").read_text())
        node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="server" for t in n.targets))
        build=Mock();c=staging.load()
        ns=dict(build_server=build,token="synthetic",repository=object(),c=c,workflow=object(),args=SimpleNamespace(instance="a"*32),TEST_CONTENT_EXTRACTION_PORT=staging.TEST_CONTENT_EXTRACTION_PORT)
        exec(compile(ast.Module(body=[node],type_ignores=[]),"server-entry", "exec"),ns)
        self.assertEqual(build.call_args.args[1],staging.TEST_CONTENT_EXTRACTION_PORT)
        self.assertTrue(any(isinstance(n,ast.ImportFrom) and n.module=="staging_environment" and any(a.name=="TEST_CONTENT_EXTRACTION_PORT" for a in n.names) for n in tree.body))
    def test_map_port_must_not_alias_extraction(self):
        from dataclasses import replace
        from tools.school_map_tile_control import port
        from staging_environment import IsolationError
        with self.assertRaises(IsolationError):port(replace(staging.load(),admin_port=18879))

if __name__=="__main__":
    unittest.main()

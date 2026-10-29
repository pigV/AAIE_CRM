import importlib
import unittest
from tools import staging_control as staging

class ExtractionTestConfiguration(unittest.TestCase):
    def test_admin_uses_only_test_extraction_endpoint(self):
        c=staging.load()
        self.assertEqual(staging.environment(c).get("AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL"),"http://127.0.0.1:8792")
        self.assertIn('(remote ip "localhost:8792")',staging.sandbox_profile(c))
        self.assertNotIn('localhost:18892',staging.sandbox_profile(c))
    def test_extraction_has_no_outbound_network_or_production_paths(self):
        module=importlib.import_module("tools.test_extraction_control")
        c=staging.load();profile=module.sandbox_profile(c);env=module.environment(c)
        self.assertIn('(local ip "localhost:8792")',profile)
        self.assertNotIn('(allow network-outbound',profile)
        self.assertIn('(deny file-read* (subpath "/Volumes"))',profile)
        self.assertIn('(deny signal)',profile)
        self.assertTrue(env["AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH"].startswith(str(c.root)))
        self.assertTrue(env["PADDLE_PDX_CACHE_HOME"].startswith(str(c.root)))
        self.assertEqual(env["BILLING_MODE"],"FREE_ONLY")
        self.assertEqual(env["AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"],"false")
if __name__=="__main__":
    unittest.main()

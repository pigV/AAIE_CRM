"""Shared shell regression without importing application startup or databases."""
import ast
import subprocess
import types
import unittest
from pathlib import Path
import ui
ROOT=Path(__file__).resolve().parents[1]
BASE='64e0136bd408102d7fded34a252a5abc4e715c5c'
class ShellRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old=subprocess.check_output(['git','show',BASE+':ui.py'],cwd=ROOT,text=True)
        cls.old=types.ModuleType('baseline_ui');cls.old.__file__=str(ROOT/'ui.py')
        exec(compile(old,'baseline-ui','exec'),cls.old.__dict__)
    def test_ungranted_shared_navigation_unchanged(self):
        for role in ['Manager','Consultant','Content Staff','Unknown']:
            for username in ['admin','sonnyz','ordinary']:
                user={'role_name':role,'username':username,'module_permissions_json':'["dashboard","students","content_center","reports"]'}
                self.assertEqual(ui._nav_items('dashboard',user),self.old._nav_items('dashboard',user))
    def test_common_styles_unchanged(self):
        for path in ['assets/style.css','assets/ui-premium-v2.css']:
            self.assertEqual((ROOT/path).read_bytes(),subprocess.check_output(['git','show',BASE+':'+path],cwd=ROOT))
    def test_database_only_registers_one_module_no_schema_change(self):
        old=subprocess.check_output(['git','show',BASE+':database.py'],cwd=ROOT,text=True)
        new=(ROOT/'database.py').read_text()
        addition='    ("agent_reach", "Agent Reach", "Agent Reach", "/agent-reach", 40, "Content Management"),\n'
        self.assertEqual(new.replace(addition,''),old)
    def test_permission_grant_does_not_add_admin_features(self):
        u={'role_name':'Staff','username':'staff','module_permissions_json':'["agent_reach"]'}
        nav=ui._nav_items('agent_reach',u)
        for route in ['/users','/settings','/audit-logs','/revenue']:
            self.assertNotIn('href="'+route+'"',nav)
if __name__=='__main__':unittest.main()

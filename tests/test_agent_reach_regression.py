"""Current shared-shell and capability boundaries, independent of Git history."""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
import ui
from database import Database, MODULE_DEFINITIONS

ROOT=Path(__file__).resolve().parents[1]
# Shared navigation/theme rules at 15b02dc. Unrelated module styles may evolve.
SHARED_STYLE_BASELINES={'assets/style.css': 'e45d2a2a0fced8c325a2edcd2ff5dc2a3ca4b0dd0710b926f31e2155340f2619', 'assets/ui-premium-v2.css': '2a9e06f631030f1cd37e57b954885e00a93cf6cb956eb52e05173450902d9697'}
SHARED_SELECTORS={"assets/style.css":(":root",".sidebar",".topbar"),
                  "assets/ui-premium-v2.css":(".ui-premium-v2",)}

def shared_shell_signature(css,selectors):
    rows=[]
    for selector in selectors:
        blocks=re.findall(r"(?m)^"+re.escape(selector)+r"\s*\{([^}]+)\}",css)
        if not blocks:
            raise AssertionError("Missing shared shell selector: "+selector)
        rows.append((selector,[" ".join(block.split()) for block in blocks]))
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest()

class ShellRegression(unittest.TestCase):
    def test_ungranted_agent_does_not_hide_existing_navigation(self):
        for role in ("Manager","Consultant","Content Staff","Unknown"):
            for username in ("admin","sonnyz","ordinary"):
                with self.subTest(role=role,username=username):
                    user={"role_name":role,"username":username,
                          "module_permissions_json":'["dashboard","students","content_center","reports"]'}
                    navigation=ui._nav_items("dashboard",user)
                    self.assertNotIn('href="/agent-reach"',navigation)
                    self.assertIn('href="/students"',navigation)
                    self.assertIn('href="/"',navigation)

    def test_shared_navigation_and_theme_declarations_are_preserved(self):
        for path,expected in SHARED_STYLE_BASELINES.items():
            with self.subTest(path=path):
                self.assertEqual(shared_shell_signature((ROOT/path).read_text(),SHARED_SELECTORS[path]),expected)

    def test_single_registration_uses_separate_runtime_storage_not_crm_schema(self):
        registrations=[row for row in MODULE_DEFINITIONS if row[0]=="agent_reach"]
        self.assertEqual(len(registrations),1)
        self.assertEqual(registrations[0][3],"/agent-reach")
        base=ROOT/"STAGING_DATA/agent-shell-fixtures"
        base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            db=Database(Path(folder)/"synthetic.db")
            db.initialize()
            names={row["name"] for row in db.all("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"students","roles","users"}.issubset(names))
            self.assertFalse(any(name.startswith("agent_reach") for name in names))

    def test_permission_grant_does_not_add_admin_features(self):
        user={"role_name":"Staff","username":"staff","module_permissions_json":'["agent_reach"]'}
        navigation=ui._nav_items("agent_reach",user)
        self.assertIn('href="/agent-reach"',navigation)
        for route in ("/users","/settings","/audit-logs","/revenue"):
            self.assertNotIn('href="'+route+'"',navigation)

if __name__=="__main__":unittest.main()

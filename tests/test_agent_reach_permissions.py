import ast,json,unittest
from pathlib import Path
import ui
ROOT=Path(__file__).resolve().parents[1]
class NativePermissionContract(unittest.TestCase):
    def test_admin_menu(self):
        self.assertIn('href="/agent-reach"',ui._nav_items('agent_reach',{'role_name':'Admin','username':'other-admin'}))
    def test_independent_module_registered(self):
        tree=ast.parse((ROOT/'database.py').read_text())
        definitions=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='MODULE_DEFINITIONS' for t in n.targets))
        self.assertIn('agent_reach',{r[0] for r in definitions})
    def test_content_permission_does_not_grant(self):
        self.assertNotIn('href="/agent-reach"',ui._nav_items('',{'role_name':'Content Staff','username':'sonnyz','module_permissions_json':'["content_center"]'}))
if __name__=='__main__':unittest.main()

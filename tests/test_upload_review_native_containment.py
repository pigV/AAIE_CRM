"""Exercise the actual containment boundary outside the stricter staging creation hook."""
import ast,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from nas import ensure_within,NasSafetyError
ROOT=Path(__file__).resolve().parents[1]
class NativeContainmentTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  tree=ast.parse((ROOT/'app.py').read_text())
  function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='upload_from_review')
  ns={'ensure_within':ensure_within,'Upload':lambda field,filename,content_type,data:SimpleNamespace(data=data)}
  exec(compile(ast.Module(body=[function],type_ignores=[]),'app.py','exec'),ns);cls.load=staticmethod(ns['upload_from_review'])
 def test_symlink_to_another_local_fixture_is_rejected_before_file_read(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp') as name:
   base=Path(name);folder=base/'review';folder.mkdir();outside=base/'outside';outside.write_bytes(b'synthetic-only')
   (folder/'blob').symlink_to(outside)
   with self.assertRaises(NasSafetyError):self.load(folder,{'blob':'blob','original_name':'test.txt'})
 def test_regular_file_within_review_is_read_unchanged(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp') as name:
   folder=Path(name);(folder/'blob').write_bytes(b'synthetic-only')
   self.assertEqual(self.load(folder,{'blob':'blob','original_name':'test.txt'}).data,b'synthetic-only')

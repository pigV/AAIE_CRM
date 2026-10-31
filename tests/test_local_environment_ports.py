import json
from pathlib import Path
import tempfile
import unittest
from staging_environment import Staging, SOURCE, IsolationError
from tools import staging_control

class LocalEnvironmentPorts(unittest.TestCase):
    def test_production_pair_is_rejected_even_when_both_match_old_defaults(self):
        value=json.loads((SOURCE/'STAGING_DATA/staging.json').read_text())
        value.update(admin_port=8780,parent_port=8781)
        with tempfile.NamedTemporaryFile(mode='w',dir=SOURCE/'STAGING_DATA',suffix='.json') as f:
            json.dump(value,f);f.flush()
            with self.assertRaises(IsolationError):Staging.load(f.name)

    def test_crm_environment_does_not_route_to_production_extraction(self):
        c=staging_control.load()
        self.assertEqual(staging_control.environment(c)['AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL'],'http://127.0.0.1:18892')

    def test_isolated_source_is_readable_but_production_and_ports_are_denied(self):
        import subprocess
        c=staging_control.load()
        profile=c.root/'tmp/ports-test.sb';profile.write_text(staging_control.sandbox_profile(c))
        code="""import ctypes,json,os,socket
from pathlib import Path
root=Path(%r)
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib');lib.sandbox_check.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int];lib.sandbox_check.restype=ctypes.c_int
def denied(operation,path):return lib.sandbox_check(os.getpid(),operation.encode(),1,str(path).encode())==1
r={'source_read_allowed':not denied('file-read-data',root/'app.py'),'source_write_denied':denied('file-write-data',root/'app.py'),'production_read_denied':denied('file-read-data','/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030/STAGING_DATA/database/staging.db'),'production_write_denied':denied('file-write-data','/Users/faqiang/Downloads/AAIE_CRM_Test_5985902_20261030/STAGING_DATA/database/staging.db')}
for port in [8780,8781,8790,8792]:
 for action in ['bind','connect']:
  try:
   with socket.socket() as s:getattr(s,action)(('127.0.0.1',port))
  except PermissionError:r[str(port)+action]=True
  except OSError:r[str(port)+action]=False
  else:r[str(port)+action]=False
print(json.dumps(r))
""" % str(SOURCE)
        result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),staging_control.PYTHON,'-B','-c',code],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        for key,value in report.items():self.assertTrue(value,key)

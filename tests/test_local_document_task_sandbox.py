import tempfile,unittest,json
from pathlib import Path
from local_document_lab.run import run_offline
from local_document_lab.sandbox import DATA,task_profile
class TaskSandboxTests(unittest.TestCase):
 def setUp(self):
  (DATA/'tmp').mkdir(parents=True,exist_ok=True)
 def test_task_cannot_read_sibling_source_or_connect_to_network(self):
  with tempfile.TemporaryDirectory(dir=DATA/'tmp') as name:
   root=Path(name);job=root/'job';job.mkdir();other=root/'other.txt';other.write_text('synthetic')
   policy=job/'task.sb';policy.write_text(task_profile(job))
   code="import socket; from pathlib import Path; p=Path("+repr(str(other))+"); denied=[]\nfor op in (lambda:p.read_text(),lambda:socket.create_connection(('127.0.0.1',9),timeout=.1)):\n try: op();denied.append(False)\n except PermissionError: denied.append(True)\nassert denied==[True,True],denied"
   result=run_offline(['-c',code],job/'test.log',10,sandbox_path=policy)
   self.assertEqual(result['returncode'],0)

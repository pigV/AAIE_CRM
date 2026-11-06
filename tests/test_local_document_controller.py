import unittest,tempfile
from pathlib import Path
from tools.staging_control import sandbox_profile
from staging_environment import Staging
from application_document_extraction import ROOT
class ControllerTests(unittest.TestCase):
 def setUp(self):
  (ROOT/'STAGING_DATA/tmp').mkdir(parents=True,exist_ok=True)
 def test_sandboxed_web_process_can_use_file_queue_without_exec_grants(self):
  c=Staging(ROOT/'STAGING_DATA',18960,18961,'local_review_')
  # Test generated policy actually executing a controller under the outer sandbox.
  import subprocess,json,os
  marker=c.root/'local-document-review.json';old=marker.read_bytes() if marker.exists() else None
  try:
   marker.write_text('{"enabled":true}')
   with tempfile.TemporaryDirectory(dir=c.root/'tmp') as tmp:
    policy=Path(tmp)/'outer.sb';policy.write_text(sandbox_profile(c))
    code='from application_document_extraction import DirectoryReviewQueue; from pathlib import Path; q=DirectoryReviewQueue(Path('+repr(str(Path(tmp)/'queue'))+'),"test"); assert q.submit(b"%PDF-test","coe")["status"]=="manual_review"'
    result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(policy),'/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3','-B','-c',code],cwd=ROOT,capture_output=True,text=True)
    self.assertEqual(result.returncode,0,result.stderr)
  finally:
   if old is None:marker.unlink()
   else:marker.write_bytes(old)

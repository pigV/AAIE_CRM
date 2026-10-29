"""Run real Content Extraction with native Staging filesystem guards."""
import argparse
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from staging_environment import ACTIVE as c
if c is None: raise RuntimeError("Native Staging required")
c.assert_sandbox()
parser=argparse.ArgumentParser()
parser.add_argument("--instance",required=True)
args=parser.parse_args()
if len(args.instance)!=32 or any(ch not in "0123456789abcdef" for ch in args.instance):
    raise RuntimeError("Invalid test runtime identity")
os.umask(0o077)
os.nice(10)
sys.path.insert(0,str(c.check(c.root/"extraction-runtime/site-packages")))
from content_extraction.repository import ContentExtractionRepository
from content_extraction_service import build_service_token, build_local_workflow, build_server
repository=ContentExtractionRepository(c.check(c.root/"database/content_extraction_service.db"))
repository.initialize()
repository.recover_interrupted_runs()
token=build_service_token(c.root)
workflow=build_local_workflow(repository,c.root,environ=os.environ)
server=build_server("127.0.0.1",8792,token,repository,c.root,local_workflow=workflow,
                    source_mode="test",runtime_version="test-5985902-"+args.instance)
try: server.serve_forever()
finally: server.server_close()

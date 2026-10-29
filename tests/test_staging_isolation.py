import json
from pathlib import Path
import tempfile
import unittest
from staging_environment import Staging, SOURCE, PRODUCTION_DB, PROTECTED_STORAGE, IsolationError

class StagingContract(unittest.TestCase):
    def setUp(self):
        self.c=Staging.load(SOURCE/'STAGING_DATA/staging.json')
    def test_db_storage_and_descendant_collisions(self):
        for path in [PRODUCTION_DB,*PROTECTED_STORAGE,PROTECTED_STORAGE[0]/'fixture',PROTECTED_STORAGE[1]/'fixture']:
            with self.subTest(path=path),self.assertRaises(IsolationError): self.c.check(path)
    def test_outside_paths_and_traversal_rejected(self):
        for path in [SOURCE/'app.py',self.c.root/'../app.py',Path('relative.db')]:
            with self.subTest(path=path),self.assertRaises(IsolationError): self.c.check(path)
    def test_local_operation_targets_accepted(self):
        for path in [self.c.db,self.c.storage/'upload.txt',self.c.root/'backups/reset']:
            self.assertEqual(self.c.check(path),path)
    def test_production_config_reference_fails_closed(self):
        for root in [str(PROTECTED_STORAGE[0]),str(PROTECTED_STORAGE[1]),str(PROTECTED_STORAGE[0]/'test')]:
            with tempfile.NamedTemporaryFile(mode='w',dir=self.c.root,suffix='.json') as f:
                json.dump({'mode':'staging','root':root,'admin_port':8880,'parent_port':8881,'namespace':'aaie_staging_'},f);f.flush()
                with self.assertRaises(IsolationError): Staging.load(f.name)
    def test_no_sandbox_fails_closed(self):
        with self.assertRaises(IsolationError):self.c.assert_sandbox()
    def test_controller_rejects_unhealthy_identity(self):
        from unittest.mock import patch, MagicMock
        from tools.staging_control import health
        payload={'environment':'staging','service':'admin','port':self.c.admin_port,
                 'database_path':str(self.c.db),'storage_path':str(self.c.storage),
                 'status':'error','dependencies':{'database':'ok','nas':'ok'}}
        with patch('tools.staging_control.urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value=MagicMock(read=lambda:json.dumps(payload))
            with self.assertRaises(IsolationError):health(self.c,'admin')

    def test_port_collision_and_cookie_namespace(self):
        for patch in [{'admin_port':8780},{'parent_port':8880},{'namespace':'aaie_session'},{'mode':'test'}]:
            d=json.loads((SOURCE/'STAGING_DATA/staging.json').read_text());d.update(patch)
            with tempfile.NamedTemporaryFile(mode='w',dir=self.c.root,suffix='.json') as f:
                json.dump(d,f);f.flush()
                with self.assertRaises(IsolationError):Staging.load(f.name)

if __name__=='__main__':unittest.main()

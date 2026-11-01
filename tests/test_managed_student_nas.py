"""Managed production must preserve the original student NAS storage contract."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import nas
import staging_environment
from runtime_configuration import RuntimeConfiguration
from staging_environment import ManagedRuntime

class Settings:
    def setting(self, key, default=None):
        # These migrated legacy settings must not redirect production student files.
        return {'mode':'production', 'production_root':'/obsolete/local/storage',
                'test_root':'/obsolete/local/storage'}.get(key,default)

class ManagedStudentNasTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.source=self.base/'main';self.source.mkdir()
        self.mount=self.base/'NAS';self.mount.mkdir();self.nasroot=self.mount/'CRM_TEST';self.nasroot.mkdir()
        self.schools=self.base/'Schools';self.schools.mkdir()
        for target,value in [('nas.TEST_ROOT',self.nasroot),('nas.STUDENTS_MOUNT',self.mount),
                             ('staging_environment.NAS_READ_ROOTS',(self.nasroot,self.schools))]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.configure('production')
        self.manager=nas.NasManager(Settings())

    def configure(self, mode):
        self.root=self.base/'runtime'/('production' if mode=='production' else 'staging/test')
        (self.root/'config').mkdir(parents=True,exist_ok=True);(self.root/'storage').mkdir(exist_ok=True)
        (self.root/'nas-readonly.json').write_text('{"enabled":true}')
        (self.root/'nas-write.json').write_text(json.dumps({'enabled':mode=='production'}))
        path=self.root/'config/runtime.json'
        path.write_text(json.dumps(dict(version=1,mode=mode,root=str(self.root),database='staging.db',
            admin_port=8780 if mode=='production' else 31880,parent_port=8781 if mode=='production' else 31881,
            extraction_port=8792 if mode=='production' else 31892,
            namespace='aaie_production_' if mode=='production' else 'aaie_staging_test_')))
        self.adapter=ManagedRuntime.from_configuration(RuntimeConfiguration.load(path,source_root=self.source))
        p=patch.object(nas,'STAGING',self.adapter);p.start();self.addCleanup(p.stop)

    def test_new_student_and_categories_are_created_on_nas(self):
        folder=self.manager.create_student_tree('TEST001','NAS','Student')
        self.assertEqual(folder,self.nasroot/'NAS Student')
        self.assertEqual({p.name for p in folder.iterdir()},
            {'Applications','Offer','COE + OSHC','VISA','Homestay + Airport Pickup','Forms','Payments','Others'})
        self.assertEqual(list(self.adapter.storage.iterdir()),[])
        self.assertEqual(self.manager.status()['root'],str(self.nasroot))

    def test_duplicate_name_keeps_legacy_student_id_suffix(self):
        (self.nasroot/'NAS Student').mkdir()
        folder=self.manager.create_student_tree('TEST002','NAS','Student')
        self.assertEqual(folder,self.nasroot/'NAS Student (TEST002)')

    def test_missing_nas_does_not_create_local_student_or_report_ready(self):
        self.nasroot.rmdir()
        state=self.manager.status()
        self.assertFalse(state['ready']);self.assertEqual(state['access_error_code'],'root_unavailable')
        with self.assertRaises(nas.NasSafetyError):self.manager.create_student_tree('TEST003','No','Nas')
        self.assertEqual(list(self.adapter.storage.iterdir()),[])
        self.assertFalse(self.nasroot.exists())

    def test_read_denied_nas_does_not_report_ready(self):
        scan=nas.os.scandir
        def denied(path):
            if Path(path)==self.nasroot:raise PermissionError('NAS read denied')
            return scan(path)
        with patch('nas_access_capability.os.scandir',side_effect=denied):
            state=self.manager.status()
            self.assertFalse(state['ready']);self.assertEqual(state['access_error_code'],'read_denied')

    def test_missing_write_authorization_does_not_fall_back_to_local(self):
        (self.root/'nas-write.json').write_text('{"enabled":false}')
        self.assertFalse(self.manager.status()['ready'])
        with self.assertRaises(nas.NasSafetyError):self.manager.create_student_tree('TEST004','No','Grant')
        self.assertEqual(list(self.nasroot.iterdir()),[])
        self.assertEqual(list(self.adapter.storage.iterdir()),[])

    def test_real_write_probe_failure_prevents_student_tree_creation(self):
        real_open=nas.os.open
        def denied(path,*args,**kwargs):
            if Path(path).parent==self.nasroot:raise PermissionError('NAS write denied')
            return real_open(path,*args,**kwargs)
        with patch('nas_access_capability.os.open',side_effect=denied):
            with self.assertRaises(nas.NasSafetyError):self.manager.create_student_tree('TEST005','No','Write')
        self.assertEqual(list(self.nasroot.iterdir()),[])
        self.assertEqual(list(self.adapter.storage.iterdir()),[])

    def test_existing_local_student_attachment_remains_readable_and_writable(self):
        folder=self.adapter.storage/'Existing Student';(folder/'Applications').mkdir(parents=True)
        student={'folder_path':str(folder)};p=folder/'Applications/document.txt';p.write_text('existing attachment')
        self.assertEqual(self.manager.require_student_file_readable(student,p),p)
        self.assertEqual(self.manager.category_folder(student,'Applications'),folder/'Applications')
        self.assertEqual(p.read_text(),'existing attachment')

    def test_existing_nas_student_upload_and_read_stay_in_same_folder(self):
        folder=self.nasroot/'Existing Student';(folder/'Applications').mkdir(parents=True)
        student={'folder_path':str(folder)}
        p=self.manager.category_folder(student,'Applications')/'document.txt';p.write_bytes(b'attachment')
        self.assertEqual(self.manager.require_student_file_readable(student,p).read_bytes(),b'attachment')
        self.assertEqual(list(self.adapter.storage.iterdir()),[])

    def _assert_existing_local_access(self):
        folder=self.adapter.storage/'Local Survivor';(folder/'Applications').mkdir(parents=True)
        student={'folder_path':str(folder)};p=folder/'Applications/original.txt';p.write_bytes(b'local-original')
        self.assertEqual(self.manager.require_student_file_readable(student,p).read_bytes(),b'local-original')
        upload=self.manager.category_folder(student,'Applications')/'new.txt';upload.write_bytes(b'local-upload')
        self.assertEqual(self.manager.require_student_file_readable(student,upload).read_bytes(),b'local-upload')

    def test_existing_local_attachment_survives_missing_nas(self):
        self.nasroot.rmdir()
        self._assert_existing_local_access()
        self.assertFalse(self.manager.status()['ready'])

    def test_existing_local_attachment_survives_revoked_nas_write_grant(self):
        (self.root/'nas-write.json').write_text('{"enabled":false}')
        self._assert_existing_local_access()
        self.assertFalse(self.manager.status()['ready'])

    def test_outside_student_folder_is_rejected(self):
        for outside in (self.base/'Other',self.mount/'2026/Student'):
            with self.subTest(path=outside),self.assertRaises(RuntimeError):
                self.manager.student_folder({'folder_path':str(outside)})

    def test_staging_keeps_local_creation_despite_readonly_nas_grant(self):
        self.configure('staging')
        folder=self.manager.create_student_tree('TEST006','Local','Test')
        self.assertEqual(folder,self.adapter.storage/'Local Test')
        self.assertEqual(list(self.nasroot.iterdir()),[])

if __name__=='__main__':unittest.main()

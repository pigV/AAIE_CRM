import unittest
from unittest.mock import patch
from tests import test_lifecycle_module_dispositions as fixture
from business_changes.schema import install


class ModulePolicySyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleModuleDispositionTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.connection
        if not self.c.in_transaction:self.c.execute('BEGIN')
        install(self.c);self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
    def test_existing_policy_retry_and_requirement_change_publish_once(self):
        first=self.f.record();self.assertEqual(first,self.f.record())
        self.f.record(event_uid='synthetic-required',disposition='required',source_record_type=None,source_record_id=None,reason='Synthetic requirement changed')
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
    def test_failed_publication_leaves_no_partial_policy(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.f.record()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM lifecycle_module_disposition_events').fetchone()[0])

import unittest
from unittest.mock import patch

from tests import test_business_change_case_snapshots as fixture
from business_changes.contracts import Subject
from business_changes.versions import version_of
from higher_education_repository import HigherEducationRepository


class HigherEducationMasterStaffTests(unittest.TestCase):
    setUp=fixture.VisaCaseSnapshotTests.setUp

    def provider(self):
        with self.db.transaction() as connection:
            return HigherEducationRepository(connection).create_provider('SYNTHETIC ADMIN UNIVERSITY','University',1,self.f.stamp,review_status='Approved')

    def test_original_provider_staff_audit_failure_rolls_back_master_and_event(self):
        self.h.form={'provider_name':'SYNTHETIC FAILED UNIVERSITY','provider_type':'University'}
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        with patch.object(self.db,'audit',side_effect=RuntimeError('synthetic staff audit failure')):
            with self.assertRaises(RuntimeError):self.h.higher_education_provider_create('POST')
        self.assertIsNone(self.db.one("SELECT id FROM higher_education_providers WHERE normalized_name='synthetic failed university'"))
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_university_update_publishes_source_and_defers_response_until_commit(self):
        provider=self.provider()
        with self.db.read_snapshot() as connection:before=version_of(connection,Subject('he_provider',str(provider)))
        self.h.form={'provider_name':'SYNTHETIC UPDATED UNIVERSITY','active_status':'Inactive'}
        self.h.australian_university_update('POST',str(provider))
        with self.db.read_snapshot() as connection:
            self.assertGreater(version_of(connection,Subject('he_provider',str(provider))),before)
        self.assertEqual('Inactive',self.db.one('SELECT active_status FROM higher_education_providers WHERE id=?',(provider,))[0])

    def test_former_admin_cannot_update_shared_university(self):
        provider=self.provider()
        role=self.db.one("SELECT id FROM roles WHERE name='Consultant'")[0]
        self.db.execute('UPDATE users SET role_id=? WHERE id=1',(role,))
        self.h.form={'provider_name':'SYNTHETIC UNAUTHORIZED UPDATE','active_status':'Inactive'}
        result=self.h.australian_university_update('POST',str(provider))
        self.assertEqual(403,result[0],result)
        self.assertEqual('Active',self.db.one('SELECT active_status FROM higher_education_providers WHERE id=?',(provider,))[0])

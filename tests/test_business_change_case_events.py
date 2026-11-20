import sqlite3
import unittest
from unittest.mock import patch
from tests import test_student_lifecycle_case_integration as fixture
from tests.student_lifecycle_test_support import activate_progression
from business_changes.schema import install


class CaseEventSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.StudentLifecycleCaseIntegrationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.c=sqlite3.connect(self.f.database_path);self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute("INSERT OR REPLACE INTO system_settings(key,value) VALUES('business_change_sync_v1','enabled')");self.c.commit()
        draft=self.f.create_progression_draft();activate_progression(self.f.progression_service,draft)
        self.case_id=draft.case_id
    def material(self):return self.f.progression_service.add_material(self.case_id,actor_user_id=1,
        actor_permissions={'progression_cases.materials'},flags={'read_enabled':True,'progression_write_enabled':True},
        material_key='synthetic-extra',material_name='Synthetic extra material',is_required=True,
        student_document_id=None,upload_metadata={},notes=None)
    def test_material_service_event_refreshes_its_case_student_and_cycle(self):
        before=self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]
        self.material()
        self.assertEqual(before+1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        rows=self.c.execute('SELECT subject_kind FROM business_change_impacts WHERE event_seq=(SELECT max(seq) FROM business_change_events)').fetchall()
        self.assertTrue({'student','student_case','application_cycle','dashboard','module'}<={r[0] for r in rows})
    def test_failure_rolls_back_material_and_original_domain_event(self):
        before={t:self.c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('progression_case_materials','progression_case_events','business_change_events')}
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.material()
        for table,count in before.items():self.assertEqual(count,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)

"""Persisted module handling must survive Transfer non-blocking projection."""
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_lifecycle_disposition_business_ui import LifecycleDispositionBusinessUiTests
from student_lifecycle.module_progress import load_transfer_v2_module_progress
from student_lifecycle.disposition_ui import render_business_disposition_control
from student_progression_summary import load_lifecycle_workflow_modules


class TransferVisaPersistenceTests(LifecycleDispositionBusinessUiTests):
    def modules(self):
        projection = SimpleNamespace(stage_key='materials_preparation', stage_order=10,
            current_branch_id=None, current_offer_event_id=None, payment_id=None,
            coe_confirmation_id=None, blocker='missing materials')
        items=load_transfer_v2_module_progress(projection)
        with patch('student_progression_summary.load_lifecycle_projection_map',
                   return_value={7: SimpleNamespace(lifecycle_type='transfer',
                         modules_by_key={x.module_key:x for x in items})}):
            return {x['key']:x for x in load_lifecycle_workflow_modules(self.connection,7)}

    def control(self, module):
        return render_business_disposition_control(module,
            {'student_id':'S1','cycle_id':7,'csrf':'test'},event_uid='test-event')

    def test_existing_persisted_event_wins_over_nonblocking_projection_without_writes(self):
        before=self.connection.total_changes
        for attempt in range(3):
            visa=self.modules()['visa']
            self.assertEqual('not_applicable',visa['state'])
            self.assertTrue(visa['completed'])
            self.assertIn('value="reuse" selected',self.control(visa))
            self.assertIn('value="visa:31" selected',self.control(visa))
        self.assertEqual(before,self.connection.total_changes)

    def test_latest_explicit_new_wins_over_older_existing(self):
        self.connection.execute("""INSERT INTO lifecycle_module_disposition_events VALUES
          (3,'new-visa',7,'visa','required',NULL,NULL,NULL,1,
           '2026-09-07T10:00:00','2026-09-07T10:00:00')""")
        html=self.control(self.modules()['visa'])
        self.assertIn('value="new" selected',html)
        self.assertNotIn('value="visa:31" selected',html)

    def test_absent_persisted_value_initializes_new_without_cross_cycle_reuse(self):
        self.connection.execute("DELETE FROM lifecycle_module_disposition_events WHERE application_cycle_id=7")
        self.connection.execute("""INSERT INTO lifecycle_module_disposition_events VALUES
          (5,'other-visa',2,'visa','existing','old','visa',32,1,
           '2026-09-08T10:00:00','2026-09-08T10:00:00')""")
        self.assertIn('value="new" selected',self.control(self.modules()['visa']))

    def test_unrelated_module_save_does_not_change_existing_selection(self):
        self.connection.execute("""INSERT INTO lifecycle_module_disposition_events VALUES
          (4,'skip-accommodation',7,'homestay_airport','not_applicable','No need',NULL,NULL,1,
           '2026-09-07T10:00:00','2026-09-07T10:00:00')""")
        modules=self.modules()
        self.assertIn('value="reuse" selected',self.control(modules['visa']))
        self.assertIn('value="skip" selected',self.control(modules['homestay_airport']))

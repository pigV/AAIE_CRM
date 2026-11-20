import unittest
from unittest.mock import patch
from tests import test_business_change_checklist as fixture


class ChecklistCommandTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ChecklistSyncTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=self.f.c

    def save(self,*,key='one',version=0,notes='first',authorize=lambda view:None,student_id='S1'):
        from business_changes.materials import save_checklist_command
        return save_checklist_command(self.c,student_id=student_id,case_id=1,
            updates=((8,'uploaded',notes),),actor_user_id=1,stamp='2026-11-20T00:00:00+11:00',
            command_key=key,expected_version=version,authorize=authorize)

    def test_retry_returns_receipt_without_a_second_event(self):
        first=self.save();second=self.save()
        self.assertFalse(first.replayed);self.assertTrue(second.replayed)
        self.assertEqual(first.result,second.result)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_competing_old_form_cannot_overwrite_first_commit(self):
        from business_changes.commands import VersionConflict
        self.save()
        with self.assertRaises(VersionConflict):self.save(key='two',notes='stale form')
        self.assertEqual('first',self.c.execute('SELECT notes FROM visa_checklist_items WHERE id=8').fetchone()[0])
        self.save(key='two',version=1,notes='fresh form')
        self.assertEqual('fresh form',self.c.execute('SELECT notes FROM visa_checklist_items WHERE id=8').fetchone()[0])

    def test_changed_retry_payload_and_foreign_owner_are_rejected(self):
        from business_changes.commands import CommandConflict
        self.save()
        with self.assertRaises(CommandConflict):self.save(notes='changed retry')
        with self.assertRaises(ValueError):self.save(student_id='FOREIGN')

    def test_replay_still_requires_current_authorization(self):
        self.save()
        def denied(view):raise PermissionError('synthetic revoked')
        with self.assertRaises(PermissionError):self.save(authorize=denied)

    def test_publish_failure_rolls_back_source_and_receipt(self):
        with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.save()
        self.assertEqual('missing',self.c.execute('SELECT status FROM visa_checklist_items WHERE id=8').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_commands').fetchone()[0])

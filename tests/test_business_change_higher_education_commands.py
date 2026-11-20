import unittest

from tests import test_business_change_higher_education_staff as fixture
from business_changes.commands import CommandConflict,VersionConflict
from business_changes.contracts import Subject
from business_changes.versions import version_of
from higher_education_repository import HigherEducationRepository,HigherEducationValidationError


class HigherEducationCommandsTests(unittest.TestCase):
    setUp=fixture.HigherEducationStaffBoundaryTests.setUp
    prepare=fixture.HigherEducationStaffBoundaryTests.prepare

    def save(self,*,nonce='synthetic-he-command',expected=0,payload=None,action='create_plan',identifier=None,operation=None):
        from business_changes.higher_education_commands import save_action
        with self.db.transaction() as connection:
            return save_action(connection,action=action,identifier=identifier or self.f.cycle,
                actor=self.h.user,nonce=nonce,expected_version=expected,payload=payload or {},
                occurred_at=self.f.stamp,authorize=lambda view:None,
                operation=operation or (lambda view:HigherEducationRepository(view).create_plan(self.f.cycle,1,self.f.stamp)))

    def test_same_form_retry_has_one_plan_audit_and_receipt(self):
        self.prepare();first=self.save();second=self.save()
        self.assertFalse(first.replayed);self.assertTrue(second.replayed)
        self.assertEqual(first.result,second.result)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

    def test_changed_payload_and_stale_source_do_not_write(self):
        self.prepare();self.save(payload={'intake':'2027'})
        with self.assertRaises(CommandConflict):self.save(payload={'intake':'2028'})
        with self.assertRaises(VersionConflict):self.save(nonce='new-synthetic-command')
        self.assertEqual(1,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

    def test_replay_deleted_item_rechecks_current_student_ownership(self):
        self.prepare()
        with self.db.transaction() as connection:
            repo=HigherEducationRepository(connection)
            provider=repo.create_provider('SYNTHETIC UNIVERSITY','University',1,self.f.stamp,review_status='Approved')
            plan=repo.create_plan(self.f.cycle,1,self.f.stamp)
            item=repo.add_plan_item(plan,provider,'Bachelor','SYNTHETIC DEGREE','2027',1,self.f.stamp)
            expected=version_of(connection,Subject('he_plan_item',str(item)))
        def remove(view):
            HigherEducationRepository(view).remove_draft_plan_item(item,actor_user_id=1,occurred_at=self.f.stamp)
        result=self.save(action='remove_item',identifier=item,expected=expected,operation=remove)
        self.assertFalse(result.replayed)
        self.assertTrue(self.save(action='remove_item',identifier=item,expected=expected,operation=remove).replayed)
        self.db.execute('UPDATE students SET deleted_at=? WHERE student_id=?',(self.f.stamp,self.f.sid))
        with self.assertRaises(HigherEducationValidationError):self.save(action='remove_item',identifier=item,expected=expected,operation=remove)

    def test_failed_operation_rolls_back_even_if_outer_caller_catches(self):
        self.prepare()
        def write(view):
            HigherEducationRepository(view).create_plan(self.f.cycle,1,self.f.stamp)
            raise ValueError('synthetic failure')
        with self.assertRaises(ValueError):self.save(operation=write)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans')[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM business_change_commands WHERE command_type LIKE 'higher_education_staff_%'")[0])

    def test_closed_cycle_rejects_new_writes(self):
        self.prepare()
        with self.db.transaction() as connection:
            from application_cycle import close_cycle
            close_cycle(connection,self.f.cycle,'completed',self.f.stamp,1)
        with self.assertRaises(HigherEducationValidationError):
            self.save(nonce='new-closed-cycle-intent')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

    def test_success_receipt_can_be_checked_after_cycle_closes(self):
        self.prepare();self.save()
        with self.db.transaction() as connection:
            from application_cycle import close_cycle
            close_cycle(connection,self.f.cycle,'completed',self.f.stamp,1)
        self.assertTrue(self.save().replayed)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM higher_education_plans')[0])

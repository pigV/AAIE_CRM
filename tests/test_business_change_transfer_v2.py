import sqlite3
import unittest
from uuid import uuid4
from tests import test_transfer_v2_admissions as fixture
from student_lifecycle.transfer_v2_admissions import TransferV2AdmissionsService,TransferV2AdmissionsValidationError
from student_lifecycle.transfer_v2_service import TransferV2Service
from student_lifecycle.transfer_v2_models import TransferV2TargetInput


class TransferRetryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TransferV2AdmissionsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.service=TransferV2AdmissionsService(self.f.database_path,clock=lambda:fixture.NOW)
    def kwargs(self):return dict(cycle_id=self.f.overview.cycle_id,branch_id=self.f.branch.branch_id,
        offer_event_id=self.f.offer_new,actor_user_id=1,idempotency_key='default-production-retry')
    def test_default_identity_replays_same_retained_offer_state(self):
        first=self.service.confirm_offer_valid(**self.kwargs());second=self.service.confirm_offer_valid(**self.kwargs())
        self.assertEqual(first,second)
    def test_default_identity_rejects_reused_key_with_changed_offer(self):
        self.service.confirm_offer_valid(**self.kwargs());kw=self.kwargs();kw['offer_event_id']=self.f.offer_old
        with self.assertRaises(TransferV2AdmissionsValidationError):self.service.confirm_offer_valid(**kw)
    def test_legacy_random_identity_is_replayed_without_rewriting_history(self):
        legacy=TransferV2AdmissionsService(self.f.database_path,clock=lambda:fixture.NOW,uid_factory=lambda prefix:prefix+':'+uuid4().hex)
        first=legacy.confirm_offer_valid(**self.kwargs());second=self.service.confirm_offer_valid(**self.kwargs())
        self.assertEqual(first,second)
    def test_default_target_branch_retry_returns_same_branch(self):
        creator=TransferV2Service(self.f.database_path,clock=lambda:fixture.NOW)
        kw=dict(target=TransferV2TargetInput(None,'Synthetic Extra School',state='VIC'),actor_user_id=1,idempotency_key='default-new-branch')
        first=creator.add_target_branch(self.f.overview.cycle_id,**kw)
        second=creator.add_target_branch(self.f.overview.cycle_id,**kw)
        self.assertEqual(first,second)

    def test_target_branch_reused_key_with_changed_school_is_rejected(self):
        from student_lifecycle.transfer_v2_service import TransferV2ValidationError
        creator=TransferV2Service(self.f.database_path,clock=lambda:fixture.NOW)
        kw=dict(target=TransferV2TargetInput(None,'Synthetic Extra School',state='VIC'),actor_user_id=1,idempotency_key='changed-target')
        creator.add_target_branch(self.f.overview.cycle_id,**kw)
        kw['target']=TransferV2TargetInput(None,'Different Synthetic School',state='VIC')
        with self.assertRaises(TransferV2ValidationError):creator.add_target_branch(self.f.overview.cycle_id,**kw)

    def test_library_request_reused_key_with_changed_payload_is_rejected(self):
        from student_lifecycle.transfer_v2_service import TransferV2ValidationError
        creator=TransferV2Service(self.f.database_path,clock=lambda:fixture.NOW)
        overview=creator.add_target_branch(self.f.overview.cycle_id,target=TransferV2TargetInput(None,'Synthetic Library School',state='VIC'),actor_user_id=1,idempotency_key='library-target')
        branch=next(x for x in overview.branches if x.school_name=='Synthetic Library School')
        kw=dict(branch_id=branch.branch_id,proposed_name='Synthetic Library School',proposed_state='VIC',proposed_region=None,proposed_school_type=None,actor_user_id=1,idempotency_key='same-library-request')
        first=creator.request_school_library(self.f.overview.cycle_id,**kw)
        self.assertEqual(first,creator.request_school_library(self.f.overview.cycle_id,**kw))
        kw['proposed_region']='Changed Region'
        with self.assertRaises(TransferV2ValidationError):creator.request_school_library(self.f.overview.cycle_id,**kw)

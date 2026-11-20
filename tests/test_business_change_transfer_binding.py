import unittest
from unittest.mock import patch
from tests import test_transfer_v2_authoritative_business_records as fixture
from tests.test_business_change_transfer_sync import enable
from student_lifecycle.transfer_v2_business_records import bind_authoritative_offer,TransferV2BusinessRecordError


class TransferBusinessBindingSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TransferV2AuthoritativeBusinessRecordTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=enable(self.f.database_path);self.addCleanup(self.c.close)
        self.c.execute("INSERT INTO offers(id,student_id,offer_status) VALUES(501,'S1','received')")
    def bind(self,document_id=None):return bind_authoritative_offer(self.c,student_id='S1',branch_id=self.f.branch_id,
        offer_id=501,document_id=document_id,actor_user_id=1,occurred_at=fixture.NOW)
    def test_record_binding_retry_has_one_shared_binding_event(self):
        first=self.bind();second=self.bind();self.assertEqual(first,second)
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM business_change_events WHERE event_type='transfer.business_record_bound'").fetchone()[0])
    def test_binding_publish_failure_keeps_source_unbound(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.bind()
        self.assertIsNone(self.c.execute('SELECT application_cycle_id FROM offers WHERE id=501').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM case_record_links').fetchone()[0])
    def test_foreign_document_reference_is_rejected_before_linking(self):
        self.c.execute("INSERT INTO student_documents(id,student_id,document_type,file_path,uploaded_at) VALUES(11,'S2','Offer','/synthetic/foreign.pdf',?)",(fixture.NOW,))
        with self.assertRaises(TransferV2BusinessRecordError):self.bind(11)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM case_record_links').fetchone()[0])

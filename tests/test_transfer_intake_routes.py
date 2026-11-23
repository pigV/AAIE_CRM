"""Verify real completion and recovery handlers retain signed facts and enforce scope."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app
from database import Database
from tests.transfer_v2_test_support import create_transfer_v2_database

NOW='2026-11-23T11:00:00+11:00'

class TransferCompletionHandoffRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'routes.db';create_transfer_v2_database(self.path)
        self.db=Database(self.path)
        self.override=patch.object(app,'db',self.db);self.override.start();self.addCleanup(self.override.stop)
        with self.db.transaction() as conn:
            conn.executescript('''CREATE TABLE external_transfer_case_requests(
              id INTEGER PRIMARY KEY,student_id TEXT,contract_id INTEGER,idempotency_key TEXT UNIQUE,
              request_status TEXT,case_id INTEGER,attempt_count INTEGER DEFAULT 0,last_error_code TEXT,
              last_error_message TEXT,next_retry_at TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT);''')

    def test_real_completion_keeps_signature_and_records_recoverable_handoff_failure(self):
        with patch.object(app,'emit_contract_business_event',return_value={'signed':True}),patch.object(app,'is_workflow_v2_active',return_value=True):
            result=app.record_contract_completion_fact(102,NOW,1)
        self.assertEqual({'signed':True},result)
        pending=self.db.one('SELECT * FROM external_transfer_case_requests WHERE contract_id=102')
        self.assertIsNotNone(pending,'Completion must record downstream handoff failure')
        self.assertEqual('failed',pending['request_status'])
        self.assertEqual('FullyExecuted',self.db.one('SELECT contract_status FROM contracts WHERE id=102')[0])

    def test_recovery_route_rejects_contract_owned_by_another_student(self):
        handler=object.__new__(app.CRMHandler); handler.user={'id':1};handler.form={};handler.csrf='c'
        handler.require=lambda *a: True; handler.student_capability_subject=lambda s: {'student_id':s}
        response={};handler.error_page=lambda message,status:response.update(status=status)
        method=getattr(handler,'student_transfer_intake_action',None)
        self.assertIsNotNone(method,'Scoped recovery handler is missing')
        with patch.object(app,'can_manage_transfer_contract',return_value=True):
            method('POST','S1','102','retry')
        self.assertEqual(404,response['status'])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM external_transfer_case_requests')[0])

class AutomaticCompletionRevenueTests(unittest.TestCase):
    def setUp(self):
        from tests import test_transfer_intake_financial as support
        self.fixture=support.TransferIntakeFinancialTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db=Database(self.fixture.f.path)
        replacement=patch.object(app,'db',self.db);replacement.start();self.addCleanup(replacement.stop)

    def test_signing_completion_automatically_creates_one_case_and_fee(self):
        with patch.object(app,'emit_contract_business_event',return_value={'signed':True}),patch.object(app,'is_workflow_v2_active',return_value=True):
            app.record_contract_completion_fact(102,NOW,1)
            app.record_contract_completion_fact(102,NOW,1)
        self.assertEqual('completed',self.db.one('SELECT request_status FROM external_transfer_case_requests WHERE contract_id=102')[0])
        self.assertEqual([(240000,0)],[tuple(r) for r in self.db.all("SELECT net_cents,gst_cents FROM revenue_fee_items WHERE student_id='S2'")])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM application_cycles WHERE student_id='S2' AND status='active'")[0])
        self.assertEqual('FullyExecuted',self.db.one('SELECT contract_status FROM contracts WHERE id=102')[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM revenue_payments")[0], 'Signing must not invent a customer payment')

    def test_existing_customer_manual_entry_keeps_1200_price_and_current_relationship(self):
        from student_lifecycle.transfer_v2_models import TransferV2CreateInput
        from student_lifecycle.transfer_v2_service import TransferV2Service
        request=TransferV2CreateInput('S1',101,'aaie_existing',10,'Current College','Year 8','Year 9',2027,'Term 1',())
        overview=TransferV2Service(self.fixture.f.path,clock=lambda:NOW).create_cycle(request,actor_user_id=1,idempotency_key='existing-price-regression')
        self.assertEqual([(120000,0)],[tuple(r) for r in self.db.all("SELECT net_cents,gst_cents FROM revenue_fee_items WHERE student_id='S1'")])
        self.assertEqual(80,self.db.one('SELECT current_school_enrolment_id FROM transfer_cases WHERE case_id=?',(overview.case_id,))[0])

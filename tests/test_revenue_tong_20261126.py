import json
from pathlib import Path
import unittest

from revenue.read_models import RevenueReadModels
from revenue.views import render_revenue_case_workbench, render_revenue_workspace_page
from revenue.workbench import RevenueWorkbench
from tests.revenue_test_support import RevenueDatabaseFixture


class TongRevenueDisplayTests(RevenueDatabaseFixture, unittest.TestCase):
    def _seed(self, db):
        db.executescript("""
            ALTER TABLE visa_cases ADD COLUMN student_id TEXT;
            ALTER TABLE visa_cases ADD COLUMN applicant_name TEXT;
            ALTER TABLE visa_cases ADD COLUMN case_type TEXT;
            ALTER TABLE visa_cases ADD COLUMN archived_at TEXT;
            INSERT INTO visa_cases VALUES(77,100,'S-REVENUE','Mother Example','590 Guardian Visa',NULL);
            INSERT INTO visa_cases VALUES(81,100,'S-REVENUE','Father Example','600 Visitor Visa',NULL);
            INSERT INTO visa_cases VALUES(79,100,'S-REVENUE','Student Example','Renewal',NULL);
            INSERT INTO revenue_commercial_cases VALUES(1,'case-1',100,'S-REVENUE','open',0,'{}',1,'2026-09-01',NULL);
            INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-09-01','2026-09-01T00:00:00Z');
            INSERT INTO revenue_billing_accounts VALUES(1,'account-1','individual','S-REVENUE','AUD','active',1,'2026-09-01');
            INSERT INTO revenue_payment_methods VALUES(1,'method-1','au_personal_account','澳洲个人账户','active');
            INSERT INTO revenue_payment_method_rule_versions VALUES(1,'rule-1',1,'2026-01-01',NULL,0,0,'active','2026-09-01');
        """)
        migrations = Path(__file__).resolve().parents[1] / 'migrations'
        for name in ('20261125_056_visa_payment_authority.sql', '20261125_057_visa_allocation_recheck.sql'):
            db.executescript((migrations / name).read_text())
        for index, (case_id, milestone) in enumerate((
            (77, 'guardian-590'), (81, 'visitor-600'), (79, 'student-visa-renewal'), (None, 'k12-new')
        ), 1):
            item = dict(visa_case_id=case_id, application_cycle_id=100, milestone_code=milestone, net_cents=90000)
            db.execute(
                "INSERT INTO revenue_rule_snapshots VALUES(?,?,'fee','rule',?,?,1,'2026-09-01')",
                (index, f'snapshot-{index}', json.dumps({'items': [item]}),
                 f'{index:064x}'),
            )
            db.execute(
                """INSERT INTO revenue_fee_items
                     (id,uid,commercial_case_id,billing_account_id,student_id,rule_snapshot_id,
                      milestone_code,description,net_cents,gst_cents,gross_cents,currency_code,status,created_at)
                   VALUES(?,?,1,1,'S-REVENUE',?,?,?,90000,0,90000,'AUD','unbilled','2026-09-01')""",
                (index, f'fee-{index}', index, milestone, 'Visa service fee' if case_id else 'Client service fee'),
            )
            if index in (1, 2):
                db.execute(
                    "INSERT INTO revenue_fee_item_visa_links VALUES(?,?,?,?,1,1,'2026-09-01')",
                    (index, f'visa-link-{index}', index, case_id),
                )
        db.execute("INSERT INTO revenue_fee_item_contract_links VALUES(4,'contract-link-4',4,70,1,1,'2026-09-01')")
        for index in (1, 2, 4):
            db.execute(
                """INSERT INTO revenue_documents
                      (id,uid,document_kind,storage_path,sha256,mime_type,size_bytes,source_kind,created_by,created_at)
                   VALUES(?,?,'client_payment_evidence',?,?, 'application/pdf',1,'upload',1,'2026-09-01')""",
                (index, f'proof-{index}', f'isolated-proof-{index}.pdf', f'{index:064x}'),
            )
            db.execute(
                """INSERT INTO revenue_payments
                      (id,uid,payment_direction,counterparty_kind,billing_account_id,payment_method_id,
                       received_date,amount_cents,allocated_cents,currency_code,status,evidence_document_id,confirmed_by,confirmed_at)
                   VALUES(?,?,'received','client',1,1,'2026-09-01',90000,90000,'AUD','confirmed',?,1,'2026-09-01')""",
                (index, f'payment-{index}', index),
            )
            db.execute(
                """INSERT INTO revenue_client_payment_allocations
                      (uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,gross_charge_aud_cents,
                       payment_method_rule_version_id,method_code_snapshot,gst_rate_basis_points,created_by,created_at)
                   VALUES(?,?,?,90000,0,90000,1,'au_personal_account',0,1,'2026-09-01')""",
                (f'allocation-{index}', index, index),
            )

    def test_unclassified_fee_omits_warning_in_billing_rows_and_payment_picker(self):
        with self.connect() as db:
            self._seed(db)
            data = RevenueReadModels(db).workspace('billing', {}, case_uid='case-1')
            page = render_revenue_workspace_page(data, 'csrf', 'token', mobile=False)
        self.assertIn('Student Example · 签证服务费 · 案件 #79', page)
        self.assertNotIn('类别待核实', page)
        self.assertNotIn('500', page)

    def test_payment_history_uses_each_allocated_visa_case(self):
        with self.connect() as db:
            self._seed(db)
            data = RevenueWorkbench(db).load('case-1')
            pages = tuple(render_revenue_case_workbench(data, mobile=mobile) for mobile in (False, True))
        rows = {row['evidence_view_url'].split('/')[3]: row for row in data['payment_history']}
        self.assertEqual(('Mother Example', '590 陪读签证'),
                         (rows['proof-1']['visa_applicant_name'], rows['proof-1']['visa_category']))
        self.assertEqual(('Father Example', '600 旅游签证'),
                         (rows['proof-2']['visa_applicant_name'], rows['proof-2']['visa_category']))
        self.assertIsNone(rows['proof-4']['visa_applicant_name'])
        for page in pages:
            history = page.split('<h3>收款记录</h3>', 1)[1]
            for value in ('申请人', '签证类别', 'Mother Example', '590 陪读签证', 'Father Example', '600 旅游签证'):
                self.assertIn(value, history)

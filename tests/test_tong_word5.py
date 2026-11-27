"""Approved Tong Word 5 collection, correction and display regression."""
import unittest
from tests.test_revenue_client_payment_v1 import RevenueClientPaymentV1Tests
from revenue.read_models import RevenueReadModels
from revenue.views import render_revenue_case_workbench

class TongWord5Tests(RevenueClientPaymentV1Tests):
    def test_cash_aud_zero_gst_is_saved_and_replayed_once(self):
        kwargs=dict(method="cash",actual_currency="AUD",actual_amount=7129,base=7129,key="word5-cash")
        result=self._record(**kwargs)
        replay=self._record(**kwargs)
        self.assertEqual(result["payment"]["uid"],replay["payment"]["uid"])
        row=self.connection.execute("SELECT * FROM revenue_client_payment_allocations").fetchone()
        self.assertEqual(("cash",7129,0),(row["method_code_snapshot"],row["allocated_base_cents"],row["gst_cents"]))
        self.assertEqual(1,self.connection.execute("SELECT count(*) FROM revenue_payments").fetchone()[0])

    def test_cash_rejects_wrong_currency_and_nonzero_gst_rule(self):
        with self.assertRaisesRegex(ValueError,"must be AUD"):
            self._record(method="cash",actual_currency="CNY",actual_amount=100,base=100,key="wrong-currency")
        self.connection.execute("UPDATE revenue_payment_method_rule_versions SET gst_rate_basis_points=1000 WHERE payment_method_id=3")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError,"GST"):
            self._record(method="cash",actual_currency="AUD",actual_amount=110,base=100,key="wrong-gst")
        self.assertEqual(0,self.connection.execute("SELECT count(*) FROM revenue_payments").fetchone()[0])

    def test_cash_is_available_in_actual_collection_form_data(self):
        workspace=RevenueReadModels(self.connection).workspace("billing")
        cash=next((m for m in workspace["action"]["payment_methods"] if m["code"]=="cash"),None)
        self.assertIsNotNone(cash)
        self.assertEqual(("AUD",0),(cash["currency_code"],cash["gst_rate_basis_points"]))

    def test_hidden_balances_are_not_rendered_but_gst_and_payment_action_remain(self):
        item=dict(description="Client service fee",final_base_cents=220000,base_received_cents=7129,
                  gst_collected_cents=0,outstanding_base_cents=212871,hide_base_balances=True,
                  add_payment_href="/revenue/cases/case-1/billing")
        data=dict(schema_version="revenue.v1",title="收费与佣金档案",sections=[dict(key="client_billing",title="客户收费",rows=[])],client_billing_summary=[item])
        for mobile in (False,True):
            html=render_revenue_case_workbench(data,mobile=mobile)
            self.assertNotIn("基础服务费已收",html)
            self.assertNotIn("未收基础服务费",html)
            self.assertIn("2,200.00",html)
            self.assertIn("GST 已收",html)
        item["hide_base_balances"]=False
        html=render_revenue_case_workbench(data)
        self.assertIn("基础服务费已收",html)
        self.assertIn("未收基础服务费",html)

    def test_fee_correction_preserves_contract_snapshot_and_actual_payment(self):
        from revenue.fee_corrections import correct_service_fee
        self.connection.execute("CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        self._record(method="au_personal_account",actual_currency="AUD",actual_amount=7129,base=7129,key="prior-payment")
        tables=("contracts","revenue_rule_snapshots","revenue_payments","revenue_client_payment_allocations")
        before={t:[tuple(r) for r in self.connection.execute("SELECT * FROM "+t)] for t in tables}
        args=dict(fee_item_uid="fee-1",student_id="S-REVENUE",expected_base_cents=240000,new_base_cents=220000,
                  hide_base_balances=True,actor_user_id=1,reason="Approved Word correction",idempotency_key="word5-correction")
        result=correct_service_fee(self.service.repository,**args)
        replay=correct_service_fee(self.service.repository,**args)
        self.assertEqual(result,replay)
        self.assertEqual(220000,self.connection.execute("SELECT net_cents FROM revenue_fee_items").fetchone()[0])
        self.assertEqual(before,{t:[tuple(r) for r in self.connection.execute("SELECT * FROM "+t)] for t in tables})
        self.assertEqual(212871,self.service.fee_payment_summary("fee-1")["outstanding_base_cents"])
        self.assertEqual("true",self.connection.execute("SELECT value FROM system_settings WHERE key='revenue.student.S-REVENUE.hide_service_fee_balances'").fetchone()[0])
        event=self.connection.execute("SELECT after_json FROM revenue_audit_events WHERE action='client_service_fee_corrected'").fetchall()
        self.assertEqual(1,len(event))
        self.assertEqual(240000,result["before"]["net_cents"])
        self.assertEqual(220000,result["after"]["net_cents"])

    def test_fee_correction_refuses_wrong_student_stale_amount_and_overpayment(self):
        from revenue.fee_corrections import correct_service_fee
        self.connection.execute("CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        args=dict(fee_item_uid="fee-1",student_id="WRONG",expected_base_cents=240000,new_base_cents=220000,
                  hide_base_balances=False,actor_user_id=1,reason="Approved correction",idempotency_key="reject")
        with self.assertRaises(ValueError): correct_service_fee(self.service.repository,**args)
        args["student_id"]="S-REVENUE";args["expected_base_cents"]=230000
        with self.assertRaises(ValueError): correct_service_fee(self.service.repository,**args)
        args["expected_base_cents"]=240000
        self._record(method="au_personal_account",actual_currency="AUD",actual_amount=230000,base=230000,key="paid")
        with self.assertRaises(ValueError): correct_service_fee(self.service.repository,**args)
        self.assertEqual(240000,self.connection.execute("SELECT net_cents FROM revenue_fee_items").fetchone()[0])

    def test_explicit_legacy_invoice_preservation_keeps_invoice_unchanged(self):
        from revenue.fee_corrections import correct_service_fee
        self.connection.execute("CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        self.connection.execute("INSERT INTO revenue_invoices(uid,document_kind,invoice_number,commercial_case_id,billing_account_id,issue_date,net_cents,gst_cents,gross_cents,status,created_at) VALUES('old-invoice','client_invoice','OLD-1',1,1,'2026-07-14',240000,0,240000,'issued','2026-07-14')")
        self.connection.execute("INSERT INTO revenue_invoice_lines(uid,invoice_id,source_kind,source_uid,student_id,description,net_cents,gst_cents,gross_cents) VALUES('old-line',1,'fee_item','fee-1','S-REVENUE','Original fee',240000,0,240000)")
        self.connection.execute("UPDATE revenue_fee_items SET status='invoiced'")
        self.connection.commit()
        before=tuple(self.connection.execute("SELECT * FROM revenue_invoices").fetchone())
        args=dict(fee_item_uid='fee-1',student_id='S-REVENUE',expected_base_cents=240000,new_base_cents=220000,
                  hide_base_balances=True,actor_user_id=1,reason='Explicit preservation of original invoice',idempotency_key='preserve-invoice')
        with self.assertRaises(ValueError):correct_service_fee(self.service.repository,**args)
        result=correct_service_fee(self.service.repository,**args,preserve_issued_invoice=True)
        self.assertEqual(20000,result['reduction_cents'])
        self.assertEqual(['old-invoice'],result['preserved_invoice_uids'])
        self.assertEqual(before,tuple(self.connection.execute("SELECT * FROM revenue_invoices").fetchone()))
        self.assertEqual(240000,self.connection.execute("SELECT net_cents FROM revenue_invoice_lines").fetchone()[0])

import json,sqlite3,unittest
from pathlib import Path
from tests.revenue_test_support import RevenueDatabaseFixture
from revenue.repository import RevenueRepository
from revenue.historical_receipts import HistoricalReceiptService
MIGRATION=Path(__file__).resolve().parents[1]/'migrations/20261123_053_historical_client_receipts.sql'
class HistoricalReceiptFixture(RevenueDatabaseFixture):
 def setUp(self):
  super().setUp();self.c=self.connect();self.c.executescript(MIGRATION.read_text());self.c.execute("ALTER TABLE visa_cases ADD COLUMN visa_subclass TEXT DEFAULT '500'");self.s=HistoricalReceiptService(RevenueRepository(self.c),1,'Admin')
  self.data=dict(student_id='S-REVENUE',application_cycle_id=100,service_kind='client_service',claimed_received=True,agreed_aud_cents=220000,actual_cents=220000,currency_code='AUD',settled_aud_cents=220000,received_date='2026-08-01',method_code='au_personal_account',contract_state='historical_missing',source_note='历史服务费2200澳元，负责人已核实，付款凭证待补。',expected_revision=0)
 def save(self,**kw):return self.s.save({**self.data,**kw},'draft')
class HistoricalReceiptsTests(HistoricalReceiptFixture,unittest.TestCase):
 def test_save_incomplete_review_is_not_a_payment_or_case(self):
  r=self.save(actual_cents=None,received_date=None,method_code=None);self.assertIsInstance(r,dict);self.assertEqual('draft',r['status'])
  for t in ['revenue_payments','revenue_commercial_cases','revenue_fee_items','revenue_ledger_entries']:self.assertEqual(0,self.c.execute('SELECT count(*) FROM '+t).fetchone()[0])
 def test_historical_2200_confirmed_once_no_200_debt_and_no_fake_contract(self):
  r=self.save();self.assertIsInstance(r,dict);v=self.s.confirm(r['uid'],1,'confirm',acknowledged=True);self.assertIsInstance(v,dict)
  self.assertEqual(220000,v['actual_cents']);self.assertEqual(v,self.s.confirm(r['uid'],1,'confirm',acknowledged=True))
  self.assertEqual(1,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0]);self.assertEqual(0,self.c.execute('SELECT count(*) FROM contracts').fetchone()[0]);self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0]);self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_invoices').fetchone()[0])
 def test_cny_does_not_invent_aud_settlement(self):
  r=self.save(currency_code='CNY',actual_cents=1208088,agreed_aud_cents=None,settled_aud_cents=None,method_code='wechat');self.assertIsInstance(r,dict);v=self.s.confirm(r['uid'],1,'cny',acknowledged=True);self.assertEqual('CNY',v['currency_code']);self.assertIsNone(v['settled_aud_cents'])
  self.assertEqual({'CNY'},{x[0] for x in self.c.execute('SELECT currency_code FROM revenue_ledger_entries')})
 def test_missing_date_cannot_post(self):
  r=self.save(received_date=None);self.assertIsInstance(r,dict)
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
  self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])
 def test_revision_drift_cannot_post(self):
  r=self.save();self.assertIsInstance(r,dict)
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],2,'confirm',acknowledged=True)
 def test_non_admin_cannot_save_or_confirm(self):
  s=HistoricalReceiptService(RevenueRepository(self.c),2,'Consultant')
  with self.assertRaises(PermissionError):s.save(self.data,'bad')
 def test_wrong_student_cycle_is_rejected(self):
  with self.assertRaises(ValueError):self.save(student_id='OTHER')
 def test_currency_and_method_must_match(self):
  r=self.save(currency_code='CNY');self.assertIsInstance(r,dict)
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
 def test_explicit_confirmation_is_required(self):
  r=self.save();self.assertIsInstance(r,dict)
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm')
 def test_closed_business_cycle_stays_closed(self):
  self.c.execute("UPDATE application_cycles SET status='closed' WHERE id=100");self.c.commit();r=self.save();self.assertIsInstance(r,dict);self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
  self.assertEqual('closed',self.c.execute('SELECT status FROM application_cycles WHERE id=100').fetchone()[0]);self.assertEqual('closed',self.c.execute('SELECT status FROM revenue_commercial_cases').fetchone()[0])
 def test_unknown_amount_can_be_saved_but_not_posted(self):
  r=self.save(actual_cents=None);self.assertIsInstance(r,dict)
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
 def test_existing_normal_service_fee_blocks_historical_post(self):
  from revenue.services import RevenueService
  service=RevenueService(RevenueRepository(self.c));case=service.open_case(100,1,'open');account=service.ensure_student_billing_account('S-REVENUE',1,'account','2026-08-01');service.create_fee_items(case['uid'],account['uid'],[{'milestone_code':'k12-new','description':'Client service fee','net_cents':240000,'gst_cents':0}],1,'fee','existing-rule')
  r=self.save();self.assertIsInstance(r,dict)
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
 def test_historical_price_cannot_be_repriced_by_new_fee_creation(self):
  from revenue.services import RevenueService
  r=self.save();self.s.confirm(r['uid'],1,'confirm',acknowledged=True);case=self.c.execute('SELECT uid FROM revenue_commercial_cases').fetchone()[0];service=RevenueService(RevenueRepository(self.c));account=service.ensure_student_billing_account('S-REVENUE',1,'account','2026-08-01')
  with self.assertRaises(ValueError):service.create_fee_items(case,account['uid'],[{'milestone_code':'k12-new','description':'Client service fee','net_cents':240000,'gst_cents':0}],1,'new-fee','current-price-rule')
 def test_existing_container_is_not_closed_by_historical_confirmation(self):
  from revenue.services import RevenueService
  RevenueService(RevenueRepository(self.c)).open_case(100,1,'prior-case');self.c.execute("UPDATE application_cycles SET status='closed' WHERE id=100");self.c.commit();r=self.save();self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
  self.assertEqual('open',self.c.execute('SELECT status FROM revenue_commercial_cases').fetchone()[0])
if __name__=='__main__':unittest.main()

class HistoricalReceiptIntegrationTests(HistoricalReceiptFixture,unittest.TestCase):
 def test_independent_university_visa_900_does_not_create_application_fee(self):
  self.c.execute("UPDATE application_cycles SET education_stream='higher_education' WHERE id=100");self.c.execute('INSERT INTO visa_cases(id,application_cycle_id) VALUES(203,100)');self.c.commit()
  r=self.save(service_kind='student_visa',visa_case_id=203,contract_state='not_required',actual_cents=90000,agreed_aud_cents=90000,settled_aud_cents=90000)
  posted=self.s.confirm(r['uid'],1,'visa-confirm',acknowledged=True);self.assertEqual(90000,posted['actual_cents']);self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0])
  from revenue.services import RevenueService
  case=self.c.execute('SELECT uid FROM revenue_commercial_cases').fetchone()[0]
  status,fees=RevenueService(RevenueRepository(self.c)).ensure_visa_fee(case,None,{'visa_case_id':203},1,'duplicate','rule')
  self.assertEqual('NEEDS_ATTENTION',status);self.assertEqual([],fees)
 def test_draft_visa_reassigned_before_confirm_is_rejected(self):
  self.c.execute('INSERT INTO visa_cases(id,application_cycle_id) VALUES(203,100)');self.c.commit();r=self.save(service_kind='student_visa',visa_case_id=203)
  self.c.execute('UPDATE visa_cases SET application_cycle_id=NULL WHERE id=203');self.c.commit()
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'bad',acknowledged=True)
 def test_confirmed_history_does_not_change_when_current_price_rule_changes(self):
  from revenue.read_models import RevenueReadModels
  r=self.save();self.s.confirm(r['uid'],1,'confirm',acknowledged=True);case=self.c.execute('SELECT id FROM revenue_commercial_cases').fetchone()[0]
  self.assertEqual('历史已收清',RevenueReadModels(self.c)._billing_status(case))
  self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_fee_items').fetchone()[0])
 def test_failure_after_payment_insert_rolls_back_case_payment_and_ledger(self):
  r=self.save();self.c.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON revenue_ledger_entries BEGIN SELECT RAISE(ABORT,'injected'); END");self.c.commit()
  with self.assertRaises(sqlite3.DatabaseError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
  for t in ['revenue_payments','revenue_ledger_entries','revenue_commercial_cases']:self.assertEqual(0,self.c.execute('SELECT count(*) FROM '+t).fetchone()[0])
  self.assertEqual('draft',self.c.execute('SELECT status FROM revenue_historical_receipts').fetchone()[0])
 def test_update_requires_review_identity_and_revision(self):
  r=self.save()
  with self.assertRaises(ValueError):self.s.save({**self.data,'expected_revision':1,'review_uid':'wrong'},'bad-update')
  updated=self.s.save({**self.data,'expected_revision':1,'review_uid':r['uid'],'source_note':'负责人补充确认历史付款来源与实际日期。'},'update');self.assertEqual(2,updated['revision'])
 def test_cancelled_draft_does_not_block_normal_pricing(self):
  from revenue.historical_receipts import blocks_repricing
  r=self.save();self.s.cancel(r['uid'],1,'cancel');self.assertFalse(blocks_repricing(self.c,100,'client_service'))
 def test_missing_receipt_is_separate_from_confirmed_money_on_mobile(self):
  from revenue.historical_receipt_views import page_data,render
  r=self.save();self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
  html=render(page_data(self.c,'S-REVENUE'),'csrf','token',mobile=True,can_administer=True)
  self.assertIn('历史已收清',html);self.assertIn('2,200.00',html);self.assertIn('付款凭证',html);self.assertIn('未生成（不自动补开）',html);self.assertNotIn('method="post"',html);self.assertNotIn('200.00 欠',html)
 def test_ui_amount_conversion_rejects_more_than_two_decimals(self):
  from revenue.ui_actions import RevenueUIActions
  action=RevenueUIActions(self.c,Path(self.temp_dir.name),1,'Admin')
  form={**self.data,'claimed_received':'1','expected_revision':'0','actual_cents':'2200.00','agreed_aud_cents':'2200','settled_aud_cents':'2200','idempotency_key':'ui'}
  r=action.historical_collection('save',form);self.assertEqual(220000,r['actual_cents'])
  with self.assertRaises(ValueError):action.historical_collection('save',{**form,'actual_cents':'2200.001','idempotency_key':'bad'})
 def test_historical_actions_are_admin_only_even_with_revenue_grant(self):
  from revenue.access_policy import can_perform_revenue_action
  user={'role_name':'Consultant','module_permissions_json':'["revenue_commission"]'}
  for action in ['historical/save','historical/confirm','historical/evidence','historical/cancel']:self.assertFalse(can_perform_revenue_action(user,action))

class HistoricalFeeHandoffTests(RevenueDatabaseFixture,unittest.TestCase):
 def test_contract_completion_preserves_fact_but_does_not_reprice_history(self):
  self.setUpHistory()
  from revenue.operational_orchestrator import RevenueOperationalOrchestrator
  self.c.execute("INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-11-20','2026-11-21T00:00:00Z')");self.c.commit()
  try:result=RevenueOperationalOrchestrator(self.c,actor_user_id=1,actor_role='Admin').contract_fully_executed(70)
  except ValueError as exc:self.fail('历史核对应只阻止重新计费，不能使合同完成失败：'+str(exc))
  self.assertEqual('HISTORICAL_REVIEW',result['fee_status']);self.assertEqual([],result['fee_items'])
 def setUpHistory(self):
  self.c=self.connect();self.c.executescript(MIGRATION.read_text())
  HistoricalReceiptService(RevenueRepository(self.c),1,'Admin').save({'student_id':'S-REVENUE','application_cycle_id':100,'service_kind':'client_service','claimed_received':True,'agreed_aud_cents':220000,'actual_cents':220000,'currency_code':'AUD','received_date':'2026-08-01','method_code':'au_personal_account','contract_state':'historical_missing','source_note':'历史费用已核实为2200，付款凭证后补。'},'history')

class HistoricalReviewFindingsTests(HistoricalReceiptFixture,unittest.TestCase):
 def visa_draft(self):
  self.c.execute('INSERT INTO visa_cases(id,application_cycle_id) VALUES(203,100)');self.c.commit()
  return self.save(service_kind='student_visa',visa_case_id=203,agreed_aud_cents=90000,actual_cents=90000,settled_aud_cents=90000)
 def doc(self):
  from revenue.services import RevenueService
  return RevenueService(RevenueRepository(self.c)).register_document('historical_payment_evidence','/test/original.pdf','a'*64,'application/pdf',10,'upload',1,'doc')
 def test_guardian_590_cannot_be_booked_as_student_visa(self):
  r=self.visa_draft();self.c.execute("UPDATE visa_cases SET visa_subclass='590' WHERE id=203");self.c.commit()
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
 def test_unknown_visa_subclass_requires_review_before_post(self):
  r=self.visa_draft();self.c.execute('UPDATE visa_cases SET visa_subclass=NULL WHERE id=203');self.c.commit()
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
 def test_history_for_one_student_visa_does_not_block_another_case(self):
  r=self.visa_draft();self.c.execute("INSERT INTO visa_cases(id,application_cycle_id,visa_subclass) VALUES(204,100,'590')");self.c.commit()
  from revenue.services import RevenueService
  service=RevenueService(RevenueRepository(self.c));case=service.open_case(100,1,'case');account=service.ensure_student_billing_account('S-REVENUE',1,'acct','2026-08-01')
  status,_=service.ensure_visa_fee(case['uid'],account['uid'],{'visa_case_id':204,'milestone_code':'guardian-590','description':'Visa service fee','net_cents':90000,'gst_cents':0},1,'guardian','guardian-rule')
  self.assertEqual('CREATED',status)
 def test_student_history_remains_visible_beside_guardian_fee(self):
  self.visa_draft();self.c.execute("INSERT INTO visa_cases(id,application_cycle_id,visa_subclass) VALUES(204,100,'590')");self.c.commit()
  from revenue.services import RevenueService
  from revenue.read_models import RevenueReadModels
  from revenue.views import render_revenue_workspace
  service=RevenueService(RevenueRepository(self.c));case=service.open_case(100,1,'case');account=service.ensure_student_billing_account('S-REVENUE',1,'acct','2026-08-01');service.ensure_visa_fee(case['uid'],account['uid'],{'visa_case_id':204,'milestone_code':'guardian-590','description':'Visa service fee','net_cents':90000,'gst_cents':0},1,'guardian','rule')
  html=render_revenue_workspace(RevenueReadModels(self.c).dashboard(),'csrf');self.assertIn('已确认收款 · 资料待补',html);self.assertIn('陪读签证',html)
 def test_multiple_signed_contracts_cannot_hide_package_coverage(self):
  r=self.visa_draft()
  for col in ['service_scope_json TEXT','consulting_and_visa_service_fee TEXT','discount_amount TEXT','final_fee_amount TEXT']:self.c.execute('ALTER TABLE contracts ADD COLUMN '+col)
  self.c.execute("INSERT INTO contracts VALUES(70,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02','[\"student_visa_lodgement\"]','2400','0','2400')")
  self.c.execute("INSERT INTO contracts VALUES(71,'S-REVENUE',100,'FullyExecuted','2026-08-01','2026-08-02','[]','100','0','100')");self.c.commit()
  with self.assertRaises(ValueError):self.s.confirm(r['uid'],1,'confirm',acknowledged=True)
 def test_pending_history_does_not_hide_formal_outstanding(self):
  from revenue.services import RevenueService
  from revenue.read_models import RevenueReadModels
  from revenue.views import render_revenue_workspace
  service=RevenueService(RevenueRepository(self.c));case=service.open_case(100,1,'case');account=service.ensure_student_billing_account('S-REVENUE',1,'acct','2026-08-01');service.create_fee_items(case['uid'],account['uid'],[{'milestone_code':'k12-new','description':'Client service fee','net_cents':240000,'gst_cents':0}],1,'fee','current-rule');self.save()
  html=render_revenue_workspace(RevenueReadModels(self.c).dashboard(),'csrf');self.assertTrue('2,400' in html or '2400' in html);self.assertIn('还差',html)
 def test_cancelled_review_has_explicit_reopen_link(self):
  from revenue.historical_receipt_views import render,page_data
  r=self.save();self.s.cancel(r['uid'],1,'cancel');html=render(page_data(self.c,'S-REVENUE'),'csrf','token',can_administer=True);self.assertIn('重新核对',html)
 def test_cancelled_review_cannot_accept_old_upload(self):
  r=self.save();self.s.cancel(r['uid'],1,'cancel');document=self.doc()
  with self.assertRaises(ValueError):self.s.attach(r['uid'],document['uid'],'payment_evidence','attach')
 def test_evidence_link_stops_when_student_is_hidden(self):
  import revenue.historical_receipts as h
  r=self.save();d=self.doc();self.s.attach(r['uid'],d['uid'],'payment_evidence','attach');self.c.execute('ALTER TABLE students ADD COLUMN deleted_at TEXT');self.c.execute("UPDATE students SET deleted_at='2026-11-01' WHERE student_id='S-REVENUE'");self.c.commit()
  check=getattr(h,'can_view_evidence',lambda *args:True)
  self.assertFalse(check(self.c,d['uid']))

class HistoricalMobileRouteTests(unittest.TestCase):
 def test_native_mobile_route_uses_mobile_shell_and_read_only_render(self):
  import app
  from unittest.mock import patch,MagicMock
  class Handler:
   user={'role_name':'Admin'};csrf='csrf';path='/mobile/revenue/historical-receipts';query={}
   def mobile_shell(self,title,body,active):return 'mobile-shell:'+body
   def send_html(self,html,**kw):return html
  with patch('app.can_access_revenue',return_value=True),patch('app.db.connect',return_value=MagicMock()),patch('app.db._release_connection'),patch('revenue.historical_receipt_views.page_data',return_value={}),patch('revenue.historical_receipt_views.render',return_value='history') as render,patch('app.page',side_effect=AssertionError('mobile route used desktop shell')):
   self.assertEqual('mobile-shell:history',app.CRMHandler.revenue_historical_receipts(Handler(),'GET'))
   self.assertTrue(render.call_args.kwargs['mobile'])

class HistoricalWorkbenchDisplayTests(unittest.TestCase):
 def test_history_does_not_contradict_empty_normal_payment_list(self):
  from revenue.views import render_revenue_case_workbench
  record={'service_kind':'client_service','status':'confirmed','agreed_aud_cents':220000,'actual_cents':220000,'settled_aud_cents':220000,'currency_code':'AUD'}
  data={'schema_version':'revenue.v1','title':'收费与佣金档案','sections':[{'key':'invoice_payment_receipt','title':'收款、收据与历史发票'}],'historical_collections':[record]}
  html=render_revenue_case_workbench(data,mobile=True)
  self.assertIn('历史已收清',html);self.assertNotIn('暂无收款记录',html);self.assertIn('历史收款及凭证见上方核对',html)

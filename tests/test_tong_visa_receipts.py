"""Independent Visa receipt authority and frozen applicant identity."""
import json,sqlite3,unittest
from pathlib import Path
from pypdf import PdfReader
from tests import test_revenue_client_payment_v1 as legacy
from tests import test_tong_word_20261125 as payment_fixture

class VisaReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture=payment_fixture.IndependentVisaPaymentTests('test_visa_fee_rejects_cross_cycle_authority')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.connection=self.fixture.connection
        self.service=self.fixture.service
        migration=Path(__file__).resolve().parents[1]/'migrations/20261125_058_visa_receipt_snapshots.sql'
        if migration.exists():self.connection.executescript(migration.read_text())
        self.fixture.seed_independent()
        self.connection.execute('ALTER TABLE visa_cases ADD COLUMN applicant_name TEXT')
        self.connection.execute("UPDATE visa_cases SET visa_subclass='590',applicant_name='Mother TEST' WHERE id=901")
        item={'visa_case_id':901,'application_cycle_id':100,'milestone_code':'guardian-590','net_cents':90000,'gst_cents':0}
        self.connection.execute("INSERT INTO revenue_rule_snapshots VALUES(4,'guardian-snap','fee','guardian-rule',?,?,1,'2026-09-01')",(json.dumps({'items':[item]}),'d'*64))
        self.connection.execute("UPDATE revenue_fee_items SET rule_snapshot_id=4,milestone_code='guardian-590',net_cents=90000,gross_cents=90000 WHERE id=1")
        self.connection.commit()

    def _record(self, **kwargs):
        return self.fixture._record(**kwargs)

    def issue(self, uid, key):
        return self.service.issue_client_receipt(payment_uid=uid,document_root=legacy.secure_directory(self)/'pdf',actor_user_id=1,actor_role='Admin',actor_has_revenue_access=False,idempotency_key=key,issuer_name='AAIE TEST',issuer_abn='12345678901')

    def test_guardian_receipt_without_contract_is_frozen_and_idempotent(self):
        payment=self._record(method='au_company_account',actual_currency='AUD',actual_amount=99000,base=90000,key='guardian-paid')
        first=self.issue(payment['uid'],'issue-first')
        snapshot=first['snapshot']
        self.assertEqual('Mother TEST',snapshot['applicant_name'])
        self.assertEqual('Jimmy Zhang',snapshot['student_name'])
        self.assertEqual(901,snapshot['visa_case_id'])
        self.assertEqual((90000,9000,99000),(snapshot['allocated_base_cents'],snapshot['gst_cents'],snapshot['gross_charge_aud_cents']))
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_fee_item_contract_links').fetchone()[0])
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_receipt_snapshots').fetchone()[0])
        self.connection.execute("UPDATE visa_cases SET applicant_name='Changed Parent' WHERE id=901");self.connection.commit()
        replay=self.issue(payment['uid'],'issue-again')
        self.assertEqual(first['uid'],replay['uid']);self.assertEqual('Mother TEST',replay['snapshot']['applicant_name'])
        text='\n'.join(p.extract_text() or '' for p in PdfReader(first['document']['storage_path']).pages)
        for value in ('Mother TEST','Jimmy Zhang','VISA-901','590','AUD 900.00','AUD 90.00','AUD 990.00'):self.assertIn(value,text)
        self.assertNotIn('CONTRACT-70',text)
        for sql in ("UPDATE revenue_visa_receipt_snapshots SET applicant_name='x'",'DELETE FROM revenue_visa_receipt_snapshots',"UPDATE revenue_receipts SET amount_cents=1","DELETE FROM revenue_receipts"):
            with self.assertRaises(sqlite3.IntegrityError):self.connection.execute(sql)

    def test_changed_case_ownership_rejects_receipt_before_number_or_pdf(self):
        payment=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=90000,base=90000,key='paid-before-conflict')
        self.connection.execute("UPDATE visa_cases SET student_id='OTHER' WHERE id=901");self.connection.commit()
        with self.assertRaises(ValueError):self.issue(payment['uid'],'bad-issue')
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_receipts').fetchone()[0])
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_number_sequences').fetchone()[0])

    def test_visitor_receipt_has_father_and_retains_cny_actual_amount(self):
        self.connection.execute("UPDATE visa_cases SET visa_subclass='600',applicant_name='Father TEST' WHERE id=901")
        item={'visa_case_id':901,'application_cycle_id':100,'milestone_code':'visitor-600','net_cents':90000,'gst_cents':0}
        self.connection.execute("INSERT INTO revenue_rule_snapshots VALUES(5,'visitor-snap','fee','visitor-rule',?,?,1,'2026-09-01')",(json.dumps({'items':[item]}),'e'*64))
        self.connection.execute("UPDATE revenue_fee_items SET rule_snapshot_id=5,milestone_code='visitor-600' WHERE id=1");self.connection.commit()
        p=self._record(method='alipay',actual_currency='CNY',actual_amount=450000,base=90000,key='father-payment')
        r=self.issue(p['uid'],'father-receipt')
        self.assertEqual(('Father TEST','600','CNY',450000,90000),(r['snapshot']['applicant_name'],r['snapshot']['visa_subclass'],r['snapshot']['actual_currency_code'],r['snapshot']['actual_amount_cents'],r['snapshot']['allocated_base_cents']))

    def test_student_visa_receipt_has_no_school_contract(self):
        self.connection.execute("UPDATE visa_cases SET visa_subclass='500',applicant_name='Jimmy Zhang' WHERE id=901")
        item={'visa_case_id':901,'application_cycle_id':100,'milestone_code':'student-visa-renewal','net_cents':90000,'gst_cents':0}
        self.connection.execute("INSERT INTO revenue_rule_snapshots VALUES(5,'student-snap','fee','student-rule',?,?,1,'2026-09-01')",(json.dumps({'items':[item]}),'e'*64))
        self.connection.execute("UPDATE revenue_fee_items SET rule_snapshot_id=5,milestone_code='student-visa-renewal' WHERE id=1");self.connection.commit()
        p=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=90000,base=90000,key='student-payment')
        r=self.issue(p['uid'],'student-receipt')
        self.assertEqual('500',r['snapshot']['visa_subclass'])
        self.assertEqual(1,self.connection.execute('SELECT count(*) FROM revenue_visa_receipt_snapshots').fetchone()[0])
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_receipt_snapshots').fetchone()[0])

    def test_database_rejects_cancelled_fee_and_mismatched_receipt_header(self):
        p1=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=45000,base=45000,key='partial-one')
        first=self.issue(p1['uid'],'receipt-partial-one')
        p2=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=45000,base=45000,key='partial-two')
        self.connection.commit()
        header=dict(self.connection.execute('SELECT * FROM revenue_receipts WHERE id=?',(first['receipt']['id'],)).fetchone());header.pop('id')
        doc=dict(first['document']);doc.pop('id');doc.update(uid='negative-doc',storage_path='negative-test-only.pdf',sha256='b'*64)
        d=self.connection.execute('INSERT INTO revenue_documents('+','.join(doc)+') VALUES('+','.join('?' for _ in doc)+')',list(doc.values())).lastrowid
        header.update(uid='negative-receipt',receipt_number='NEGATIVE-2',payment_id=p2['payment']['id'],document_id=d)
        self.connection.commit()
        for scenario in ('valid','cancelled','changed_amount','bad_header','changed_account'):
            self.connection.execute('SAVEPOINT negative')
            current=dict(header)
            if scenario=='cancelled':self.connection.execute("UPDATE revenue_fee_items SET status='cancelled' WHERE id=1")
            if scenario=='changed_amount':self.connection.execute("UPDATE revenue_fee_items SET net_cents=100,gross_cents=100 WHERE id=1")
            if scenario=='bad_header':current.update(amount_cents=1,currency_code='USD')
            if scenario=='changed_account':self.connection.execute('UPDATE revenue_payments SET billing_account_id=NULL WHERE id=?',(p2['payment']['id'],))
            rid=self.connection.execute('INSERT INTO revenue_receipts('+','.join(current)+') VALUES('+','.join('?' for _ in current)+')',list(current.values())).lastrowid
            snap=dict(first['snapshot']);snap.pop('id');snap.update(uid='negative-snapshot',receipt_id=rid,receipt_number='NEGATIVE-2',snapshot_sha256='f'*64,transaction_reference='TX-partial-two')
            query='INSERT INTO revenue_visa_receipt_snapshots('+','.join(snap)+') VALUES('+','.join('?' for _ in snap)+')'
            if scenario=='valid':self.connection.execute(query,list(snap.values()))
            else:
                with self.assertRaisesRegex(sqlite3.IntegrityError,'snapshot authority mismatch'):
                    self.connection.execute(query,list(snap.values()))
            self.connection.execute('ROLLBACK TO negative');self.connection.execute('RELEASE negative')

    def test_legacy_guardian_with_student_name_requires_identity_review(self):
        self.connection.execute("UPDATE visa_cases SET applicant_name='Jimmy Zhang' WHERE id=901");self.connection.commit()
        p=self._record(method='au_personal_account',actual_currency='AUD',actual_amount=90000,base=90000,key='legacy-misnamed-guardian')
        with self.assertRaisesRegex(ValueError,'家长'):
            self.issue(p['uid'],'misnamed-guardian-receipt')
        self.assertEqual(0,self.connection.execute('SELECT count(*) FROM revenue_receipts').fetchone()[0])

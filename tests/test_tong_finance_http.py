"""Actual isolated HTTP acceptance with synthetic student data and proof bytes."""
import json
import os
from pathlib import Path
import sqlite3
import unittest
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:19146'
DB = ROOT/'STAGING_DATA/database/staging.db'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def request(path, fields=None, proof=None, *, role='manager'):
    headers={'Cookie':'aaie_staging_aaie_session=tong-finance-'+role}
    data=None
    if fields is not None:
        if proof is not None:
            boundary='----TongFinance'+uuid4().hex
            parts=[]
            for key,value in fields.items():
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="historical_evidence"; filename="proof.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode()+proof+b'\r\n')
            parts.append(f'--{boundary}--\r\n'.encode())
            data=b''.join(parts); headers['Content-Type']='multipart/form-data; boundary='+boundary
        else:
            data=urllib.parse.urlencode(fields).encode(); headers['Content-Type']='application/x-www-form-urlencoded'
    try:
        response=urllib.request.build_opener(NoRedirect).open(urllib.request.Request(BASE+path,data,headers),timeout=20)
    except urllib.error.HTTPError as e: response=e
    with response: return response.status,response.read(),dict(response.headers)

@unittest.skipUnless(os.environ.get('AAIE_TONG_FINANCE_HTTP_ACCEPTANCE') == '1',
                     'Explicit task HTTP acceptance opt-in required')
class TongFinanceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DB.exists(): raise unittest.SkipTest('Start tests/tong_finance_isolation.py --serve first')
        with urllib.request.urlopen(BASE+'/health',timeout=5) as response:
            health=json.load(response)
        if health.get('environment') != 'staging' or health.get('database_path') != str(DB):
            raise RuntimeError('Refuse a listener outside the task database')
        status,body,_=request('/revenue/cases')
        if status != 200: raise RuntimeError('Isolated Manager session unavailable')

    def test_runtime_health_identity(self):
        for role,port in (('admin',19146),('parent',19147)):
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=5) as response:
                health=json.load(response)
            self.assertEqual('ok',health['status'])
            self.assertEqual('staging',health['environment'])
            self.assertEqual(role,health['service'])
            self.assertEqual(str(DB),health['database_path'])
            self.assertEqual(str(ROOT/'STAGING_DATA/storage'),health['storage_path'])

    def test_collection_refresh_confirm_evidence_replay_and_access(self):
        sid='99'+str(uuid4().int%10**8).zfill(8); cid=int(sid)
        with sqlite3.connect(DB) as c:
            c.execute('''INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,
                created_date,updated_at,form_data_json) VALUES(?,'Before','Synthetic','Active','2020-01-01','2020-01-01','{}')''',(sid,))
            c.execute('''INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,
                outcome,closed_at,opened_at,created_at,updated_at,education_stream)
                VALUES(?,?,?,'initial',1,'closed','completed','2020-02-01','2020-01-01','2020-01-01','2020-02-01','school_education')''',(cid,'http-'+sid,sid))
        path='/revenue/cases?q='+sid
        status,body,headers=request(path)
        self.assertEqual(200,status); self.assertIn('待核实',body.decode()); self.assertEqual('private, no-store',headers['Cache-Control'])
        form=dict(student_id=sid,application_cycle_id=str(cid),service_kind='client_service',
            claimed_received='0',expected_revision='0',contract_state='historical_missing',
            source_note='合成历史收款验收，金额与日期尚待核实。',idempotency_key='http-draft-'+sid,
            csrf='csrf-tong-finance-manager')
        self.assertEqual(303,request('/revenue/historical/save',form)[0])
        with sqlite3.connect(DB) as c:
            uid,revision=c.execute('SELECT uid,revision FROM revenue_historical_receipts WHERE student_id=?',(sid,)).fetchone()
            self.assertEqual(0,c.execute('SELECT count(*) FROM revenue_payments p JOIN revenue_historical_receipts h ON h.payment_id=p.id WHERE h.student_id=?',(sid,)).fetchone()[0])
            c.execute("UPDATE students SET passport_first_name='After' WHERE student_id=?",(sid,))
        status,body,_=request(path+'&refresh=1'); text=body.decode()
        self.assertEqual(200,status); self.assertIn('After Synthetic',text); self.assertIn('收款待核实',text)
        self.assertIn('已从学生档案读取最新姓名和财务数据',text)
        proof=b'%PDF-1.4\nSynthetic payment evidence for '+sid.encode()
        form.update(review_uid=uid,expected_revision=str(revision),claimed_received='1',actual_cents='2200',
            agreed_aud_cents='2200',settled_aud_cents='2200',currency_code='AUD',received_date='2020-01-20',
            method_code='au_personal_account',idempotency_key='http-complete-'+sid)
        self.assertEqual(303,request('/revenue/historical/save',form,proof)[0])
        self.assertEqual(303,request('/revenue/historical/save',form,proof)[0])
        with sqlite3.connect(DB) as c:
            revision=c.execute('SELECT revision FROM revenue_historical_receipts WHERE uid=?',(uid,)).fetchone()[0]
            doc=c.execute('''SELECT d.uid FROM revenue_documents d JOIN revenue_historical_receipt_evidence e ON e.document_id=d.id
                JOIN revenue_historical_receipts h ON h.id=e.historical_receipt_id WHERE h.uid=?''',(uid,)).fetchone()[0]
            self.assertEqual(1,c.execute('SELECT count(*) FROM revenue_historical_receipt_evidence e JOIN revenue_historical_receipts h ON h.id=e.historical_receipt_id WHERE h.uid=?',(uid,)).fetchone()[0])
        confirm=dict(review_uid=uid,revision=str(revision),acknowledged='1',
            idempotency_key='http-confirm-'+sid,csrf='csrf-tong-finance-manager')
        self.assertEqual(303,request('/revenue/historical/confirm',confirm)[0])
        self.assertEqual(303,request('/revenue/historical/confirm',confirm)[0])
        status,payload,_=request('/revenue/documents/'+doc+'/view'); self.assertEqual(200,status); self.assertEqual(proof,payload)
        self.assertIn('历史已收清',request(path+'&refresh=1')[1].decode())
        self.assertEqual(403,request('/revenue/historical/cancel',confirm)[0])
        self.assertEqual(403,request('/revenue/historical/save',{**form,'csrf':'csrf-tong-finance-consultant'},role='consultant')[0])
        self.assertEqual(403,request('/revenue/historical/save',{**form,'csrf':'wrong'})[0])
        mobile=request('/mobile/revenue/historical-receipts?student='+sid)[1].decode()
        self.assertIn('历史已收清',mobile)
        finance_content=mobile.split('<main class="revenue-workspace hcr-workspace">',1)[1].split('</main>',1)[0]
        self.assertNotIn('method="post"',finance_content)
        with sqlite3.connect(DB) as c:
            self.assertEqual('closed',c.execute('SELECT status FROM application_cycles WHERE id=?',(cid,)).fetchone()[0])
            self.assertEqual(0,c.execute('SELECT count(*) FROM revenue_fee_items WHERE student_id=?',(sid,)).fetchone()[0])
            self.assertEqual(1,c.execute('SELECT count(*) FROM revenue_payments p JOIN revenue_historical_receipts h ON h.payment_id=p.id WHERE h.student_id=?',(sid,)).fetchone()[0])
        (ROOT/'STAGING_DATA/http-acceptance.json').write_text(json.dumps({'status':'passed','student_id':sid,
            'review_uid':uid,'document_uid':doc,'synthetic_only':True},indent=2))

if __name__=='__main__': unittest.main()

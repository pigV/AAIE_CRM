"""One synthetic school chain with original native upload and canonical services.

Only schema is read from task staging. Every business row and attachment is new
and local to this test; no production or other HTTP fixture rows are copied.
"""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app
from application_cycle import create_student_cycle
from database import Database
from workflow_cutover import activate_workflow_v2,REQUIRED_EVIDENCE_KEYS
from school_plan_lifecycle import activate_created_report,normalize_parent_decision,save_parent_confirmation
from workflow_application_service import create_private_application,submit_private_application
from workflow_offer_events import select_final_acceptance,record_final_school_submission
from student_application_stage_distribution import load_application_stage_result_map


class SchoolMaterialChainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        with app.db.read_snapshot() as conn:
            ddl=[row[0] for row in conn.execute("""SELECT sql FROM sqlite_master WHERE sql IS NOT NULL
                AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END,name""")]
        path=self.root/'school.db'
        # Clone only DDL in one transaction. Autocommitting every index/table
        # makes a fresh isolated fixture spend minutes flushing tiny writes.
        with sqlite3.connect(path) as conn:conn.executescript('BEGIN;\n'+';\n'.join(ddl)+';\nCOMMIT;')
        self.db=Database(path);self.db.initialize()
        self.student_root=self.root/'student';self.student_root.mkdir()
        class Nas:
            def student_folder(inner,student):return Path(student['folder_path'])
            def category_folder(inner,student,category):
                folder=inner.student_folder(student)/category;folder.mkdir(parents=True,exist_ok=True);return folder
            def require_category_access(inner,student,category,**kwargs):return inner.category_folder(student,category)
            def require_student_file_readable(inner,student,path):return app.ensure_within(path,inner.student_folder(student))
        for item in (patch.object(app,'db',self.db),patch.object(app,'nas',Nas()),
                patch.object(app,'UPLOAD_REVIEW_DIR',self.root/'review'),patch.object(app,'PREVIEW_DIR',self.root/'previews')):
            item.start();self.addCleanup(item.stop)
        (self.root/'previews').mkdir()
        self.stamp=app.now_iso();self.sid='SCHOOL-CHAIN';self.school='SYNTHETIC ONLY School'
        self.db.execute("""INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,
            created_date,updated_at,form_data_json,folder_path,nas_status)
            VALUES(?,'SYNTHETIC','SCHOOL CHAIN','Active',?,?,'{}',?,'ready')""",(self.sid,self.stamp,self.stamp,str(self.student_root)))
        with self.db.transaction() as conn:
            self.cycle=create_student_cycle(conn,self.sid,'initial',self.stamp,1).id
            activate_workflow_v2(conn,evidence={key:True for key in REQUIRED_EVIDENCE_KEYS},actor='synthetic-school-chain')
            conn.execute("INSERT OR REPLACE INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled',?)",(self.stamp,))
        self.actor=dict(self.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        self.h=object.__new__(app.CRMHandler);self.h.user=self.actor;self.h.csrf='synthetic'
        self.h.headers={};self.h.query={};self.h.client_address=('127.0.0.1',0)
        self.h.flash_redirect=lambda url,message,kind='success':(url,message,kind)
        self.h.redirect=lambda url:('redirect',url)
        self.h.error_page=lambda message,status=400:(status,message)
        self.h.deny_module_access=lambda module:(403,module)
        # Start after an explicitly completed contract; this test covers school
        # materials, not real parent identity or company-signing acceptance.
        proof=self.pdf('SYNTHETIC ONLY - completed contract');file=self.student_root/'contract.pdf';file.write_bytes(proof)
        self.db.execute("""INSERT INTO contracts(student_id,application_cycle_id,contract_category,contract_type,
            contract_title,contract_status,final_signed_file_path,fully_executed_at,generated_at,updated_at)
            VALUES(?,?,'留学合同','StudyAbroadContract','SYNTHETIC ONLY','FullyExecuted',?,?,?,?)""",
            (self.sid,self.cycle,str(file),self.stamp,self.stamp,self.stamp))

    @staticmethod
    def pdf(text):
        from reportlab.pdfgen import canvas
        out=io.BytesIO();c=canvas.Canvas(out);c.drawString(40,700,text);c.save();return out.getvalue()

    def stage(self):
        with self.db.read_snapshot() as conn:return load_application_stage_result_map(conn,[self.sid])[self.sid].key

    def review(self,module,kind,context=None,extra=None):
        data=self.pdf('SYNTHETIC ONLY - '+kind)
        token=app.create_upload_review(self.sid,module,[app.Upload('files',kind+'.pdf','application/pdf',data)],self.actor,context or {})
        self.h.form=dict(csrf='synthetic',confirm_item_0='1',document_type_0=kind,**(extra or {}))
        result=self.h.upload_review_confirm('POST',self.sid,token)
        self.assertEqual('success',result[2],result)
        row=self.db.one('SELECT * FROM student_documents WHERE student_id=? AND document_type=? ORDER BY id DESC LIMIT 1',(self.sid,kind))
        self.assertIsNotNone(row);self.assertEqual(data,Path(row['file_path']).read_bytes());self.assertTrue(Path(row['preview_path']).is_file())
        return row

    def test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle(self):
        city=self.db.execute("INSERT INTO school_cities(city_name,state) VALUES('Synthetic Chain City','VIC')")
        school=self.db.execute("INSERT INTO schools(school_name,city_id,sector_key,school_type,state,created_at,updated_at) VALUES(?,?,'private','Secondary','VIC',?,?)",(self.school,city,self.stamp,self.stamp))
        plan_path=self.student_root/'school-plan.pdf';plan_path.write_bytes(self.pdf('SYNTHETIC ONLY school plan: '+self.school))
        report=self.db.execute("""INSERT INTO school_plan_reports(student_id,application_id,report_version,selected_school_ids,
            created_at,updated_at,application_cycle_id,file_name,file_path) VALUES(?,?,1,?,?,?,?,?,?)""",
            (self.sid,self.sid,json.dumps([school]),self.stamp,self.stamp,self.cycle,plan_path.name,str(plan_path)))
        link=self.db.execute("""INSERT INTO school_plan_report_links(report_id,student_id,application_id,token_hash,
            token_last_4,share_url,expires_at,created_at) VALUES(?,?,?,?,'test','http://synthetic.invalid','2099-01-01',?)""",
            (report,self.sid,self.sid,hashlib.sha256(b'synthetic-school-chain').hexdigest(),self.stamp))
        signature=self.student_root/'synthetic-parent.png'
        from PIL import Image
        Image.new('RGBA',(30,20),(10,20,30,255)).save(signature)
        with self.db.transaction() as conn:
            activate_created_report(conn,report,self.actor,self.stamp)
            decision=normalize_parent_decision({'plan_sector':'private'},[{'school_id':school,'school_name':self.school}],{'selected_school_ids':[str(school)]})
            context={'report_id':report,'student_id':self.sid,'application_id':self.sid,'link_id':link,
                'confirmed_at':self.stamp,'signature_image_path':str(signature),'confirmed_by_name':'SYNTHETIC PARENT'}
            save_parent_confirmation(conn,context,decision)
            confirmed=conn.execute('SELECT id FROM school_plan_confirmed_schools WHERE report_id=?',(report,)).fetchone()[0]
            application=create_private_application(conn,self.cycle,confirmed,actor_user_id=1,occurred_at=self.stamp)
        self.assertEqual('06_school_application_submission',self.stage())
        with self.db.transaction() as conn:submit_private_application(conn,application.id,actor_user_id=1,occurred_at=self.stamp)
        self.assertEqual('07_school_response',self.stage())
        offer_doc=self.review('Offer','Offer',{'school_name':self.school})
        offer=self.db.one('SELECT id FROM offers WHERE student_id=? AND offer_file_path=?',(self.sid,offer_doc['file_path']))[0]
        # Native Offer review already emits the formal event. Reuse its exact
        # identity instead of inventing a second external-offer key.
        received=self.db.one('SELECT * FROM offer_received_events WHERE legacy_offer_id=?',(offer,))
        self.assertIsNotNone(received);self.assertEqual(application.id,received['private_application_id'])
        self.assertEqual(self.cycle,received['application_cycle_id'])
        self.assertEqual('08_offer_acceptance',self.stage())
        self.review('Offer','SignedOffer',{'school_name':self.school})
        self.review('Payments','OfferDepositReceipt',{'amount':'100.00','currency':'AUD','payment_method':'Bank Transfer','paid_date':self.stamp[:10]})
        with self.db.transaction() as conn:
            accepted=select_final_acceptance(conn,self.cycle,received['id'],selected_at=self.stamp,actor_user_id=1,
                signed_offer_completed_at=self.stamp,payment_completed_at=self.stamp)
            record_final_school_submission(conn,accepted.id,submitted_at=self.stamp,submission_channel='synthetic manual review',submission_reference='SYNTHETIC ONLY',actor_user_id=1)
        self.assertEqual('09_waiting_coe',self.stage())
        coe_doc=self.review('COE + OSHC','COE',{'school_name':self.school},
            {'intake_defer_0':'1','extract_coe_provider_0':self.school,'extract_school_name_0':self.school,'extract_coe_course_0':'Secondary Year 10',
             'extract_coe_start_date_0':'2027-01-25','extract_coe_end_date_0':'2035-12-31'})
        self.assertEqual('10_visa_submission',self.stage())
        self.assertEqual(0, self.db.one('SELECT count(*) FROM final_school_coe_confirmations')[0])
        html = self.h.coe_panel(self.sid, self.db.all("SELECT * FROM coe_oshc WHERE student_id=?", (self.sid,)))
        self.assertNotIn("confirm-final-school", html)
        self.assertIn("当前 CoE", html)
        # Removing the currently valid source must also remove readiness.
        self.db.execute("UPDATE student_documents SET is_active=0,document_status='voided',voided_at=? WHERE id=?",(self.stamp,coe_doc['id']))
        self.assertEqual('09_waiting_coe',self.stage())
        self.db.execute("UPDATE student_documents SET is_active=1,document_status='active',voided_at=NULL WHERE id=?",(coe_doc['id'],))
        self.assertEqual('10_visa_submission',self.stage())
        self.assertTrue(Path(coe_doc['file_path']).is_file())

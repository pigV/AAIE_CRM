"""Regression coverage for parent completion, final downloads and AAIE cell bounds."""
import io
import base64
import json
import sqlite3
from urllib.parse import urlencode
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pdfplumber
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
import app
from database import Database


class ParentCompletionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'test.db')
        self.db.initialize()
        self.patch = patch.object(app, 'db', self.db)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.student_root = self.root / 'student'
        self.student_root.mkdir()
        class TestNas:
            def student_folder(inner, student):
                return Path(student['folder_path'])
            def category_folder(inner, student, category):
                folder=inner.student_folder(student)/category
                folder.mkdir(parents=True,exist_ok=True)
                return folder
            def require_student_file_readable(inner,student,source):
                return app.ensure_within(Path(source),inner.student_folder(student))
        self.nas_patch = patch.object(app, 'nas', TestNas())
        self.nas_patch.start(); self.addCleanup(self.nas_patch.stop)
        stamp = '2026-10-30T10:00:00+11:00'
        self.db.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,created_date,updated_at,folder_path,form_data_json) VALUES(?,?,?,?,?,?,?,?)", ('SIGNTEST','Test','Parent','Active',stamp,stamp,str(self.student_root),'{}'))
        self.final = self.student_root / 'complete.pdf'
        c = canvas.Canvas(str(self.final)); c.drawString(50,700,'Complete contract fixture'); c.save()
        self.cid = self.db.execute("INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,service_scope_json,final_signed_file_path,generated_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ('SIGNTEST','SIGNTEST-1','留学合同','StudyAbroadContract','Synthetic contract','FullyExecuted','[]',str(self.final),stamp,stamp))
        self.token = 'test-parent-completion'
        self.lid = self.db.execute("INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,signing_url,status,expires_at,created_at,bound_wechat_openid,bound_wechat_nickname,signed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (self.cid,'SIGNTEST',app.contract_signing_token_hash(self.token),'tion','http://localhost/contract-sign/test-parent-completion','signed',stamp,stamp,'test-openid','Test Parent',stamp))
        self.sid = self.db.execute("INSERT INTO contract_signatures(contract_id,student_id,link_id,signed_at,status,archive_status,signature_image_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (self.cid,'SIGNTEST',self.lid,stamp,'signed','archived',str(self.root/'parent.png'),stamp,stamp))
        self.h = object.__new__(app.CRMHandler)
        self.link = self.h.contract_signing_row_by_token(self.token)
        self.h.headers = {'Cookie': f'aaie_csl_{self.lid}='+self.h.contract_signing_cookie_value(self.link,'test-openid')}
        self.h.client_address = ('127.0.0.1',1234)
        self.h.query = {}; self.h.user = None
        self.output = {}
        self.h.send_html = lambda body,*a,**kw: self.output.update(html=body, status=kw.get('status', a[0] if a else 200))
        self.h.contract_signing_pdf_preview_pages_data_uris = lambda *a: []
        self.h.send_response = lambda status: self.output.update(status=status)
        self.h.send_header = lambda k,v: self.output.setdefault('headers',{}).update({k:v})
        self.h.end_headers = lambda: None
        self.h.wfile = io.BytesIO()

    def failed(self):
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned',final_signed_file_path='' WHERE id=?",(self.cid,))
        self.db.execute("UPDATE contract_signatures SET archive_status='failed',archive_error='private NAS path' WHERE id=?",(self.sid,))


class ParentCompletionTests(ParentCompletionFixture):
    def test_failed_reopen_does_not_claim_both_parties_completed(self):
        self.failed()
        self.h.contract_signing_portal('GET',self.token)
        body=self.output['html']
        self.assertNotIn('双方签名及 AAIE 公章已加盖完成',body)
        self.assertNotIn('<h2>合同已完成签署</h2>',body)
        self.assertNotIn('/final/download',body)
        self.assertNotIn('private NAS path',body)
        self.assertIn('无需重新签名',body)

    def test_incomplete_contract_cannot_download_stale_final_file(self):
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned' WHERE id=?",(self.cid,))
        self.h.contract_signing_final_pdf('GET',self.token,'download')
        self.assertEqual(self.h.wfile.getvalue(),b'')
        self.assertIn('暂不可用',self.output.get('html',''))

    def test_failed_archive_cannot_download_previous_final(self):
        self.db.execute("UPDATE contract_signatures SET archive_status='failed' WHERE id=?",(self.sid,))
        self.h.contract_signing_final_pdf('GET',self.token,'download')
        self.assertEqual(self.h.wfile.getvalue(),b'')

    def test_preview_failure_keeps_complete_download_link(self):
        def broken_preview(*args):
            raise RuntimeError('preview unavailable')
        self.h.contract_signing_pdf_preview_pages_data_uris=broken_preview
        self.h.contract_signing_portal('GET',self.token)
        self.assertIn('下载完整合同 PDF',self.output['html'])
        self.assertIn('/final/download',self.output['html'])
        self.h.contract_signing_final_pdf('GET',self.token,'download')
        self.assertEqual(self.output['status'],200)
        self.assertEqual(self.h.wfile.getvalue(),self.final.read_bytes())

    def test_other_wechat_identity_cannot_download(self):
        self.h.headers = {'Cookie':'aaie_csl_'+str(self.lid)+'=wrong'}
        self.h.contract_signing_final_pdf('GET',self.token,'download')
        self.assertEqual(self.h.wfile.getvalue(),b'')
        self.assertNotIn('Content-Type',self.output.get('headers',{}))


class SigningDateTests(unittest.TestCase):
    def test_signing_date_is_melbourne_across_utc_midnight_and_dst(self):
        for raw, expected in [('2026-10-29T14:30:00+00:00','2026年10月30日'),('2026-06-20T14:30:00+00:00','2026年06月21日'),('2026-10-30','2026年10月30日')]:
            with self.subTest(raw=raw), patch.dict('os.environ',{'TZ':'UTC'}):
                self.assertEqual(app.parent_signature_date_text(raw),expected)

    def test_current_company_date_uses_melbourne_even_on_utc_host(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                instant=datetime(2026,10,29,14,30,tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz else instant
        try:
            with patch.object(app,'datetime',Clock), patch.dict('os.environ',{'TZ':'UTC'}):
                time.tzset()
                self.assertEqual(app.local_company_signing_date(),'2026年10月30日')
        finally:
            time.tzset()


class SignatureBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'source.pdf'; self.final=self.root/'final.pdf'
        self.sig=self.root/'sig.png'; self.seal=self.root/'seal.png'
        Image.new('RGBA',(240,100),(0,0,0,255)).save(self.sig)
        Image.new('RGBA',(160,160),(0,0,0,255)).save(self.seal)
        self.assets=patch.object(app,'active_contract_asset_paths',return_value=(self.sig,self.seal))
        self.assets.start(); self.addCleanup(self.assets.stop)
        self.settings={'signature_page':'last','signature_x':220,'signature_y':250,'signature_width':130,'stamp_page':'last','stamp_x':260,'stamp_y':228,'stamp_width':90,'company_date_page':'last','company_date_x':185,'company_date_y':185,'company_date_font_size':30}
        self.placement=patch.object(app,'contract_stamp_placement',return_value=self.settings)
        self.placement.start(); self.addCleanup(self.placement.stop)

    def source_pdf(self,borders=True):
        c=canvas.Canvas(str(self.source),pagesize=app.A4)
        c.setFont(app.reportlab_chinese_font(),10)
        if borders:
            for y in (300,479,504): c.line(62, y,544,y)
            for x in (62,303,544): c.line(x,300,x,504)
        c.drawString(68,486,'AAIE / 甲方（签字 / 盖章）')
        c.drawString(68,460,'代理方（甲方盖章）：AAIE')
        c.drawString(68,438,'授权代表：Sonny Zhang')
        c.drawString(68,423,'甲方签署日期：')
        c.save()

    def test_date_and_images_fit_left_cell_even_with_large_setting(self):
        self.source_pdf()
        app.apply_aaie_signature_and_stamp(self.source,self.final,'2026年10月30日')
        with pdfplumber.open(self.final) as pdf:
            page=pdf.pages[0]
            digits=[c for c in page.chars if c['text'].isdigit()]
            self.assertEqual(len(digits),8)
            self.assertLessEqual(max(c['x1'] for c in digits),301)
            self.assertGreaterEqual(min(c['x0'] for c in digits),140)
            self.assertEqual(len(page.images),2)
            for image in page.images:
                self.assertGreaterEqual(image['x0'],64)
                self.assertLessEqual(image['x1'],301)
                self.assertGreaterEqual(image['y0'],302)
                self.assertLessEqual(image['y1'],min(c['y0'] for c in digits)-3)

    def test_enlarged_signature_and_seal_are_close_below_date(self):
        self.source_pdf()
        app.apply_aaie_signature_and_stamp(self.source,self.final,'2026年10月30日')
        with pdfplumber.open(self.final) as pdf:
            page=pdf.pages[0]
            signature,seal=page.images
            # Previous visible widths were 82pt and 48pt; both dimensions grow 15%.
            self.assertAlmostEqual(signature['x1']-signature['x0'],94.3,places=3)
            self.assertAlmostEqual(seal['x1']-seal['x0'],55.2,places=3)
            self.assertAlmostEqual(signature['y1']-signature['y0'],94.3*100/240,places=3)
            self.assertAlmostEqual(seal['y1']-seal['y0'],55.2,places=3)
            date_bottom=min(c['y0'] for c in page.chars if c['text'].isdigit())
            gap=date_bottom-seal['y1']
            self.assertGreaterEqual(gap,3)
            self.assertLessEqual(gap,8)  # Approximately 2mm, never touching the date.
            self.assertGreater(min(signature['x1'],seal['x1'])-max(signature['x0'],seal['x0']),0)
            self.assertGreater(min(signature['y1'],seal['y1'])-max(signature['y0'],seal['y0']),0)

    def test_missing_master_cell_fails_instead_of_drawing_outside(self):
        self.source_pdf(borders=False)
        with self.assertRaises(ValueError):
            app.apply_aaie_signature_and_stamp(self.source,self.final,'2026年10月30日')
        self.assertFalse(self.final.exists())

    def test_missing_asset_does_not_publish_pdf(self):
        self.source_pdf()
        with patch.object(app,'active_contract_asset_paths',return_value=(self.root/'missing.png',self.seal)):
            with self.assertRaises(ValueError): app.apply_aaie_signature_and_stamp(self.source,self.final)
        self.assertFalse(self.final.exists())


class ParentSubmissionTests(ParentCompletionFixture):
    def prepare_submission(self):
        from test_contract_master_layout import ContractMasterLayoutTests
        rendered=ContractMasterLayoutTests()._render(contract_uid='SYNTHETIC-SIGNING-TEST', student_full_name='TEST ONLY - NOT A REAL CONTRACT')
        self.source=self.student_root/'source.pdf'
        self.source.write_bytes(rendered['pdf_bytes'])
        self.db.execute("DELETE FROM contract_signatures WHERE link_id=?",(self.lid,))
        self.db.execute("UPDATE contract_signing_links SET status='active',signed_at=NULL,expires_at='2099-01-01T00:00:00+11:00' WHERE id=?",(self.lid,))
        self.db.execute("UPDATE contracts SET contract_status='Sent',contract_file_path=?,final_signed_file_path='',parent_signer_name='Test Parent' WHERE id=?",(str(self.source),self.cid))
        self.sign_dir=patch.object(app,'CONTRACT_SIGNING_DIR',self.root/'signing')
        self.sign_dir.start();self.addCleanup(self.sign_dir.stop)
        self.preview_dir=patch.object(app,'PREVIEW_DIR',self.root/'previews')
        self.preview_dir.start();self.addCleanup(self.preview_dir.stop)
        signature=self.root/'aaie-test-signature.png';seal=self.root/'aaie-test-seal.png'
        Image.new('RGBA',(240,80),(0,0,0,255)).save(signature)
        Image.new('RGBA',(160,160),(0,0,0,255)).save(seal)
        self.assets_patch=patch.object(app,'active_contract_asset_paths',return_value=(signature,seal))
        self.assets_patch.start();self.addCleanup(self.assets_patch.stop)
        self.clock_patch=patch.object(app,'now_iso',return_value='2026-10-29T14:30:00+00:00')
        self.clock_patch.start();self.addCleanup(self.clock_patch.stop)
        image=Image.new('RGBA',(420,150),(255,255,255,0))
        ImageDraw.Draw(image).line((18,102,90,38,160,112,248,44,398,92),fill=(14,35,65,255),width=9)
        data=io.BytesIO();image.save(data,format='PNG')
        self.request_bytes=urlencode({'declaration':'yes','parent_name':'Test Parent','signature_data':'data:image/png;base64,'+base64.b64encode(data.getvalue()).decode()}).encode()
        self.h.headers.update({'Content-Type':'application/x-www-form-urlencoded','Content-Length':str(len(self.request_bytes)),'User-Agent':'MicroMessenger synthetic test'})
        self.h.rfile=io.BytesIO(self.request_bytes)

    def test_submit_generates_full_pdf_and_reopen_downloads_same_bytes(self):
        self.prepare_submission()
        original=self.source.read_bytes()
        self.h.contract_signing_submit('POST',self.token)
        self.assertIn('下载完整合同 PDF',self.output.get('html',''))
        contract=self.db.one('SELECT * FROM contracts WHERE id=?',(self.cid,))
        self.assertEqual(contract['contract_status'],'FullyExecuted')
        self.assertEqual(self.db.one('SELECT archive_status FROM contract_signatures WHERE link_id=?',(self.lid,))['archive_status'],'archived')
        final=Path(contract['final_signed_file_path'])
        source_reader=app.PdfReader(str(self.source));final_reader=app.PdfReader(str(final))
        self.assertEqual(len(final_reader.pages),len(source_reader.pages)+1)
        for before,after in zip(source_reader.pages,final_reader.pages):
            self.assertTrue(set((before.extract_text() or '').split()) <= set((after.extract_text() or '').split()))
        signature_page=final_reader.pages[-2]
        self.assertIn('2026年10月30日',signature_page.extract_text())
        with pdfplumber.open(final) as pdf:
            self.assertEqual(len(pdf.pages[-2].images),3)
        self.assertEqual(self.source.read_bytes(),original)
        self.h.contract_signing_portal('GET',self.token)
        self.assertIn('下载完整合同 PDF',self.output['html'])
        self.h.contract_signing_final_pdf('GET',self.token,'download')
        self.assertEqual(self.h.wfile.getvalue(),final.read_bytes())
        self.h.rfile=io.BytesIO(self.request_bytes)
        self.h.contract_signing_submit('POST',self.token)
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM contract_signatures WHERE link_id=?',(self.lid,))['n'],1)
        self.assertEqual(final.read_bytes(),self.h.wfile.getvalue())

    def test_asset_failure_retains_signature_and_retry_completes_original_date(self):
        self.prepare_submission()
        with patch.object(app,'active_contract_asset_paths',return_value=(self.root/'missing.png',self.root/'missing-seal.png')):
            self.h.contract_signing_submit('POST',self.token)
        signature=self.db.one('SELECT * FROM contract_signatures WHERE link_id=?',(self.lid,))
        self.assertEqual(signature['archive_status'],'failed')
        contract=self.db.one('SELECT * FROM contracts WHERE id=?',(self.cid,))
        self.assertEqual(contract['contract_status'],'ParentSigned')
        self.assertEqual(contract['signed_at'],signature['signed_at'])
        parent_bytes=Path(signature['signature_image_path']).read_bytes()
        self.assertTrue(parent_bytes)
        self.assertNotIn('/final/download',self.output['html'])
        self.h.contract_signing_portal('GET',self.token)
        self.assertIn('无需重新签名',self.output['html'])
        with patch.object(app,'now_iso',return_value='2026-11-02T12:00:00+11:00'):
            result=self.h.contract_signing_archive_documents(signature['id'])
        self.h.contract_signing_portal('GET',self.token)
        self.assertIn('下载完整合同 PDF',self.output['html'])
        self.assertEqual(Path(signature['signature_image_path']).read_bytes(),parent_bytes)
        final=app.PdfReader(result['final_document_path'])
        self.assertIn('2026年10月30日',final.pages[-2].extract_text())
        self.assertNotIn('2026年11月02日',final.pages[-2].extract_text())

    def test_archive_retry_rebuilds_confirmation_from_contract_snapshot(self):
        self.prepare_submission()
        self.h.contract_signing_submit('POST',self.token)
        signature=self.db.one('SELECT * FROM contract_signatures WHERE link_id=?',(self.lid,))
        cached=Path(signature['confirmation_pdf_path'])
        old=canvas.Canvas(str(cached));old.drawString(50,700,'STALE CONFIRMATION father');old.save()
        self.db.execute("UPDATE contracts SET standalone_data_json=? WHERE id=?", (json.dumps({'contract_snapshot': {'signer': {'relationship': '母亲'}}}), self.cid))
        result=self.h.contract_signing_archive_documents(signature['id'])
        final=app.PdfReader(result['final_document_path'])
        self.assertIn('乙方与学生关系\n母亲',final.pages[-1].extract_text())
        self.assertNotIn('STALE CONFIRMATION',final.pages[-1].extract_text())

    def test_signature_record_failure_does_not_promise_recoverable_signature(self):
        self.prepare_submission()
        execute=self.db.execute
        def failing_insert(sql,params=()):
            if 'INSERT INTO contract_signatures(' in sql:
                raise sqlite3.OperationalError('database is locked')
            return execute(sql,params)
        with patch.object(self.db,'execute',side_effect=failing_insert):
            self.h.contract_signing_submit('POST',self.token)
        self.assertEqual(self.output['status'],503)
        self.assertNotIn('无需重新签名',self.output['html'])
        self.assertIn('签署未完成',self.output['html'])
        self.assertEqual(self.db.one('SELECT COUNT(*) AS n FROM contract_signatures WHERE link_id=?',(self.lid,))['n'],0)
        self.assertEqual(self.db.one('SELECT status FROM contract_signing_links WHERE id=?',(self.lid,))['status'],'active')
        self.assertEqual(self.db.one('SELECT contract_status FROM contracts WHERE id=?',(self.cid,))['contract_status'],'Sent')


class ConfirmationRelationshipTests(ParentCompletionFixture):
    def test_confirmation_uses_contract_relationship_not_first_parent(self):
        self.db.execute("INSERT INTO parents(student_id,parent_type,passport_first_name) VALUES(?,?,?)", ('SIGNTEST','father','Other Parent'))
        cases = [
            ({'display_bindings': {'fields': {'guardian_relationship': {'value': '母亲'}}}, 'signer': {'relationship': '父亲'}}, '母亲'),
            ({'signer': {'relationship': '父亲'}}, '父亲'),
            ({'parent_signer_relationship': '祖母'}, '祖母'),
            ({}, '父母/监护人'),
        ]
        with patch.object(app, 'CONTRACT_SIGNING_DIR', self.root/'signing'):
            for snapshot, expected in cases:
                with self.subTest(expected=expected):
                    self.db.execute("UPDATE contracts SET standalone_data_json=? WHERE id=?", (json.dumps({'contract_snapshot': snapshot}), self.cid))
                    target=self.h.contract_signing_generate_confirmation_pdf(self.sid)
                    text=app.PdfReader(target).pages[0].extract_text()
                    self.assertIn('乙方与学生关系\n'+expected, text)
                    self.assertNotIn('\nfather\n', text)

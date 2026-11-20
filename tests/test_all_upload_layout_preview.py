import io,mimetypes,tempfile,unittest
from pathlib import Path
from visa_workspace_candidate import VisaWorkspaceCandidateMixin
from document_review_layout import preview_panel

class PreviewBoundaryTests(unittest.TestCase):
    def test_preview_escapes_filename_and_preserves_two_page_controls(self):
        html=preview_panel('/owned/preview','/owned/preview?download=1','<unsafe>.pdf')
        self.assertNotIn('<unsafe>',html);self.assertIn('&lt;unsafe&gt;.pdf',html)
        self.assertIn('data-review-preview-next',html);self.assertIn('data-review-preview-root',html)
    def handler(self,allowed=True,owned=True):
        folder=Path(tempfile.mkdtemp());source=folder/'source.pdf';source.write_bytes(b'SYNTHETIC');png=folder/'preview.png';png.write_bytes(b'png')
        class DB:
            def one(self,sql,args):return {'id':3,'original_name':'source.pdf','file_path':str(source)} if owned and args==(3,7) else None
        g={'db':DB(),'can_access_module':lambda *args:allowed,'pdf_page_count':lambda *a,**k:2,'make_preview':lambda *a,**k:png,'PREVIEW_DIR':folder,'mimetypes':mimetypes}
        class H(VisaWorkspaceCandidateMixin):
            user={};query={}
            def _visa_runtime(self):return g
            def visa_material_case_or_404(self,*args):return {'id':7,'student_id':None}
            def _visa_document_path(self,*args):return source
            def error_page(self,message,status):return status
            def send_response(self,status):self.status=status
            def send_header(self,k,v):self.headers[k]=v
            def end_headers(self):pass
        h=H();h.headers={};h.wfile=io.BytesIO();return h
    def test_foreign_pending_file_and_non_get_are_rejected(self):
        self.assertEqual(404,self.handler(owned=False).visa_batch_pending_preview('GET','',7,3))
        self.assertEqual(403,self.handler(allowed=False).visa_batch_pending_preview('GET','',7,3))
        self.assertEqual(405,self.handler().visa_batch_pending_preview('POST','',7,3))
    def test_unreadable_pdf_returns_a_controlled_preview_error(self):
        h=self.handler()
        def broken(*args,**kwargs):raise RuntimeError('invalid pdf')
        h._visa_runtime()['pdf_page_count']=broken
        self.assertEqual(400,h.visa_batch_pending_preview('GET','',7,3))

    def test_owned_preview_and_download_return_exact_bytes_and_page_headers(self):
        h=self.handler();h.query={'page':['2']};h.visa_batch_pending_preview('GET','',7,3)
        self.assertEqual(200,h.status);self.assertEqual(b'png',h.wfile.getvalue());self.assertEqual('2',h.headers['X-Preview-Page-Count'])
        h=self.handler();h.query={'download':['1']};h.visa_batch_pending_preview('GET','',7,3)
        self.assertEqual(b'SYNTHETIC',h.wfile.getvalue());self.assertIn('attachment',h.headers['Content-Disposition'])

import unittest
from tests import test_business_change_school_chain as chain_fixture
import app


class UploadDiscardTests(unittest.TestCase):
    def test_discard_one_file_keeps_the_other_file_and_review_history(self):
        f = chain_fixture.SchoolMaterialChainTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        files = [app.Upload('files', name, 'application/pdf', f.pdf(name))
                 for name in ('keep.pdf', 'wrong.pdf')]
        token = app.create_upload_review(f.sid, 'Applications', files, f.actor, {})
        _, metadata = app.load_upload_review(token)
        f.h.form = dict(csrf='synthetic', confirm_item_0='1', document_type_0='StudentPassport', discard_item_1='1')
        result = f.h.upload_review_confirm('POST', f.sid, token)
        self.assertEqual('success', result[2], result)
        states = [f.db.one('SELECT status FROM upload_review_items WHERE id=?', (item['item_id'],))[0]
                  for item in metadata['files']]
        self.assertEqual(['Confirmed', 'Cancelled'], states)
        docs = f.db.all('SELECT * FROM student_documents WHERE student_id=? AND folder_category=?', (f.sid, 'Applications'))
        self.assertEqual(1, len(docs))
        from pathlib import Path
        self.assertEqual(files[0].data, Path(docs[0]['file_path']).read_bytes())

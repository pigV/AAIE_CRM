from contextlib import contextmanager
import unittest
from unittest.mock import patch
from tests import test_student_document_lifecycle as fixture


class MaterialRouteTests(unittest.TestCase):
    def test_ajax_business_rejection_is_visible_and_not_a_success_redirect(self):
        import app
        f = fixture.StudentDocumentLifecycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.c.execute('CREATE TABLE workflow_status(student_id TEXT,materials_checked INTEGER)')
        f.c.execute("INSERT INTO workflow_status VALUES('s1',1)")
        class DB:
            @contextmanager
            def transaction(self):
                yield f.c
            @contextmanager
            def business_scope(self, view):
                yield
        h = object.__new__(app.CRMHandler)
        h.user = {'id':1}
        h.headers = {'Accept':'application/json'}
        h.form = {'withdrawn':'1','reason':'Wrong file'}
        h.json_response = lambda payload,status=200:(status,payload)
        h.flash_redirect = lambda *args: self.fail('AJAX rejection must not redirect to HTML')
        with patch.object(app,'db',DB()), patch.object(app,'can_workflow_module_save',return_value=True), patch('business_changes.authorization.current_staff',return_value={'id':1}):
            status, payload = h.student_source_material_withdraw('POST','s1',1)
        self.assertEqual(400,status)
        self.assertFalse(payload['ok'])
        self.assertIn('审核',payload['message'])
        self.assertEqual(1,f.c.execute('SELECT is_active FROM student_documents').fetchone()[0])

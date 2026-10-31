"""Brochure access and report failure handling without a business database."""
import ast
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote

from staging_environment import Staging, NAS_READ_ROOTS, SOURCE, IsolationError
from tests.test_school_map_tiles import handler_methods


class SchoolLibraryPermissionTests(unittest.TestCase):
    def test_school_only_opt_in_does_not_enable_student_access_or_nas_writes(self):
        with tempfile.TemporaryDirectory(dir=SOURCE/'STAGING_DATA/tmp') as folder:
            root = Path(folder)
            (root/'school-library-readonly.json').write_text('{"enabled":true}')
            c = Staging(root, 18880, 18881, 'aaie_staging_')
            self.assertEqual(getattr(c, 'read_roots', c.nas_read_roots), (NAS_READ_ROOTS[1],))
            self.assertEqual(c.nas_read_roots, ())
            self.assertEqual(c.nas_write_roots, ())
            with self.assertRaises(IsolationError):
                c.check(NAS_READ_ROOTS[1]/'VIC/brochure.pdf')


class BrochureFailureTests(unittest.TestCase):
    def test_permission_failure_returns_specific_message_before_report_save(self):
        ns = handler_methods('school_plan_generate')
        class NoWritesDatabase:
            def one(self, sql, params):
                if 'FROM students' in sql:
                    return {'student_id':'TEST','passport_first_name':'Map','passport_last_name':'Test'}
                return {'next_version':1}
            def connect(self): return None
            def _release_connection(self, connection): pass
            def transaction(self): raise AssertionError('Failed report must not be registered')
        with tempfile.TemporaryDirectory(dir=SOURCE/'STAGING_DATA/tmp') as folder:
            ns.update(db=NoWritesDatabase(), quote=quote, Path=Path,
                      infer_plan_scope=lambda *args:None, PlanScopeError=ValueError,
                      compact_filename_part=lambda value, fallback:value,
                      next_version_path=lambda *args:(Path(folder)/'report.pdf',1),
                      school_plan_report_attachment_year=lambda snapshot:'2026',
                      school_plan_append_school_brochures=lambda *args:(_ for _ in ()).throw(
                          PermissionError(1,'Operation not permitted','Box Hill High School.pdf')))
            handler=SimpleNamespace(form={'student_id':'TEST','include_school_brochure_pdf':'1'},
                school_plan_selected_values=lambda key:['1'] if key=='school_ids' else ['basic'],
                school_plan_module_options=lambda:[('basic','Basic')],
                school_plan_build_snapshot=lambda *args:{},
                school_plan_report_folder=lambda *args:Path(folder),
                school_plan_render_html=lambda snapshot:'<html>Report</html>',
                school_plan_render_pdf=lambda *args:None,
                flash_redirect=lambda url,message,kind:(url,message,kind))
            try:
                result=ns['school_plan_generate'](handler)
            except PermissionError as exc:
                self.fail(f'Permission failure escaped report flow: {exc}')
        self.assertEqual(result[2],'error')
        self.assertIn('宣传册',result[1])
        self.assertIn('未创建报告版本',result[1])

    def test_failed_merge_is_not_reported_as_success_with_no_attachments(self):
        tree=ast.parse((SOURCE/'app.py').read_text())
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef)
                      and node.name=='school_plan_append_school_brochures')
        import subprocess, shutil
        ns=dict(Path=Path,tempfile=tempfile,subprocess=subprocess,shutil=shutil,
                append_service_log=lambda *args,**kwargs:None,
                school_plan_pdfunite_path=lambda:Path('/allowed/pdfunite'),
                school_plan_appendix_divider_pdf_bytes=lambda year:b'%PDF fixture')
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<brochure-merge>','exec'),ns)
        with tempfile.TemporaryDirectory(dir=SOURCE/'STAGING_DATA/tmp') as folder:
            path=Path(folder)/'report.pdf';path.write_bytes(b'%PDF original report')
            ns['school_plan_find_school_brochure_pdf']=lambda *args:{'id':1,'school_id':1,
                'file_path':str(path),'document_type':'School Brochure',
                'original_filename':'School.pdf','stored_filename':'School.pdf'}
            with patch.object(subprocess,'run',side_effect=PermissionError('Tool blocked')):
                with self.assertRaisesRegex(RuntimeError,'宣传册'):
                    ns['school_plan_append_school_brochures'](path,path,[1],'2026')
            self.assertEqual(path.read_bytes(),b'%PDF original report')


if __name__=='__main__':unittest.main()

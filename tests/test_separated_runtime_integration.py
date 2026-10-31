"""Application integration without importing the business application."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from cryptography.fernet import Fernet

SOURCE = Path(__file__).resolve().parents[1]


def function(name, filename="app.py"):
    tree = ast.parse((SOURCE / filename).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.Module(body=[node], type_ignores=[])


class SeparatedRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.code = self.root / "code"; self.code.mkdir()
        self.data = self.root / "data"; self.data.mkdir()

    def test_managed_adapter_keeps_legacy_staging_rejection(self):
        import staging_environment as s
        self.assertTrue(hasattr(s, "ManagedRuntime"), "Managed external data adapter is missing")
        # The original loader remains source-local, so a test config cannot
        # silently gain access to the new production root.
        import json
        p=self.data/"staging.json"
        p.write_text(json.dumps(dict(mode="staging",root=str(self.data),admin_port=29880,
                                    parent_port=29881,namespace="aaie_staging_")))
        with self.assertRaises(s.IsolationError):s.Staging.load(p)

    def test_missing_key_is_never_generated_by_managed_ensure_dirs(self):
        key=self.data/"config/field_encryption.key"
        class Config:
            def require_ready(self):
                raise RuntimeError("field_key_missing")
        ns=dict(STAGING=SimpleNamespace(runtime_configuration=Config(),read_roots=()),
                Fernet=Fernet,os=__import__("os"),
                ensure_student_template=lambda:None,
                ensure_study_contract_template=lambda p:None,
                append_service_log=lambda *a,**k:None)
        names=("LOCAL_BACKUP_DIR PREVIEW_DIR SCHOOL_PLAN_REPORT_DIR SCHOOL_PLAN_CONFIRMATION_DIR "
               "CONTRACT_SIGNING_DIR UPLOAD_REVIEW_DIR ASSET_DIR LOG_DIR TEMPLATE_DIR "
               "MANAGED_TEMPLATE_DIR TEMPLATE_ARCHIVE_DIR BETTER_EDUCATION_IMPORT_DIR "
               "SCHOOL_LIBRARY_FILE_DIR SIGNATURE_DIR STAMP_DIR").split()
        ns.update({n:self.data/n.lower() for n in names})
        ns.update(SECRET_PATH=key,STUDY_CONTRACT_TEMPLATE_PATH=self.data/"template.docx")
        exec(compile(function("ensure_dirs"),"<ensure-dirs>","exec"),ns)
        try:ns["ensure_dirs"]()
        except RuntimeError as exc:self.assertIn("field_key_missing",str(exc))
        self.assertFalse(key.exists(),"Managed startup generated a replacement encryption key")
        self.assertFalse((self.data/"template_dir").exists(),"Managed startup created runtime structure")

    def test_managed_server_never_initializes_database_or_templates(self):
        calls=[]
        class Config:
            def require_ready(self):return {"ready":True}
        class Server:
            def __init__(self,*args):self.server_port=29880
            def serve_forever(self):raise KeyboardInterrupt
            def server_close(self):pass
        ns=dict(SAFE_PRODUCTION_ACTIVE=False,STAGING=SimpleNamespace(runtime_configuration=Config()),
                APP_WRITE_GATE=None,startup_readonly=lambda g:False,
                ensure_dirs=lambda:None,
                db=SimpleNamespace(initialize=lambda:calls.append("initialize")),
                ensure_template_management_seed=lambda:calls.append("template_seed"),
                CLASS_TWO_TASKS=(),append_service_log=lambda *a,**k:None,
                wechat_oauth_startup_messages=lambda:[],
                ReusableThreadingHTTPServer=Server,CRMHandler=object,VERSION="synthetic",
                crm_admin_lan_url=lambda:"http://127.0.0.1:29880",
                start_optional_startup_task=lambda *a,**k:None,
                startup_backup=lambda:None,os=__import__("os"))
        exec(compile(function("run_server"),"<run-server>","exec"),ns)
        ns["run_server"]("127.0.0.1",29880,"admin")
        self.assertEqual(calls,[],"Managed startup ran implicit schema/seed initialization")

    def test_perf_logs_follow_active_data_root(self):
        ns=dict(STAGING=SimpleNamespace(root=self.data),Path=Path,__file__=str(self.code/"database.py"),
                _db_perf_enabled=lambda:True,now_iso=lambda:"synthetic")
        exec(compile(function("_db_perf_log","database.py"),"<database-perf>","exec"),ns)
        ns["_db_perf_log"]("sample")
        self.assertFalse((self.code/"logs").exists(),"Performance logging wrote into the code tree")
        self.assertIn("sample",(self.data/"logs/database.log").read_text())

    def test_code_assets_stay_in_code_tree_for_managed_runtime(self):
        tree=ast.parse((SOURCE/"app.py").read_text())
        node=next(n for n in tree.body if isinstance(n,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id=="ASSET_DIR" for t in n.targets))
        ns=dict(STAGING=SimpleNamespace(root=self.data),SOURCE_DIR=self.code,APP_DIR=self.data)
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<assets>","exec"),ns)
        self.assertEqual(ns["ASSET_DIR"],self.code/"assets")


    def test_vendor_python_packages_stay_in_code_tree(self):
        tree=ast.parse((SOURCE/"app.py").read_text())
        node=next(n for n in tree.body if isinstance(n,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id=="CONTENT_EXTRACTION_VENDOR_DIR" for t in n.targets))
        ns=dict(SOURCE_DIR=self.code,APP_DIR=self.data)
        exec(compile(ast.Module(body=[node],type_ignores=[]),"<vendor>","exec"),ns)
        self.assertEqual(ns["CONTENT_EXTRACTION_VENDOR_DIR"],self.code/"vendor/content_extraction")


class ControllerContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("tools.runtime_control"),
                             "Separated runtime controller is missing")

    def test_foreign_pid_record_cannot_be_stopped(self):
        from tools.runtime_control import validate_process_identity, RuntimeControlError
        cfg=SimpleNamespace(source_root=Path("/synthetic/code"),
                            config_sha256="expected",root=Path("/synthetic/runtime"))
        record={"pid":1,"started":"birth","config_sha256":"expected",
                "data_root":"/synthetic/runtime","role":"admin"}
        with self.assertRaises(RuntimeControlError):
            validate_process_identity(cfg,"admin",record,"unrelated process","birth")

    def test_production_action_requires_explicit_authority(self):
        from tools.runtime_control import require_control_authority, RuntimeControlError
        with self.assertRaises(RuntimeControlError):
            require_control_authority(SimpleNamespace(mode="production"),False)
        require_control_authority(SimpleNamespace(mode="staging"),False)

if __name__=="__main__":unittest.main()

import json
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.runtime_invoice_mail_config import load_mail_config


class RuntimeInvoiceMailTests(unittest.TestCase):
    def test_absent_and_untrusted_config_are_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"config").mkdir()
            config = SimpleNamespace(root=root,mode="staging")
            self.assertIsNone(load_mail_config(config))
            path = root/"config/invoice-mail.json"
            path.write_text('{"enabled":true}')
            path.chmod(0o644)
            self.assertIsNone(load_mail_config(config))
            path.chmod(0o600)
            self.assertIsNone(load_mail_config(config))

    def test_missing_configuration_prevents_network_and_child_process(self):
        from tools import runtime_control
        with tempfile.TemporaryDirectory() as directory:
            config=SimpleNamespace(root=Path(directory),mode="staging")
            with patch.object(runtime_control.socket,"getaddrinfo") as resolve, \
                 patch.object(runtime_control.subprocess,"run") as spawn:
                self.assertFalse(runtime_control.run_invoice_mail(config,probe=True)["ready"])
            resolve.assert_not_called()
            spawn.assert_not_called()

    def test_disabled_configuration_keeps_validated_pdf_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            (root/"config").mkdir()
            (root/"pdf").mkdir(mode=0o700)
            config=SimpleNamespace(root=root,mode="staging",db=root/"database/probe.db")
            path=root/"config/invoice-mail.json"
            values=dict(enabled=False,mode="staging",recipient="zhang0723@gmail.com",pdf_root=str(root/"pdf"))
            path.write_text(json.dumps(values))
            path.chmod(0o600)
            settings=load_mail_config(config)
            self.assertFalse(settings.enabled)
            self.assertEqual(root/"pdf",settings.pdf_root)
            path.write_text(json.dumps({**values,"recipient":"ap@school.example.test"}))
            self.assertIsNone(load_mail_config(config))

    def test_disabled_allows_auth_probe_and_rejects_worker_start(self):
        from contextlib import nullcontext
        from io import StringIO
        from tools import runtime_control as control

        config = SimpleNamespace(root=Path("/isolated"),source_root=Path("/source"))
        settings = SimpleNamespace(enabled=False)
        response = SimpleNamespace(returncode=0,stdout='{"smtp_login":true}')
        with patch.object(control,"load_mail_config",return_value=settings), \
             patch.object(control,"runtime_file",return_value=nullcontext(StringIO())), \
             patch.object(control,"invoice_mail_profile",return_value="profile"), \
             patch.object(control,"environment",return_value={}), \
             patch.object(control,"invoice_mail_keychain",return_value="/keychain"), \
             patch.object(control,"process_command",return_value=["worker"]), \
             patch.object(control.socket,"getaddrinfo",return_value=[(None,None,None,None,("192.0.2.1",465))]) as resolve, \
             patch.object(control.subprocess,"run",return_value=response) as probe, \
             patch.object(control.subprocess,"Popen") as spawn:
            self.assertFalse(control.run_invoice_mail(config)["ready"])
            resolve.assert_not_called()
            self.assertTrue(control.run_invoice_mail(config,probe=True)["ready"])
            self.assertEqual("--probe",probe.call_args.args[0][-1])
            spawn.assert_not_called()

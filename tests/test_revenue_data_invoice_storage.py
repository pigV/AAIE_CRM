from pathlib import Path
from tempfile import TemporaryDirectory
from hashlib import sha256
import unittest

from revenue.data_invoice_storage import InvoicePDFStore
from revenue.secure_storage import StorageUnavailable


class DataInvoiceStorageTests(unittest.TestCase):
    def test_authenticated_immutable_file_and_replacement(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/"pdf"
            root.mkdir(mode=0o700)
            store=InvoicePDFStore(root,root.stat().st_dev,root.stat().st_ino,b"k"*32)
            pdf=b"%PDF-1.4 synthetic"
            digest=sha256(pdf).hexdigest()
            path=store.write(pdf)
            self.assertNotIn(pdf,path.read_bytes())
            self.assertEqual(path,store.write(pdf))
            self.assertEqual(pdf,store.read(path,digest,len(pdf)))
            sealed=path.read_bytes()
            path.write_bytes(sealed[:-1]+bytes([sealed[-1]^1]))
            with self.assertRaises(StorageUnavailable):
                store.read(path,digest,len(pdf))
            path.unlink()
            with self.assertRaises(StorageUnavailable):
                store.read(path,digest,len(pdf))
            root.chmod(0o770)
            with self.assertRaises(StorageUnavailable):
                store.write(pdf)
            root.chmod(0o700)
            root.rename(root.with_name("previous"))
            root.symlink_to(root.with_name("previous"),target_is_directory=True)
            with self.assertRaises(StorageUnavailable):
                store.write(pdf)
            root.unlink()
            root.mkdir(mode=0o700)
            with self.assertRaises(StorageUnavailable):
                store.write(pdf)

    def test_official_issuance_cannot_fall_back_to_plaintext(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from revenue.toorak_claims import ToorakClaims
        issuer=SimpleNamespace(formal=True,simulation=False)
        with patch("revenue.toorak_claims.configured_invoice_store",return_value=None):
            with self.assertRaises(StorageUnavailable):
                ToorakClaims._persist_invoice(issuer,[],{},"missing-store")

    def test_invalid_optional_descriptor_does_not_raise_during_bootstrap(self):
        import json
        from types import SimpleNamespace
        from tools.runtime_invoice_mail_config import invoice_pdf_environment
        with TemporaryDirectory() as directory:
            config=SimpleNamespace(root=Path(directory),mode="staging")
            parent=config.root/"config"
            parent.mkdir()
            path=parent/"invoice-pdf.json"
            path.write_text(json.dumps(dict(root="relative",keychain_item="AAIE CRM Data Invoice PDF Rehearsal")))
            path.chmod(0o600)
            self.assertEqual({},invoice_pdf_environment(config))

    def test_managed_revocation_stops_before_keychain_read(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from revenue.data_invoice_storage import PRODUCTION_ROOT, configured_invoice_store
        with patch.dict("os.environ",{"AAIE_CRM_ENV":"production","AAIE_CRM_RUNTIME_CONFIG":"/config/runtime.json",
                                     "AAIE_INVOICE_PDF_ROOT":str(PRODUCTION_ROOT),
                                     "AAIE_INVOICE_PDF_KEYCHAIN_ITEM":"AAIE CRM Production Invoice PDF"}), \
             patch("runtime_configuration.RuntimeConfiguration.load",return_value=SimpleNamespace(mode="production")), \
             patch("tools.runtime_invoice_mail_config.invoice_pdf_environment",return_value={}), \
             patch("revenue.data_invoice_storage.subprocess.run") as read:
            with self.assertRaises(StorageUnavailable):
                configured_invoice_store()
            read.assert_not_called()

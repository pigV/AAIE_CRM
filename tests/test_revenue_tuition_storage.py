"""Exercise the dedicated proof store on the real Data mount without stubs."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from revenue.secure_storage import StorageUnavailable, configured_root
from revenue.tuition_storage import TuitionEvidenceStorage


DATA_VOLUME = Path("/Volumes/Data")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(DATA_VOLUME.is_mount() and PROJECT_ROOT.is_relative_to(DATA_VOLUME),
                     "requires a CRM checkout on the Data volume")
class TuitionEvidenceStorageTests(unittest.TestCase):
    def setUp(self):
        root = PROJECT_ROOT / "STAGING_DATA" / "tuition-storage-fixtures"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before = DATA_VOLUME.stat().st_mode
        self.secure = self.root/"secure"
        self.secure.mkdir(mode=0o700)
        self.store = TuitionEvidenceStorage(self.root/"legacy", self.secure)
        self.pdf = b"%PDF-1.4\nTEST ONLY\n%%EOF"
        self.upload = SimpleNamespace(filename="offer.pdf",content_type="application/pdf",data=self.pdf)

    def test_private_dedicated_store_under_data_writes_and_verifies_original(self):
        proof = self.store.stage(self.upload, {".pdf"})
        self.assertEqual(self.secure/"tuition-evidence", proof.path.parent)
        self.assertEqual(self.pdf,self.store.read(proof.path,proof.sha256,proof.size_bytes))
        self.assertEqual(sha256(self.pdf).hexdigest(),proof.sha256)
        self.assertEqual(0o700,proof.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600,proof.path.stat().st_mode & 0o777)
        self.assertFalse(self.store.stage(self.upload,{".pdf"}).created)
        with self.assertRaises(StorageUnavailable):
            self.store.read(proof.path,"b"*64,proof.size_bytes)
        with self.assertRaises(StorageUnavailable):
            self.store.read(self.root/proof.path.name,proof.sha256,proof.size_bytes)
        if self.before & 0o022:
            with self.assertRaises(StorageUnavailable):
                configured_root({"REVENUE_SECURE_STORAGE_ROOT":str(self.secure)})
        self.assertEqual(self.before,DATA_VOLUME.stat().st_mode)

    def test_refuses_symbolic_link_and_non_private_leaf(self):
        leaf = self.secure/"tuition-evidence"
        outside = self.root/"outside"
        outside.mkdir(mode=0o700)
        leaf.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageUnavailable):
            self.store.stage(self.upload,{".pdf"})
        leaf.unlink()
        leaf.mkdir(mode=0o700)
        leaf.chmod(0o750)
        self.assertEqual(0o750, leaf.stat().st_mode & 0o777)
        with self.assertRaises(StorageUnavailable):
            self.store.stage(self.upload,{".pdf"})

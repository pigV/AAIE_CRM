import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from local_document_lab.provenance import implementation_signature,verify_model_snapshot,packet_identity_matches


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_generation_code_change_invalidates_signature_even_with_same_prompt(self):
        for name in ('infer_v2.py','evidence.py','project.py'):
            path=self.root/'local_document_lab'/name;path.parent.mkdir(exist_ok=True);path.write_text('same prompt; implementation v1')
        path=self.root/'STAGING_DATA/environment.lock';path.parent.mkdir();path.write_text('locked')
        before=implementation_signature(self.root)
        (self.root/'local_document_lab/infer_v2.py').write_text('same prompt; implementation v2')
        self.assertNotEqual(before,implementation_signature(self.root))

    def test_model_revision_receipt_does_not_hide_changed_weight_file(self):
        data=b'actual weights';(self.root/'model.safetensors').write_bytes(data)
        (self.root/'download-receipt.json').write_text(json.dumps({'revision':'pinned','files':[{'name':'model.safetensors','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}))
        self.assertTrue(verify_model_snapshot(self.root))
        (self.root/'model.safetensors').write_bytes(b'corrupted')
        with self.assertRaises(ValueError):verify_model_snapshot(self.root)

    def test_changed_document_kind_or_identity_invalidates_packet(self):
        self.assertTrue(packet_identity_matches({'id':'one','kind':'coe'},{'id':'one','kind':'coe'}))
        self.assertFalse(packet_identity_matches({'id':'one','kind':'coe'},{'id':'one','kind':'caaw'}))
        self.assertFalse(packet_identity_matches({'id':'one','kind':'coe'},{'id':'two','kind':'coe'}))

if __name__=='__main__':unittest.main()

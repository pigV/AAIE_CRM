import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from local_document_lab.run import packet_is_current, run_offline
from local_document_lab.parse import packet_from_document
from local_document_lab.sandbox import DATA


class RunnerTests(unittest.TestCase):
    def setUp(self):
     (DATA/'tmp').mkdir(parents=True,exist_ok=True)
    def test_cached_packet_does_not_survive_changed_pdf_or_parser(self):
        with tempfile.TemporaryDirectory(dir=DATA/'tmp') as folder:
            source=Path(folder)/'file.pdf';source.write_bytes(b'new pdf')
            packet={'source_sha256':hashlib.sha256(b'old pdf').hexdigest(),'parser_signature':'v1'}
            self.assertFalse(packet_is_current(packet,source,'v1'))
            packet['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertTrue(packet_is_current(packet,source,'v1'))
            self.assertFalse(packet_is_current(packet,source,'v2'))

    def test_multpage_table_does_not_assign_first_page_to_all_cells(self):
        class Box:
            def model_dump(self,**kwargs):return {'l':1,'t':2,'r':3,'b':4}
        cell=SimpleNamespace(text='IEC Marrickville',bbox=Box(),start_row_offset_idx=1,start_col_offset_idx=2)
        item=SimpleNamespace(label='table',prov=[SimpleNamespace(page_no=1,bbox=Box()),SimpleNamespace(page_no=2,bbox=Box())],
            data=SimpleNamespace(table_cells=[cell]),export_to_markdown=lambda **kwargs:'School | IEC Marrickville')
        doc=SimpleNamespace(iterate_items=lambda:[(item,0)],pages={1:None,2:None})
        packet=packet_from_document(doc,'test','confirmation')
        self.assertIsNone(packet['blocks'][1]['page'])
        self.assertIsNone(packet['blocks'][1]['bbox'])
        self.assertEqual(packet['blocks'][1]['page_candidates'],[1,2])

    def test_worker_over_memory_budget_is_stopped(self):
        with tempfile.TemporaryDirectory(dir=DATA/'tmp') as folder:
            result=run_offline(['-c','import time; data=bytearray(80*1024*1024); time.sleep(10)'],Path(folder)/'log',
                               timeout=15,memory_limit_bytes=48*1024**2)
        self.assertEqual(result['returncode'],137)
        self.assertEqual(result['error'],'memory_budget_exceeded')

if __name__=='__main__':unittest.main()

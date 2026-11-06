"""The model gets source identity/text, not expensive floating-point geometry."""
from copy import deepcopy
import json
import unittest
from local_document_lab.infer_v4 import select_blocks

class PromptBudgetTests(unittest.TestCase):
    def test_usable_evidence_fits_without_geometry_but_original_packet_is_unchanged(self):
        blocks=[{'id':f'b{i}','kind':'table_cell','text':'School: Sample College Start Date: 27 January 2027',
                 'page':3,'table_id':'table','row':i,'column':1,'page_candidates':[3],
                 'bbox':{'l':123.00000000000003,'t':234.00000000000003,'r':456.00000000000003,'b':567.00000000000003,'coord_origin':'TOPLEFT'}} for i in range(50)]
        before=deepcopy(blocks);selected=select_blocks(blocks,max_chars=11000)
        self.assertEqual(len(selected),50)
        self.assertEqual(blocks,before)
        self.assertLessEqual(sum(len(json.dumps(b,ensure_ascii=False)) for b in selected),11000)
        for source,view in zip(blocks,selected):
            self.assertNotIn('bbox',view)
            for key in ('id','kind','text','page','table_id','row','column'):
                self.assertEqual(view[key],source[key])

    def test_selection_never_exceeds_existing_character_budget(self):
        blocks=[{'id':str(i),'kind':'text','text':'School course details '*80,'page':1} for i in range(50)]
        selected=select_blocks(blocks,max_chars=5000)
        self.assertLessEqual(sum(len(json.dumps(b,ensure_ascii=False)) for b in selected),5000)

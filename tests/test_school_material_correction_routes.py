import unittest
from unittest.mock import patch
import app
from tests import test_business_change_school_chain as chain
from school_material_corrections import material_state

class SchoolMaterialCorrectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.f=chain.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
    def test_native_void_restore_and_business_versions_keep_original_file(self):
        f=self.f
        doc=f.review('COE + OSHC','CAAW',{'school_name':f.school},{'intake_defer_0':'1'})
        record=f.db.one('SELECT id,file_path FROM coe_oshc WHERE file_path=?',(doc['file_path'],))
        for action in ('void','restore'):
            with f.db.read_snapshot() as c:state=material_state(c,f.sid,'coe',record['id'])
            f.h.form={'kind':'coe','record_id':str(record['id']),'action':action,'revision':state['revision'],'reason':'SYNTHETIC correction route'}
            result=f.h.student_school_material_corrections('POST',f.sid)
            self.assertEqual('success',result[2],result)
        from pathlib import Path
        self.assertTrue(Path(record['file_path']).is_file())
        self.assertEqual(1,f.db.one('SELECT is_active FROM student_documents WHERE id=?',(doc['id'],))[0])
        self.assertEqual(2,f.db.one("SELECT count(*) FROM business_change_events WHERE event_type='school_material.corrected'")[0])
    def test_adopted_coe_post_is_blocked_and_correction_page_lists_retained_materials(self):
        f=self.f
        record=f.db.one("SELECT id FROM coe_oshc WHERE student_id=? AND document_kind='COE'",(f.sid,))
        with f.db.read_snapshot() as c:state=material_state(c,f.sid,'coe',record['id'])
        f.h.form={'kind':'coe','record_id':str(record['id']),'action':'void','revision':state['revision'],'reason':'SYNTHETIC adopted'}
        result=f.h.student_school_material_corrections('POST',f.sid)
        self.assertEqual('error',result[2]);self.assertIn('采用',result[1])
        f.h.send_html=lambda html:html
        with patch.object(app.nas,'status',return_value={},create=True),patch.object(app,'page',side_effect=lambda title,body,*args:body):
            html=f.h.student_school_material_corrections('GET',f.sid)
        self.assertIn('学校材料纠错',html);self.assertIn('保留原件',html)

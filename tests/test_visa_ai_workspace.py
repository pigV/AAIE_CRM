"""Actual workspace routes + actual provider/persistence, injected transport only."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import tests.test_visa_workspace_security as support
from visa_ai_candidate import VisaAIProvider
import visa_documents_candidate as documents

class VisaAIWorkspaceTests(unittest.TestCase):
    setUp=support.VisaWorkspaceSecurityTests.setUp
    handler=support.VisaWorkspaceSecurityTests.handler
    extraction=support.VisaWorkspaceSecurityTests.extraction

    def provider(self,kind,callback=None):
        def send(payload,timeout):
            self.assertFalse(self.conn.in_transaction,'Model request cannot hold a write transaction')
            self.sent=payload
            if callback:callback()
            keys={'document_kind':'grant','decision_type':'grant','decision_date':'2026-09-05','official_date':'2026-09-05','length_of_stay':'3 years','expiry_date':'','medical_completed_date':''} if kind=='letter' else {'Summary':'Reviewed','Risk':'Review dates','Missing':'None identified','Action Required':'Staff review','full_analysis':'Evidence document 1 reviewed.'}
            return {'status':'completed','id':'resp_test','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'result':keys,'evidence_ids':[1]})}]}]}
        return VisaAIProvider(api_key='test-credential',model='test-model',transport=send)

    def route(self):
        handler=self.handler();handler._visa_text_from_document=lambda *args:'Date of grant: 5 September 2026'
        handler._visa_document_path=lambda *args:Path('/synthetic/grant.pdf')
        handler.visa_evidence_ownership_context=lambda case_id:{'property_owner':'Father'}
        return handler

    def test_ai_detect_persists_audited_proposal_without_confirming(self):
        doc,_=self.extraction();handler=self.route();handler.form={'action':'ai-extract','document_id':str(doc['id'])}
        with patch('visa_ai_candidate.provider_from_environment',return_value=self.provider('letter')):handler.visa_case_workspace('POST','1')
        self.assertEqual(302,handler.response[0])
        row=self.db.one('SELECT * FROM visa_letter_extractions ORDER BY id DESC LIMIT 1')
        self.assertEqual('openai_responses:test-model',row['extraction_method'])
        self.assertEqual('Pending',self.db.one('SELECT result_status FROM visa_cases WHERE id=1')[0])
        audit=self.db.one("SELECT * FROM visa_document_audit WHERE action='ai_letter_extracted'")
        self.assertEqual('resp_test',json.loads(audit['after_json'])['metadata']['response_id'])

    def test_missing_configuration_is_informational_without_new_proposal(self):
        doc,_=self.extraction();handler=self.route();handler.form={'action':'ai-extract','document_id':str(doc['id'])}
        handler._visa_send=lambda title,body:setattr(handler,'response',(200,body))
        with patch('visa_ai_candidate.provider_from_environment',return_value=None):handler.visa_case_workspace('POST','1')
        self.assertEqual(200,handler.response[0]);self.assertEqual(1,self.db.one('SELECT COUNT(*) FROM visa_letter_extractions')[0])

    def test_analysis_uses_submitted_files_and_retains_full_result(self):
        doc,_=self.extraction();documents.mark_submitted(self.conn,1,doc['id'],**self.actor)
        documents.register_document(self.conn,1,file_path='/synthetic/private.pdf',original_name='PRIVATE',version=1,sha256='b'*64,**self.actor)
        handler=self.route()
        with patch('visa_ai_candidate.provider_from_environment',return_value=self.provider('analysis')):handler.visa_submission_analysis('POST','1')
        self.assertEqual(302,handler.response[0]);self.assertNotIn('PRIVATE',json.dumps(self.sent))
        audit=self.db.one("SELECT after_json FROM visa_document_audit WHERE action='ai_analysis_completed'")
        self.assertEqual('Evidence document 1 reviewed.',json.loads(audit[0])['result']['full_analysis'])
        captured=[];handler._visa_send=lambda title,body:captured.append(body)
        handler.visa_submission_analysis('GET','1');self.assertIn('Evidence document 1 reviewed.',captured[0])

    def test_archive_during_detection_rejects_stale_proposal(self):
        doc,_=self.extraction();handler=self.route();handler.form={'action':'ai-extract','document_id':str(doc['id'])}
        archive=lambda:documents.set_document_archived(self.conn,1,doc['id'],archived=True,**self.actor)
        with patch('visa_ai_candidate.provider_from_environment',return_value=self.provider('letter',archive)):handler.visa_case_workspace('POST','1')
        self.assertEqual(400,handler.response[0]);self.assertEqual(1,self.db.one('SELECT COUNT(*) FROM visa_letter_extractions')[0])

    def test_archive_during_analysis_prevents_stale_result_persistence(self):
        doc,_=self.extraction();documents.mark_submitted(self.conn,1,doc['id'],**self.actor);handler=self.route()
        archive=lambda:documents.set_document_archived(self.conn,1,doc['id'],archived=True,**self.actor)
        with patch('visa_ai_candidate.provider_from_environment',return_value=self.provider('analysis',archive)):handler.visa_submission_analysis('POST','1')
        self.assertEqual(400,handler.response[0]);self.assertEqual(0,self.db.one("SELECT COUNT(*) FROM visa_document_audit WHERE action='ai_analysis_completed'")[0])

    def test_owner_change_during_analysis_rejects_misattributed_result(self):
        doc,_=self.extraction();documents.mark_submitted(self.conn,1,doc['id'],**self.actor);handler=self.route()
        owner={'property_owner':'Father'};handler.visa_evidence_ownership_context=lambda case_id:dict(owner)
        def changed_owner():owner['property_owner']='Applicant'
        with patch('visa_ai_candidate.provider_from_environment',return_value=self.provider('analysis',changed_owner)):handler.visa_submission_analysis('POST','1')
        self.assertEqual(400,handler.response[0]);self.assertEqual(0,self.db.one("SELECT COUNT(*) FROM visa_document_audit WHERE action='ai_analysis_completed'")[0])

if __name__=='__main__':unittest.main()

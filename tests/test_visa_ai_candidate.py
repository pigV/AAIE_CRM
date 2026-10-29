import importlib
import importlib.util
import json
import unittest

class VisaAIProviderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('visa_ai_candidate'),'Visa model adapter missing')
        self.module=importlib.import_module('visa_ai_candidate');self.sent=[]
        self.evidence={'document_id':7,'version':2,'sha256':'a'*64}
        self.letter={'document_kind':'grant','decision_type':'grant','decision_date':'2026-09-05','official_date':'2026-09-05','length_of_stay':'Until 2029-03-15','expiry_date':'2029-03-15','medical_completed_date':''}
        self.analysis={'Summary':'Submitted letter reviewed','Risk':'Review required','Missing':'None identified','Action Required':'Staff must confirm','full_analysis':'Evidence document 7 supports the recorded grant date.'}
    def response(self,result,ids=None):
        return {'id':'resp_test','status':'completed','model':'test-model','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'result':result,'evidence_ids':[7] if ids is None else ids})}]}]}
    def provider(self,result=None,response=None):
        def transport(payload,timeout):self.sent.append(payload);return response or self.response(result or self.letter)
        return self.module.VisaAIProvider(api_key='synthetic-key',model='test-model',transport=transport)
    def test_missing_configuration_does_not_guess_model_or_call_network(self):
        self.assertIsNone(self.module.provider_from_environment({}))
        self.assertIsNone(self.module.provider_from_environment({'VISA_AI_PROVIDER':'openai','OPENAI_API_KEY':'synthetic'}))
    def test_extract_suggestions_returns_auditable_strict_result(self):
        output=self.provider().extract_letter('Date of grant: 5 September 2026',self.evidence)
        self.assertEqual(self.letter,output['result']);self.assertEqual(64,len(output['metadata']['request_hash']))
        self.assertEqual('a'*64,output['metadata']['source_evidence'][0]['sha256'])
        self.assertFalse(self.sent[0]['store']);self.assertTrue(self.sent[0]['text']['format']['strict'])
        self.assertNotIn('synthetic-key',json.dumps(output))
    def test_model_cannot_invent_medical_completion_or_invalid_business_date(self):
        for fields in [{'medical_completed_date':'2026-09-07'},{'decision_date':'2026-02-30'},{'decision_type':'refusal'}]:
            with self.assertRaises(self.module.VisaAIError):self.provider({**self.letter,**fields}).extract_letter('text',self.evidence)
    def test_analysis_transmits_only_pinned_submitted_evidence(self):
        context={'scope':'IMMI_SUBMITTED_ONLY','documents':[{**self.evidence,'submission_id':3,'text':'official text'}],'ownership':{'property_owner':'Father'},'prepared_documents':[{'text':'PRIVATE PREPARED DO NOT SEND'}]}
        output=self.provider(self.analysis).analyze(context)
        self.assertEqual(self.analysis,output['result'])
        payload=json.dumps(self.sent[0]);self.assertNotIn('PRIVATE PREPARED',payload);self.assertIn('Father',payload)
    def test_analysis_rejects_non_submitted_scope_or_unpinned_evidence_before_transport(self):
        for context in [{'scope':'PREPARED','documents':[]},{'scope':'IMMI_SUBMITTED_ONLY','documents':[{'document_id':7,'text':'text'}]}]:
            with self.assertRaises(self.module.VisaAIError):self.provider(self.analysis).analyze(context)
        self.assertEqual([],self.sent)
    def test_output_cannot_reference_unknown_evidence(self):
        with self.assertRaises(self.module.VisaAIError):self.provider(response=self.response(self.letter,[999])).extract_letter('text',self.evidence)
    def test_refusal_incomplete_and_malformed_response_fail_closed(self):
        for response in [{'status':'incomplete'}, {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'No'}]}]},self.response({'not_result':'bad'})]:
            with self.assertRaises(self.module.VisaAIError):self.provider(response=response).extract_letter('text',self.evidence)
    def test_transport_exception_and_echoed_secret_are_redacted(self):
        def failure(*args):raise RuntimeError('synthetic-key credential reflected')
        provider=self.module.VisaAIProvider(api_key='synthetic-key',model='test-model',transport=failure)
        with self.assertRaises(self.module.VisaAIError) as error:provider.extract_letter('text',self.evidence)
        self.assertNotIn('synthetic-key',str(error.exception))
        with self.assertRaises(self.module.VisaAIError):self.provider(response=self.response({**self.letter,'length_of_stay':'synthetic-key'})).extract_letter('text',self.evidence)

if __name__=='__main__':unittest.main()

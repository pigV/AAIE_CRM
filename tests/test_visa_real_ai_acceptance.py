"""Acceptance-harness checks; these are NOT real model acceptance results."""
import importlib, importlib.util, unittest

class VisaRealAIAcceptanceHarnessTests(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('tools.visa_real_ai_acceptance'),'Acceptance runner absent')
  self.m=importlib.import_module('tools.visa_real_ai_acceptance')
 def test_default_run_defers_without_provider_lookup_or_outbound(self):
  from unittest.mock import patch
  import json
  with patch.object(self.m.Path,'mkdir'), patch.object(self.m.Path,'write_bytes') as write, patch.object(self.m,'provider_from_environment',side_effect=AssertionError('No provider lookup in current release')), patch.object(self.m,'RecordedRealProvider',side_effect=AssertionError('No live provider')), patch('builtins.print'):
   self.assertEqual(0,self.m.run('offline-release-test'))
  report=json.loads(write.call_args_list[-1].args[0])
  self.assertEqual('DEFERRED',report['status'])
  self.assertEqual(0,report['request_count']);self.assertEqual(0,report['expected_request_count'])
  self.assertEqual(11,len(report['results']))
  self.assertTrue(all(r['status']=='DEFERRED' for r in report['results']))
 def test_automated_success_never_equals_final_real_acceptance(self):
  result=self.m.acceptance_status({'status':'PASS'})
  self.assertEqual('PASS',result['automated_check_status'])
  self.assertEqual('REVIEW_PENDING',result['status'])
  self.assertEqual('FAIL',self.m.acceptance_status({'status':'FAIL'})['status'])
 def test_prepared_leak_rejected_before_counting_network_request(self):
  provider=self.m.RecordedRealProvider(api_key='synthetic-unused',model='synthetic-unused',output=self.m.BASE)
  with self.assertRaises(self.m.VisaAIError):provider._send({'text':self.m.PREPARED_MARKER},1)
  self.assertEqual(0,provider.request_count)
 def test_builtin_cases_cover_A_to_H_without_external_file_sources(self):
  cases=self.m.scenarios();self.assertEqual(set('ABCDEFGH'),{s['group'] for s in cases});self.assertEqual(11,len(cases))
  self.assertTrue(all(s['synthetic'] is True for s in cases))
  self.assertTrue(all('file_path' not in str(s) for s in cases))
 def test_wrong_grant_date_is_failure_not_accepted_by_keywords(self):
  scenario=self.m.scenarios()[0];actual=dict(scenario['expected']);actual['decision_date']='2026-09-06'
  result=self.m.evaluate(scenario,actual,{'evidence_ids':[1]})
  self.assertEqual('FAIL',result['status']);self.assertIn('decision_date',result['incorrect_fields'])
 def test_keyword_presence_alone_does_not_prove_parent_asset_ownership(self):
  scenario=next(s for s in self.m.scenarios() if s['id']=='F-father')
  result={'Summary':'Father Applicant property','Risk':'Father','Missing':'None','Action Required':'Check','full_analysis':'Father Applicant '+scenario['asset']}
  self.assertEqual('FAIL',self.m.evaluate(scenario,result,{'evidence_ids':[1]})['status'])
 def test_explicit_owner_assertion_required_and_misattribution_rejected(self):
  scenario=next(s for s in self.m.scenarios() if s['id']=='F-father');asset=scenario['asset']
  result={k:'Reviewed' for k in ('Summary','Risk','Missing','Action Required','full_analysis')};result['full_analysis']=f'Father owns {asset}.'
  self.assertEqual('PASS',self.m.evaluate(scenario,result,{'evidence_ids':[1]})['status'])
  result['full_analysis']=f'Applicant owns {asset}. Father provides support.'
  self.assertEqual('FAIL',self.m.evaluate(scenario,result,{'evidence_ids':[1]})['status'])
 def test_missing_source_and_medical_invention_are_rejected(self):
  scenario=next(s for s in self.m.scenarios() if s['group']=='C');actual=dict(scenario['expected']);actual['medical_completed_date']='2026-09-08'
  judged=self.m.evaluate(scenario,actual,{'evidence_ids':[]})
  self.assertEqual('FAIL',judged['status']);self.assertIn('evidence_ids',judged['missing_fields'])
 def test_funding_words_without_amount_and_owner_are_not_enough(self):
  scenario=next(s for s in self.m.scenarios() if s['group']=='G');result={k:'Savings Employment Income Investment' for k in ('Summary','Risk','Missing','Action Required','full_analysis')}
  self.assertEqual('FAIL',self.m.evaluate(scenario,result,{'evidence_ids':[1]})['status'])

if __name__=='__main__':unittest.main()

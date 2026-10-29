"""AI-READY inactive mode; no credentials or network required."""
import os
import unittest
from unittest.mock import patch
import tests.test_visa_ai_workspace as support
import visa_documents_candidate as documents
from visa_workspace_candidate import document_workspace_html

class VisaAIUnconfiguredTests(unittest.TestCase):
 setUp=support.VisaAIWorkspaceTests.setUp
 handler=support.VisaAIWorkspaceTests.handler
 extraction=support.VisaAIWorkspaceTests.extraction
 route=support.VisaAIWorkspaceTests.route
 def test_all_ai_requests_are_informational_without_reading_files_or_network(self):
  doc,_=self.extraction();documents.mark_submitted(self.conn,1,doc['id'],**self.actor)
  before=self.conn.total_changes
  with patch.dict(os.environ,{},clear=True),patch('socket.socket',side_effect=AssertionError('Network forbidden')):
   for method in ('GET','POST'):
    handler=self.route();handler._visa_send=lambda title,body:(200,body)
    handler._visa_text_from_document=lambda *args: self.fail('Inactive AI must not read or OCR files')
    response=handler.visa_submission_analysis(method,'1')
    self.assertEqual(200,response[0]);self.assertIn('AI-READY',response[1]);self.assertIn('disabled',response[1])
   handler=self.route();handler._visa_send=lambda title,body:(200,body)
   handler._visa_text_from_document=lambda *args:self.fail('Inactive AI must not read letters')
   response=handler.visa_case_ai_extract('1',doc['id'])
   self.assertEqual(200,response[0]);self.assertIn('AI-READY',response[1])
  self.assertEqual(before,self.conn.total_changes)
 def test_unconfigured_buttons_disabled_deterministic_workflow_remains_available(self):
  doc,_=self.extraction()
  with patch.dict(os.environ,{},clear=True),patch('socket.socket',side_effect=AssertionError('Network forbidden')):
   html=document_workspace_html(1,'csrf',[dict(doc)],[],[])
  self.assertIn('<button disabled',html);self.assertNotIn('<button>Extract Letter</button>',html)
  self.assertIn('AI-READY',html);self.assertIn('deterministic workflow',html)

if __name__=='__main__':unittest.main()

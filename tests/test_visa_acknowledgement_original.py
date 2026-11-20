"""Receipt wording seen in the copied original; no identity data or runtime imports."""
import unittest
from visa_documents_candidate import propose_letter

class OriginalAcknowledgementTests(unittest.TestCase):
    def test_received_application_is_receipt_suggestion_never_a_decision(self):
        text = '''We received your Student (subclass 500) visa application on 19 November 2026.
Application status
Student (subclass 500) visa: Received
Application summary
Date of application 19 November 2026
If you provide us with fraudulent documents or claims, this may result in processing delays
and your application being refused.
Failure to notify us of these changes can have serious consequences and even if the visa
you have applied for is granted, it may later be cancelled.'''
        value=propose_letter(text)
        self.assertEqual('lodgement_receipt',value['document_kind'])
        self.assertEqual('2026-11-19',value['official_date'])
        self.assertEqual('',value['decision_type'])
        self.assertEqual('',value['decision_date'])

    def test_generic_or_future_application_wording_is_not_a_receipt(self):
        for text in ('We will receive your visa application after payment.\nDate of application 19 November 2026',
                     'Application summary\nDate of application 19 November 2026'):
            self.assertEqual('unknown',propose_letter(text)['document_kind'])

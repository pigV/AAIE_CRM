from email.message import EmailMessage
import smtplib
import unittest
from unittest.mock import Mock, patch

from revenue.school_invoice_smtp import RehearsalSMTP


class SchoolInvoiceSMTPTests(unittest.TestCase):
    def setUp(self):
        self.transport = RehearsalSMTP("finance@example.test", "tester@example.test")
        self.message = EmailMessage()
        self.message["From"] = self.transport.sender
        self.message["To"] = self.transport.recipient
        self.message.set_content("TEST ONLY")
        self.server = Mock()
        self.server.mail.return_value = (250, b"ok")
        self.server.rcpt.return_value = (250, b"ok")
        self.server.data.return_value = (250, b"ok")

    def deliver(self, allowed=lambda: True):
        with patch.object(RehearsalSMTP,"connect",return_value=self.server) as connect:
            result = self.transport.submit(self.transport.recipient,self.message.as_bytes(),allowed)
        return result,connect

    def test_single_envelope_recipient(self):
        self.assertEqual(("sent",None,250),self.deliver()[0])
        self.server.rcpt.assert_called_once_with(self.transport.recipient)
        self.server.data.assert_called_once()

    def test_wrong_header_stops_before_connection(self):
        self.message.replace_header("To","school@example.test")
        result,connect = self.deliver()
        self.assertEqual("cancelled",result[0])
        connect.assert_not_called()

    def test_definitive_failure_and_ambiguous_failure_are_distinct(self):
        self.server.rcpt.return_value = (450,b"retry")
        self.assertEqual(("failed","smtp_rejected",450),self.deliver()[0])
        self.server.data.assert_not_called()
        self.server.rcpt.return_value = (250,b"ok")
        self.server.data.side_effect = smtplib.SMTPServerDisconnected()
        self.assertEqual(("needs_review","smtp_uncertain",None),self.deliver()[0])

    def test_kill_switch_rechecked_before_data(self):
        self.assertEqual("cancelled",self.deliver(lambda:False)[0][0])
        self.server.data.assert_not_called()

    def test_cleanup_failure_cannot_make_accepted_or_uncertain_mail_retryable(self):
        self.server.close.side_effect = OSError("cleanup")
        self.assertEqual(("sent",None,250),self.deliver()[0])
        self.server.data.side_effect = smtplib.SMTPServerDisconnected()
        self.assertEqual(("needs_review","smtp_uncertain",None),self.deliver()[0])

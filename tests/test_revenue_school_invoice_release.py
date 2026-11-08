import unittest
from email import policy
from email.parser import BytesParser

from revenue.school_invoice_release import release_values


class SchoolInvoiceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.verified = release_values({}, "verified", "finance@school.example", "FIN-100", "", (), True)

    def test_default_and_verify_do_not_authorize_send(self):
        self.assertFalse(self.verified["enabled"])
        self.assertFalse(release_values(self.verified,"off","","","",(),True)["enabled"])

    def test_first_requires_prior_verified_address_and_separate_approval(self):
        first = release_values(self.verified,"first","finance@school.example","FIN-100","APP-200",
                               ("claim-a","claim-b"),True)
        self.assertTrue(first["enabled"])
        self.assertEqual("zhang0723@gmail.com",first["delivery_recipient"])
        for current, recipient, approval in (({}, "finance@school.example","APP-200"),
                                               (self.verified,"other@school.example","APP-200"),
                                               (self.verified,"finance@school.example","")):
            with self.assertRaises(ValueError):
                release_values(current,"first",recipient,"FIN-100",approval,("claim-a",),True)

    def test_production_rejects_rehearsal_address(self):
        with self.assertRaises(ValueError):
            release_values({},"verified","zhang0723@gmail.com","FIN-100","",(),False)

    def test_real_and_rehearsal_wording_are_separate(self):
        from revenue.school_invoice_mail import _message
        event = dict(uid="invoice-test",invoice_number="COM-TEST",recipient="finance@school.example",
                     gross_cents=55000,transport="production")
        mime = BytesParser(policy=policy.default)
        official = mime.parsebytes(_message(event,"2026-11-07T00:00:00+00:00",b"pdf","finance@example.com"))
        self.assertEqual("Commission invoice COM-TEST | Toorak College",official["Subject"])
        self.assertNotIn("TEST ONLY",official.get_body().get_content())
        candidate = mime.parsebytes(_message({**event,"transport":"candidate"},
                                             "2026-11-07T00:00:00+00:00",b"pdf","finance@example.com"))
        self.assertIn("TEST ONLY",candidate["Subject"])

from pathlib import Path
import unittest

from tests.revenue_test_support import RevenueDatabaseFixture


class FormalAuthorityTests(RevenueDatabaseFixture, unittest.TestCase):
    def test_authority_schema_keeps_formal_invoice_link_unique(self):
        from revenue.authority_formal import AuthorityInvoice

        db = self.connect()
        sql = Path(__file__).resolve().parents[1] / "migrations/20261110_044_authority_commission.sql"
        db.executescript(sql.read_text())
        self.assertIn("draft_uid", [row[1] for row in db.execute("PRAGMA table_info(revenue_authority_invoices)")])
        self.assertTrue(AuthorityInvoice)


def seed_formal_authorities(repo, source):
    from revenue.authority_formal import register_authority_payer
    from revenue.invoice_templates import CommissionInvoiceTemplates, PROFILE_DEFAULT, default_layout
    from revenue.school_invoice_mail import save_mail_settings
    from revenue.services import RevenueService

    repo.connection.executescript((source/"migrations/20261110_044_authority_commission.sql").read_text())
    repo.connection.executescript("""
        ALTER TABLE students ADD COLUMN passport_first_name TEXT;
        ALTER TABLE students ADD COLUMN passport_last_name TEXT;
    """)
    repo.connection.executemany(
        "UPDATE students SET passport_first_name='TEST',passport_last_name=? WHERE student_id=?",
        [(name, student) for name, student in (
            ("Melbourne","TEST-VIC-MEL"),("Geelong","TEST-VIC-GEE"),("Sydney","TEST-NSW-SYD"),
            ("Private","TEST-VIC-PRIVATE"),("Renewal","TEST-VIC-RENEWAL"),
        )],
    )
    for state, name in (("VIC","TEST Victorian Education Authority"),("NSW","TEST NSW Education Authority")):
        result = register_authority_payer(repo,state,name,1,rehearsal=True)
        entity = result["entity_id"]
        editor = CommissionInvoiceTemplates(RevenueService(repo),1,"Admin",formal=True,simulation=True,entity_id=entity)
        profile = {**PROFILE_DEFAULT,"issuer_name":"AAIE TEST ONLY","issuer_address":"100 Example Street, Melbourne",
                   "issuer_email":"finance@aaie.example.test","issuer_website":"aaie.example.test","issuer_abn":"10000000000","account_name":"AAIE TEST",
                   "bank_name":"Example Bank","bank_address":"1 Example Street, Melbourne",
                   "bsb":"111222","account_number":"123456789","swift":"TESTAU00",
                   "bill_to_address":f"10 Example Road, {state}"}
        editor.save_profile(profile,f"authority-{state}-profile",expected_uid="",approved=True)
        template = editor.save(default_layout(),f"authority-{state}-template",name="Invoice 1762 TEST")
        editor.publish(template["uid"],"",f"authority-{state}-publish")
        save_mail_settings(repo,entity,1,f"authority-{state}-mail",f"finance@{state.lower()}.example.test",True,True,"")

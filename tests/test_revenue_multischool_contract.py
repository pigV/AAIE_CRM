from datetime import date, timedelta
from types import SimpleNamespace
import unittest

from revenue.repository import RevenueRepository
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contracts import SchoolCommissionContracts
from revenue.services import RevenueService
from tests.revenue_test_support import RevenueDatabaseFixture


class MultiSchoolContractTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect()
        self.connection.execute("ALTER TABLE schools ADD COLUMN school_name TEXT")
        self.connection.execute("ALTER TABLE schools ADD COLUMN deleted_at TEXT")
        self.connection.execute("UPDATE schools SET school_name='Synthetic School B' WHERE id=10")
        self.connection.execute("INSERT INTO schools(id,school_name) VALUES(11,'Synthetic School C')")
        service = RevenueService(RevenueRepository(self.connection))
        binder = SchoolCommissionContracts(service, 1, "Admin")
        today = date.today()
        self.start = today.isoformat()
        self.entity = binder.bind(10, "B School Legal Pty Ltd", self.start, "bind-b")
        self.contracts = SchoolCommissionContracts(
            service, 1, "Admin", formal=True, school_id=10, entity_id=self.entity["id"],
        )
        self.pdf = SimpleNamespace(
            path=self.database_path.with_name("signed.pdf"), sha256="a" * 64,
            mime_type="application/pdf", size_bytes=123,
        )
        self.inputs = {
            "sample_tuition": "12000", "effective_from": self.start,
            "effective_to": (today + timedelta(days=365)).isoformat(), "gst_mode": "none",
            "rate_percent": "8", "commencement_days": "7", "payment_days": "15",
            "registration_required": True, "clause_reference": "第 2 页第 4 条",
        }

    def draft(self):
        return self.contracts.upload_configurable_draft(10,self.pdf,"draft-b",signed_confirmed=True)

    def test_school_b_preview_activation_replay_and_toorak_isolation(self):
        draft = self.draft()
        preview = self.contracts.preview_configurable(draft["uid"],self.inputs)
        self.assertEqual((96000,0,96000),tuple(preview["rows"][0][k] for k in
                                              ("net_cents","gst_cents","total_cents")))
        self.assertEqual({
            "payment_days_from_valid_invoice":15,
            "commencement_days":7,
            "registration_required":True,
        },preview["configuration"]["claim"])
        self.assertEqual(0,self.connection.execute("SELECT count(*) FROM revenue_contract_versions").fetchone()[0])
        with self.assertRaises(ValueError):
            self.contracts.preview_toorak(draft["uid"],self.inputs)
        with self.assertRaises(ValueError):
            self.contracts.activate_configurable(draft["uid"],{**self.inputs,"rate_percent":"9"},
                                                  preview["digest"],"stale-b")
        result = self.contracts.activate_configurable(draft["uid"],self.inputs,preview["digest"],"activate-b")
        self.assertEqual(result,self.contracts.activate_configurable(draft["uid"],self.inputs,preview["digest"],"activate-b"))
        self.assertTrue(result["configuration"]["school_commission_configurable"])
        self.assertEqual(self.start,result["effective_from"])
        row = self.connection.execute("SELECT document_kind FROM revenue_documents").fetchone()
        self.assertEqual(FORMAL_CONTRACT,row["document_kind"])
        with self.assertRaises(ValueError):
            self.contracts.activate_configurable(draft["uid"],self.inputs,preview["digest"],"activate-b-new-key")

    def test_invalid_or_overlapping_rule_inputs_and_wrong_school(self):
        with self.assertRaises(ValueError):
            self.contracts.upload_configurable_draft(11,self.pdf,"wrong-school",signed_confirmed=True)
        with self.assertRaises(ValueError):
            self.contracts.upload_configurable_draft(10,self.pdf,"unsigned",signed_confirmed=False)
        draft = self.draft()
        overlap = [
            {"rate_percent":"8","tuition_kind":"any","visa_subclasses":[]},
            {"rate_percent":"10","tuition_kind":"local","visa_subclasses":["188"]},
        ]
        for change in ({"payment_days":"21"},{"commencement_days":"-1"},{"clause_reference":""},
                       {"gst_mode":""},{"registration_required":""},{"tiers":overlap}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.contracts.preview_configurable(draft["uid"],{**self.inputs,**change})
        distinct = [
            {"rate_percent":"5","tuition_kind":"local","visa_subclasses":["188"]},
            {"rate_percent":"17","tuition_kind":"international","visa_subclasses":["500"]},
        ]
        preview = self.contracts.preview_configurable(draft["uid"],{**self.inputs,"tiers":distinct,"gst_mode":"exclusive"})
        self.assertEqual([66000,224400],[row["total_cents"] for row in preview["rows"]])

    def test_overlapping_contract_dates_rejected_and_renewal_preserves_history(self):
        first = self.draft()
        preview = self.contracts.preview_configurable(first["uid"],self.inputs)
        self.contracts.activate_configurable(first["uid"],self.inputs,preview["digest"],"activate-first")
        next_pdf = SimpleNamespace(**{**vars(self.pdf),"sha256":"b" * 64,
                                      "path":self.pdf.path.with_name("renewal.pdf")})
        second = self.contracts.upload_configurable_draft(10,next_pdf,"draft-renewal",signed_confirmed=True)
        preview = self.contracts.preview_configurable(second["uid"],self.inputs)
        with self.assertRaisesRegex(ValueError,"重叠"):
            self.contracts.activate_configurable(second["uid"],self.inputs,preview["digest"],"overlap")
        next_start = date.fromisoformat(self.inputs["effective_to"])+timedelta(days=1)
        renewal = {**self.inputs,"effective_from":next_start.isoformat(),
                   "effective_to":(next_start+timedelta(days=365)).isoformat(),"rate_percent":"9"}
        preview = self.contracts.preview_configurable(second["uid"],renewal)
        self.contracts.activate_configurable(second["uid"],renewal,preview["digest"],"activate-renewal")
        contracts = self.connection.execute(
            "SELECT effective_from,effective_to,status FROM revenue_contract_versions ORDER BY id"
        ).fetchall()
        self.assertEqual([(self.start,self.inputs["effective_to"],"active"),
                          (renewal["effective_from"],renewal["effective_to"],"active")],
                         [tuple(row) for row in contracts])

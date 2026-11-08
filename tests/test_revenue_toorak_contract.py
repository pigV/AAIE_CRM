from datetime import date, timedelta
from io import BytesIO
import sqlite3
from types import SimpleNamespace
import unittest

from pypdf import PdfWriter

from revenue.ui_actions import RevenueUIActions
from tests.revenue_secure_test_support import secure_directory
from tests.revenue_test_support import RevenueDatabaseFixture


class ToorakContractTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c = self.connect()
        self.c.execute("ALTER TABLE schools ADD COLUMN school_name TEXT")
        self.c.execute("ALTER TABLE schools ADD COLUMN deleted_at TEXT")
        self.c.execute("UPDATE schools SET school_name='Toorak College' WHERE id=10")
        self.actions = RevenueUIActions(self.c, secure_directory(self), 1, "Admin")
        stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(stream)
        self.pdf = SimpleNamespace(filename="fixture.pdf", content_type="application/pdf", data=stream.getvalue())

    def call(self, action, *args):
        method = getattr(self.actions, action, None)
        self.assertTrue(callable(method), f"Missing Toorak action: {action}")
        return method(*args)

    def draft(self):
        draft = self.call("upload_toorak_draft", {"school_id": "10", "idempotency_key": "draft"}, self.pdf)
        return {"draft_uid": draft["uid"], "local_tuition": "10000",
                "international_tuition": "10000", "local_visa": "subclass_188",
                "international_visa": "full_student"}

    def test_pending_draft_previews_exactly_one_tier_per_scenario_and_blocks_activation(self):
        form = self.draft()
        preview = self.call("preview_toorak_rule", form)
        self.assertEqual([50000, 170000], [row["commission_cents"] for row in preview["rows"]])
        self.assertTrue(all(row["gst_cents"] is None and row["total_cents"] is None for row in preview["rows"]))
        self.assertFalse(preview["can_activate"])
        with self.assertRaises(ValueError):
            self.call("activate_toorak_rule", {**form, "preview_digest": preview["digest"], "idempotency_key": "activate"})
        for visa in ("full_student", "unknown", "subclass_188,full_student"):
            with self.subTest(visa=visa):
                mismatched = self.call("preview_toorak_rule", {**form, "local_visa": visa})
                self.assertIsNone(mismatched["rows"][0]["commission_cents"])
        self.assertEqual(0, self.c.execute("SELECT count(*) FROM revenue_contract_versions").fetchone()[0])

    def test_distinct_pdfs_keep_distinct_draft_source_links(self):
        first = self.draft()
        stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=200)
        writer.write(stream)
        pdf = SimpleNamespace(filename="other.pdf", content_type="application/pdf", data=stream.getvalue())
        draft = self.call("upload_toorak_draft", {"school_id": "10", "idempotency_key": "other"}, pdf)
        self.assertNotEqual(first["draft_uid"], draft["uid"])
        p1 = self.call("preview_toorak_rule", first)
        p2 = self.call("preview_toorak_rule", {**first, "draft_uid": draft["uid"]})
        self.assertNotEqual(p1["document_uid"], p2["document_uid"])

    def test_confirmed_synthetic_package_activates_atomically_and_replays(self):
        today = date.today()
        form = {**self.draft(), "effective_from": today.isoformat(),
                "effective_to": (today + timedelta(days=729)).isoformat(), "gst_mode": "none"}
        preview = self.call("preview_toorak_rule", form)
        with self.assertRaises(ValueError):
            self.call("activate_toorak_rule", {**form, "local_tuition": "11000",
                "preview_digest": preview["digest"], "idempotency_key": "changed"})
        confirm = {**form, "preview_digest": preview["digest"], "idempotency_key": "activate"}
        result = self.call("activate_toorak_rule", confirm)
        self.assertEqual(result, self.call("activate_toorak_rule", confirm))
        self.assertEqual([500, 1700], [t["rate_basis_points"] for t in result["configuration"]["tiers"]])
        self.assertEqual(1, self.c.execute("SELECT count(*) FROM revenue_contract_versions").fetchone()[0])
        self.assertEqual(0, self.c.execute("SELECT count(*) FROM revenue_commission_rate_instalments").fetchone()[0])
        self.assertEqual(0, self.c.execute("SELECT count(*) FROM revenue_commission_lines").fetchone()[0])

    def test_rule_write_failure_rolls_back_both_tiers_and_the_formal_contract(self):
        today = date.today()
        form = {**self.draft(), "effective_from": today.isoformat(),
                "effective_to": (today + timedelta(days=730)).isoformat(), "gst_mode": "exclusive"}
        preview = self.call("preview_toorak_rule", form)
        self.assertEqual([55000, 187000], [row["total_cents"] for row in preview["rows"]])
        self.c.execute("""CREATE TRIGGER fail_tier_package BEFORE INSERT ON revenue_commission_rule_versions
                          BEGIN SELECT RAISE(ABORT, 'synthetic package write failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic package write failure"):
            self.call("activate_toorak_rule", {**form, "preview_digest": preview["digest"],
                      "idempotency_key": "activate"})
        for table in ("revenue_contract_versions", "revenue_commission_rule_versions",
                      "revenue_contract_extractions", "revenue_entity_provider_links"):
            with self.subTest(table=table):
                self.assertEqual(0, self.c.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        self.assertEqual(1, self.c.execute("SELECT count(*) FROM revenue_documents").fetchone()[0])

    def test_active_formal_package_is_excluded_from_generic_selectors(self):
        from unittest.mock import patch
        from revenue.controlled_onboarding import ControlledOnboardingService
        from revenue.operational_orchestrator import RevenueOperationalOrchestrator
        from revenue.school_commission_pilot import PilotPolicy

        today = date.today()
        entity = self.actions._school_contracts().bind(10,"Toorak College",today.isoformat(),"bind")
        pilot = PilotPolicy(school_id=10,entity_id=entity["id"],admin_ids=(1,),enabled=True,stage="contracts")
        official = RevenueUIActions(self.c,self.actions.document_root,1,"Admin",school_pilot=pilot)
        draft = official.upload_toorak_draft(
            {"school_id":"10","signed_confirmed":"1","idempotency_key":"signed"},self.pdf,
        )
        form = {"draft_uid":draft["uid"],"local_tuition":"10000","international_tuition":"10000",
                "local_visa":"subclass_188","international_visa":"full_student","gst_mode":"exclusive",
                "effective_from":today.isoformat(),"effective_to":(today+timedelta(days=729)).isoformat()}
        preview = official.preview_toorak_rule(form)
        official.activate_toorak_rule({**form,"preview_digest":preview["digest"],"idempotency_key":"formal"})
        orchestrator = RevenueOperationalOrchestrator(self.c,1,"Admin")
        self.assertIsNone(orchestrator._school_entity_and_rule(10)[1])
        onboarding = ControlledOnboardingService(self.actions.repository)
        with patch.object(onboarding,"_accepted_institution",return_value=({"id":1},{"kind":"school","school_id":10})):
            proposal, _ = onboarding._commission_proposal(None,{})
        self.assertEqual("missing_institution_contract",proposal["reason"])

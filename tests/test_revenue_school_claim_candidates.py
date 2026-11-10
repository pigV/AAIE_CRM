import unittest
from unittest.mock import Mock, patch

from revenue.school_claim_candidates import SchoolClaimCandidates


class SchoolClaimCandidateTests(unittest.TestCase):
    def setUp(self):
        self.claims = Mock()
        self.claims.formal = self.claims.simulation = True
        self.claims.entity_id = 3
        self.claims.forecast.school_id = 10
        self.claims.connection.execute.return_value.fetchall.return_value = []
        self.claims.check_commencement.return_value = None
        self.claims.check_formal_payment_terms.return_value = 30
        self.claims._formal_evidence.side_effect = lambda case, student, uid, kind, label: (
            {"document_uid": uid, "sha256": "hash", "filename": label}, b"%PDF-"
        )
        self.rows = [
            {"case_uid": "case-" + key, "case_id": idx, "component_uid": key, "component_id": idx,
             "student_id": key, "student_name": key, "href": "/revenue/cases/case-" + key}
            for idx, key in enumerate(("local", "international"), 1)
        ]
        self.forecasts = {
            key: {"digest": key, "net_cents": net, "gst_cents": net // 10, "gross_cents": net + net // 10,
                  "rule": {"entity_id": 3, "rule_id": 1, "contract_uid": "v1", "version_no": 1},
                  "tier": {"rate_basis_points": rate}, "tuition": {"tuition_cents": 1000000},
                  "visa": {"subclass": visa}, "commencement": {"actual_commencement_date": "2026-11-05"}}
            for key, net, rate, visa in (("local", 50000, 500, "188"), ("international", 170000, 1700, "500"))
        }
        self.claims.forecast.preview.side_effect = lambda case, key, *args: self.forecasts[key]
        self.proofs = Mock(side_effect=lambda conn, case, student, entity, kind, simulation:
                           [{"id": student + kind}])
        model = self.enterContext(patch("revenue.school_claim_candidates.RevenueReadModels"))
        model.return_value.school_claim_components.side_effect = (
            lambda school, keys=None: [row for row in self.rows if keys is None or row["component_uid"] in keys]
        )
        self.enterContext(patch("revenue.school_claim_candidates.formal_evidence_options", self.proofs))
        self.service = SchoolClaimCandidates(self.claims)

    def test_select_one_or_both_and_confirm_without_claim(self):
        self.assertEqual(55000, self.service.review(["local"])["gross_cents"])
        self.assertEqual(187000, self.service.review(["international"])["gross_cents"])
        combined = self.service.review(["local", "international"])
        self.assertEqual(242000, combined["gross_cents"])
        confirmed = self.service.confirm(["international", "local"], combined["digest"], reviewed=True)
        self.assertEqual(combined, confirmed)
        self.claims.issue_claim.assert_not_called()
        self.claims.forecast.save.assert_not_called()

    def test_missing_evidence_and_existing_claim_keep_amount_but_disable_selection(self):
        self.proofs.side_effect = lambda conn, case, student, entity, kind, simulation: (
            [] if student == "international" and kind == "ToorakSchoolTuitionPaid" else [{"id": kind}]
        )
        self.claims.connection.execute.return_value.fetchall.return_value = [
            {"uid": "old", "status": "claimed", "claimed": 1, "revenue_entity_id": 3,
             "canonical_json": "{}", "gross_cents": 55000}
        ]
        # Only the local component has the prior Claim.
        self.claims.connection.execute.side_effect = lambda sql, args: (
            Mock(fetchall=lambda: [{"uid": "old", "status": "claimed", "claimed": 1, "revenue_entity_id": 3,
                                   "canonical_json": "{}", "gross_cents": 55000}])
            if args[0] == 1 else Mock(fetchall=lambda: [])
        )
        local, overseas = self.service.list()
        self.assertIn("已申领", " ".join(local["reasons"]))
        self.assertIn("学校课程费实收证据", " ".join(overseas["reasons"]))
        self.assertEqual(187000, overseas["forecast"]["gross_cents"])
        for key in ("local", "international"):
            with self.assertRaises(ValueError):
                self.service.review([key])

    def test_stale_or_invalid_selection_is_rejected(self):
        combined = self.service.review(["local", "international"])
        self.forecasts["international"] = {**self.forecasts["international"], "digest": "new", "gross_cents": 200000}
        with self.assertRaisesRegex(ValueError, "（international）.*重新复核"):
            self.service.confirm(["local", "international"], combined["digest"], reviewed=True,
                                 row_digests=combined["row_digests"])
        for ids in ([], ["local", "local"], ["wrong-school"]):
            with self.assertRaises(ValueError):
                self.service.review(ids)
        with self.assertRaisesRegex(ValueError, "请确认"):
            self.service.confirm(["local"], "", reviewed=False)

    def test_generate_uses_confirmed_components_and_existing_claim_service(self):
        reviewed = self.service.review(["local", "international"])
        self.claims.repository.command.side_effect = lambda **kwargs: kwargs["operation"]()
        self.claims.forecast.save.side_effect = lambda case, key, *_: {"uid": "line-" + key}
        self.claims.preview.side_effect = lambda case, *args: {"digest": "proof-" + case}
        self.claims.issue_claim.side_effect = lambda case, *args: {
            "uid": case, "claim_number": case, "gross_cents": 1,
            "net_cents": 1, "gst_cents": 0,
        }
        self.claims.actor = 21
        with self.assertRaisesRegex(ValueError, "请确认"):
            self.service.generate(["local", "international"], reviewed["digest"], reviewed=False)
        generated = self.service.generate(["international", "local"], reviewed["digest"], reviewed=True)
        self.assertEqual(2, len(generated["claims"]))
        self.assertEqual(2, self.claims.issue_claim.call_count)
        self.assertEqual(2, self.claims.forecast.save.call_count)
        self.assertEqual({"agent_document_id", "enrolment_document_id", "payment_document_id"},
                         set(self.claims.issue_claim.call_args.args[2]))

    def test_invoice_gst_options_use_tuition_rates_and_bind_confirmation(self):
        for forecast in self.forecasts.values():
            forecast["gst_mode"] = "exclusive"
        for mode, amounts in (
            ("exclusive", (220000, 22000, 242000)),
            ("inclusive", (200000, 20000, 220000)),
            ("none", (220000, 0, 220000)),
        ):
            with self.subTest(mode=mode):
                result = self.service.review(["local", "international"], gst_mode=mode)
                self.assertEqual(amounts, tuple(result[k] for k in ("net_cents", "gst_cents", "gross_cents")))
                self.assertEqual(mode, result["gst_mode"])
        default = self.service.review(["local"], gst_mode="")
        self.assertEqual("exclusive", default["gst_mode"])
        with self.assertRaisesRegex(ValueError, "重新复核"):
            self.service.confirm(["local"], default["digest"], reviewed=True, gst_mode="inclusive")

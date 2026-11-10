import unittest

from revenue.institution_commission_draft import extract_draft_terms
from revenue.school_contract_pages import render_contract_upload


PAGES = [
    "Date of Agreement 26 June 2025\nCompetion Date 30 June 2028",
    "23. GST\nAll prices are exclusive of GST.",
    """Attachment 2
Commission Schedule
(a) a standard commission rate of 12.5% of the tuition fee paid by the student in the student's first year of tuition
(b) an additional commission of 5% of the student's tuition fees for the intensive English Language Program. Two school terms.
2 Terms and Conditions of Standard Commission Payment and English Bonus
(a) The Agreement must be in place at the initial offer. Visa subclass 500. No outstanding fees.
(c) No English Bonus for a course less than 12 months.
(d) Standard Commission pro-rata below 12 months.
3 Payment terms
automated disbursement process on a quarterly basis, after studying for four weeks or more.""",
    "Attachment 2\n3.2 GST\nThe supplier will receive a RCTI with the Commission payment.",
]


class VicRctiTests(unittest.TestCase):
    def test_actual_contract_shape_is_additive_and_not_invoice_based(self):
        profile, fields = extract_draft_terms(PAGES, "VIC", authority="VIC")
        self.assertEqual("vic_rcti", profile)
        self.assertEqual(["12.5", "5"], [fields[key]["value"] for key in ("standard", "english_bonus")])
        self.assertEqual(["2025-06-26", "2028-06-30"], [fields[key]["value"] for key in ("start", "end")])
        self.assertEqual("exclusive", fields["gst"]["value"])
        self.assertEqual(3, fields["english_bonus"]["sources"][0]["page"])
        self.assertEqual("unsupported", extract_draft_terms(PAGES, "NSW", authority="NSW")[0])

    def test_intake_drop_box_has_no_upload_confirmation_checkbox(self):
        html = render_contract_upload([], [], "csrf", "token", authorities=[("VIC", "维州教育局")])
        self.assertIn('data-school-contract-drop', html)
        self.assertNotIn('name="signed_confirmed"', html)

    def test_estimates_use_separate_paid_tuition_and_apply_eligibility(self):
        from revenue.vic_rcti import estimate
        terms = {"start": "2025-06-26", "end": "2028-06-30", "gst": "exclusive",
                 "rates": {"standard": 1250, "english_bonus": 500}}
        facts = {"offer_on": "2026-01-01", "duration": "full", "studying": True,
                 "paid_tuition_cents": 1_000_000, "english_paid_cents": 200_000}
        output = estimate(terms, facts, "2026-02-01", "2026-03-01")
        self.assertEqual((125_000, 10_000, 13_500, 148_500), tuple(output[k] for k in (
            "standard_cents", "english_bonus_cents", "gst_cents", "gross_cents")))
        no_bonus = estimate(terms, {**facts, "english_paid_cents": 0}, "2026-02-01", "2026-03-01")
        self.assertEqual(137_500, no_bonus["gross_cents"])
        short = estimate(terms, {**facts, "duration": "short"}, "2026-02-01", "2026-03-01")
        self.assertIn("按比例待复核", short["reason"])
        self.assertNotIn("gross_cents", short)
        for changes, end, reason in (({}, "2026-02-28", "尚未满四周"),
                                     ({"offer_on": "2025-06-25"}, "2026-03-01", "适用期"),
                                     ({"studying": False}, "2026-03-01", "实际就读")):
            self.assertIn(reason, estimate(terms, {**facts, **changes}, "2026-02-01", end)["reason"])

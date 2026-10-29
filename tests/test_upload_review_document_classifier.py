import unittest


class UploadReviewDocumentClassifierTests(unittest.TestCase):
    def classifier(self):
        try:
            from upload_review_document_classifier import classify_offer_filename
        except ModuleNotFoundError as exc:
            self.fail(f"Offer filename classifier module is missing: {exc}")
        return classify_offer_filename

    def test_classifies_offer_filename_variants(self):
        classify = self.classifier()
        cases = {
            "62531 LINGRUI SHE - FFP Letter of Offer_Signed.pdf": "SignedOffer",
            "Signed Offer.pdf": "SignedOffer",
            "signed-offer.pdf": "SignedOffer",
            "offer_signed.pdf": "SignedOffer",
            "SIGNED.OFFER.pdf": "SignedOffer",
            "SignedOffer.pdf": "SignedOffer",
            "OfferSigned.pdf": "SignedOffer",
            "executed-offer.pdf": "SignedOffer",
            "Acceptance Offer.pdf": "OfferAcceptance",
            "Offer Acceptance.pdf": "OfferAcceptance",
            "Acceptance_of_Offer.pdf": "OfferAcceptance",
            "OfferAcceptance.pdf": "OfferAcceptance",
            "Letter of Offer.pdf": "Offer",
            "Offer Letter.pdf": "Offer",
            "OfferLetter_66752.pdf": "Offer",
            "document.pdf": "",
            "": "",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(classify(filename), expected)

    def test_signed_offer_wins_over_acceptance(self):
        classify = self.classifier()
        self.assertEqual(classify("Parent Signed Offer Acceptance.pdf"), "SignedOffer")
        self.assertEqual(classify("acceptance_offer_signed.pdf"), "SignedOffer")

    def test_unsigned_is_not_a_signed_substring_match(self):
        classify = self.classifier()
        self.assertEqual(classify("Unsigned Offer.pdf"), "Offer")

    def test_none_is_safe(self):
        classify = self.classifier()
        self.assertEqual(classify(None), "")


if __name__ == "__main__":
    unittest.main()

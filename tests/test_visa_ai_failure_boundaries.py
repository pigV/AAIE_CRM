"""Injected network faults, explicitly not real-model acceptance."""
import io
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from visa_ai_candidate import VisaAIProvider, VisaAIError, _NoRedirect
from tests import test_visa_ai_workspace as workspace_support


class VisaAITransportFailureTests(unittest.TestCase):
    def test_default_transport_redacts_http_errors_and_timeout(self):
        pin = {"document_id": 7, "version": 1, "sha256": "a" * 64}
        faults = [(HTTPError(VisaAIProvider.ENDPOINT, 429, "secret-body", {}, io.BytesIO(b"secret-body")), "AI_HTTP_429"),
                  (TimeoutError("secret-body"), "AI_TIMEOUT"),
                  (URLError("secret-body"), "AI_TRANSPORT_ERROR")]
        for fault, expected in faults:
            with self.subTest(expected=expected):
                provider = VisaAIProvider(api_key="synthetic-only-credential", model="test-model")
                with patch("visa_ai_candidate.request.build_opener") as opener:
                    opener.return_value.open.side_effect = fault
                    with self.assertRaises(VisaAIError) as caught:
                        provider.extract_letter("Synthetic Visa grant notice", pin)
                self.assertEqual(expected, str(caught.exception))
                self.assertNotIn("secret-body", str(caught.exception))
                self.assertEqual("openai_https", provider._transport_kind)

    def test_redirect_handler_rejects_external_destination(self):
        with self.assertRaisesRegex(VisaAIError, "AI_REDIRECT_REJECTED"):
            _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://external.invalid")


class VisaAIWorkspaceFailureTests(unittest.TestCase):
    setUp = workspace_support.VisaAIWorkspaceTests.setUp
    handler = workspace_support.VisaAIWorkspaceTests.handler
    extraction = workspace_support.VisaAIWorkspaceTests.extraction
    route = workspace_support.VisaAIWorkspaceTests.route

    def test_timeout_does_not_persist_proposal_or_decision(self):
        doc, _ = self.extraction()
        handler = self.route()
        handler.form = {"action": "ai-extract", "document_id": str(doc["id"])}
        def timeout(*args):
            raise TimeoutError("synthetic fault only")
        provider = VisaAIProvider(api_key="synthetic-only-credential", model="test-model", transport=timeout)
        tables = ["visa_letter_extractions", "visa_letter_reviews", "visa_document_audit", "visas", "visa_decision_events"]
        before = {name: self.conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in tables}
        with patch("visa_ai_candidate.provider_from_environment", return_value=provider):
            handler.visa_case_workspace("POST", "1")
        self.assertGreaterEqual(handler.response[0], 400)
        self.assertEqual(before, {name: self.conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in tables})
        self.assertEqual("Pending", self.db.one("SELECT result_status FROM visa_cases WHERE id=1")[0])


if __name__ == "__main__":
    unittest.main()

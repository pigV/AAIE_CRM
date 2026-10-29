import unittest

from content_extraction.billing import FREE_ONLY, FreeOnlyBillingGate


class FreeOnlyBillingGateTests(unittest.TestCase):
    def test_free_only_is_default_and_local_provider_is_always_allowed(self):
        gate = FreeOnlyBillingGate.from_environ({})

        self.assertEqual(gate.mode, FREE_ONLY)
        self.assertTrue(gate.allows("paddle", "local"))
        self.assertTrue(gate.allows("easyocr", "local"))
        self.assertEqual(gate.metadata("paddle", "local"), {
            "billing_mode": "FREE_ONLY",
            "billing_type": "local",
            "cost": 0,
            "allowed": True,
        })

    def test_cloud_provider_is_skipped_when_free_status_is_missing_false_or_unknown(self):
        for value in (None, "", "false", "unknown", "expired", "0"):
            with self.subTest(value=value):
                environ = {} if value is None else {"TENCENT_OCR_FREE_ONLY_ALLOWED": value}
                gate = FreeOnlyBillingGate.from_environ(environ)
                self.assertFalse(gate.allows("tencent", "cloud"))

    def test_cloud_provider_requires_explicit_free_only_confirmation(self):
        gate = FreeOnlyBillingGate.from_environ({
            "TENCENT_OCR_FREE_ONLY_ALLOWED": "true",
            "IFLYTEK_OCR_FREE_ONLY_ALLOWED": "1",
        })

        self.assertTrue(gate.allows("tencent", "cloud"))
        self.assertTrue(gate.allows("iflytek", "cloud"))

    def test_non_free_only_mode_never_enables_cloud_provider(self):
        gate = FreeOnlyBillingGate.from_environ({
            "BILLING_MODE": "PAID",
            "TENCENT_OCR_FREE_ONLY_ALLOWED": "true",
        })

        self.assertFalse(gate.allows("tencent", "cloud"))
        self.assertTrue(gate.allows("paddle", "local"))


if __name__ == "__main__":
    unittest.main()

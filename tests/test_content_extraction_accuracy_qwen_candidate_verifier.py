import unittest

from tests.test_content_extraction_accuracy_spark_arbiter import item


class QwenCandidateVerifierTests(unittest.TestCase):
    def verifier(self, content, captured=None):
        from content_extraction.accuracy.qwen_candidate_verifier import QwenCandidateVerifier

        def request(payload):
            if captured is not None:
                captured.update(payload)
            return {"choices": [{"message": {"content": content}}]}

        return QwenCandidateVerifier(
            "http://127.0.0.1:18091", "sha256:qwen3-8b", request_json=request,
        )

    def test_exact_a_b_x_are_valid_and_only_b_selects_proposal(self):
        for raw, choice, selected in (
            ("A", "A", "下马杯"),
            (" B ", "B", "下马威"),
            ("X", "X", ""),
        ):
            with self.subTest(raw=raw):
                result = self.verifier(raw).verify(item(), "下马威")
                self.assertTrue(result.choice_valid)
                self.assertEqual(result.selected_option, choice)
                self.assertEqual(result.selected_text, selected)

    def test_invalid_output_fails_closed_without_generated_text(self):
        for raw in ("答案是B", "b", "```B```", '{"answer":"B"}', "AB"):
            with self.subTest(raw=raw):
                result = self.verifier(raw).verify(item(), "下马威")
                self.assertFalse(result.choice_valid)
                self.assertEqual(result.status, "invalid_response")
                self.assertEqual(result.selected_text, "")

    def test_prompt_is_bounded_selection_only_and_reasoning_is_disabled(self):
        captured = {}
        verifier = self.verifier("B", captured)
        result = verifier.verify(item(), "下马威")

        self.assertEqual(result.selected_text, "下马威")
        self.assertEqual(captured["model"], "Qwen3-8B-GGUF-Q4_K_M")
        self.assertEqual(captured["temperature"], 0)
        self.assertLessEqual(captured["max_tokens"], 4)
        prompt = captured["messages"][0]["content"]
        self.assertIn("A = 下马杯", prompt)
        self.assertIn("B = 下马威", prompt)
        self.assertIn("X = 无法确定", prompt)
        self.assertIn("不得生成候选", prompt)

    def test_endpoint_must_be_exact_loopback_origin(self):
        from content_extraction.accuracy.qwen_candidate_verifier import QwenCandidateVerifier

        for endpoint in (
            "https://127.0.0.1:18091", "http://localhost:18091",
            "http://127.0.0.1:18091/path", "http://127.0.0.1.example:18091",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "qwen_verifier_endpoint_must_be_loopback"):
                    QwenCandidateVerifier(endpoint, "sha256:qwen3-8b")


if __name__ == "__main__":
    unittest.main()

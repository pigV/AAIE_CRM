import unittest

from tests.test_content_extraction_accuracy_spark_arbiter import item


class CandidateProposalFlowTests(unittest.TestCase):
    def qwen(self, option, valid=True):
        from content_extraction.accuracy.qwen_candidate_verifier import QwenVerificationResult

        selected = "下马威" if option == "B" and valid else (
            "下马杯" if option == "A" and valid else ""
        )
        return QwenVerificationResult(
            "completed" if valid else "invalid_response",
            option if valid else "", selected, valid, option, 1,
        )

    def test_qwen_b_projects_local_patch_and_preserves_prefix_suffix(self):
        from content_extraction.accuracy.candidate_proposal_flow import (
            evaluate_verified_proposal,
        )

        decision = evaluate_verified_proposal(item(), "下马威", self.qwen("B"))

        self.assertEqual(decision.status, "AUTO_RESOLVED")
        self.assertEqual(decision.final_text, "高中立马来个下马威开始拼选课")
        self.assertEqual(len(decision.guard_decisions), 4)
        self.assertTrue(all(value.status == "PASS" for value in decision.guard_decisions))

    def test_qwen_a_x_invalid_or_disagreement_keep_review_without_guards(self):
        from content_extraction.accuracy.candidate_proposal_flow import (
            evaluate_verified_proposal,
        )

        for qwen in (
            self.qwen("A"), self.qwen("X"), self.qwen("B", valid=False),
            self.qwen("B"),
        ):
            proposal = "另一个候选" if qwen.selected_option == "B" and qwen.choice_valid else "下马威"
            with self.subTest(option=qwen.selected_option, proposal=proposal):
                decision = evaluate_verified_proposal(item(), proposal, qwen)
                self.assertEqual(decision.status, "KEEP_REVIEW")
                self.assertEqual(decision.final_text, item().carrier_text)
                self.assertEqual(decision.guard_decisions, ())

    def test_unsafe_large_rewrite_never_falls_back_to_whole_sentence(self):
        from content_extraction.accuracy.candidate_proposal_flow import (
            evaluate_verified_proposal,
        )
        from content_extraction.accuracy.qwen_candidate_verifier import QwenVerificationResult

        proposal = "完全不同且没有共同锚点的一句话"
        qwen = QwenVerificationResult("completed", "B", proposal, True, "B", 1)
        decision = evaluate_verified_proposal(item(), proposal, qwen)
        self.assertEqual(decision.status, "KEEP_REVIEW")
        self.assertIn("projection_unsafe", decision.reasons)


if __name__ == "__main__":
    unittest.main()

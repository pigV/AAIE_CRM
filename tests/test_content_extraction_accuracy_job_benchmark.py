import unittest


class JobBenchmarkTests(unittest.TestCase):
    def test_frozen_cross_modal_payload_runs_without_provider_execution_or_truth_claim(self):
        from content_extraction.accuracy.job_benchmark import run_frozen_job_correction

        payload = {
            "clean_raw_transcript": "原文正确", "duration_ms": 1000,
            "fusion_status": "completed", "confidence_level": "HIGH",
            "source_identity": {"source_video_id": "job_18", "timebase": "milliseconds"},
            "segments": [{
                "id": "cm-1", "start_ms": 0, "end_ms": 1000,
                "clean_text": "原文正确", "ocr_text": "", "asr_text": "原文正确",
                "selected_source": "asr", "included_in_clean_transcript": True,
                "text_role": "spoken_speech", "content_relation": "modality_only",
                "alternatives": ["原文正确"], "conflicts": [], "critical_tokens": [],
                "ocr_evidence": None, "asr_evidence": {"strength": "STRONG"},
                "ocr_provenance": {}, "asr_provenance": {"provider_texts": {
                    "funasr": "原文正确", "whispercpp": "原文正确"}},
                "selection_reason": "asr", "fusion_status": "completed",
                "confidence_level": "HIGH", "provisional_selection": False,
            }],
        }
        output = run_frozen_job_correction(payload, proposal_engine=None)
        self.assertEqual(output["source_video_id"], "job_18")
        self.assertEqual(output["final_transcript"], "原文正确")
        self.assertFalse(output["is_ground_truth"])
        self.assertFalse(output["provider_execution_performed"])
        self.assertEqual(output["model_suitability"], "UNVERIFIED_BY_HUMAN_GROUND_TRUTH")


if __name__ == "__main__":
    unittest.main()

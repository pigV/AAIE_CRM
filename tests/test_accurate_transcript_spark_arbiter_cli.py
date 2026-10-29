from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from content_extraction.accuracy.review_queue import (
    ImmutableReviewQueueStore,
    ReviewAlternative,
    ReviewQueue,
    ReviewRequiredItem,
)
from tests.test_content_extraction_accuracy_spark_lite import (
    RecordingTransport,
    valid_environ,
)


def _review_item():
    carrier = "沒有觀世界哪來的世界觀"
    return ReviewRequiredItem(
        review_id="jobjob-19-cm-0001",
        segment_id="cm-0001",
        start_ms=1000,
        end_ms=2400,
        context_before="上文",
        carrier_text=carrier,
        context_after="下文",
        suspect_span=carrier,
        start_offset=0,
        end_offset=len(carrier),
        alternatives=(
            ReviewAlternative("沒有觀世界哪來的世界觀", ("whispercpp",)),
            ReviewAlternative("没有观世界，哪来的世界观", ("funasr",)),
        ),
        critical_token_type=None,
        ocr_evidence={},
        asr_evidence={
            "whispercpp": "沒有觀世界哪來的世界觀",
            "funasr": "没有观世界，哪来的世界观",
        },
        qwen_proposal=None,
        reason_for_review=("provider_text_disagreement",),
        risk_level="P2",
        audio_clip_path="",
        video_clip_path="",
    )


def _response(content="B"):
    return {
        "code": 0,
        "message": "Success",
        "sid": "cha000b0003@dx1905cd86d6bb86d552",
        "model": "lite",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 1, "total_tokens": 41},
    }


class SparkArbiterCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.temporary.name) / "job_19.json"
        queue = ReviewQueue.create("job_19", 100, (_review_item(),), 0)
        ImmutableReviewQueueStore(self.queue_path).create(queue)

    def tearDown(self):
        self.temporary.cleanup()

    def test_inspect_item_is_read_only_and_reports_real_candidate_protocol(self):
        from scripts.accurate_transcript_spark_arbiter import inspect_item

        before = self.queue_path.read_bytes()
        result = inspect_item(self.queue_path, "jobjob-19-cm-0001")
        self.assertEqual(result["review_id"], "jobjob-19-cm-0001")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual([value["option"] for value in result["candidates"]], ["A", "B"])
        self.assertEqual(result["precheck_category"], "NON_SUBSTANTIVE_VARIANT")
        self.assertEqual(result["arbiter_candidates"], [])
        self.assertFalse(result["eligible_for_one_item_smoke"])
        self.assertEqual(self.queue_path.read_bytes(), before)

    def test_non_substantive_item_smoke_is_prechecked_without_transport_call(self):
        from scripts.accurate_transcript_spark_arbiter import run_smoke

        transport = RecordingTransport(_response("B"))
        result = run_smoke(
            self.queue_path,
            "jobjob-19-cm-0001",
            valid_environ(),
            transport=transport,
        )
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(result["arbitration"]["request_count"], 0)
        self.assertFalse(result["arbitration"]["cache_hit"])
        self.assertEqual(result["arbitration"]["error_code"], "NON_SUBSTANTIVE_VARIANT")
        self.assertEqual(result["decision"], "AUTO_RESOLVED")

    def test_fail_closed_gate_makes_zero_requests(self):
        from scripts.accurate_transcript_spark_arbiter import run_smoke

        carrier = "高中立马来个下马杯开始拼选课"
        lexical = replace(
            _review_item(),
            review_id="lexical-review",
            segment_id="lexical-segment",
            carrier_text=carrier,
            suspect_span="下马杯",
            start_offset=6,
            end_offset=9,
            alternatives=(
                ReviewAlternative(carrier, ("funasr",)),
                ReviewAlternative("高中立马来个下马威开始拼选课", ("whispercpp",)),
            ),
            asr_evidence={
                "funasr": carrier,
                "whispercpp": "高中立马来个下马威开始拼选课",
            },
        )
        lexical_path = Path(self.temporary.name) / "lexical.json"
        ImmutableReviewQueueStore(lexical_path).create(
            ReviewQueue.create("lexical", 100, (lexical,), 0),
        )
        transport = RecordingTransport(_response("B"))
        environ = valid_environ(IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED="false")
        result = run_smoke(
            lexical_path,
            "lexical-review",
            environ,
            transport=transport,
        )
        self.assertEqual(transport.calls, [])
        self.assertEqual(result["arbitration"]["request_count"], 0)
        self.assertEqual(result["decision"], "KEEP_REVIEW")


if __name__ == "__main__":
    unittest.main()

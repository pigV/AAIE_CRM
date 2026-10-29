from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from content_extraction.accuracy.external.arbiter_schemas import SparkArbitrationResult
from content_extraction.accuracy.external.schemas import SparkLiteConfig, SparkUsage
from content_extraction.accuracy.review_queue import (
    ReviewAlternative,
    ReviewQueue,
    ReviewRequiredItem,
)


def _item(review_id, carrier, suspect, alternatives, *, start=0, critical=None):
    return ReviewRequiredItem(
        review_id=review_id,
        segment_id=review_id,
        start_ms=1000,
        end_ms=2200,
        context_before="前文",
        carrier_text=carrier,
        context_after="后文",
        suspect_span=suspect,
        start_offset=start,
        end_offset=start + len(suspect),
        alternatives=tuple(
            ReviewAlternative(text, (source,)) for text, source in alternatives
        ),
        critical_token_type=critical,
        ocr_evidence={},
        asr_evidence={source: text for text, source in alternatives},
        qwen_proposal=None,
        reason_for_review=("provider_text_disagreement",),
        risk_level="P0" if critical else "P2",
        audio_clip_path="",
        video_clip_path="",
    )


def _lexical(review_id="eligible-1", selected="B"):
    carrier = "高中来个下马杯开始选课"
    item = _item(
        review_id,
        carrier,
        "下马杯",
        ((carrier, "funasr"), ("高中来个下马威开始选课", "whispercpp")),
        start=4,
    )
    return item, selected


def _non_substantive():
    carrier = "沒有世界哪來世界觀"
    return _item(
        "non-substantive",
        carrier,
        carrier,
        ((carrier, "whispercpp"), ("没有世界，哪来世界观", "funasr")),
    )


def _no_alternative():
    carrier = "只有一个候选"
    return _item(
        "no-alternative",
        carrier,
        carrier,
        ((carrier, "funasr"),),
    )


def _result(choice="B", *, status="completed", error_code="", request_count=1):
    return SparkArbitrationResult(
        status=status,
        selected_option=choice if status == "completed" else "",
        selected_text="下马威" if choice == "B" and status == "completed" else (
            "下马杯" if choice == "A" and status == "completed" else ""
        ),
        allowed_options=("A", "B", "X"),
        request_id="masked...sid",
        usage=SparkUsage(20, 1, 21),
        free_only_status=(
            "CONFIRMED_BY_OPERATOR" if status == "completed" else "FAIL_CLOSED"
        ),
        error_code=error_code,
        request_count=request_count,
        estimated_cost=0,
        http_status=200,
        outer_code=0,
        outer_message="Success",
        response_model="lite",
        raw_choice_preview=choice,
        normalized_choice=choice,
        choice_valid=status == "completed" and choice in {"A", "B", "X"},
    )


class FakeArbiter:
    def __init__(self, results, gate=None):
        self.results = list(results)
        self.calls = []
        self.config = gate or SparkLiteConfig(
            api_password="private", billing_mode="FREE_ONLY",
            free_only_attestation="true",
        )

    def arbitrate(self, review, candidates, sample_id=""):
        self.calls.append((review.review_id, tuple(value.option for value in candidates), sample_id))
        return self.results.pop(0)


class ControlledBulkJobTests(unittest.TestCase):
    def test_only_eligible_items_call_spark_and_workload_removes_local_and_guard_resolutions(self):
        from content_extraction.accuracy.spark_arbiter_bulk import evaluate_controlled_job

        lexical, _choice = _lexical()
        queue = ReviewQueue.create(
            "job_19", 100,
            (_non_substantive(), _no_alternative(), lexical), 0,
        )
        arbiter = FakeArbiter((_result("B"),))
        result = evaluate_controlled_job(queue, arbiter, sleep_fn=lambda _value: None)

        self.assertEqual([value[0] for value in arbiter.calls], ["eligible-1"])
        self.assertEqual(result["spark_eligible"], 1)
        self.assertEqual(result["spark_requests"], 1)
        self.assertEqual(result["result_categories"]["SPARK_RESOLVED_SAFE"], 1)
        self.assertEqual(result["after_non_substantive"]["review_items"], 2)
        self.assertEqual(result["after_spark"]["review_items"], 1)
        self.assertEqual(result["after_spark"]["review_characters"], len("只有一个候选"))
        self.assertEqual(result["after_spark"]["review_ratio"], len("只有一个候选") / 100)

    def test_x_and_selected_carrier_remain_review_without_false_guard_rejection(self):
        from content_extraction.accuracy.spark_arbiter_bulk import evaluate_controlled_job

        first, _ = _lexical("x-item")
        second, _ = _lexical("carrier-item")
        queue = ReviewQueue.create("job_19", 100, (first, second), 0)
        arbiter = FakeArbiter((_result("X"), _result("A")))
        result = evaluate_controlled_job(queue, arbiter, sleep_fn=lambda _value: None)

        self.assertEqual(result["result_categories"]["SPARK_UNCERTAIN_X"], 1)
        self.assertEqual(
            result["result_categories"]["SPARK_SELECTED_BUT_GUARD_REJECTED"], 1,
        )
        self.assertEqual(result["selected_carrier_retained"], 1)
        self.assertEqual(result["guards_rejected"], 0)
        self.assertEqual(result["auto_resolved"], 0)
        self.assertEqual(result["after_spark"]["review_items"], 2)

    def test_provider_failure_stops_current_job_without_calling_later_items(self):
        from content_extraction.accuracy.spark_arbiter_bulk import evaluate_controlled_job

        first, _ = _lexical("first")
        second, _ = _lexical("second")
        arbiter = FakeArbiter((
            _result("", status="failed", error_code="SPARK_AUTHORIZATION_FAILED"),
            _result("B"),
        ))
        result = evaluate_controlled_job(
            ReviewQueue.create("job_19", 100, (first, second), 0),
            arbiter,
            sleep_fn=lambda _value: None,
        )

        self.assertEqual([value[0] for value in arbiter.calls], ["first"])
        self.assertFalse(result["job_complete"])
        self.assertEqual(result["stop_reason"], "SPARK_AUTHORIZATION_FAILED")
        self.assertEqual(result["result_categories"]["SPARK_REQUEST_ERROR"], 1)

    def test_two_consecutive_invalid_responses_stop_without_third_request(self):
        from content_extraction.accuracy.spark_arbiter_bulk import evaluate_controlled_job

        items = tuple(_lexical(f"item-{index}")[0] for index in range(3))
        invalid = _result(
            "invalid", status="invalid_response",
            error_code="INVALID_ARBITRATION_RESPONSE",
        )
        arbiter = FakeArbiter((invalid, invalid, _result("B")))
        result = evaluate_controlled_job(
            ReviewQueue.create("job_19", 100, items, 0),
            arbiter,
            sleep_fn=lambda _value: None,
        )

        self.assertEqual(len(arbiter.calls), 2)
        self.assertFalse(result["job_complete"])
        self.assertEqual(result["stop_reason"], "CONSECUTIVE_INVALID_RESPONSES")
        self.assertEqual(result["result_categories"]["SPARK_INVALID_RESPONSE"], 2)

    def test_free_only_gate_is_rechecked_before_every_request(self):
        from content_extraction.accuracy.spark_arbiter_bulk import evaluate_controlled_job

        first, _ = _lexical("first")
        second, _ = _lexical("second")
        arbiter = FakeArbiter((_result("B"), _result("B")))
        checks = []

        def preflight():
            checks.append(True)
            return len(checks) == 1

        result = evaluate_controlled_job(
            ReviewQueue.create("job_19", 100, (first, second), 0),
            arbiter,
            sleep_fn=lambda _value: None,
            preflight_fn=preflight,
        )
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(arbiter.calls), 1)
        self.assertEqual(result["stop_reason"], "FREE_ONLY_GATE_FAILED")


class ControlledBulkCheckpointTests(unittest.TestCase):
    def test_checkpoint_is_private_atomic_and_resume_uses_no_adapter(self):
        from content_extraction.accuracy.spark_arbiter_bulk import (
            load_completed_checkpoint,
            run_controlled_bulk,
        )

        lexical, _ = _lexical()
        queue = ReviewQueue.create("job_19", 100, (lexical,), 0)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkpoint = root / "checkpoints" / "job_19.json"
            first_arbiter = FakeArbiter((_result("B"),))
            first = run_controlled_bulk(
                (("job_19", queue, "file-sha", checkpoint),),
                arbiter_factory=lambda _queue_hash: first_arbiter,
                sleep_fn=lambda _value: None,
            )
            self.assertEqual(first["actual_request_count"], 1)
            self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)
            loaded = load_completed_checkpoint(checkpoint, queue, "file-sha")
            self.assertEqual(loaded["source_queue_hash"], queue.queue_hash)

            second_arbiter = FakeArbiter(())
            second = run_controlled_bulk(
                (("job_19", queue, "file-sha", checkpoint),),
                arbiter_factory=lambda _queue_hash: second_arbiter,
                sleep_fn=lambda _value: None,
            )
            self.assertEqual(second["actual_request_count"], 0)
            self.assertEqual(second["checkpoint_accumulated_requests"], 1)
            self.assertEqual(second_arbiter.calls, [])

    def test_checkpoint_queue_identity_mismatch_fails_closed(self):
        from content_extraction.accuracy.spark_arbiter_bulk import (
            atomic_write_private_json,
            load_completed_checkpoint,
        )

        lexical, _ = _lexical()
        queue = ReviewQueue.create("job_19", 100, (lexical,), 0)
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "job_19.json"
            atomic_write_private_json(checkpoint, {
                "job_complete": True,
                "source_queue_hash": "wrong",
                "source_queue_version": queue.queue_version,
                "source_queue_file_sha256": "file-sha",
            })
            with self.assertRaisesRegex(ValueError, "checkpoint_queue_identity_mismatch"):
                load_completed_checkpoint(checkpoint, queue, "file-sha")

    def test_checkpoint_refuses_to_overwrite_source_queue(self):
        from content_extraction.accuracy.spark_arbiter_bulk import atomic_write_private_json

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "queue.json"
            source.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checkpoint_cannot_overwrite_source_queue"):
                atomic_write_private_json(source, {"value": 1}, forbidden_paths=(source,))
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), {})

    def test_generator_job_input_is_counted_before_iteration(self):
        from content_extraction.accuracy.spark_arbiter_bulk import run_controlled_bulk

        lexical, _ = _lexical()
        queue = ReviewQueue.create("job_19", 100, (lexical,), 0)
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "job_19.json"
            jobs = ((value for value in (("job_19", queue, "sha", checkpoint),)))
            result = run_controlled_bulk(
                jobs,
                arbiter_factory=lambda _queue_hash: FakeArbiter((_result("B"),)),
                sleep_fn=lambda _value: None,
            )
            self.assertTrue(result["bulk_complete"])


if __name__ == "__main__":
    unittest.main()

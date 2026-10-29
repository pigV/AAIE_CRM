import io
import unittest
from types import SimpleNamespace

from content_extraction.ocr.evidence_gate import OCRFrameEvidenceDecision
from content_extraction.quality_diagnostics import (
    build_provider_accounting,
    classify_ocr_conflict,
    summarize_conflict_diagnoses,
)
from scripts.content_extraction_real_benchmark import (
    default_benchmark_local_adapters,
    logged_process_factory,
)


class BenchmarkHarnessTests(unittest.TestCase):
    def test_default_benchmark_local_adapters_are_paddle_and_apple_vision(self):
        adapters = default_benchmark_local_adapters(object(), object())
        self.assertEqual(
            [adapter.provider for adapter in adapters],
            ["paddle", "apple_vision"],
        )

    def test_easyocr_diagnostics_are_not_left_on_an_unread_pipe(self):
        captured = {}
        log = io.StringIO()

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return "process"

        factory = logged_process_factory(log, popen=fake_popen)
        result = factory(["easyocr-worker"], stderr="unread-pipe", text=True)

        self.assertEqual(result, "process")
        self.assertIs(captured["kwargs"]["stderr"], log)
        self.assertTrue(captured["kwargs"]["text"])

    @staticmethod
    def _result(provider, text, box):
        return SimpleNamespace(
            provider=provider, status="completed", text=text,
            bounding_boxes=(box,) if box else (),
        )

    def _frame(self, first, second, first_box=None, second_box=None):
        default = {"x": 10, "y": 10, "width": 200, "height": 40}
        return SimpleNamespace(
            frame_id="f1", fused_text=first, start_ms=0, end_ms=1000,
            provider_results=(
                self._result("paddle", first, first_box or default),
                self._result("easyocr", second, second_box or default),
            ),
        )

    def test_conflict_root_causes_follow_approved_mutually_exclusive_order(self):
        accepted = OCRFrameEvidenceDecision(True, "spoken_subtitle", "", 1, 0.1, True)
        rejected = OCRFrameEvidenceDecision(False, "unknown", "no_ocr_text", 0, 0, True)
        ui = OCRFrameEvidenceDecision(False, "ui", "role_ui", 1, 0.1, True)
        cases = (
            ("E", self._frame("LIKE 99", "LIKE 98"), ui, None),
            ("F", self._frame(
                "孩子上课", "孩子上课了",
                {"x": 0, "y": 0, "width": 80, "height": 30},
                {"x": 300, "y": 200, "width": 80, "height": 30},
            ), accepted, None),
            ("B", self._frame("孩子上课", "孩子上课了"), accepted, None),
            ("D", self._frame("学习GPA", "学习GPS"), accepted, None),
            ("G", self._frame("孩子正在上课", "孩子正在上课"), accepted,
             SimpleNamespace(fused_text="孩子正在", start_ms=-1000, end_ms=0)),
            ("C", self._frame("孩子考砸了", "孩子考杂了"), accepted, None),
            ("A", self._frame("", ""), rejected, None),
            ("H", self._frame("same", "same"), accepted, None),
        )

        actual = tuple(
            classify_ocr_conflict(frame, evidence, previous).category
            for _want, frame, evidence, previous in cases
        )

        self.assertEqual(actual, tuple(want for want, *_rest in cases))

    def test_conflict_summary_separates_input_selection_from_true_recognition(self):
        diagnoses = tuple(
            SimpleNamespace(category=value) for value in "EEFGCBADH"
        )

        summary = summarize_conflict_diagnoses(diagnoses)

        self.assertEqual(summary["categories"]["E"]["count"], 2)
        self.assertEqual(summary["INPUT_SELECTION_ERROR"]["count"], 4)
        self.assertEqual(summary["TRUE_OCR_RECOGNITION_DIFFERENCE"]["count"], 4)
        self.assertEqual(summary["unclassified"]["count"], 1)
        self.assertEqual(summary["total"], 9)

    def test_provider_accounting_keeps_current_checkpoint_and_diagnostic_scopes(self):
        accounting = build_provider_accounting(
            current={"paddle": 0, "easyocr": 0, "funasr": 1},
            checkpoint={"paddle": 263, "easyocr": 263},
            diagnostic={
                "calls": {"paddle": 503, "easyocr": 503, "funasr": 2},
                "exact": False,
                "note": "local OCR lower bound",
            },
        )

        self.assertEqual(accounting["current_run_delta"]["paddle"], 0)
        self.assertEqual(accounting["checkpoint_accumulated"]["paddle"], 263)
        self.assertEqual(
            accounting["diagnostic_rerun_total"]["calls"]["paddle"], 503
        )
        self.assertFalse(accounting["diagnostic_rerun_total"]["exact"])


if __name__ == "__main__":
    unittest.main()

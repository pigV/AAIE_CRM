import json
from pathlib import Path
import tempfile
import unittest

from tests.test_content_extraction_accuracy_review_queue import review_sample


class AccurateTranscriptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.clip_root = self.root / "clips"
        self.clip_root.mkdir()

    def create_queue(self, job_id=18, samples=None, total=100):
        from content_extraction.accuracy.review_queue import (
            ImmutableReviewQueueStore,
            build_review_queue,
        )
        queue = build_review_queue(
            f"job_{job_id}", samples if samples is not None else [review_sample()], total,
        )
        store = ImmutableReviewQueueStore(self.root / "queues" / f"job_{job_id}.json")
        store.create(queue)
        return queue

    def create_carriers(self, job_id, segments):
        path = self.root / "carriers" / f"job_{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": "accurate-transcript-carriers-v1",
            "crm_job_id": job_id,
            "segments": [
                {"segment_id": segment_id, "text": text}
                for segment_id, text in segments
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def coordinator(self):
        from content_extraction.accuracy.integration import AccurateTranscriptCoordinator
        return AccurateTranscriptCoordinator(self.root, clip_root=self.clip_root)

    def test_no_review_queue_with_carriers_is_auto_complete(self):
        self.create_carriers(18, (("s1", "第一句"), ("s2", "第二句")))

        result = self.coordinator().status_for(18)

        self.assertEqual(result["mode"], "LATEST")
        self.assertEqual(result["status"], "TRANSCRIPT_READY")
        self.assertEqual(result["readiness"], "CLEAN")
        self.assertEqual(result["final_accurate_transcript"], "第一句\n第二句")
        self.assertTrue(result["final_available"])

    def test_pending_and_large_review_publish_complete_carrier_with_uncertainties(self):
        queue = self.create_queue(18)
        self.create_carriers(18, ((queue.items[0].segment_id, queue.items[0].carrier_text),))
        pending = self.coordinator().status_for(18)
        self.assertEqual(pending["status"], "TRANSCRIPT_READY")
        self.assertEqual(pending["readiness"], "WITH_UNCERTAINTIES")
        self.assertEqual(pending["final_accurate_transcript"], "学生签证是500类别")
        self.assertTrue(pending["final_available"])
        self.assertEqual(pending["uncertainty_metadata"][0]["current_text"], "500")
        self.assertEqual(pending["uncertainty_metadata"][0]["candidate_texts"], ["500", "590"])
        self.assertEqual(pending["uncertainty_metadata"][0]["start_offset"], 5)
        self.assertEqual(pending["uncertainty_metadata"][0]["end_offset"], 8)

        samples = [review_sample(
            sample_id=f"review-{index}",
            segment_id=f"segment-{index}",
            before_text=f"签证{500 + index}类别",
            critical_tokens=[{"token_type": "VISA_CLASS", "value": str(500 + index)}],
            alternatives=[str(500 + index), str(590 + index)],
        ) for index in range(16)]
        self.create_queue(19, samples=samples, total=1000)
        self.create_carriers(19, tuple(
            (f"segment-{index}", f"签证{500 + index}类别") for index in range(16)
        ))
        large = self.coordinator().status_for(19)
        self.assertEqual(large["status"], "TRANSCRIPT_READY")
        self.assertEqual(large["readiness"], "WITH_UNCERTAINTIES")
        self.assertEqual(large["review_items"], 16)
        self.assertTrue(large["final_accurate_transcript"])
        self.assertEqual(large["historical_limited_review_gate"], "FAIL")

    def test_exact_workload_boundary_remains_limited(self):
        samples = [review_sample(
            sample_id=f"review-{index}", segment_id=f"segment-{index}",
            before_text="数字0", alternatives=["0", "零"],
            critical_tokens=[{"token_type": "NUMBER", "value": "0"}],
        ) for index in range(15)]
        queue = self.create_queue(20, samples=samples, total=150)
        self.create_carriers(20, tuple(
            (item.segment_id, item.carrier_text) for item in queue.items
        ))

        result = self.coordinator().status_for(20)

        self.assertEqual(result["review_items"], 15)
        self.assertEqual(result["review_ratio"], 0.10)
        self.assertEqual(result["status"], "TRANSCRIPT_READY")
        self.assertEqual(result["readiness"], "WITH_UNCERTAINTIES")

    def test_review_projection_has_clip_ids_and_no_local_paths(self):
        queue = self.create_queue(18)
        clip_name = Path(queue.items[0].video_clip_path).name
        (self.clip_root / clip_name).write_bytes(b"video")

        projection = self.coordinator().review_for(18)
        encoded = json.dumps(projection, ensure_ascii=False)

        self.assertEqual(projection["queue_hash"], queue.queue_hash)
        self.assertEqual(projection["revision"], 0)
        self.assertEqual(projection["items"][0]["clip_id"], clip_name)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("video_clip_path", encoded)
        self.assertNotIn("audio_clip_path", encoded)

    def test_review_projection_exposes_only_local_candidate_spans(self):
        sample = review_sample(
            before_text="高中来个下马杯开始选课",
            alternatives=("高中来个下马杯开始选课", "高中来个下马威开始选课"),
            provider_texts={
                "funasr": "高中来个下马杯开始选课",
                "whispercpp": "高中来个下马威开始选课",
            },
        )
        self.create_queue(21, samples=(sample,), total=12)

        projection = self.coordinator().review_for(21)

        self.assertEqual(
            [item["text"] for item in projection["items"][0]["alternatives"]],
            ["威"],
        )
        self.assertEqual(projection["items"][0]["suspect_span"], "杯")

    def test_confirmed_human_patch_produces_completed_final_text(self):
        queue = self.create_queue(18)
        item = queue.items[0]
        self.create_carriers(18, ((item.segment_id, item.carrier_text), ("s2", "下一句")))

        result = self.coordinator().submit_review(
            18, item.review_id, queue.queue_hash, 0, "CONFIRMED", "590",
            ("alternative:590",), "crm_quick_review", "2026-08-10T00:00:00Z",
        )

        self.assertEqual(result["status"], "TRANSCRIPT_READY")
        self.assertEqual(result["readiness"], "CLEAN")
        self.assertEqual(result["final_accurate_transcript"], "学生签证是590类别\n下一句")
        self.assertEqual(result["audit"][0]["resolution_source"], "HUMAN_REVIEW")
        self.assertFalse(result["audit"][0]["ground_truth"])

    def test_ambiguous_p0_keeps_original_and_marks_uncertain_but_corruption_fails_closed(self):
        queue = self.create_queue(18)
        item = queue.items[0]
        self.create_carriers(18, ((item.segment_id, item.carrier_text),))
        ambiguous = self.coordinator().submit_review(
            18, item.review_id, queue.queue_hash, 0, "AMBIGUOUS", "", (),
            "crm_quick_review", "2026-08-10T00:00:00Z",
        )
        self.assertEqual(ambiguous["status"], "TRANSCRIPT_READY")
        self.assertEqual(ambiguous["readiness"], "WITH_UNCERTAINTIES")
        self.assertEqual(ambiguous["final_accurate_transcript"], item.carrier_text)
        self.assertEqual(ambiguous["uncertainty_metadata"][0]["current_text"], item.suspect_span)

        corrupt_path = self.root / "resolutions" / "job_18.json"
        corrupt_path.write_text("{broken", encoding="utf-8")
        corrupt = self.coordinator().status_for(18)
        self.assertEqual(corrupt["status"], "ACCURATE_TRANSCRIPT_INCOMPLETE")
        self.assertEqual(corrupt["error_code"], "CORRUPT_RESOLUTION")

    def test_guard_rejected_human_patch_keeps_original_and_marks_uncertain(self):
        queue = self.create_queue(18)
        item = queue.items[0]
        self.create_carriers(18, ((item.segment_id, item.carrier_text),))

        result = self.coordinator().submit_review(
            18, item.review_id, queue.queue_hash, 0, "EDITED",
            "这是一个会覆盖完整句子的过长替换内容", (), "crm_quick_review",
            "2026-08-10T00:00:00Z",
        )

        self.assertEqual(result["status"], "TRANSCRIPT_READY")
        self.assertEqual(result["readiness"], "WITH_UNCERTAINTIES")
        self.assertEqual(result["final_accurate_transcript"], item.carrier_text)
        self.assertEqual(result["unresolved"][0]["status"], "GUARD_REJECTED")

    def test_media_is_allowlisted_by_queue_and_bounded(self):
        queue = self.create_queue(18)
        clip_name = Path(queue.items[0].video_clip_path).name
        clip = self.clip_root / clip_name
        clip.write_bytes(b"video-bytes")

        media = self.coordinator().media_for(18, clip_name)

        self.assertEqual(media.content_type, "video/mp4")
        self.assertEqual(media.body, b"video-bytes")
        with self.assertRaisesRegex(KeyError, "MEDIA_NOT_FOUND"):
            self.coordinator().media_for(18, "other.m4v")


if __name__ == "__main__":
    unittest.main()

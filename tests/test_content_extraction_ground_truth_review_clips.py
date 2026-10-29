from pathlib import Path
import tempfile
import unittest

from tests.test_content_extraction_ground_truth_review_models import sample_item


class GroundTruthReviewClipTests(unittest.TestCase):
    def test_command_adds_context_and_clamps_to_video_duration(self):
        from content_extraction.accuracy.review.clips import build_clip_command

        item = sample_item()
        item["source"].update({
            "media_path": "/private/video.mp4", "duration_ms": 3000,
            "start_ms": 1000, "end_ms": 2500,
        })
        command = build_clip_command(item, Path("/private/output.m4v"))
        self.assertEqual(command[0], "/usr/bin/avconvert")
        self.assertEqual(command[command.index("--start") + 1], "0.000")
        self.assertEqual(command[command.index("--duration") + 1], "3.000")
        self.assertIn("Preset640x480", command)
        self.assertNotIn("--disableMetadataFilter", command)

    def test_command_uses_exact_middle_window_when_context_fits(self):
        from content_extraction.accuracy.review.clips import build_clip_command

        item = sample_item()
        item["source"].update({
            "media_path": "/private/video.mp4", "duration_ms": 10000,
            "start_ms": 3000, "end_ms": 5000,
        })
        command = build_clip_command(item, Path("/private/output.m4v"))
        self.assertEqual(command[command.index("--start") + 1], "1.500")
        self.assertEqual(command[command.index("--duration") + 1], "5.000")

    def test_generate_uses_deterministic_names_and_returns_safe_clip_urls(self):
        from content_extraction.accuracy.review.clips import generate_review_clips

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"video")
            item = sample_item("safe-sample_1")
            item["source"].update({
                "media_path": str(media), "duration_ms": 3000,
                "start_ms": 1000, "end_ms": 2000,
            })
            calls = []
            def runner(command, **kwargs):
                calls.append((command, kwargs))
                Path(command[command.index("--output") + 1]).write_bytes(b"clip")
            updated, clip_map = generate_review_clips({"samples": [item]}, root / "clips", runner=runner)
            self.assertEqual(updated["samples"][0]["clip_url"], "/media/safe-sample_1.m4v")
            self.assertEqual(clip_map["safe-sample_1.m4v"], (root / "clips" / "safe-sample_1.m4v").resolve())
            self.assertEqual(len(calls), 1)

    def test_unsafe_sample_id_and_invalid_timing_fail_closed(self):
        from content_extraction.accuracy.review.clips import build_clip_command

        item = sample_item("../escape")
        item["source"].update({"media_path": "/x", "duration_ms": 3000})
        with self.assertRaisesRegex(ValueError, "unsafe_sample_id"):
            build_clip_command(item, Path("/tmp/out.m4v"))
        item = sample_item()
        item["source"].update({
            "media_path": "/x", "duration_ms": 3000,
            "start_ms": 2500, "end_ms": 1000,
        })
        with self.assertRaisesRegex(ValueError, "invalid_clip_timing"):
            build_clip_command(item, Path("/tmp/out.m4v"))


if __name__ == "__main__":
    unittest.main()

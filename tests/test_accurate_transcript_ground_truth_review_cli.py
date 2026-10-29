from pathlib import Path
import tempfile
import unittest


class GroundTruthReviewCLITests(unittest.TestCase):
    def test_parse_legacy_media_rejects_duplicates_and_resolves_paths(self):
        from scripts.accurate_transcript_ground_truth_review import parse_legacy_media

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.write_bytes(b"video")
            self.assertEqual(parse_legacy_media([f"5={path}"]), {5: path.resolve()})
            with self.assertRaisesRegex(ValueError, "duplicate_legacy_job_id"):
                parse_legacy_media([f"5={path}", f"5={path}"])

    def test_build_clip_map_only_accepts_expected_dataset_urls(self):
        from scripts.accurate_transcript_ground_truth_review import build_clip_map

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clip = root / "sample-1.m4v"
            clip.write_bytes(b"clip")
            dataset = {"samples": [{"clip_url": "/media/sample-1.m4v"}]}
            self.assertEqual(build_clip_map(dataset, root), {"sample-1.m4v": clip.resolve()})
            dataset["samples"][0]["clip_url"] = "/media/../secret.m4v"
            with self.assertRaisesRegex(ValueError, "unsafe_clip_url"):
                build_clip_map(dataset, root)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from content_extraction.preprocessing.ffmpeg_frames import (
    FFmpegFrameError,
    FFmpegFrameExtractor,
    SamplingProfile,
)
from content_extraction.preprocessing.opencv_subtitles import (
    CandidateEvidence,
    CropRegion,
    OpenCVSubtitleDetector,
    PreprocessingProfile,
)
from content_extraction.preprocessing.subtitle_segments import (
    SubtitleObservation,
    build_subtitle_segments,
    classify_subtitle_segments,
)


class FakeCompletedProcess:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


class FFmpegFrameExtractorTests(unittest.TestCase):
    def test_requires_an_explicit_sampling_profile_and_maps_non_integer_timestamps(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            output_pattern = Path(command[-1])
            for number in range(1, 4):
                Path(str(output_pattern).replace("%06d", f"{number:06d}")).write_bytes(b"jpg")
            return FakeCompletedProcess()

        with tempfile.TemporaryDirectory() as folder:
            extractor = FFmpegFrameExtractor("/vendor/ffmpeg", runner=runner)
            profile = SamplingProfile("fast-subtitles", fps=2.5, max_frames=20, timeout_seconds=9)
            frames = extractor.extract("/media/video.mp4", Path(folder), profile)

        self.assertEqual([frame.timestamp_ms for frame in frames], [0, 400, 800])
        command, kwargs = calls[0]
        self.assertIn("fps=2.5", command)
        self.assertIn("20", command)
        self.assertEqual(kwargs["timeout"], 9)

    def test_supports_two_and_three_fps_without_a_permanent_default(self):
        self.assertEqual(SamplingProfile("normal", 2, 100, 10).interval_ms, 500)
        self.assertEqual(SamplingProfile("fast", 3, 100, 10).interval_ms, 333)
        with self.assertRaises(TypeError):
            SamplingProfile()

    def test_reports_timeout_nonzero_exit_and_bounded_frame_overflow(self):
        with tempfile.TemporaryDirectory() as folder:
            timeout_runner = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError())
            extractor = FFmpegFrameExtractor("ffmpeg", runner=timeout_runner)
            with self.assertRaisesRegex(FFmpegFrameError, "FRAME_EXTRACTION_TIMEOUT"):
                extractor.extract("video.mp4", Path(folder), SamplingProfile("test", 2, 2, 1))

        with tempfile.TemporaryDirectory() as folder:
            extractor = FFmpegFrameExtractor(
                "ffmpeg", runner=lambda *_args, **_kwargs: FakeCompletedProcess(1, b"private path")
            )
            with self.assertRaisesRegex(FFmpegFrameError, "FRAME_EXTRACTION_FAILED"):
                extractor.extract("video.mp4", Path(folder), SamplingProfile("test", 2, 2, 1))

        def overflow_runner(command, **_kwargs):
            pattern = Path(command[-1])
            for number in range(1, 4):
                Path(str(pattern).replace("%06d", f"{number:06d}")).write_bytes(b"jpg")
            return FakeCompletedProcess()

        with tempfile.TemporaryDirectory() as folder:
            extractor = FFmpegFrameExtractor("ffmpeg", runner=overflow_runner)
            with self.assertRaisesRegex(FFmpegFrameError, "FRAME_LIMIT_EXCEEDED"):
                extractor.extract("video.mp4", Path(folder), SamplingProfile("test", 2, 2, 1))


class OpenCVSubtitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import cv2
            import numpy
        except ImportError as exc:
            raise unittest.SkipTest(f"OpenCV fixture test unavailable: {exc}")
        cls.cv2 = cv2
        cls.np = numpy

    def _write_frame(self, path, text=None, y=190, inverse=False):
        background = 255 if inverse else 20
        foreground = 0 if inverse else 255
        image = self.np.full((240, 420, 3), background, dtype=self.np.uint8)
        if text:
            self.cv2.putText(
                image, text, (30, y), self.cv2.FONT_HERSHEY_SIMPLEX,
                0.9, (foreground, foreground, foreground), 2, self.cv2.LINE_AA,
            )
        self.cv2.imwrite(str(path), image)

    def _write_large_frame(self, path, lines=(), document_lines=(), ui=()):
        image = self.np.full((1280, 720, 3), 35, dtype=self.np.uint8)
        for index, text in enumerate(document_lines):
            self.cv2.putText(
                image, text, (70, 160 + index * 34), self.cv2.FONT_HERSHEY_SIMPLEX,
                0.42, (235, 235, 235), 1, self.cv2.LINE_AA,
            )
        for text, x, y in ui:
            self.cv2.putText(
                image, text, (x, y), self.cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (245, 245, 245), 1, self.cv2.LINE_AA,
            )
        for text, y in lines:
            size = self.cv2.getTextSize(
                text, self.cv2.FONT_HERSHEY_SIMPLEX, 1.35, 4
            )[0]
            x = max(12, (720 - size[0]) // 2)
            self.cv2.putText(
                image, text, (x, y), self.cv2.FONT_HERSHEY_SIMPLEX,
                1.35, (255, 255, 255), 4, self.cv2.LINE_AA,
            )
        self.cv2.imwrite(str(path), image)

    def test_detects_black_white_white_black_and_position_change_but_skips_blank(self):
        profile = PreprocessingProfile("opencv-subtitle-v1", minimum_score=0.02)
        detector = OpenCVSubtitleDetector(profile)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            dark = folder / "dark.jpg"
            light = folder / "light.jpg"
            upper = folder / "upper.jpg"
            blank = folder / "blank.jpg"
            self._write_frame(dark, "VISA 590", inverse=False)
            self._write_frame(light, "School 2026", inverse=True)
            self._write_frame(upper, "AUD 10,000", y=95, inverse=False)
            self._write_frame(blank)

            dark_crop = detector.detect(dark, folder / "dark-crop.jpg")
            light_crop = detector.detect(light, folder / "light-crop.jpg")
            upper_crop = detector.detect(upper, folder / "upper-crop.jpg")
            no_crop = detector.detect(blank, folder / "blank-crop.jpg")

        self.assertTrue(dark_crop.image_hash)
        self.assertTrue(light_crop.image_hash)
        self.assertLess(upper_crop.region.top, dark_crop.region.top)
        self.assertIsNone(no_crop)

    def test_explicit_crop_override_is_validated_and_exact_images_have_same_hash(self):
        profile = PreprocessingProfile("opencv-subtitle-v1", minimum_score=0.02)
        detector = OpenCVSubtitleDetector(profile)
        region = CropRegion(0.0, 0.5, 1.0, 1.0)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            frame = folder / "frame.jpg"
            self._write_frame(frame, "Same subtitle")
            first = detector.detect(frame, folder / "crop-1.jpg", region_override=region)
            second = detector.detect(frame, folder / "crop-2.jpg", region_override=region)
            self.assertEqual(first.image_hash, second.image_hash)
            self.assertEqual(first.region, region)
            with self.assertRaises(ValueError):
                detector.detect(frame, folder / "invalid.jpg", CropRegion(0.8, 0.1, 0.2, 0.9))

    def test_localizes_compact_and_double_line_subtitles_to_tight_relative_bands(self):
        detector = OpenCVSubtitleDetector(
            PreprocessingProfile("opencv-subtitle-v2", minimum_score=0.02)
        )
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            compact = folder / "compact.jpg"
            double = folder / "double.jpg"
            self._write_large_frame(compact, lines=(("VISA 590", 650),))
            self._write_large_frame(
                double, lines=(("STUDY IN CANADA", 520), ("VISA 590", 590))
            )

            compact_result = detector.inspect(compact, folder / "compact-crop.jpg")
            double_result = detector.inspect(double, folder / "double-crop.jpg")

        self.assertTrue(compact_result.evidence.accepted)
        self.assertLess(compact_result.evidence.region.height_ratio, 0.18)
        self.assertEqual(compact_result.evidence.line_band_count, 1)
        self.assertTrue(double_result.evidence.accepted)
        self.assertLessEqual(double_result.evidence.line_band_count, 2)
        self.assertLess(double_result.evidence.region.height_ratio, 0.22)

    def test_rejects_dense_document_and_fragmented_ui_but_keeps_large_document_overlay(self):
        detector = OpenCVSubtitleDetector(
            PreprocessingProfile("opencv-subtitle-v2", minimum_score=0.02)
        )
        document = tuple(f"EAL progress report line {index} 2018" for index in range(20))
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            dense = folder / "dense.jpg"
            overlay = folder / "overlay.jpg"
            ui = folder / "ui.jpg"
            self._write_large_frame(dense, document_lines=document)
            self._write_large_frame(
                overlay, document_lines=document, lines=(("ESL CLASS", 720),)
            )
            self._write_large_frame(
                ui, ui=(("user123", 12, 100), ("99", 645, 760), ("LIKE", 620, 850))
            )

            dense_result = detector.inspect(dense, folder / "dense-crop.jpg")
            overlay_result = detector.inspect(overlay, folder / "overlay-crop.jpg")
            ui_result = detector.inspect(ui, folder / "ui-crop.jpg")

        self.assertFalse(dense_result.evidence.accepted)
        self.assertEqual(dense_result.evidence.classification, "screen_document")
        self.assertFalse(ui_result.evidence.accepted)
        self.assertEqual(ui_result.evidence.classification, "ui")
        self.assertTrue(overlay_result.evidence.accepted)
        self.assertLess(overlay_result.evidence.region.height_ratio, 0.18)

    def test_relative_localization_allows_subtitle_position_movement(self):
        detector = OpenCVSubtitleDetector(
            PreprocessingProfile("opencv-subtitle-v2", minimum_score=0.02)
        )
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            upper = folder / "upper.jpg"
            lower = folder / "lower.jpg"
            self._write_large_frame(upper, lines=(("UPPER SUBTITLE", 390),))
            self._write_large_frame(lower, lines=(("LOWER SUBTITLE", 900),))
            first = detector.inspect(upper, folder / "upper-crop.jpg")
            second = detector.inspect(lower, folder / "lower-crop.jpg")

        self.assertTrue(first.evidence.accepted)
        self.assertTrue(second.evidence.accepted)
        self.assertLess(first.evidence.region.top, second.evidence.region.top)


class SubtitleSegmentBuilderTests(unittest.TestCase):
    def test_groups_exact_and_near_duplicates_and_sets_timeline(self):
        observations = (
            SubtitleObservation(0, "/crop/a1.jpg", "hash-a", 0xFFFF0000FFFF0000, 0.7),
            SubtitleObservation(500, "/crop/a2.jpg", "hash-a2", 0xFFFF0000FFFF0001, 0.9),
            SubtitleObservation(1000, "/crop/b.jpg", "hash-b", 0x0000FFFF0000FFFF, 0.8),
            SubtitleObservation(1500, "/crop/b2.jpg", "hash-b", 0x0000FFFF0000FFFF, 0.6),
        )
        segments = build_subtitle_segments(
            "run-1", observations, duration_ms=2000,
            preprocessing_version="opencv-v1", similarity_threshold=0.85,
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual((segments[0].start_ms, segments[0].end_ms), (0, 1000))
        self.assertEqual(segments[0].representative_path, "/crop/a2.jpg")
        self.assertEqual((segments[1].start_ms, segments[1].end_ms), (1000, 2000))

    def test_no_subtitles_returns_no_segments(self):
        self.assertEqual(
            build_subtitle_segments("run-1", (), 3000, "opencv-v1"),
            (),
        )

    @staticmethod
    def _evidence(top=0.4, height=0.08, classification="candidate"):
        return CandidateEvidence.for_test(
            top=top, height=height, classification=classification
        )

    def test_temporal_roles_reject_document_and_fixed_overlay(self):
        observations = (
            SubtitleObservation(0, "/crop/doc.jpg", "doc", 1, 0.8),
            SubtitleObservation(1000, "/crop/overlay.jpg", "overlay", 2, 0.8),
        )
        segments = build_subtitle_segments(
            "run-roles", observations, 16000, "opencv-v2", similarity_threshold=0.99
        )
        evidence = {
            "/crop/doc.jpg": self._evidence(classification="screen_document"),
            "/crop/overlay.jpg": self._evidence(),
        }

        roles = classify_subtitle_segments(segments, evidence)

        self.assertEqual(roles[segments[0].id], "screen_document")
        self.assertEqual(roles[segments[1].id], "fixed_overlay")

    def test_progressive_replacement_and_position_movement_remain_spoken_subtitles(self):
        observations = (
            SubtitleObservation(0, "/crop/a.jpg", "a", 1, 0.8),
            SubtitleObservation(1000, "/crop/ab.jpg", "ab", 2, 0.8),
            SubtitleObservation(2000, "/crop/b.jpg", "b", 3, 0.8),
        )
        segments = build_subtitle_segments(
            "run-progressive", observations, 3000, "opencv-v2", similarity_threshold=0.99
        )
        evidence = {
            "/crop/a.jpg": self._evidence(top=0.32),
            "/crop/ab.jpg": self._evidence(top=0.33, height=0.12),
            "/crop/b.jpg": self._evidence(top=0.62),
        }

        roles = classify_subtitle_segments(segments, evidence)

        self.assertEqual(tuple(roles.values()), (
            "spoken_subtitle", "spoken_subtitle", "spoken_subtitle"
        ))


if __name__ == "__main__":
    unittest.main()

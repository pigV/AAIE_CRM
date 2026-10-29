import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from content_extraction.preprocessing.audio import (
    AudioNormalizationError,
    AudioNormalizationStage,
)


def write_wav(path, *, rate=16000, channels=1, width=2, frames=1600):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"\x00" * frames * channels * width)


class AudioNormalizationStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"original-video")

    def test_ffmpeg_creates_one_shared_mono_pcm16_asset_without_touching_source(self):
        commands = []

        def runner(command, **kwargs):
            commands.append((command, kwargs))
            write_wav(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        stage = AudioNormalizationStage(
            "/vendor/ffmpeg", self.root / "runtime", runner=runner
        )
        result = stage.run(self.source, crm_job_id=42)

        self.assertEqual((result.sample_rate, result.channels, result.sample_width), (16000, 1, 2))
        self.assertEqual(result.duration_ms, 100)
        self.assertEqual(self.source.read_bytes(), b"original-video")
        self.assertTrue(Path(result.path).is_file())
        command = commands[0][0]
        self.assertEqual(command[0], "/vendor/ffmpeg")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(command[command.index("-f") + 1], "wav")

    def test_invalid_ffmpeg_output_fails_without_publishing_wav(self):
        def runner(command, **_kwargs):
            Path(command[-1]).write_bytes(b"not-wave")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        stage = AudioNormalizationStage(
            "/vendor/ffmpeg", self.root / "runtime", runner=runner
        )

        with self.assertRaises(AudioNormalizationError):
            stage.run(self.source, crm_job_id=42)

        self.assertFalse((self.root / "runtime/jobs/job_42/audio/normalized.wav").exists())

    def test_ffmpeg_failure_is_sanitized(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, b"", b"/private/secret failed")

        stage = AudioNormalizationStage(
            "/vendor/ffmpeg", self.root / "runtime", runner=runner
        )

        with self.assertRaisesRegex(AudioNormalizationError, "audio_normalization_failed") as caught:
            stage.run(self.source, crm_job_id=42)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

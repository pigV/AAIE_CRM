import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from content_extraction.asr.whispercpp import (
    WhisperCppAdapter,
    WhisperCppProcessOutput,
    WhisperCppProcessRunner,
    WhisperCppRuntimeConfig,
)
from content_extraction.models import ASRResult


def write_wav(path, *, frames=1600, rate=16000, channels=1, sample_width=2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(rate)
        handle.writeframes(b"\0" * frames * channels * sample_width)


def json_full_payload(*, text="孩子在高二学习EAL，GPA很重要。", tokens=True):
    segment = {
        "timestamps": {"from": "00:00:00,120", "to": "00:00:02,800"},
        "offsets": {"from": 120, "to": 2800},
        "text": text,
        "tokens": [],
    }
    if tokens:
        segment["tokens"] = [
            {
                "text": "[_BEG_]",
                "timestamps": {"from": "00:00:00,000", "to": "00:00:00,000"},
                "offsets": {"from": 0, "to": 0},
                "id": 50365,
                "p": 0.92,
            },
            {
                "text": " EAL",
                "timestamps": {"from": "00:00:00,600", "to": "00:00:01,100"},
                "offsets": {"from": 600, "to": 1100},
                "id": 470,
                "p": 0.81,
            },
            {
                "text": " GPA",
                "timestamps": {"from": "00:00:01,300", "to": "00:00:01,800"},
                "offsets": {"from": 1300, "to": 1800},
                "id": 471,
                "p": 0.76,
            },
            {
                "text": " invalid",
                "timestamps": {"from": "00:00:02,000", "to": "00:00:01,900"},
                "offsets": {"from": 2000, "to": 1900},
                "id": 472,
                "p": 0.4,
            },
        ]
    return {
        "systeminfo": "AVX = 0 | NEON = 1 | METAL = 1 | ACCELERATE = 1",
        "model": {"type": "medium", "multilingual": True},
        "params": {"model": "ggml-medium-q5_0.bin", "language": "zh"},
        "result": {"language": "zh"},
        "transcription": [segment],
    }


class FakeRunner:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = []

    def run(self, config, audio_path):
        self.calls.append((config, Path(audio_path)))
        if self.error:
            raise self.error
        return self.output


class WhisperCppAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.binary = root / "whisper-cli"
        self.model = root / "ggml-medium-q5_0.bin"
        self.audio = root / "sample.wav"
        self.binary.write_bytes(b"binary")
        self.binary.chmod(0o755)
        self.model.write_bytes(b"model")
        write_wav(self.audio)
        self.config = WhisperCppRuntimeConfig(
            binary_path=str(self.binary),
            model_path=str(self.model),
            state_dir=str(root / "state"),
            timeout_seconds=2,
        )

    def tearDown(self):
        self.folder.cleanup()

    def output(self, *, payload=None, stderr=""):
        return WhisperCppProcessOutput(
            stdout="",
            stderr=stderr,
            returncode=0,
            total_latency_ms=321.5,
            startup_latency_ms=None,
            model_load_latency_ms=125.0,
            inference_latency_ms=190.0,
            network_request_count=0,
            offline_enforced=True,
            json_payload=payload or json_full_payload(),
        )

    def test_missing_runtime_files_fail_closed_without_worker_call(self):
        cases = (
            ("binary_path", "WHISPERCPP_BINARY_MISSING"),
            ("model_path", "WHISPERCPP_MODEL_MISSING"),
        )
        for field, expected_code in cases:
            with self.subTest(field=field):
                values = self.config.__dict__.copy()
                values[field] = str(Path(self.folder.name) / f"missing-{field}")
                runner = FakeRunner()

                result = WhisperCppAdapter(
                    WhisperCppRuntimeConfig(**values), runner
                ).transcribe(self.audio, source_audio_id="audio-1")

                self.assertEqual(result.status, "model_missing")
                self.assertEqual(result.error_code, expected_code)
                self.assertEqual(runner.calls, [])

    def test_runtime_download_flag_fails_closed(self):
        values = self.config.__dict__.copy()
        values["download_enabled"] = True
        runner = FakeRunner()

        result = WhisperCppAdapter(
            WhisperCppRuntimeConfig(**values), runner
        ).transcribe(self.audio, source_audio_id="audio-1")

        self.assertEqual(result.error_code, "WHISPERCPP_DOWNLOAD_DISABLED")
        self.assertEqual(runner.calls, [])

    def test_invalid_empty_and_wrong_format_audio_fail_before_worker(self):
        invalid = Path(self.folder.name) / "invalid.wav"
        invalid.write_text("not wav", encoding="utf-8")
        empty = Path(self.folder.name) / "empty.wav"
        stereo = Path(self.folder.name) / "stereo.wav"
        write_wav(empty, frames=0)
        write_wav(stereo, channels=2)
        runner = FakeRunner()
        adapter = WhisperCppAdapter(self.config, runner)

        results = [
            adapter.transcribe(invalid, source_audio_id="invalid"),
            adapter.transcribe(empty, source_audio_id="empty"),
            adapter.transcribe(stereo, source_audio_id="stereo"),
        ]

        self.assertEqual(results[0].error_code, "WHISPERCPP_INVALID_AUDIO")
        self.assertEqual(results[1].error_code, "WHISPERCPP_EMPTY_AUDIO")
        self.assertEqual(results[2].error_code, "WHISPERCPP_INVALID_AUDIO")
        self.assertEqual(runner.calls, [])

    def test_success_preserves_raw_text_native_segments_and_nullable_confidence(self):
        runner = FakeRunner(output=self.output())

        result = WhisperCppAdapter(self.config, runner).transcribe(
            self.audio, source_audio_id="audio-1", source_video_id="video-1"
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.full_text, "孩子在高二学习EAL，GPA很重要。")
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.segments[0].start_ms, 120)
        self.assertEqual(result.segments[0].end_ms, 2800)
        self.assertIsNone(result.segments[0].confidence)
        self.assertIsNone(result.confidence)
        self.assertEqual(result.timestamp_granularity, "token")
        self.assertEqual(result.timestamp_quality, "native")
        self.assertEqual([item.text for item in result.words], ["EAL", "GPA"])
        self.assertEqual(result.words[1].start_ms, 1300)
        self.assertEqual(result.billing_type, "local")
        self.assertEqual(result.cost, 0)
        self.assertEqual(result.raw_response["json"]["result"]["language"], "zh")
        self.assertEqual(result.raw_response["network_request_count"], 0)
        self.assertEqual(len(runner.calls), 1)

    def test_absent_native_token_offsets_do_not_create_fake_words(self):
        result = WhisperCppAdapter(
            self.config,
            FakeRunner(output=self.output(payload=json_full_payload(tokens=False))),
        ).transcribe(self.audio, source_audio_id="audio-1")

        self.assertEqual(result.words, ())
        self.assertEqual(result.timestamp_granularity, "segment")
        self.assertEqual(result.timestamp_quality, "native")
        self.assertFalse(result.capabilities["word_timestamps"])

    def test_raw_critical_tokens_are_not_post_corrected(self):
        result = WhisperCppAdapter(
            self.config, FakeRunner(output=self.output())
        ).transcribe(self.audio, source_audio_id="audio-1")

        self.assertIn("EAL", result.full_text)
        self.assertIn("GPA", result.full_text)
        self.assertIn("EAL", result.raw_response["critical_tokens"])
        self.assertIn("GPA", result.raw_response["critical_tokens"])

    def test_worker_timeout_failure_and_malformed_json_are_isolated(self):
        timeout = WhisperCppAdapter(
            self.config, FakeRunner(error=TimeoutError("worker timeout"))
        ).transcribe(self.audio, source_audio_id="timeout")
        failed = WhisperCppAdapter(
            self.config, FakeRunner(error=RuntimeError("worker failed"))
        ).transcribe(self.audio, source_audio_id="failed")
        malformed = WhisperCppAdapter(
            self.config, FakeRunner(output=self.output(payload={"result": {}}))
        ).transcribe(self.audio, source_audio_id="malformed")

        self.assertEqual(timeout.error_code, "WHISPERCPP_TIMEOUT")
        self.assertEqual(failed.error_code, "WHISPERCPP_WORKER_FAILED")
        self.assertEqual(malformed.error_code, "WHISPERCPP_MALFORMED_OUTPUT")
        self.assertEqual(timeout.provider, "whispercpp")
        self.assertEqual(failed.provider, "whispercpp")
        self.assertEqual(malformed.provider, "whispercpp")

    def test_hashes_and_local_request_id_are_derived_from_files(self):
        result = WhisperCppAdapter(
            self.config, FakeRunner(output=self.output())
        ).transcribe(self.audio, source_audio_id="audio-1")

        self.assertEqual(
            result.model_hash, hashlib.sha256(self.model.read_bytes()).hexdigest()
        )
        self.assertEqual(
            result.audio_hash, hashlib.sha256(self.audio.read_bytes()).hexdigest()
        )
        self.assertEqual(result.request_id_source, "local")
        self.assertTrue(result.request_id.startswith("local:whispercpp:"))


class WhisperCppProcessRunnerTests(unittest.TestCase):
    def test_offline_command_json_full_and_minimal_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sandbox = root / "sandbox-exec"
            sandbox.write_bytes(b"sandbox")
            sandbox.chmod(0o755)
            captured = {}

            def run_command(command, **kwargs):
                captured["command"] = command
                captured["environment"] = kwargs["env"]
                prefix = Path(command[command.index("-of") + 1])
                prefix.with_suffix(".json").write_text(
                    json.dumps(json_full_payload()), encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "",
                    "whisper_print_timings: load time = 125.00 ms\n"
                    "whisper_print_timings: total time = 321.50 ms\n",
                )

            config = WhisperCppRuntimeConfig(
                binary_path=str(root / "whisper-cli"),
                model_path=str(root / "model.bin"),
                state_dir=str(root / "state"),
            )
            old_tencent = os.environ.get("TENCENT_OCR_SECRET_ID")
            old_iflytek = os.environ.get("IFLYTEK_OCR_API_SECRET")
            os.environ["TENCENT_OCR_SECRET_ID"] = "must-not-inherit"
            os.environ["IFLYTEK_OCR_API_SECRET"] = "must-not-inherit"
            try:
                output = WhisperCppProcessRunner(
                    run_command=run_command, sandbox_path=str(sandbox)
                ).run(config, root / "audio.wav")
            finally:
                if old_tencent is None:
                    os.environ.pop("TENCENT_OCR_SECRET_ID", None)
                else:
                    os.environ["TENCENT_OCR_SECRET_ID"] = old_tencent
                if old_iflytek is None:
                    os.environ.pop("IFLYTEK_OCR_API_SECRET", None)
                else:
                    os.environ["IFLYTEK_OCR_API_SECRET"] = old_iflytek

            self.assertEqual(captured["command"][0], str(sandbox))
            self.assertTrue(any("deny network" in item for item in captured["command"]))
            self.assertIn("-ojf", captured["command"])
            self.assertIn("-l", captured["command"])
            self.assertIn("zh", captured["command"])
            self.assertNotIn("TENCENT_OCR_SECRET_ID", captured["environment"])
            self.assertNotIn("IFLYTEK_OCR_API_SECRET", captured["environment"])
            self.assertTrue(output.offline_enforced)
            self.assertEqual(output.network_request_count, 0)
            self.assertEqual(output.model_load_latency_ms, 125.0)
            self.assertEqual(output.json_payload["result"]["language"], "zh")

    def test_nonzero_exit_and_missing_json_raise_provider_local_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sandbox = root / "sandbox-exec"
            sandbox.write_bytes(b"sandbox")
            sandbox.chmod(0o755)
            config = WhisperCppRuntimeConfig(
                binary_path=str(root / "whisper-cli"),
                model_path=str(root / "model.bin"),
                state_dir=str(root / "state"),
            )

            def failed(command, **kwargs):
                return subprocess.CompletedProcess(command, 2, "", "local failure")

            def missing_json(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaises(RuntimeError):
                WhisperCppProcessRunner(
                    run_command=failed, sandbox_path=str(sandbox)
                ).run(config, root / "audio.wav")
            with self.assertRaises(RuntimeError):
                WhisperCppProcessRunner(
                    run_command=missing_json, sandbox_path=str(sandbox)
                ).run(config, root / "audio.wav")


class WhisperCppSmokeContractTests(unittest.TestCase):
    def test_smoke_invokes_one_adapter_once_and_preserves_result(self):
        from content_extraction.whispercpp_smoke import run_smoke

        class SmokeAdapter:
            def __init__(self):
                self.calls = []

            def transcribe(self, audio_path, *, source_audio_id, source_video_id=""):
                self.calls.append(Path(audio_path))
                return ASRResult(
                    provider="whispercpp", status="completed",
                    full_text="高二学习EAL和GPA。", segments=(), words=(),
                    language="zh", duration_ms=27000,
                    request_id="local:whispercpp:smoke", request_id_source="local",
                    engine_version="1.9.2", model_name="medium",
                    model_version="q5_0", model_hash="model-hash",
                    billing_type="local", cost=0,
                    raw_response={"network_request_count": 0, "offline_enforced": True},
                    error_code="", error_message="", source_video_id="",
                    source_audio_id="smoke-audio", audio_hash="audio-hash",
                    timestamp_granularity="segment", timestamp_quality="native",
                    capabilities={"word_timestamps": False, "punctuation": "native"},
                    latency_ms=100, confidence=None,
                )

        adapter = SmokeAdapter()
        report = run_smoke("sample.wav", "/runtime", adapter=adapter)

        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(report["full_text"], "高二学习EAL和GPA。")
        self.assertEqual(report["timestamp_granularity"], "segment")
        self.assertEqual(report["network_request_count"], 0)
        self.assertEqual(report["cost"], 0)


if __name__ == "__main__":
    unittest.main()

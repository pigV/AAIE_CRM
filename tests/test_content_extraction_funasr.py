import hashlib
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from content_extraction.asr.funasr import (
    FunASRAdapter,
    FunASRProcessOutput,
    FunASRProcessRunner,
    FunASRRuntimeConfig,
    sanitize_funasr_text,
)
from content_extraction.models import ASRResult, ASRSegment, ASRWord


def write_wav(path, *, frames=1600, rate=16000, channels=1, sample_width=2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(rate)
        handle.writeframes(b"\0" * frames * channels * sample_width)


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


class ASRResultContractTests(unittest.TestCase):
    def test_serialization_preserves_nullable_confidence_and_real_timestamps(self):
        result = ASRResult(
            provider="funasr",
            status="completed",
            full_text="孩子10岁申请VISA",
            segments=(ASRSegment(100, 900, "孩子10岁", None),),
            words=(ASRWord(100, 300, "孩子", None),),
            language="zh",
            duration_ms=1000,
            request_id="local:funasr:audio-1",
            request_id_source="local",
            engine_version="1.4.1",
            model_name="SenseVoiceSmall",
            model_version="q8",
            model_hash="abc123",
            billing_type="local",
            cost=0,
            raw_response={"stdout": "孩子10岁申请VISA"},
            error_code="",
            error_message="",
            source_video_id="video-1",
            source_audio_id="audio-1",
            audio_hash="audio-hash",
            timestamp_granularity="vad_segment",
            timestamp_quality="native",
            capabilities={"vad": True, "punctuation": "absent"},
            latency_ms=42.5,
        )

        payload = result.to_unified_dict()

        self.assertIsNone(payload["segments"][0]["confidence"])
        self.assertEqual(payload["segments"][0]["start_ms"], 100)
        self.assertEqual(payload["words"][0]["end_ms"], 300)
        self.assertEqual(payload["billing_type"], "local")
        self.assertEqual(payload["cost"], 0)
        self.assertIsNone(payload["error"])


class FunASRAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.binary = root / "llama-funasr-sensevoice"
        self.model = root / "sensevoice-small-q8.gguf"
        self.vad = root / "fsmn-vad.gguf"
        self.audio = root / "sample.wav"
        for path, content in (
            (self.binary, b"binary"),
            (self.model, b"model"),
            (self.vad, b"vad"),
        ):
            path.write_bytes(content)
        self.binary.chmod(0o755)
        write_wav(self.audio)
        self.config = FunASRRuntimeConfig(
            binary_path=str(self.binary),
            model_path=str(self.model),
            vad_path=str(self.vad),
            state_dir=str(root / "state"),
            timeout_seconds=2,
            engine_version="1.4.1",
        )

    def tearDown(self):
        self.folder.cleanup()

    def test_missing_runtime_components_fail_closed_without_runner_call(self):
        cases = (
            ("binary_path", "FUNASR_BINARY_MISSING", "model_missing"),
            ("model_path", "FUNASR_MODEL_MISSING", "model_missing"),
            ("vad_path", "FUNASR_VAD_MISSING", "model_missing"),
        )
        for field, error_code, status in cases:
            with self.subTest(field=field):
                values = self.config.__dict__.copy()
                values[field] = str(Path(self.folder.name) / f"missing-{field}")
                runner = FakeRunner()
                result = FunASRAdapter(FunASRRuntimeConfig(**values), runner).transcribe(
                    self.audio, source_audio_id="audio-1"
                )
                self.assertEqual(result.status, status)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(runner.calls, [])

    def test_runtime_download_flag_fails_closed(self):
        values = self.config.__dict__.copy()
        values["download_enabled"] = True
        runner = FakeRunner()

        result = FunASRAdapter(FunASRRuntimeConfig(**values), runner).transcribe(
            self.audio, source_audio_id="audio-1"
        )

        self.assertEqual(result.error_code, "FUNASR_DOWNLOAD_DISABLED")
        self.assertEqual(runner.calls, [])

    def test_invalid_and_empty_audio_fail_before_worker(self):
        runner = FakeRunner()
        invalid = Path(self.folder.name) / "invalid.wav"
        invalid.write_text("not wav", encoding="utf-8")
        empty = Path(self.folder.name) / "empty.wav"
        write_wav(empty, frames=0)

        invalid_result = FunASRAdapter(self.config, runner).transcribe(
            invalid, source_audio_id="invalid"
        )
        empty_result = FunASRAdapter(self.config, runner).transcribe(
            empty, source_audio_id="empty"
        )

        self.assertEqual(invalid_result.error_code, "FUNASR_INVALID_AUDIO")
        self.assertEqual(empty_result.error_code, "FUNASR_EMPTY_AUDIO")
        self.assertEqual(runner.calls, [])

    def test_success_keeps_raw_text_critical_tokens_and_absent_confidence(self):
        output = FunASRProcessOutput(
            stdout="<|zh|><|NEUTRAL|><|Speech|><|woitn|>孩子10岁申请500签证VISA。\n",
            stderr="engine diagnostic",
            returncode=0,
            total_latency_ms=321.5,
            startup_latency_ms=None,
            model_load_latency_ms=None,
            inference_latency_ms=None,
            network_request_count=0,
            offline_enforced=True,
            segments=(),
        )
        runner = FakeRunner(output=output)

        result = FunASRAdapter(self.config, runner).transcribe(
            self.audio,
            source_audio_id="audio-1",
            source_video_id="video-1",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.full_text, "孩子10岁申请500签证VISA。")
        self.assertEqual(result.language, "zh")
        self.assertIsNone(result.confidence)
        self.assertEqual(result.timestamp_granularity, "none")
        self.assertEqual(result.timestamp_quality, "none")
        self.assertEqual(result.segments, ())
        self.assertEqual(result.words, ())
        self.assertEqual(result.billing_type, "local")
        self.assertEqual(result.cost, 0)
        self.assertEqual(result.capabilities["punctuation"], "native")
        self.assertIn("VISA", result.raw_response["critical_tokens"])
        self.assertIn("500", result.raw_response["critical_tokens"])
        self.assertEqual(result.raw_response["network_request_count"], 0)
        self.assertTrue(result.raw_response["offline_enforced"])
        self.assertEqual(len(runner.calls), 1)

    def test_sanitizes_leading_embedded_and_consecutive_control_tags(self):
        raw = (
            "<|zh|><|NEUTRAL|><|Speech|><|withitn|>第一句。"
            "<|zh|><|NEUTRAL|><|BGM|><|withitn|>第二句VISA。"
        )
        output = FunASRProcessOutput(
            stdout=raw + "\n", stderr="", returncode=0, total_latency_ms=1,
            startup_latency_ms=None, model_load_latency_ms=None,
            inference_latency_ms=None, network_request_count=0,
            offline_enforced=True,
            segments=((0, 1000, "<|Speech|><|withitn|>第一句。"),),
        )

        result = FunASRAdapter(
            self.config, FakeRunner(output=output)
        ).transcribe(self.audio, source_audio_id="tagged")

        self.assertEqual(result.full_text, "第一句。第二句VISA。")
        self.assertEqual(result.segments[0].text, "第一句。")
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.raw_response["stdout"], raw + "\n")
        self.assertNotIn("NEUTRAL", result.raw_response["critical_tokens"])
        self.assertNotIn("SPEECH", result.raw_response["critical_tokens"])
        self.assertNotIn("WITHITN", result.raw_response["critical_tokens"])

    def test_sanitize_funasr_text_does_not_rewrite_ordinary_angle_text(self):
        self.assertEqual(
            sanitize_funasr_text("保留<普通文字>但删除<|NEUTRAL|>标签"),
            "保留<普通文字>但删除标签",
        )

    def test_fixture_categories_preserve_raw_text_and_extract_known_tokens(self):
        cases = (
            ("这是纯中文教育口播。", (), ()),
            ("学生正在准备VCE和ATAR。", ("VCE", "ATAR"), ("ASCII_ACRONYM",)),
            ("孩子10岁开始学习。", ("10岁",), ("AGE",)),
            ("计划在2026年入学。", ("2026",), ("DATE",)),
            ("申请500签证并购买OSHC。", ("500", "OSHC"), ("VISA_SUBCLASS", "ASCII_ACRONYM")),
        )
        for text, expected_tokens, expected_types in cases:
            with self.subTest(text=text):
                output = FunASRProcessOutput(
                    stdout=f"<|zh|><|NEUTRAL|><|Speech|><|woitn|>{text}\n",
                    stderr="", returncode=0, total_latency_ms=1,
                    startup_latency_ms=None, model_load_latency_ms=None,
                    inference_latency_ms=None, network_request_count=0,
                    offline_enforced=True, segments=(),
                )
                result = FunASRAdapter(
                    self.config, FakeRunner(output=output)
                ).transcribe(self.audio, source_audio_id="fixture")

                self.assertEqual(result.full_text, text)
                for token in expected_tokens:
                    self.assertIn(token, result.raw_response["critical_tokens"])
                token_types = {
                    item["type"]
                    for item in result.raw_response["critical_token_details"]
                }
                for token_type in expected_types:
                    self.assertIn(token_type, token_types)

    def test_native_vad_segments_are_not_promoted_to_word_timestamps(self):
        output = FunASRProcessOutput(
            stdout="第一段第二段",
            stderr="",
            returncode=0,
            total_latency_ms=50,
            startup_latency_ms=5,
            model_load_latency_ms=10,
            inference_latency_ms=35,
            network_request_count=0,
            offline_enforced=True,
            segments=((120, 800, "第一段"), (1200, 1900, "第二段")),
        )

        result = FunASRAdapter(self.config, FakeRunner(output=output)).transcribe(
            self.audio, source_audio_id="audio-1"
        )

        self.assertEqual(result.timestamp_granularity, "vad_segment")
        self.assertEqual(result.timestamp_quality, "native")
        self.assertEqual(result.segments[1].start_ms, 1200)
        self.assertIsNone(result.segments[1].confidence)
        self.assertEqual(result.words, ())

    def test_worker_timeout_and_malformed_output_are_isolated(self):
        timeout_result = FunASRAdapter(
            self.config, FakeRunner(error=TimeoutError("worker timeout"))
        ).transcribe(self.audio, source_audio_id="timeout")
        malformed = FunASRProcessOutput(
            stdout="\n", stderr="", returncode=0, total_latency_ms=1,
            startup_latency_ms=None, model_load_latency_ms=None,
            inference_latency_ms=None, network_request_count=0,
            offline_enforced=True, segments=(),
        )
        malformed_result = FunASRAdapter(
            self.config, FakeRunner(output=malformed)
        ).transcribe(self.audio, source_audio_id="malformed")

        self.assertEqual(timeout_result.error_code, "FUNASR_TIMEOUT")
        self.assertEqual(malformed_result.error_code, "FUNASR_MALFORMED_OUTPUT")
        self.assertEqual(timeout_result.provider, "funasr")
        self.assertEqual(malformed_result.provider, "funasr")

    def test_model_and_audio_hashes_are_derived_from_local_files(self):
        output = FunASRProcessOutput(
            stdout="测试", stderr="", returncode=0, total_latency_ms=1,
            startup_latency_ms=None, model_load_latency_ms=None,
            inference_latency_ms=None, network_request_count=0,
            offline_enforced=True, segments=(),
        )

        result = FunASRAdapter(self.config, FakeRunner(output=output)).transcribe(
            self.audio, source_audio_id="audio-1"
        )

        expected_model = hashlib.sha256(self.model.read_bytes()).hexdigest()
        expected_audio = hashlib.sha256(self.audio.read_bytes()).hexdigest()
        self.assertEqual(result.model_hash, expected_model)
        self.assertEqual(result.audio_hash, expected_audio)
        self.assertEqual(result.request_id_source, "local")
        self.assertTrue(result.request_id.startswith("local:funasr:"))


class FunASRProcessRunnerTests(unittest.TestCase):
    def test_offline_sandbox_and_minimal_environment_exclude_cloud_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sandbox = root / "sandbox-exec"
            sandbox.write_bytes(b"sandbox")
            sandbox.chmod(0o755)
            captured = {}

            def run_command(command, **kwargs):
                captured["command"] = command
                captured["environment"] = kwargs["env"]
                return subprocess.CompletedProcess(command, 0, "测试", "")

            config = FunASRRuntimeConfig(
                binary_path=str(root / "binary"),
                model_path=str(root / "model.gguf"),
                vad_path=str(root / "vad.gguf"),
                state_dir=str(root / "state"),
            )
            old_tencent = os.environ.get("TENCENT_OCR_SECRET_ID")
            old_iflytek = os.environ.get("IFLYTEK_OCR_API_SECRET")
            os.environ["TENCENT_OCR_SECRET_ID"] = "must-not-inherit"
            os.environ["IFLYTEK_OCR_API_SECRET"] = "must-not-inherit"
            try:
                output = FunASRProcessRunner(
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
            self.assertTrue(
                any("deny network" in item for item in captured["command"])
            )
            self.assertNotIn("TENCENT_OCR_SECRET_ID", captured["environment"])
            self.assertNotIn("IFLYTEK_OCR_API_SECRET", captured["environment"])
            self.assertTrue(output.offline_enforced)
            self.assertEqual(output.network_request_count, 0)


class FunASRSmokeContractTests(unittest.TestCase):
    def test_smoke_serializes_one_adapter_result_without_rewriting_text(self):
        from content_extraction.funasr_smoke import run_smoke

        class SmokeAdapter:
            def __init__(self):
                self.calls = []

            def transcribe(self, audio_path, *, source_audio_id, source_video_id=""):
                self.calls.append(Path(audio_path))
                return ASRResult(
                    provider="funasr", status="completed",
                    full_text="孩子10岁申请VISA。", segments=(), words=(),
                    language="zh", duration_ms=15000,
                    request_id="local:funasr:smoke", request_id_source="local",
                    engine_version="runtime-llamacpp-v0.1.9",
                    model_name="SenseVoiceSmall", model_version="q8",
                    model_hash="model-hash", billing_type="local", cost=0,
                    raw_response={
                        "network_request_count": 0,
                        "offline_enforced": True,
                        "startup_latency_ms": None,
                        "model_load_latency_ms": None,
                        "inference_latency_ms": None,
                        "total_latency_ms": 100,
                    },
                    error_code="", error_message="", source_video_id="",
                    source_audio_id="smoke-audio", audio_hash="audio-hash",
                    timestamp_granularity="none", timestamp_quality="none",
                    capabilities={"vad": True, "vad_boundaries": False,
                                  "punctuation": "native"},
                    latency_ms=100, confidence=None,
                )

        adapter = SmokeAdapter()
        report = run_smoke("sample.wav", "/runtime", adapter=adapter)

        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(report["full_text"], "孩子10岁申请VISA。")
        self.assertEqual(report["timestamp_granularity"], "none")
        self.assertFalse(report["capabilities"]["vad_boundaries"])
        self.assertEqual(report["network_request_count"], 0)
        self.assertEqual(report["cost"], 0)


if __name__ == "__main__":
    unittest.main()

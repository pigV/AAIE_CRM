import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class _FakeProcess:
    def __init__(self):
        self.pid = 321
        self.returncode = None
        self.terminated = False
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


class _HungProcess(_FakeProcess):
    def __init__(self):
        super().__init__()
        self.killed = False
        self.wait_attempts = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.wait_attempts += 1
        if self.wait_attempts == 1:
            raise subprocess.TimeoutExpired("llama-server", timeout)
        return self.returncode


class LlamaRuntimeTests(unittest.TestCase):
    def make_config(self, root):
        from content_extraction.accuracy.llama_runtime import LlamaRuntimeConfig
        binary = Path(root) / "bin" / "llama-server"
        model = Path(root) / "models" / "Qwen3-8B-Q4_K_M.gguf"
        binary.parent.mkdir(parents=True)
        model.parent.mkdir(parents=True)
        binary.write_bytes(b"binary")
        binary.chmod(0o755)
        model.write_bytes(b"model")
        return LlamaRuntimeConfig(runtime_root=Path(root), binary_path=binary, model_path=model,
                                  port=18091, startup_timeout_seconds=1)

    def test_command_is_offline_loopback_single_slot_and_bounded(self):
        from content_extraction.accuracy.llama_runtime import LlamaCppRuntime
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LlamaCppRuntime(self.make_config(tmp), health_check=lambda url: True,
                                      popen_factory=lambda *args, **kwargs: _FakeProcess())
            command = runtime.command()
            self.assertIn("--offline", command)
            self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
            self.assertEqual(command[command.index("--parallel") + 1], "1")
            self.assertEqual(command[command.index("--reasoning") + 1], "off")
            self.assertIn("--no-ui", command)
            self.assertNotIn("--model-url", command)

    def test_context_manager_uses_sigterm_and_never_process_kill(self):
        from content_extraction.accuracy.llama_runtime import LlamaCppRuntime
        fake = _FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LlamaCppRuntime(self.make_config(tmp), health_check=lambda url: True,
                                      popen_factory=lambda *args, **kwargs: fake)
            with runtime as active:
                self.assertEqual(active.endpoint, "http://127.0.0.1:18091")
                self.assertEqual(active.metrics["pid"], 321)
            self.assertTrue(fake.terminated)
            self.assertTrue(runtime.metrics["shutdown_clean"])
            self.assertEqual(runtime.metrics["network_mode"], "loopback_only_offline")

    def test_missing_model_fails_closed_before_process_start(self):
        from content_extraction.accuracy.llama_runtime import LlamaCppRuntime
        called = []
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            config.model_path.unlink()
            runtime = LlamaCppRuntime(config, health_check=lambda url: True,
                                      popen_factory=lambda *args, **kwargs: called.append(args))
            with self.assertRaisesRegex(RuntimeError, "model_missing"):
                runtime.start()
            self.assertEqual(called, [])

    def test_health_timeout_terminates_process_and_reports_failure(self):
        from content_extraction.accuracy.llama_runtime import LlamaCppRuntime
        fake = _FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LlamaCppRuntime(self.make_config(tmp), health_check=lambda url: False,
                                      popen_factory=lambda *args, **kwargs: fake, sleep=lambda _: None)
            with self.assertRaisesRegex(RuntimeError, "llama_runtime_startup_timeout"):
                runtime.start()
            self.assertTrue(fake.terminated)

    def test_shutdown_timeout_kills_and_reaps_hung_process(self):
        from content_extraction.accuracy.llama_runtime import LlamaCppRuntime
        fake = _HungProcess()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LlamaCppRuntime(
                self.make_config(tmp), health_check=lambda url: True,
                popen_factory=lambda *args, **kwargs: fake,
            )
            runtime.start()

            runtime.stop()

            self.assertTrue(fake.terminated)
            self.assertTrue(fake.killed)
            self.assertEqual(fake.wait_attempts, 2)
            self.assertEqual(runtime.metrics["exit_code"], -9)
            self.assertFalse(runtime.metrics["shutdown_clean"])


if __name__ == "__main__":
    unittest.main()

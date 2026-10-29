import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import watchdog


PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM")


class FakeInspector:
    def __init__(self, *, listeners=(), commands=None, cwds=None, alive=None):
        self.listeners = list(listeners)
        self.commands = dict(commands or {})
        self.cwds = dict(cwds or {})
        self.alive = set(alive or self.listeners)
        self.signals = []

    def listener_pids(self, _port):
        return list(self.listeners)

    def process_command(self, pid):
        return self.commands.get(pid, "")

    def process_cwd(self, pid):
        return self.cwds.get(pid, "")

    def pid_alive(self, pid):
        return pid in self.alive

    def terminate(self, pid):
        self.signals.append((pid, "TERM"))
        self.alive.discard(pid)
        self.listeners = [value for value in self.listeners if value != pid]

    def kill(self, pid):
        self.signals.append((pid, "KILL"))
        self.alive.discard(pid)
        self.listeners = [value for value in self.listeners if value != pid]


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid


class ContentExtractionProductionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def lifecycle(self):
        path = Path("content_extraction_service_lifecycle.py")
        self.assertTrue(
            path.is_file(),
            "Production 8792 lifecycle module is not implemented",
        )
        spec = importlib.util.spec_from_file_location("ce_lifecycle_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def runtime_fixture(self):
        runtime = self.root / "cache/content_extractions/runtime"
        artifacts = {
            "apple": runtime / "ocr/apple_vision/bin/apple-vision-worker",
            "whisper_binary": runtime / "asr/whispercpp/bin/whisper-cli",
            "whisper_model": runtime / "asr/whispercpp/models/model.bin",
            "funasr_binary": runtime / "asr/funasr-gguf/bin/funasr",
            "funasr_model": runtime / "asr/funasr-gguf/models/sensevoice.gguf",
            "funasr_vad": runtime / "asr/funasr-gguf/models/vad.gguf",
            "llama_binary": runtime / "accuracy/llamacpp/bin/llama-server",
            "qwen_model": runtime / "accuracy/llamacpp/models/qwen.gguf",
            "ffmpeg": self.root / "vendor/ffmpeg",
        }
        for name, path in artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((name + "\n").encode())
        python = runtime / "venv/bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)
        source = self.root / "content_extraction_service.py"
        source.write_text("# fixture source\n", encoding="utf-8")
        models = runtime / "paddlex/official_models"
        for model in ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"):
            folder = models / model
            folder.mkdir(parents=True, exist_ok=True)
            for name in ("inference.json", "inference.pdiparams", "inference.yml"):
                (folder / name).write_text(f"{model}:{name}\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "runtime_version": "test-runtime-v1",
            "runtime_root": "cache/content_extractions/runtime",
            "python": {
                "path": "venv/bin/python",
                "paddle_models": {
                    "root": "paddlex/official_models",
                    "models": ["PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"],
                    "file_count": 6,
                },
            },
            "apple_vision": {"worker": self.entry(runtime, artifacts["apple"])},
            "whisper": {
                "binary": self.entry(runtime, artifacts["whisper_binary"]),
                "model": self.entry(runtime, artifacts["whisper_model"]),
            },
            "funasr": {
                "binary": self.entry(runtime, artifacts["funasr_binary"]),
                "model": self.entry(runtime, artifacts["funasr_model"]),
                "vad": self.entry(runtime, artifacts["funasr_vad"]),
            },
            "llama_qwen": {
                "binary": self.entry(runtime, artifacts["llama_binary"]),
                "model": self.entry(runtime, artifacts["qwen_model"]),
            },
            "ffmpeg": {
                "path": str(artifacts["ffmpeg"].relative_to(self.root)),
                "sha256": self.sha(artifacts["ffmpeg"]),
            },
        }
        manifest_path = runtime / "production-runtime-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return runtime, manifest_path, python, source, artifacts

    def write_canonical_environment(self):
        (self.root / ".env.production").write_text(
            "\n".join(
                (
                    "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED=true",
                    "BILLING_MODE=FREE_ONLY",
                    "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED=true",
                    "IFLYTEK_SPARK_LITE_API_PASSWORD=fake-private-test-value",
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def entry(self, runtime, path):
        return {
            "path": str(path.relative_to(runtime)),
            "sha256": self.sha(path),
        }

    def config(self, port=18792):
        module = self.lifecycle()
        runtime, manifest, python, source, _ = self.runtime_fixture()
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        return module.LifecycleConfig(
            production_root=self.root,
            runtime_root=runtime,
            python_path=python,
            service_source=source,
            manifest_path=manifest,
            host="127.0.0.1",
            port=port,
            pid_file=logs / "content-extraction.pid",
            stdout_log=logs / "content-extraction.out.log",
            stderr_log=logs / "content-extraction.err.log",
            lifecycle_log=logs / "content-extraction-lifecycle.log",
        )

    def owned_command(self, config):
        return " ".join(config.start_command())

    def test_default_contract_keeps_8781_parent_and_8792_content_extraction(self):
        module = self.lifecycle()
        config = module.production_config()
        self.assertEqual(config.production_root, PRODUCTION_ROOT)
        self.assertEqual(config.port, 8792)
        self.assertEqual(
            config.python_path,
            PRODUCTION_ROOT / "cache/content_extractions/runtime/venv/bin/python",
        )
        self.assertEqual(
            config.service_source, PRODUCTION_ROOT / "content_extraction_service.py"
        )
        self.assertNotIn(".worktrees", " ".join(config.start_command()))
        watchdog_source = Path("watchdog.py").read_text(encoding="utf-8")
        self.assertIn('"parent"', watchdog_source)
        self.assertIn('"port": 8781', watchdog_source)
        self.assertIn('"content_extraction"', watchdog_source)
        self.assertIn('"port": 8792', watchdog_source)

    def test_runtime_manifest_validation_passes_and_hash_tamper_fails_closed(self):
        module = self.lifecycle()
        config = self.config()
        valid = module.validate_runtime(config)
        self.assertTrue(valid.ok, valid.errors)

        payload = json.loads(config.manifest_path.read_text(encoding="utf-8"))
        target = config.runtime_root / payload["whisper"]["binary"]["path"]
        target.write_bytes(b"tampered")
        invalid = module.validate_runtime(config)
        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.error_code, "CONTENT_EXTRACTION_RUNTIME_INVALID")
        self.assertIn("whisper.binary:sha256_mismatch", invalid.errors)

    def test_invalid_runtime_prevents_process_spawn(self):
        module = self.lifecycle()
        config = self.config()
        config.manifest_path.unlink()
        spawn = Mock(side_effect=AssertionError("invalid runtime must not start"))

        result = module.start_service(
            config,
            inspector=FakeInspector(),
            spawn=spawn,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONTENT_EXTRACTION_RUNTIME_INVALID")
        spawn.assert_not_called()

    def test_malformed_canonical_environment_prevents_process_spawn(self):
        module = self.lifecycle()
        config = self.config()
        (self.root / ".env.production").write_text(
            "BILLING_MODE=FREE_ONLY\nMALFORMED LINE\n",
            encoding="utf-8",
        )
        spawn = Mock(side_effect=AssertionError("invalid environment must not start"))

        result = module.start_service(
            config,
            inspector=FakeInspector(),
            spawn=spawn,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "ENVIRONMENT_INVALID")
        self.assertEqual(
            result.error_code, "CONTENT_EXTRACTION_ENVIRONMENT_INVALID"
        )
        spawn.assert_not_called()

    def test_start_empty_port_uses_exact_production_command_and_writes_pid(self):
        module = self.lifecycle()
        config = self.config()
        self.write_canonical_environment()
        inspector = FakeInspector()
        captured = []

        def spawn(command, **kwargs):
            captured.append((command, kwargs))
            inspector.listeners[:] = [401]
            inspector.alive.add(401)
            inspector.commands[401] = " ".join(command)
            inspector.cwds[401] = str(config.production_root)
            return FakeProcess(401)

        result = module.start_service(
            config,
            inspector=inspector,
            spawn=spawn,
            health_probe=lambda _config: module.healthy_identity(_config),
            sleep=lambda _seconds: None,
        )
        self.assertTrue(result.ok, result)
        self.assertEqual(result.status, "STARTED")
        self.assertEqual(captured[0][0], config.start_command())
        self.assertEqual(captured[0][1]["cwd"], str(config.production_root))
        child_environment = captured[0][1]["env"]
        self.assertEqual(
            child_environment["AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"], "true"
        )
        self.assertEqual(child_environment["BILLING_MODE"], "FREE_ONLY")
        self.assertEqual(
            child_environment["IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED"], "true"
        )
        self.assertTrue(
            bool(child_environment.get("IFLYTEK_SPARK_LITE_API_PASSWORD"))
        )
        self.assertEqual(config.pid_file.read_text(encoding="utf-8"), "401\n")

    def test_restart_reloads_same_canonical_environment_without_parent_shell_export(self):
        module = self.lifecycle()
        config = self.config()
        self.write_canonical_environment()
        config.pid_file.write_text("407\n", encoding="utf-8")
        inspector = FakeInspector(
            listeners=[407],
            commands={407: self.owned_command(config)},
            cwds={407: str(config.production_root)},
        )
        captured = []

        def spawn(command, **kwargs):
            captured.append(kwargs["env"])
            inspector.listeners[:] = [408]
            inspector.alive.add(408)
            inspector.commands[408] = " ".join(command)
            inspector.cwds[408] = str(config.production_root)
            return FakeProcess(408)

        with patch.dict(
            os.environ,
            {
                "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "",
                "BILLING_MODE": "",
                "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED": "",
            },
            clear=False,
        ):
            for key in (
                "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED",
                "BILLING_MODE",
                "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED",
            ):
                os.environ.pop(key, None)
            result = module.restart_service(
                config,
                inspector=inspector,
                spawn=spawn,
                health_probe=lambda _config: module.healthy_identity(_config),
                sleep=lambda _seconds: None,
            )

        self.assertTrue(result.ok, result)
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0]["AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"], "true"
        )
        self.assertEqual(captured[0]["BILLING_MODE"], "FREE_ONLY")
        self.assertEqual(
            captured[0]["IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED"], "true"
        )

    def test_duplicate_start_returns_existing_pid_without_spawning(self):
        module = self.lifecycle()
        config = self.config()
        config.pid_file.write_text("402\n", encoding="utf-8")
        inspector = FakeInspector(
            listeners=[402],
            commands={402: self.owned_command(config)},
            cwds={402: str(config.production_root)},
        )
        spawn = Mock(side_effect=AssertionError("duplicate process"))
        result = module.start_service(
            config,
            inspector=inspector,
            spawn=spawn,
            health_probe=lambda _config: module.healthy_identity(_config),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ALREADY_RUNNING")
        self.assertEqual(result.pid, 402)
        spawn.assert_not_called()

    def test_wrong_process_on_port_fails_closed_and_is_never_stopped(self):
        module = self.lifecycle()
        config = self.config()
        inspector = FakeInspector(
            listeners=[48289],
            commands={48289: "/tmp/worktree/venv/bin/python /tmp/.worktrees/accurate-transcript/content_extraction_service.py --port 18792"},
            cwds={48289: "/tmp/.worktrees/accurate-transcript"},
        )
        result = module.start_service(
            config,
            inspector=inspector,
            spawn=Mock(side_effect=AssertionError("must not spawn")),
            health_probe=lambda _config: module.healthy_identity(_config),
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.error_code,
            "CONTENT_EXTRACTION_PORT_OCCUPIED_BY_NON_PRODUCTION_PROCESS",
        )
        self.assertEqual(inspector.signals, [])

    def test_safe_stop_only_terminates_recorded_verified_owner(self):
        module = self.lifecycle()
        config = self.config()
        config.pid_file.write_text("403\n", encoding="utf-8")
        inspector = FakeInspector(
            listeners=[403],
            commands={403: self.owned_command(config)},
            cwds={403: str(config.production_root)},
        )
        result = module.stop_service(
            config, inspector=inspector, sleep=lambda _seconds: None
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "STOPPED")
        self.assertEqual(inspector.signals, [(403, "TERM")])
        self.assertFalse(config.pid_file.exists())

    def test_safe_stop_rejects_reused_or_mismatched_pid(self):
        module = self.lifecycle()
        config = self.config()
        config.pid_file.write_text("404\n", encoding="utf-8")
        inspector = FakeInspector(
            listeners=[404], commands={404: "python unrelated.py"}, cwds={404: "/tmp"}
        )
        result = module.stop_service(config, inspector=inspector)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CONTENT_EXTRACTION_PROCESS_IDENTITY_MISMATCH")
        self.assertEqual(inspector.signals, [])

    def test_restart_stops_old_owner_starts_new_pid_and_requires_health(self):
        module = self.lifecycle()
        config = self.config()
        config.pid_file.write_text("405\n", encoding="utf-8")
        inspector = FakeInspector(
            listeners=[405],
            commands={405: self.owned_command(config)},
            cwds={405: str(config.production_root)},
        )

        def spawn(command, **_kwargs):
            inspector.listeners[:] = [406]
            inspector.alive.add(406)
            inspector.commands[406] = " ".join(command)
            inspector.cwds[406] = str(config.production_root)
            return FakeProcess(406)

        result = module.restart_service(
            config,
            inspector=inspector,
            spawn=spawn,
            health_probe=lambda _config: module.healthy_identity(_config),
            sleep=lambda _seconds: None,
        )
        self.assertTrue(result.ok, result)
        self.assertEqual(result.old_pid, 405)
        self.assertEqual(result.pid, 406)
        self.assertEqual(inspector.signals, [(405, "TERM")])

    def test_watchdog_distinguishes_all_four_states_and_recovers_only_safe_states(self):
        module = self.lifecycle()
        config = self.config()
        owned = self.owned_command(config)
        cases = (
            ("PRODUCTION_HEALTHY", [501], {501: owned}, {501: str(config.production_root)}, True),
            ("PORT_EMPTY", [], {}, {}, False),
            ("NON_PRODUCTION_OWNER", [502], {502: "python worktree.py"}, {502: "/tmp"}, True),
            ("PRODUCTION_HEALTH_FAILED", [503], {503: owned}, {503: str(config.production_root)}, False),
        )
        for state, listeners, commands, cwds, health_ok in cases:
            with self.subTest(state=state):
                inspector = FakeInspector(
                    listeners=listeners, commands=commands, cwds=cwds
                )
                observed = module.classify_service(
                    config,
                    inspector=inspector,
                    health_probe=(
                        (lambda _config: module.healthy_identity(_config))
                        if health_ok
                        else (lambda _config: module.unhealthy_identity(_config))
                    ),
                )
                self.assertEqual(observed.state, state)

        starts = []
        restarts = []
        self.assertEqual(
            module.watchdog_action(
                module.ServiceObservation("PRODUCTION_HEALTHY", 501),
                start=lambda: starts.append(True),
                restart=lambda: restarts.append(True),
            ),
            "NO_ACTION",
        )
        self.assertEqual(
            module.watchdog_action(
                module.ServiceObservation("PORT_EMPTY", None),
                start=lambda: starts.append(True),
                restart=lambda: restarts.append(True),
            ),
            "START",
        )
        self.assertEqual(
            module.watchdog_action(
                module.ServiceObservation("PRODUCTION_HEALTH_FAILED", 503),
                start=lambda: starts.append(True),
                restart=lambda: restarts.append(True),
            ),
            "RESTART",
        )
        self.assertEqual(
            module.watchdog_action(
                module.ServiceObservation("NON_PRODUCTION_OWNER", 502),
                start=lambda: starts.append(True),
                restart=lambda: restarts.append(True),
            ),
            "FAIL_CLOSED",
        )
        self.assertEqual((len(starts), len(restarts)), (1, 1))

    def test_watchdog_wiring_never_recovers_non_production_port_owner(self):
        import content_extraction_service_lifecycle as production_lifecycle

        observation = production_lifecycle.ServiceObservation(
            "NON_PRODUCTION_OWNER", 48289, (48289,), {}
        )
        with (
            patch.object(
                watchdog,
                "service_lock_path",
                return_value=self.root / "content-extraction.restart.lock",
            ),
            patch.object(
                production_lifecycle,
                "classify_service",
                return_value=observation,
            ),
            patch.object(production_lifecycle, "start_service") as start,
            patch.object(production_lifecycle, "restart_service") as restart,
        ):
            result = watchdog.ensure_service(
                "content_extraction",
                watchdog.SERVICES["content_extraction"],
                "test_wrong_owner",
            )
        self.assertFalse(result)
        start.assert_not_called()
        restart.assert_not_called()

    def test_shell_and_client_wiring_use_canonical_lifecycle_without_broad_kill(self):
        start = Path("start.command").read_text(encoding="utf-8")
        stop = Path("停止 AAIE CRM.command").read_text(encoding="utf-8")
        restart = Path("重启 AAIE CRM.command").read_text(encoding="utf-8")
        client = Path("app.py").read_text(encoding="utf-8")
        combined = start + stop + restart
        self.assertIn("content_extraction_service_lifecycle.py start", start)
        self.assertIn("content_extraction_service_lifecycle.py stop", stop)
        self.assertIn("content_extraction_service_lifecycle.py", restart)
        self.assertIn("http://127.0.0.1:8792", client)
        self.assertNotIn("pkill python", combined)
        self.assertNotIn("killall python", combined)

    def test_isolated_real_process_start_duplicate_restart_health_and_stop(self):
        module = self.lifecycle()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = self.config(port=port)
        self.write_canonical_environment()
        config.python_path.unlink()
        config.python_path.symlink_to(Path(sys.executable).resolve())
        config.service_source.write_text(
            textwrap.dedent(
                """
                import argparse
                import json
                import os
                from http.server import BaseHTTPRequestHandler, HTTPServer
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--workspace")
                parser.add_argument("--host")
                parser.add_argument("--port", type=int)
                parser.add_argument("--source-mode")
                parser.add_argument("--runtime-python")
                args = parser.parse_args()

                environment_observation = {
                    "accurate_transcript_enabled": os.environ.get(
                        "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED"
                    ),
                    "billing_mode": os.environ.get("BILLING_MODE"),
                    "spark_free_only_allowed": os.environ.get(
                        "IFLYTEK_SPARK_LITE_FREE_ONLY_ALLOWED"
                    ),
                    "spark_credential_present": bool(
                        os.environ.get("IFLYTEK_SPARK_LITE_API_PASSWORD")
                    ),
                }
                observation_path = (
                    Path(args.workspace) / f"environment-observed-{os.getpid()}.json"
                )
                observation_path.write_text(
                    json.dumps(environment_observation), encoding="utf-8"
                )

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        return
                    def do_GET(self):
                        if self.path != "/lifecycle-health":
                            self.send_response(404)
                            self.end_headers()
                            return
                        body = json.dumps({
                            "ok": True,
                            "service_name": "content_extraction",
                            "port": args.port,
                            "source_mode": args.source_mode,
                            "runtime_version": "test-runtime-v1",
                        }).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                HTTPServer((args.host, args.port), Handler).serve_forever()
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        inspector = module.NativeProcessInspector()
        first_pid = None
        second_pid = None
        try:
            started = module.start_service(
                config, inspector=inspector, ready_timeout=10
            )
            self.assertTrue(started.ok, started)
            first_pid = started.pid
            self.assertTrue(module.probe_health(config)["ok"])
            first_environment = json.loads(
                (self.root / f"environment-observed-{first_pid}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                first_environment,
                {
                    "accurate_transcript_enabled": "true",
                    "billing_mode": "FREE_ONLY",
                    "spark_free_only_allowed": "true",
                    "spark_credential_present": True,
                },
            )

            duplicate = module.start_service(
                config, inspector=inspector, ready_timeout=10
            )
            self.assertEqual(duplicate.status, "ALREADY_RUNNING")
            self.assertEqual(duplicate.pid, first_pid)

            restarted = module.restart_service(
                config, inspector=inspector
            )
            self.assertTrue(restarted.ok, restarted)
            second_pid = restarted.pid
            self.assertEqual(restarted.old_pid, first_pid)
            self.assertNotEqual(second_pid, first_pid)
            self.assertFalse(inspector.pid_alive(first_pid))
            self.assertTrue(module.probe_health(config)["ok"])
            second_environment = json.loads(
                (self.root / f"environment-observed-{second_pid}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(second_environment, first_environment)

            stopped = module.stop_service(config, inspector=inspector)
            self.assertTrue(stopped.ok, stopped)
            self.assertEqual(stopped.pid, second_pid)
            self.assertFalse(inspector.pid_alive(second_pid))
            self.assertEqual(inspector.listener_pids(port), [])
        finally:
            for pid in (second_pid, first_pid):
                if pid and inspector.pid_alive(pid):
                    inspector.terminate(pid)


if __name__ == "__main__":
    unittest.main()

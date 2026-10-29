"""Lifecycle wrapper for the pinned, offline, loopback-only llama.cpp server."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen


@dataclass(frozen=True)
class LlamaRuntimeConfig:
    runtime_root: Path
    binary_path: Path
    model_path: Path
    host: str = "127.0.0.1"
    port: int = 18091
    threads: int = 8
    context_size: int = 2048
    gpu_layers: int = 99
    startup_timeout_seconds: int = 120
    shutdown_timeout_seconds: int = 30

    @property
    def log_path(self):
        return self.runtime_root / "logs" / "phase1b-llama-server.log"


class LlamaCppRuntime:
    def __init__(self, config, *, health_check=None, popen_factory=None, sleep=None):
        self.config = config
        self._health_check = health_check or self._default_health_check
        self._popen = popen_factory or subprocess.Popen
        self._sleep = sleep or time.sleep
        self._process = None
        self._log_handle = None
        self.metrics = {
            "pid": None, "startup_ms": None, "shutdown_clean": None,
            "exit_code": None,
            "network_mode": "loopback_only_offline", "external_network_request_count": 0,
        }

    @property
    def endpoint(self):
        return f"http://{self.config.host}:{self.config.port}"

    def command(self):
        return [
            str(self.config.binary_path), "--model", str(self.config.model_path),
            "--host", self.config.host, "--port", str(self.config.port),
            "--offline", "--no-ui", "--parallel", "1", "--ctx-size",
            str(self.config.context_size), "--threads", str(self.config.threads),
            "--n-gpu-layers", str(self.config.gpu_layers),
            "--reasoning", "off", "--slots",
        ]

    @staticmethod
    def _default_health_check(url):
        try:
            with urlopen(url + "/health", timeout=2) as response:
                return response.status == 200
        except OSError:
            return False

    def _validate(self):
        if self.config.host != "127.0.0.1":
            raise RuntimeError("llama_runtime_not_loopback")
        if not self.config.binary_path.is_file() or not os.access(self.config.binary_path, os.X_OK):
            raise RuntimeError("llama_binary_missing")
        if not self.config.model_path.is_file():
            raise RuntimeError("model_missing")
    def start(self):
        self._validate()
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.config.log_path.open("ab")
        allowed_env = ("PATH", "TMPDIR", "LANG", "LC_ALL", "DYLD_LIBRARY_PATH")
        environment = {key: os.environ[key] for key in allowed_env if key in os.environ}
        environment["NO_PROXY"] = "127.0.0.1"
        started = time.perf_counter()
        self._process = self._popen(
            self.command(), cwd=str(self.config.runtime_root), env=environment,
            stdin=subprocess.DEVNULL, stdout=self._log_handle, stderr=subprocess.STDOUT,
        )
        self.metrics["pid"] = self._process.pid
        checks = max(1, int(self.config.startup_timeout_seconds * 10))
        for _ in range(checks):
            if self._process.poll() is not None:
                self.stop()
                raise RuntimeError("llama_runtime_exited_during_startup")
            if self._health_check(self.endpoint):
                self.metrics["startup_ms"] = (time.perf_counter() - started) * 1000.0
                return self
            self._sleep(0.1)
        self.stop()
        raise RuntimeError("llama_runtime_startup_timeout")

    def stop(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                exit_code = self._process.wait(timeout=self.config.shutdown_timeout_seconds)
                self.metrics["exit_code"] = exit_code
                self.metrics["shutdown_clean"] = exit_code == 0
            except subprocess.TimeoutExpired:
                self.metrics["shutdown_clean"] = False
                self._process.kill()
                try:
                    self.metrics["exit_code"] = self._process.wait(
                        timeout=self.config.shutdown_timeout_seconds
                    )
                except subprocess.TimeoutExpired:
                    self.metrics["exit_code"] = None
        elif self._process is not None:
            self.metrics["exit_code"] = self._process.returncode
            self.metrics["shutdown_clean"] = self._process.returncode == 0
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False

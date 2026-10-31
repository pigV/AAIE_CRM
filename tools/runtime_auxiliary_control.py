"""Explicit lifecycle for Content Extraction and managed or external Agent Reach.

Dependency manifest is local runtime configuration, never a production .env.
Managed workers write only to their runtime. Independent Agent retains its own policy.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError, canonical
from tools import runtime_control as control

ROLES = ("extraction", "agent-reach")


def dependencies(config):
    config.check_current()
    path = canonical(config.root / "config/auxiliary.json")
    raw = path.read_bytes()
    values = json.loads(raw)
    names = {"extraction_packages", "agent_packages", "ffmpeg", "agent_source"}
    optional = {"agent_management"}
    if not names.issubset(values) or set(values) - names - optional:
        raise RuntimeConfigurationError("Invalid auxiliary dependency manifest")
    management = values.get("agent_management", "managed")
    if management not in ("managed", "external") or (management == "external" and config.mode != "production"):
        raise RuntimeConfigurationError("Invalid Agent ownership")
    if config.mode == 'production' and management != 'external':
        raise RuntimeConfigurationError('Production Agent must remain independently managed')
    result = {key: canonical(values[key]) for key in names}
    if not result["agent_source"].is_relative_to(config.root):
        raise RuntimeConfigurationError("Agent source must be an isolated runtime copy")
    for key, value in result.items():
        if not (value.is_file() if key == "ffmpeg" else value.is_dir()):
            raise RuntimeConfigurationError("Missing auxiliary dependency:" + key)
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    result["agent_management"] = management
    return result


def managed_roles(config):
    return ("extraction",) if dependencies(config)["agent_management"] == "external" else ROLES


def external_agent_health(config):
    """Verify a pinned, independent backend. Never start or signal its process."""
    value = json.loads(canonical(config.root / "config/external-agent.json").read_text())
    if set(value) != {"pid", "started", "command"} or type(value["pid"]) is not int or value["pid"] <= 1:
        raise RuntimeConfigurationError("Invalid external Agent identity")
    if config.mode != "production" or config.agent_reach_port != 8790:
        raise RuntimeConfigurationError("External Agent requires production")
    if (control.observe(value["pid"], "command") != value["command"]
            or control.observe(value["pid"], "lstart") != value["started"]):
        raise control.RuntimeControlError("Independent Agent identity changed")
    owned = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(value["pid"]),
                            "-iTCP:8790", "-sTCP:LISTEN"],capture_output=True,text=True)
    if owned.returncode or "127.0.0.1:8790" not in owned.stdout:
        raise control.RuntimeControlError("Independent Agent does not own listener")
    value_health = read_json("http://127.0.0.1:8790/healthz")
    if value_health.get("status") != "ok" or value_health.get("database") != "ready":
        raise control.RuntimeControlError("Independent Agent database unhealthy")
    return dict(role="agent-reach",pid=value["pid"],port=8790,healthy=True,ownership="external")


def port(config, role):
    if role not in ROLES:
        raise ValueError("Unknown auxiliary service")
    return config.extraction_port if role == "extraction" else config.agent_reach_port


def process_command(config, role):
    port(config, role)
    deps = dependencies(config)
    return [control.PYTHON, "-B", str(config.source_root / "tools/runtime_auxiliary_server.py"),
            "--config", str(config.path), "--service", role,
            "--dependencies-sha256", deps["sha256"]]


def environment(config, role, deps):
    env = control.environment(config)
    # Auxiliary processes use their own entrypoints and OS sandbox, not the
    # legacy staging audit hook that assumes an Admin/Parent process.
    env.pop("AAIE_CRM_ENV")
    env.pop("AAIE_CRM_RUNTIME_CONFIG")
    env.update({
        "HOME": str(config.root / "auxiliary" / role / "home"),
        "AAIE_CRM_WORKSPACE": str(config.root),
        "AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH": str(deps["ffmpeg"]),
        "PADDLE_PDX_CACHE_HOME": str(config.root / "auxiliary/ocr-models"),
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "PADDLE_PDX_MODEL_SOURCE": "BOS", "BILLING_MODE": "FREE_ONLY",
        "AAIE_CRM_ACCURATE_TRANSCRIPT_ENABLED": "false",
        "AAIE_CRM_CONTENT_EXTRACTION_EASYOCR_ENABLED": "false",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1", "MPLCONFIGDIR": str(config.root / "cache/matplotlib"),
    })
    return env


def sandbox_profile(config, role, deps):
    q = lambda p: json.dumps(str(p))
    roots = [config.source_root, config.root, Path(control.PYTHON).parents[2],
             deps["extraction_packages"] if role == "extraction" else deps["agent_packages"]]
    files = [deps["ffmpeg"]] if role == "extraction" else []
    # Paddle's macOS CPU-feature probe uses this fixed shell pipeline.
    probes = [Path('/bin/sh'), Path('/bin/bash'), Path('/usr/sbin/sysctl'), Path('/usr/bin/grep')] if role == "extraction" else []
    # Manifest validation stats every dependency, but file contents remain
    # readable only for this role's actual dependency set.
    metadata = roots + files + [deps[k] for k in
                               ("extraction_packages", "agent_packages", "ffmpeg", "agent_source")]
    return "\n".join([
        "(version 1)", "(allow default)", "(deny file-write*)",
        f'(allow file-write* (subpath {q(config.root)}) (literal "/dev/null"))',
        '(deny file-read* (subpath "/Users") (subpath "/Volumes"))',
        *[f"(allow file-read* (subpath {q(p)}))" for p in roots],
        *[f"(allow file-read* (literal {q(p)}))" for p in files],
        *[f"(allow file-read-metadata (literal {q(p)}))"
          for p in sorted({p for root in metadata for p in (root, *root.parents)})],
        "(deny network-outbound)", "(deny network-bind)", "(deny network-inbound)",
        f'(allow network-bind network-inbound (local ip "localhost:{port(config, role)}"))',
        "(deny signal)", "(deny process-exec)",
        "(allow signal (target same-sandbox))",
        *[f"(allow process-exec (literal {q(p)}))"
          for p in [Path(control.PYTHON), Path(control.PYTHON).resolve(), *files, *probes]],
    ]) + "\n"


def read_json(url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=3) as response:
        return json.load(response)


def health(config, role, record):
    base = f"http://127.0.0.1:{port(config, role)}"
    if role == "extraction":
        public = read_json(base + "/lifecycle-health")
        expected = dict(ok=True, source_mode="production" if config.mode == "production" else "test", port=config.extraction_port,
                        service_name="content_extraction", runtime_version=config.mode + "-" + config.config_sha256)
        if any(public.get(k) != v for k, v in expected.items()):
            raise control.RuntimeControlError("Extraction listener identity mismatch")
        token = canonical(config.root / "cache/content_extractions/v2/service.token").read_text().strip()
        private = read_json(base + "/health", {"X-AAIE-Extraction-Token": token})
        if private.get("status") != "ok" or private.get("workspace") != str(config.root):
            raise control.RuntimeControlError("Extraction workspace mismatch")
    else:
        public = read_json(base + "/runtime-health")
        expected = dict(status="ok", pid=record["pid"], data_root=str(config.root),
                        config_sha256=config.config_sha256, scheduler_enabled=False,
                        port=config.agent_reach_port)
        if any(public.get(k) != v for k, v in expected.items()):
            raise control.RuntimeControlError("Agent listener identity mismatch")
    # sandbox-exec may still be replacing itself immediately after Popen.
    # Only classify the final command once its HTTP listener is ready.
    if not control.verify_process(config, role, record):
        raise control.RuntimeControlError("Auxiliary process exited")
    return dict(role=role, pid=record["pid"], port=port(config, role), healthy=True)


def status(config):
    dependencies(config)
    services = []
    for role in managed_roles(config):
        try:
            record = json.loads(control.state_path(config, role).read_text())
            services.append(health(config, role, record))
        except (OSError, ValueError, control.RuntimeControlError) as exc:
            services.append(dict(role=role, healthy=False, error=str(exc)))
    if dependencies(config)["agent_management"] == "external":
        try:
            services.append(external_agent_health(config))
        except (OSError,ValueError,control.RuntimeControlError,RuntimeConfigurationError) as exc:
            services.append(dict(role="agent-reach",healthy=False,error=str(exc),ownership="external"))
    return dict(ready=all(item["healthy"] for item in services), services=services,
                data_root=str(config.root), managed_worker_network="disabled",
                managed_worker_scheduler="disabled")


def start(config):
    deps = dependencies(config)
    roles = managed_roles(config)
    if deps["agent_management"] == "external":
        external_agent_health(config)
    for role in roles:
        if control.state_path(config, role).exists():
            raise control.RuntimeControlError("Saved auxiliary PID exists; inspect status before start")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port(config, role)))
    started = []
    try:
        for role in roles:
            folder = canonical(config.root / "auxiliary" / role / "home")
            folder.mkdir(parents=True, exist_ok=True)
            profile = canonical(config.root / "config" / (role + ".sb"))
            with control.runtime_file(config, profile, "w") as stream:
                stream.write(sandbox_profile(config, role, deps))
            with control.runtime_file(config, config.root / "logs" / (role + ".log"), "ab") as log:
                proc = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(profile),
                                         *process_command(config, role)],
                                        cwd=config.root, env=environment(config, role, deps),
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True)
            started.append((role, proc))
            birth = control.observe(proc.pid, "lstart")
            if not birth:
                raise control.RuntimeControlError(role + " exited during startup")
            record = dict(pid=proc.pid, started=birth, role=role, data_root=str(config.root),
                          config_sha256=config.config_sha256)
            with control.runtime_file(config, control.state_path(config, role), "x") as stream:
                json.dump(record, stream)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise control.RuntimeControlError(role + " exited; inspect runtime log")
                try:
                    health(config, role, record)
                    break
                except (OSError, ValueError):
                    time.sleep(.5)
            else:
                raise control.RuntimeControlError(role + " startup health timeout")
        result = status(config)
        if not result["ready"]:
            raise control.RuntimeControlError("Auxiliary final health failed")
        return result
    except BaseException:
        errors = control.cleanup_started(config, started, False, None, process_groups=True)
        if errors:
            raise control.RuntimeControlError("Auxiliary cleanup incomplete:" + ",".join(errors))
        raise


def stop(config):
    dependencies(config)
    roles = managed_roles(config)
    for role in roles:
        path = control.state_path(config, role)
        if path.exists():
            control.verify_process(config, role, json.loads(path.read_text()))
    for role in reversed(roles):
        control.stop_one(config, role, process_group=True)
    return {"state": "stopped", "data_root": str(config.root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--allow-production-control", action="store_true")
    args = parser.parse_args()
    config = RuntimeConfiguration.load(args.config, source_root=ROOT)
    dependencies(config)
    if args.action == "status":
        result = status(config)
    else:
        control.require_control_authority(config,args.allow_production_control)
        with control.lifecycle_lock(config):
            result = start(config) if args.action == "start" else stop(config)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

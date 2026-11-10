"""Explicit, sandboxed Admin/Parent lifecycle for separated code and data."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import pwd
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError, canonical, preflight
from tools.runtime_invoice_mail_config import invoice_pdf_environment, load_mail_config
from revenue.school_contract_storage import contract_pdf_environment
from staging_environment import ManagedRuntime, IsolationError, SCHOOL_REPORT_PDFUNITE
from tools import school_map_tile_control, build_document_ocr
from wechat_runtime import (SCHOOL_RADIUS_GEOCODING_ENV, runtime_wechat_environment,
                            runtime_wechat_network_rules)
from school_report_browser import sandbox_rules as school_report_browser_sandbox_rules

PYTHON = "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
ROLES = ("admin", "parent")


class RuntimeControlError(RuntimeError):
    pass


def require_control_authority(config, allow_production):
    if config.mode == "production" and not allow_production:
        raise RuntimeControlError("Production start/stop requires --allow-production-control")


def require_gate(config):
    if config.mode == 'production':
        from business_write_gate import Gate
        state = Gate(config.db, config.root/'config/business-write-gate').status()
        if state['state'] not in ('OPEN', 'CLOSED'):
            raise RuntimeControlError('Production business write gate is not provisioned')
        return state


def process_command(config, role):
    if role=="invoice-mail":
        return [PYTHON,"-B",str(config.source_root/"tools/runtime_invoice_mail_worker.py"),
                "--config",str(config.path)]
    if role in ("extraction", "agent-reach"):
        from tools.runtime_auxiliary_control import process_command as auxiliary_command
        return auxiliary_command(config, role)
    if role == "map":
        return [PYTHON, "-B", str(config.source_root / "school_map_tiles.py"),
                "--root", str(config.root / "map_tile_service"),
                "--port", str(config.map_port), "--runtime-config", str(config.path)]
    return [PYTHON, "-B", str(config.source_root / "tools/runtime_server.py"),
            "--config", str(config.path), "--service", role]


def validate_record(config, role, record):
    if (type(record.get("pid")) is not int or record["pid"] <= 1
            or record.get("role") != role
            or record.get("data_root") != str(config.root)
            or record.get("config_sha256") != config.config_sha256
            or not isinstance(record.get("started"), str) or not record["started"]):
        raise RuntimeControlError("PID identity mismatch; no signal sent")


def validate_process_identity(config, role, record, observed, started):
    validate_record(config, role, record)
    if not started or record["started"] != started:
        raise RuntimeControlError("PID birth mismatch; no signal sent")
    expected = " ".join(process_command(config, role))
    if observed != expected:
        raise RuntimeControlError("PID command mismatch; no signal sent")


def observe(pid, column):
    result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", column + "="],
                            capture_output=True, text=True, check=False)
    if result.returncode and (result.returncode != 1 or result.stderr.strip()):
        raise RuntimeControlError("Process inspection failed; no lifecycle action permitted")
    return result.stdout.strip()


def state_path(config, role):
    if role == "map":
        return canonical(config.root / "map_tile_service/pid.json")
    return canonical(config.root / "config" / (role + ".pid.json"))


def verify_process(config, role, record):
    validate_record(config, role, record)
    observed = observe(record["pid"], "command")
    started = observe(record["pid"], "lstart")
    if not observed and not started:
        return False
    validate_process_identity(config, role, record, observed, started)
    return True


@contextmanager
def runtime_file(config, path, mode):
    """Validate the target itself before opening any controller-owned writer."""
    path = canonical(path)
    if not path.is_relative_to(config.root):
        raise RuntimeControlError("Controller write outside runtime")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    if mode.startswith("a"):
        flags |= os.O_APPEND
    if mode.startswith("x"):
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeControlError("Controller target must be an unaliased regular file")
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeControlError("Controller target changed while opening")
        # On macOS, additionally check where the opened descriptor actually lives.
        if sys.platform == "darwin":
            actual = Path(os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(bytes([0]), 1)[0]))
            if canonical(actual) != path:
                raise RuntimeControlError("Controller descriptor path mismatch")
        if mode.startswith("w"):
            os.ftruncate(fd, 0)
        stream = os.fdopen(fd, "ab" if "b" in mode else "w")
        fd = None
        with stream:
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def prepare_managed_map(adapter):
    config = adapter.runtime_configuration
    folder = canonical(config.root / "map_tile_service")
    folder.mkdir(exist_ok=True)
    with runtime_file(config, folder / "sandbox.sb", "w") as stream:
        stream.write(school_map_tile_control.sandbox_profile(adapter))


def start_managed_map(adapter):
    config = adapter.runtime_configuration
    config.check_current()
    prepare_managed_map(adapter)
    state = state_path(config, "map")
    if state.exists():
        raise RuntimeControlError("Saved map state exists; inspect and stop before starting")
    with runtime_file(config, config.root / "map_tile_service/service.log", "ab") as log:
        proc = subprocess.Popen(
            ["/usr/bin/sandbox-exec", "-f", str(config.root / "map_tile_service/sandbox.sb"),
             *process_command(config, "map")],
            cwd=config.root / "map_tile_service",
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    try:
        birth = observe(proc.pid, "lstart")
        if not birth:
            raise RuntimeControlError("Map exited during startup")
        record = dict(pid=proc.pid, started=birth, role="map",
                      data_root=str(config.root), config_sha256=config.config_sha256)
        with runtime_file(config, state, "x") as stream:
            json.dump(record, stream)
        for _ in range(40):
            if proc.poll() is not None:
                raise RuntimeControlError("Map exited; inspect runtime map log")
            try:
                if school_map_tile_control.health(adapter).get("pid") != proc.pid:
                    raise RuntimeControlError("Map listener PID mismatch")
                return
            except (OSError, ValueError):
                time.sleep(.15)
        raise RuntimeControlError("Map startup timed out")
    except BaseException as exc:
        errors = cleanup_started(config, [("map", proc)], False, adapter)
        if errors:
            raise RuntimeControlError("Map startup cleanup incomplete:" + ",".join(errors)) from exc
        raise


def process_group_alive(pgid):
    result = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeControlError("Process group inspection failed")
    return any(len(parts := line.split()) == 2 and parts[0] == str(pgid)
               and not parts[1].startswith("Z") for line in result.stdout.splitlines())


def cleanup_started(config, processes, map_started, adapter, *, process_groups=False):
    """Attempt every owned child; preserve records when cleanup is incomplete."""
    errors = []
    for role, proc in reversed(processes):
        try:
            if proc.poll() is None:
                if process_groups:
                    if os.getpgid(proc.pid) != proc.pid:
                        raise RuntimeControlError("Child process group mismatch")
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=5)
            if process_groups:
                for _ in range(50):
                    if not process_group_alive(proc.pid):
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeControlError("Auxiliary descendants remain; state preserved")
            path = state_path(config, role)
            if path.exists():
                saved = json.loads(path.read_text())
                if saved.get("pid") == proc.pid:
                    path.unlink()
        except Exception as exc:
            errors.append(role + ":" + type(exc).__name__)
    if map_started:
        try:
            school_map_tile_control.stop(adapter)
        except Exception as exc:
            errors.append("map:" + type(exc).__name__)
    return errors


def environment(config, *, include_wechat=True, allow_school_radius=False):
    # A deliberately explicit environment: credentials are not inherited from
    # the terminal or loaded from another deployment's .env file.
    root = config.root
    adapter = ManagedRuntime.from_configuration(config)
    mail = load_mail_config(config)
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"), "TEMP": str(root / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "AAIE_CRM_ENV": config.mode, "AAIE_CRM_RUNTIME_CONFIG": str(config.path),
        "AAIE_CRM_DB_PERF_LOG": "0",
        "AAIE_CRM_CONTENT_EXTRACTION_SERVICE_URL": f"http://127.0.0.1:{config.extraction_port}",
        "AAIE_CRM_BACKUP_DIR": str(root / "backups"),
        "AAIE_CRM_SCHOOL_LIBRARY_DIR": str(adapter.school_library_read_roots[0]
                                            if adapter.school_library_read_roots else root / "school_library_files"),
        "AAIE_CRM_SECURE_ASSET_DIR": str(root / "secure_assets"),
        "AAIE_CRM_CONTENT_REPORT_DIR": str(root / "import_reports"),
        "REVENUE_SECURE_STORAGE_ROOT": str(mail.pdf_root if mail and config.mode=="staging" else root/"revenue_documents"),
        **invoice_pdf_environment(config),
        **contract_pdf_environment(config),
        **invoice_mail_environment(config,mail),
        "XDG_CACHE_HOME": str(root / "cache"), "HF_HOME": str(root / "cache/huggingface"),
        **(runtime_wechat_environment(config) if include_wechat else {}),
        **({SCHOOL_RADIUS_GEOCODING_ENV: "1"} if allow_school_radius else {}),
    }


def invoice_mail_environment(config, mail):
    if mail is None:
        return {}
    result = {"AAIE_INVOICE_MAIL_EXTERNAL":"1"}
    if config.mode=="staging":
        result["AAIE_SCHOOL_COMMISSION_FORMAL_REHEARSAL"]="1"
    if mail.enabled:
        result["AAIE_SCHOOL_INVOICE_MAIL_RELEASE"]="candidate" if config.mode=="staging" else "production"
        result["AAIE_SCHOOL_INVOICE_MAIL_APPROVED_TO"]=mail.recipient
    return result


def invoice_mail_keychain():
    return canonical(Path(pwd.getpwuid(os.geteuid()).pw_dir)/"Library/Keychains/login.keychain-db")


def invoice_mail_profile(config, mail, *, probe=False):
    keychain=invoice_mail_keychain()
    invoice_root=invoice_pdf_environment(config).get("AAIE_INVOICE_PDF_ROOT")
    paths = [config.source_root,config.root,Path(PYTHON).parents[2],mail.pdf_root]
    if invoice_root:
        paths.append(Path(invoice_root))
    quote = lambda path: json.dumps(str(path))
    targets = [str(config.db)+suffix for suffix in ("","-wal","-shm","-journal")]
    return "\n".join([
        "(version 1)", "(allow default)", "(deny file-write*)",
        '(deny file-read* (subpath "/Users") (subpath "/Volumes"))',
        *[f"(allow file-read* (subpath {quote(path)}))" for path in paths],
        *[f"(allow file-read-metadata (literal {quote(path)}))"
          for path in sorted({parent for entry in paths for parent in entry.parents})],
        *[f"(allow file-read* (literal {quote(path)}))" for root in paths[3:] for path in root.parents],
        f"(allow file-read* (literal {quote(keychain)}))",
        *[f"(allow file-read-metadata (literal {quote(path)}))" for path in keychain.parents],
        *([] if probe else [
            *[f"(allow file-write* (literal {quote(path)}))" for path in targets],
            f"(allow file-write* (subpath {quote(config.root/'logs')}))",
        ]),
        '(allow file-write* (literal "/dev/null"))',
        "(deny network-outbound network-inbound network-bind)",
        '(allow network-outbound (remote tcp "*:465"))',
        "(deny process-exec)",
        f'(allow process-exec (literal {quote(PYTHON)}) (literal {quote(Path(PYTHON).resolve())}) (literal "/usr/bin/security"))',
    ])+"\n"


def run_invoice_mail(config, *, probe=False):
    mail = load_mail_config(config)
    if mail is None or (not probe and not mail.enabled):
        return {"ready":False,"invoice_mail":"configuration_missing_invalid_or_disabled"}
    if not probe:
        config.require_ready()
    if not probe and state_path(config,"invoice-mail").exists():
        raise RuntimeControlError("Invoice worker PID exists; inspect status before start")
    profile=config.root/"config"/("invoice-mail-probe.sb" if probe else "invoice-mail.sb")
    with runtime_file(config,profile,"w") as stream:
        stream.write(invoice_mail_profile(config,mail,probe=probe))
    address = socket.getaddrinfo("smtphz.qiye.163.com",465,socket.AF_INET,socket.SOCK_STREAM)[0][4][0]
    env={**environment(config,include_wechat=False),"AAIE_MANAGED_SMTP_ADDRESS":address,
         "AAIE_MANAGED_SMTP_KEYCHAIN":str(invoice_mail_keychain())}
    command=["/usr/bin/sandbox-exec","-f",str(profile),*process_command(config,"invoice-mail")]
    if probe:
        result=subprocess.run([*command,"--probe"],cwd=config.source_root,env=env,stdin=subprocess.DEVNULL,
                              capture_output=True,text=True,timeout=115)
        data=json.loads(result.stdout.splitlines()[-1]) if result.stdout.strip().startswith("{") else {}
        return {"ready":result.returncode==0 and data.get("smtp_login") is True,**data,
                "invoice_mail":"probe","diagnostic":result.stdout.strip() if not data else None}
    with runtime_file(config,config.root/"logs/invoice-mail.log","ab") as log:
        process=subprocess.Popen(command,cwd=config.source_root,env=env,stdin=subprocess.DEVNULL,
                                 stdout=log,stderr=log,start_new_session=True)
    try:
        birth=observe(process.pid,"lstart")
        if not birth or process.poll() is not None:
            raise RuntimeControlError("Invoice worker exited during startup")
        with runtime_file(config,state_path(config,"invoice-mail"),"x") as stream:
            json.dump(dict(pid=process.pid,started=birth,role="invoice-mail",
                           config_sha256=config.config_sha256,data_root=str(config.root)),stream)
    except BaseException:
        process.terminate()
        process.wait(timeout=8)
        raise
    return {"invoice_mail":"started","pid":process.pid}


def invoice_mail_status(config):
    path=state_path(config,"invoice-mail")
    if not path.exists():
        return {"invoice_mail":"stopped"}
    record=json.loads(path.read_text())
    if not verify_process(config,"invoice-mail",record):
        return {"invoice_mail":"exited","pid":record["pid"]}
    mail=load_mail_config(config)
    if mail is None or not mail.enabled:
        return {"invoice_mail":"stopping","pid":record["pid"]}
    log=config.root/"logs/invoice-mail.log"
    try:
        with log.open("rb") as stream:
            stream.seek(max(0,log.stat().st_size-8192))
            entries=stream.read(8192).splitlines()
    except OSError:
        entries=[]
    for line in reversed(entries):
        try:
            item=json.loads(line)
        except (ValueError,UnicodeError):
            continue
        if isinstance(item,dict) and item.get("pid")==record["pid"] and item.get("smtp_login") is True:
            return {"invoice_mail":"running","pid":record["pid"],"smtp_login":True}
    return {"invoice_mail":"starting","pid":record["pid"]}


def sandbox_profile(config, *, allow_school_radius=False):
    q = lambda p: json.dumps(str(p))
    adapter = ManagedRuntime.from_configuration(config)
    # Production grants are explicit files in its data root. Rehearsal may read
    # approved NAS attachments but cannot write NAS or expose a LAN listener.
    if config.mode != "production" and (adapter.nas_write_roots or adapter.admin_lan_ip):
        raise RuntimeControlError("Staging cannot inherit production NAS write/LAN access")
    mail = load_mail_config(config)
    pdf_config = invoice_pdf_environment(config)
    invoice_root = pdf_config.get("AAIE_INVOICE_PDF_ROOT")
    pdf_paths = [mail.pdf_root] if mail else []
    contract_root = contract_pdf_environment(config).get("AAIE_SCHOOL_CONTRACT_PDF_ROOT")
    pdf_paths.extend(Path(path) for path in (invoice_root, contract_root) if path)
    pdf = tuple(dict.fromkeys(pdf_paths))
    keychain = invoice_mail_keychain() if invoice_root or contract_root else None
    roots = (config.source_root, config.root, Path(PYTHON).parents[2], *adapter.read_roots, *pdf)
    return "\n".join([
        "(version 1)", "(allow default)", "(deny file-write*)",
        f'(allow file-write* (subpath {q(config.root)}) (literal "/dev/null"))',
        '(deny file-read* (subpath "/Users") (subpath "/Volumes"))',
        *[f"(allow file-read* (subpath {q(p)}))" for p in roots],
        *[f"(allow file-write* (subpath {q(p)}))" for p in (*adapter.nas_write_roots,*pdf)],
        *[f"(allow file-read-metadata (literal {q(p)}))"
          for p in sorted({p for root in roots for p in root.parents})],
        *[f"(allow file-read* (literal {q(parent)}))" for path in pdf for parent in path.parents],
        *([f"(allow file-read* (literal {q(keychain)}))",
           *[f"(allow file-read-metadata (literal {q(parent)}))" for parent in keychain.parents]]
          if keychain else []),
        "(deny network-outbound)", "(deny network-bind)", "(deny network-inbound)",
        *[f'(allow network-bind network-inbound (local ip "localhost:{p}"))'
          for p in (config.admin_port, config.parent_port)],
        *([f'(allow network-bind network-inbound (local ip "*:{config.admin_port}"))']
          if adapter.admin_lan_ip else []),
        *[f'(allow network-outbound (remote ip "localhost:{p}"))' for p in config.ports],
        *runtime_wechat_network_rules(config, allow_school_radius=allow_school_radius),
        "(allow process-fork)", "(deny signal)", "(deny process-exec)",
        f"(allow process-exec (literal {q(PYTHON)}) (literal {q(Path(PYTHON).resolve())}))",
        f'(allow process-exec (literal {q(config.root / "tools/document-vision-ocr")}))',
        *school_report_browser_sandbox_rules(config.root / "tmp"),
        *([f"(allow process-exec (literal {q(SCHOOL_REPORT_PDFUNITE)}))"] if adapter.school_library_read_roots else []),
        *(['(allow process-exec (literal "/usr/bin/security"))'] if keychain else []),
        *(['(allow process-exec (literal "/sbin/mount"))'] if config.mode == "production" else []),
    ]) + "\n"


@contextmanager
def lifecycle_lock(config):
    config.check_current()
    path = canonical(config.root / "config/lifecycle.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise RuntimeControlError("Hardlinked lifecycle lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeControlError("Another lifecycle operation is in progress") from exc
        yield
    finally:
        os.close(fd)


def health(config, role, pid):
    port = config.admin_port if role == "admin" else config.parent_port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
        value = json.load(response)
    expected = {"status": "ok", "environment": config.mode, "service": role, "port": port,
                "pid": pid, "database_path": str(config.db),
                "storage_path": str(config.root / "storage"),
                "source_root": str(config.source_root), "config_sha256": config.config_sha256}
    if any(value.get(k) != v for k, v in expected.items()):
        raise RuntimeControlError("Listener identity mismatch")
    if value.get("dependencies", {}).get("database") != "ok":
        raise RuntimeControlError("Database health failed")
    if config.mode == 'production' and value.get('business_write_gate', {}).get('state') not in ('OPEN', 'CLOSED'):
        raise RuntimeControlError('Production business write gate is unknown')
    return {"role": role, "pid": pid, "port": port, "healthy": True}


def stop_one(config, role, *, process_group=False):
    path = state_path(config, role)
    if not path.exists():
        return
    record = json.loads(path.read_text())
    if not verify_process(config, role, record):
        if process_group and process_group_alive(record["pid"]):
            raise RuntimeControlError("Leader exited with descendants; state preserved")
        path.unlink()  # Matching record, and the OS confirms there is no process.
        return
    try:
        if process_group:
            if os.getpgid(record["pid"]) != record["pid"]:
                raise RuntimeControlError("Process group mismatch; no signal sent")
            os.killpg(record["pid"], signal.SIGTERM)
        else:
            os.kill(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        if process_group and process_group_alive(record["pid"]):
            raise RuntimeControlError("Leader exited with descendants; state preserved")
        path.unlink()
        return
    for _ in range(50):
        state = observe(record["pid"], "stat")
        if not state or state.startswith("Z"):
            if process_group and process_group_alive(record["pid"]):
                time.sleep(.1)
                continue
            path.unlink()
            return
        # Exit may occur between ps calls. Do not re-check a disappearing
        # command line after signaling; compare birth before any further action.
        birth = observe(record["pid"], "lstart")
        if not birth:
            if process_group and process_group_alive(record["pid"]):
                time.sleep(.1)
                continue
            path.unlink()
            return
        if birth != record["started"]:
            raise RuntimeControlError("PID reused while stopping; state preserved")
        time.sleep(.1)
    raise RuntimeControlError("Service did not stop; no force kill")


def start(config):
    require_gate(config)
    result = preflight(config, check_ports=True, owned_ports_only=True)
    if not result["ready"]:
        raise RuntimeControlError("runtime_not_ready:" + ",".join(result["errors"]))
    adapter = ManagedRuntime.from_configuration(config)
    if any(state_path(config, role).exists() for role in ROLES):
        raise RuntimeControlError("Saved PID state exists; inspect status before start")
    if canonical(config.root / "map_tile_service/pid.json").exists():
        raise RuntimeControlError("Saved map helper state exists; inspect before start")
    # Managed uploads never compile Swift inside the service sandbox.
    # Prepare the exact helper before starting any managed child.
    try:
        build_document_ocr.build(data_root=config.root, source_root=config.source_root)
    except RuntimeError as exc:
        raise RuntimeControlError(str(exc)) from exc
    profiles = {}
    for role in ROLES:
        name = "runtime-admin.sb" if role == "admin" else "runtime.sb"
        profile = canonical(config.root / "config" / name)
        with runtime_file(config, profile, "w") as stream:
            stream.write(sandbox_profile(config, allow_school_radius=role == "admin"))
        profiles[role] = profile
    started_processes = []
    map_started = False
    try:
        school_map_tile_control.start(adapter)
        map_started = True
        for role in ROLES:
            config.check_current()
            with runtime_file(config, config.root / f"logs/{role}.log", "ab") as log:
                proc = subprocess.Popen(
                    ["/usr/bin/sandbox-exec", "-f", str(profiles[role]), *process_command(config, role)],
                    cwd=config.source_root, env=environment(config, allow_school_radius=role == "admin"),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            # Popen owns this child. Cleanup below never trusts a PID file.
            started_processes.append((role, proc))
            birth = observe(proc.pid, "lstart")
            if not birth:
                raise RuntimeControlError(f"{role} exited; inspect its runtime log")
            record = dict(pid=proc.pid, started=birth, role=role,
                          config_sha256=config.config_sha256, data_root=str(config.root))
            with runtime_file(config, state_path(config, role), "x") as state:
                json.dump(record, state)
            for _ in range(60):
                if proc.poll() is not None:
                    raise RuntimeControlError(f"{role} exited; inspect its runtime log")
                try:
                    health(config, role, proc.pid)
                    break
                except (OSError, ValueError):
                    time.sleep(.25)
            else:
                raise RuntimeControlError(f"{role} health timeout")
        result = status(config)
        if not result["ready"]:
            raise RuntimeControlError("Final service health failed")
        return result
    except BaseException as exc:
        errors = cleanup_started(config, started_processes, map_started, adapter)
        if errors:
            raise RuntimeControlError("Startup cleanup incomplete:" + ",".join(errors)) from exc
        raise


def stop(config):
    # Verify every recorded owner before sending the first signal.
    for role in (*ROLES,"invoice-mail"):
        path = state_path(config, role)
        if path.exists():
            verify_process(config, role, json.loads(path.read_text()))
    adapter = ManagedRuntime.from_configuration(config)
    map_state = canonical(config.root / "map_tile_service/pid.json")
    if map_state.exists():
        school_map_tile_control.verify_process(adapter, json.loads(map_state.read_text()))
    if state_path(config,"invoice-mail").exists():
        stop_one(config,"invoice-mail",process_group=True)
    for role in reversed(ROLES):
        stop_one(config, role)
    school_map_tile_control.stop(adapter)
    return {"state": "stopped", "data_root": str(config.root)}


def status(config):
    services = []
    for role in ROLES:
        try:
            record = json.loads(state_path(config, role).read_text())
            if not verify_process(config, role, record):
                raise RuntimeControlError("Recorded process has exited")
            services.append(health(config, role, record["pid"]))
        except (OSError, ValueError, RuntimeControlError) as exc:
            services.append({"role": role, "healthy": False, "error": type(exc).__name__})
    try:
        adapter = ManagedRuntime.from_configuration(config)
        map_state = canonical(config.root / "map_tile_service/pid.json")
        record = json.loads(map_state.read_text())
        if not school_map_tile_control.verify_process(adapter, record):
            raise RuntimeControlError("Recorded map process has exited")
        value = school_map_tile_control.health(adapter)
        if value.get("pid") != record["pid"]:
            raise RuntimeControlError("Map listener PID mismatch")
        services.append({"role": "map", "healthy": True, "pid": record["pid"]})
    except (OSError, ValueError, RuntimeControlError, IsolationError, subprocess.SubprocessError) as exc:
        services.append({"role": "map", "healthy": False, "error": type(exc).__name__})
    return {"ready": all(s["healthy"] for s in services), "services": services,
            "data_root": str(config.root), "source_root": str(config.source_root),
            "extraction": "not_controlled_by_this_entrypoint",
            "external_integrations": "not_validated"}


def source_identity(config):
    """Read Git identity without refreshing, fetching, or modifying the checkout."""
    revision = subprocess.run(["/usr/bin/git", "-C", str(config.source_root),
                               "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    status_result = subprocess.run(["/usr/bin/git", "--no-optional-locks", "-C", str(config.source_root),
                                    "status", "--porcelain", "--untracked-files=normal"],
                                   capture_output=True, text=True, timeout=10)
    return {"git_head": revision.stdout.strip() if revision.returncode == 0 else None,
            "working_tree_modified": bool(status_result.stdout.strip()) if status_result.returncode == 0 else None,
            "git_inspection_warning": bool(revision.stderr.strip() or status_result.stderr.strip()),
            "release_approval": "NOT_GRANTED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight","start","status","stop","mail-probe","mail-start","mail-status","mail-stop"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--allow-production-control", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = RuntimeConfiguration.load(args.config, source_root=ROOT)
        if args.action == "preflight":
            result = preflight(config, check_ports=True)
            result.update(source_identity(config))
            result["scope"] = "Admin/Parent/map path readiness; not production release approval"
            result["extraction"] = "separately_managed_not_validated"
            sandbox_profile(config)
        elif args.action == "status":
            result = status(config)
        elif args.action == "mail-status":
            result = invoice_mail_status(config)
        else:
            require_control_authority(config, args.allow_production_control)
            with lifecycle_lock(config):
                if args.action in {"mail-probe","mail-start"}:
                    result=run_invoice_mail(config,probe=args.action=="mail-probe")
                elif args.action=="mail-stop":
                    stop_one(config,"invoice-mail",process_group=True)
                    result=invoice_mail_status(config)
                else:
                    result = start(config) if args.action == "start" else stop(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ready", True) else 1
    except (RuntimeConfigurationError, RuntimeControlError, IsolationError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.modules["tools.runtime_control"] = sys.modules[__name__]
    raise SystemExit(main())

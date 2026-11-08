"""Managed invoice queue worker and a TLS/SMTP AUTH-only diagnostic."""
import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import smtplib
import socket
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.runtime_invoice_mail_config import load_mail_config
from revenue.school_invoice_release import SENDER
from revenue.school_invoice_smtp import RehearsalSMTP


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True)
    parser.add_argument("--probe",action="store_true")
    args=parser.parse_args()
    if os.environ.get("AAIE_CRM_RUNTIME_CONFIG")!=args.config:
        print("invoice_mail_stopped: configuration identity mismatch",flush=True)
        return 2
    from runtime_configuration import RuntimeConfiguration
    config = RuntimeConfiguration.load(args.config,source_root=Path(__file__).resolve().parents[1])
    if config.mode!=os.environ.get("AAIE_CRM_ENV"):
        print("invoice_mail_stopped: runtime mode mismatch",flush=True)
        return 2
    settings = load_mail_config(config)
    root_variable = "AAIE_INVOICE_PDF_ROOT" if config.mode=="production" else "REVENUE_SECURE_STORAGE_ROOT"
    if settings is None or (not args.probe and not settings.enabled) or str(settings.pdf_root)!=os.environ.get(root_variable):
        print("invoice_mail_stopped: configuration unavailable",flush=True)
        return 2
    if args.probe:
        try:
            descriptor=os.open(config.source_root/"app.py",os.O_WRONLY|os.O_NOFOLLOW)
        except PermissionError:
            pass
        else:
            os.close(descriptor)
            print("invoice_mail_stopped: source write confinement unavailable",flush=True)
            return 2
    else:
        from staging_environment import ACTIVE
        if getattr(ACTIVE,"runtime_configuration",None)!=config:
            return 2
    if not args.probe:
        config.require_ready()
        import app
    original=socket.getaddrinfo
    address=os.environ["AAIE_MANAGED_SMTP_ADDRESS"]

    def lookup(host,*args,**kwargs):
        if host=="smtphz.qiye.163.com":
            return original(address,*args,**kwargs)
        return original(host,*args,**kwargs)

    socket.getaddrinfo=lookup
    def network_scope(event,args):
        if event=="socket.connect":
            target=args[1]
            if isinstance(target,tuple) and target[:2]!=(address,465):
                raise PermissionError("Invoice SMTP destination outside approved scope")
    sys.addaudithook(network_scope)
    try:
        smtp=RehearsalSMTP(SENDER,settings.recipient,simulation=config.mode=="staging").connect()
        try:
            print(json.dumps({"pid":os.getpid(),"tls":smtp.sock.version(),"smtp_login":True,
                              "probe_only":args.probe}),flush=True)
        finally:
            with suppress(OSError,smtplib.SMTPException):
                smtp.quit()
            smtp.close()
    except (OSError,ValueError,smtplib.SMTPException,subprocess.SubprocessError) as error:
        print(json.dumps({"pid":os.getpid(),"smtp_login":False,"error":type(error).__name__}),flush=True)
        return 2
    if args.probe:
        return 0
    stop=Event()

    def shutdown(_number,_frame):
        stop.set()

    signal.signal(signal.SIGTERM,shutdown)
    def enabled():
        config.check_current()
        return not stop.is_set() and load_mail_config(config)==settings
    app.ReusableThreadingHTTPServer._run_invoice_mail(
        SimpleNamespace(_invoice_mail_stop=stop),configuration_valid=enabled,
        approved_pdf_root=settings.pdf_root,
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())

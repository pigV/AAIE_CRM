"""TLS transport for the opt-in school invoice SMTP rehearsal."""

from contextlib import suppress
from email import policy
from email.parser import BytesParser
from functools import lru_cache
import os
import smtplib
import ssl
import subprocess

from revenue.school_invoice_release import FINANCE_COPY_ADDRESS, payer_copy_addresses, payer_mail_addresses
from revenue.school_invoice_reminders import EMAIL


@lru_cache(maxsize=1)
def _keychain_password(sender, service="AAIE CRM Staging SMTP"):
    keychain = os.environ.get("AAIE_MANAGED_SMTP_KEYCHAIN")
    command = ["/usr/bin/security","find-generic-password","-s",service,"-a",sender,"-w"]
    if keychain:
        command.append(keychain)
    return subprocess.run(
        command,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=90,
    ).stdout.decode().rstrip("\r\n")


class RehearsalSMTP:
    def __init__(self, sender, recipient, limit=3, claim_sets=(), *, mode="smtp", simulation=True):
        self.sender = sender
        self.recipient = recipient
        self.limit = limit
        self.claim_sets = claim_sets
        self.mode = mode
        self.simulation = simulation
        self.cc = FINANCE_COPY_ADDRESS if mode == "production" else None

    def connect(self):
        password = (_keychain_password(self.sender) if self.simulation else
                    _keychain_password(self.sender,"AAIE CRM Production SMTP"))
        if not password:
            raise ValueError("Missing rehearsal credential")
        smtp = smtplib.SMTP_SSL("smtphz.qiye.163.com", 465, timeout=15, context=ssl.create_default_context())
        try:
            smtp.login(self.sender, password)
        except BaseException:
            with suppress(OSError,smtplib.SMTPException):
                smtp.close()
            _keychain_password.cache_clear()
            raise
        return smtp

    def submit(self, recipient, content, allowed, *, cc=None, bcc=""):
        message = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
        expected_cc = [self.cc] if self.cc else []
        blind_copies = []
        if self.recipient is None and self.mode == "production" and not self.simulation:
            try:
                recipients = payer_mail_addresses(recipient, simulation=False)
                if cc is not None:
                    prefix, _, additional = cc.partition(", ")
                    if prefix != self.cc:
                        raise ValueError("Finance copy missing")
                    copies, blind_copies = payer_copy_addresses(recipient, additional, bcc, simulation=False)
                    expected_cc.extend(copies)
                    if cc != ", ".join(expected_cc) or bcc != ", ".join(blind_copies):
                        raise ValueError("Invalid copy recipients")
                elif bcc:
                    raise ValueError("Missing copy recipients")
            except ValueError:
                return "cancelled", "smtp_recipient_not_allowed", None
        elif (isinstance(recipient, str) and recipient == self.recipient and EMAIL.fullmatch(recipient)
              and (not self.cc or recipient.casefold() != self.cc.casefold())):
            recipients = [recipient]
        else:
            return "cancelled", "smtp_recipient_not_allowed", None
        to_headers = message.get_all("To") or []
        cc_headers = message.get_all("Cc") or []
        cc_matches = (len(cc_headers) == 1 and not cc_headers[0].defects
                      and [address.addr_spec for address in cc_headers[0].addresses] == expected_cc
                      if expected_cc else not cc_headers)
        if (len(to_headers) != 1 or to_headers[0].defects
                or [address.addr_spec for address in to_headers[0].addresses] != recipients
                or message.get_all("From") != [self.sender] or not cc_matches
                or message.get_all("Bcc")):
            return "cancelled", "smtp_recipient_not_allowed", None

        smtp = None
        try:
            if not allowed():
                return "cancelled", "smtp_disabled", None
            smtp = self.connect()
            envelope = [(smtp.mail, self.sender), *((smtp.rcpt, address) for address in recipients)]
            envelope.extend((smtp.rcpt, address) for address in (*expected_cc, *blind_copies))
            for command, address in envelope:
                code, _ = command(address)
                if code not in (250, 251):
                    return "failed", "smtp_rejected", code
            if not allowed():
                return "cancelled", "smtp_disabled", None
            try:
                code, _ = smtp.data(content)
            except smtplib.SMTPDataError as error:
                return "failed", "smtp_rejected", error.smtp_code
            except (OSError, smtplib.SMTPException):
                return "needs_review", "smtp_uncertain", None
            if code == 250:
                return "sent", None, code
            if 400 <= code <= 599:
                return "failed", "smtp_rejected", code
            return "needs_review", "smtp_uncertain", code
        except (OSError, ValueError, smtplib.SMTPException, subprocess.SubprocessError) as error:
            return "failed", "smtp_setup_failed", getattr(error, "smtp_code", None)
        finally:
            if smtp is not None:
                with suppress(OSError,smtplib.SMTPException):
                    smtp.close()

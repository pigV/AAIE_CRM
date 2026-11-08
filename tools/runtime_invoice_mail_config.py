"""Read-only admission for the optional managed invoice worker."""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import stat

from revenue.school_invoice_release import REHEARSAL_RECIPIENT
from revenue.school_invoice_reminders import EMAIL
from revenue.data_invoice_storage import PRODUCTION_ROOT, TEST_ROOT
from runtime_configuration import RuntimeConfigurationError, canonical
from revenue.secure_storage import StorageUnavailable, configured_write_root


@dataclass(frozen=True)
class InvoiceMailConfig:
    enabled: bool
    pdf_root: Path
    recipient: str


def _read_private(path):
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),"rb") as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>4096):
            return None
        return json.loads(stream.read(4097))


def invoice_pdf_environment(config):
    try:
        value=_read_private(config.root/"config/invoice-pdf.json")
        if not isinstance(value,dict) or set(value)!={"root","keychain_item"}:
            return {}
        root=Path(value["root"])
        if canonical(root)!=root or not root.is_dir():
            return {}
        if config.mode=="production":
            valid=root==PRODUCTION_ROOT and value["keychain_item"]=="AAIE CRM Production Invoice PDF"
        else:
            valid=(config.mode=="staging" and root!=TEST_ROOT and root.is_relative_to(TEST_ROOT)
                   and value["keychain_item"]=="AAIE CRM Data Invoice PDF Rehearsal")
        if valid:
            return {"AAIE_INVOICE_PDF_ROOT":str(root),"AAIE_INVOICE_PDF_KEYCHAIN_ITEM":value["keychain_item"]}
    except (OSError,ValueError,TypeError,KeyError,RuntimeConfigurationError):
        pass
    return {}


def load_mail_config(config):
    path = config.root/"config/invoice-mail.json"
    try:
        value=_read_private(path)
        if (not isinstance(value,dict) or set(value)!={"enabled","mode","pdf_root","recipient"}
                or type(value["enabled"]) is not bool or value["mode"]!=config.mode):
            return None
        recipient = value["recipient"]
        if not isinstance(recipient,str) or len(recipient)>254 or not EMAIL.fullmatch(recipient):
            return None
        if config.mode=="staging":
            if recipient!=REHEARSAL_RECIPIENT or not Path(value["pdf_root"]).is_relative_to(config.root):
                return None
        elif config.mode!="production" or recipient.lower()==REHEARSAL_RECIPIENT or recipient.lower().endswith(".test"):
            return None
        if config.mode=="production":
            configured=invoice_pdf_environment(config).get("AAIE_INVOICE_PDF_ROOT")
            if not configured or value["pdf_root"]!=configured:
                return None
            root=Path(configured)
        else:
            root = configured_write_root(config.db.parent/"revenue_documents",
                                         {"REVENUE_SECURE_STORAGE_ROOT":value["pdf_root"]})
        if root==config.root or root.is_relative_to(config.db.parent):
            return None
        return InvoiceMailConfig(value["enabled"],root,recipient)
    except (OSError,ValueError,TypeError,KeyError,StorageUnavailable):
        return None

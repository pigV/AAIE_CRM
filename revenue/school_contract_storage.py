"""Separate Keychain-backed storage authority for signed school contracts."""
import os
from pathlib import Path

from revenue.data_invoice_storage import DATA, keychain_pdf_store
from revenue.secure_storage import StorageUnavailable
from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError, canonical

ROOT_ENV = "AAIE_SCHOOL_CONTRACT_PDF_ROOT"
KEY_ENV = "AAIE_SCHOOL_CONTRACT_PDF_KEYCHAIN_ITEM"
KEYCHAIN_ITEMS = {
    "production": "AAIE CRM Production School Contract PDF",
    "staging": "AAIE CRM Staging School Contract PDF",
}


def contract_pdf_environment(config):
    from tools.runtime_invoice_mail_config import _read_private

    try:
        root = config.root / "school_contract_documents"
        item = KEYCHAIN_ITEMS.get(config.mode)
        descriptor = _read_private(config.root / "config/school-contract-pdf.json")
        if (
            item and root.is_relative_to(DATA) and canonical(root) == root and root.is_dir()
            and descriptor == {"root": str(root), "keychain_item": item}
        ):
            return {ROOT_ENV: str(root), KEY_ENV: item}
    except (OSError, ValueError, TypeError, RuntimeConfigurationError):
        pass
    return {}


def configured_contract_store():
    root, item = os.environ.get(ROOT_ENV), os.environ.get(KEY_ENV)
    runtime_path = os.environ.get("AAIE_CRM_RUNTIME_CONFIG")
    if not root or not item or not runtime_path:
        raise StorageUnavailable()
    try:
        config = RuntimeConfiguration.load(runtime_path, source_root=Path(__file__).resolve().parents[1])
        if (config.mode != os.environ.get("AAIE_CRM_ENV")
                or contract_pdf_environment(config) != {ROOT_ENV: root, KEY_ENV: item}):
            raise StorageUnavailable()
    except (OSError, RuntimeConfigurationError) as error:
        raise StorageUnavailable() from error
    return keychain_pdf_store(Path(root), item)

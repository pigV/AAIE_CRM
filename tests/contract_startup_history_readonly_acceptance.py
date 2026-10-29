#!/usr/bin/env python3
import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import Database


PRODUCTION_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()


def table_snapshot(connection, table):
    return [
        dict(row)
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    ]


def protected_snapshot(database_path):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        payload = {
            table: table_snapshot(connection, table)
            for table in ("contracts", "contract_signing_links", "contract_signatures")
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return payload, hashlib.sha256(serialized).hexdigest()
    finally:
        connection.close()


def seed_cases(database_path):
    connection = sqlite3.connect(database_path)
    try:
        stamp = "2026-09-05T09:45:00+10:00"
        suffix = uuid.uuid4().hex
        ordinary_contract_id = connection.execute(
            """INSERT INTO contracts(
               contract_uid,contract_category,contract_type,contract_title,
               contract_status,service_scope_json,generated_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                f"STARTUP-ORDINARY-{suffix}",
                "留学合同",
                "LocalStudentApplicationContract",
                "启动只读普通合同",
                "Sent",
                "[]",
                stamp,
                stamp,
            ),
        ).lastrowid
        transfer_contract_id = connection.execute(
            """INSERT INTO contracts(
               contract_uid,contract_category,contract_type,contract_title,
               contract_status,service_scope_json,generated_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                f"STARTUP-TRANSFER-{suffix}",
                "转学合同",
                "TransferApplicationContract",
                "启动只读转学合同",
                "FullyExecuted",
                "[]",
                stamp,
                stamp,
            ),
        ).lastrowid
        failed_link_id = connection.execute(
            """INSERT INTO contract_signing_links(
               contract_id,token_hash,token_last_4,signing_url,status,
               expires_at,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                ordinary_contract_id,
                f"startup-failed-{suffix}",
                suffix[-4:],
                "https://example.invalid/startup-failed",
                "active",
                "2099-01-01T00:00:00+10:00",
                stamp,
            ),
        ).lastrowid
        archived_link_id = connection.execute(
            """INSERT INTO contract_signing_links(
               contract_id,token_hash,token_last_4,signing_url,status,
               expires_at,created_at,updated_at,signed_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                transfer_contract_id,
                f"startup-archived-{suffix}",
                suffix[-4:],
                "https://example.invalid/startup-archived",
                "signed",
                "2099-01-01T00:00:00+10:00",
                stamp,
                stamp,
                stamp,
            ),
        ).lastrowid
        failed_signature_id = connection.execute(
            """INSERT INTO contract_signatures(
               contract_id,link_id,signed_at,signature_image_path,signed_pdf_path,
               confirmation_pdf_path,confirmation_statement,status,archive_status,
               archive_error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ordinary_contract_id,
                failed_link_id,
                stamp,
                "/isolated/startup-failed-signature.png",
                "/isolated/startup-failed-signed.pdf",
                "/isolated/startup-failed-confirmation.pdf",
                "confirmed",
                "signed",
                "failed",
                "synthetic archive failure",
                stamp,
                stamp,
            ),
        ).lastrowid
        archived_signature_id = connection.execute(
            """INSERT INTO contract_signatures(
               contract_id,link_id,signed_at,signature_image_path,signed_pdf_path,
               confirmation_pdf_path,confirmation_statement,status,archive_status,
               archive_error,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                transfer_contract_id,
                archived_link_id,
                stamp,
                "/isolated/startup-archived-signature.png",
                "/isolated/startup-archived-signed.pdf",
                "/isolated/startup-archived-confirmation.pdf",
                "confirmed",
                "signed",
                "archived",
                "",
                stamp,
                stamp,
            ),
        ).lastrowid
        connection.execute(
            "UPDATE contract_signing_links SET signed_signature_id=? WHERE id=?",
            (archived_signature_id, archived_link_id),
        )
        connection.commit()
        return {
            "ordinary_contract_id": ordinary_contract_id,
            "transfer_contract_id": transfer_contract_id,
            "failed_link_id": failed_link_id,
            "archived_link_id": archived_link_id,
            "failed_signature_id": failed_signature_id,
            "archived_signature_id": archived_signature_id,
            "stamp": stamp,
        }
    finally:
        connection.close()


def select_one(connection, sql, parameters):
    row = connection.execute(sql, parameters).fetchone()
    if row is None:
        raise AssertionError(f"Missing acceptance row for query: {sql}")
    return dict(row)


def run(database_path):
    database_path = database_path.resolve()
    if database_path == PRODUCTION_DATABASE:
        raise AssertionError("Acceptance harness refuses the Production database")
    if "SAFEBOX" not in database_path.parts:
        raise AssertionError("Acceptance database must be inside a SAFEBOX directory")

    cases = seed_cases(database_path)
    before_payload, before_hash = protected_snapshot(database_path)
    database = Database(database_path)
    database.initialize()
    after_first_payload, after_first_hash = protected_snapshot(database_path)
    database.initialize()
    after_second_payload, after_second_hash = protected_snapshot(database_path)

    if after_first_payload != before_payload or after_second_payload != before_payload:
        raise AssertionError("Startup changed protected historical contract business rows")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        failed_signature = select_one(
            connection,
            "SELECT archive_status,archive_error,updated_at FROM contract_signatures WHERE id=?",
            (cases["failed_signature_id"],),
        )
        archived_signature = select_one(
            connection,
            "SELECT archive_status,archive_error,updated_at FROM contract_signatures WHERE id=?",
            (cases["archived_signature_id"],),
        )
        active_link = select_one(
            connection,
            "SELECT status,updated_at,signed_at,signed_signature_id FROM contract_signing_links WHERE id=?",
            (cases["failed_link_id"],),
        )
        ordinary_contract = select_one(
            connection,
            "SELECT contract_status,updated_at FROM contracts WHERE id=?",
            (cases["ordinary_contract_id"],),
        )
        transfer_contract = select_one(
            connection,
            "SELECT contract_status,updated_at FROM contracts WHERE id=?",
            (cases["transfer_contract_id"],),
        )
        contract_199 = select_one(
            connection,
            "SELECT id,contract_status,updated_at FROM contracts WHERE id=199",
            (),
        )
        signature_103 = select_one(
            connection,
            """SELECT id,contract_id,status,archive_status,archive_error,updated_at
               FROM contract_signatures WHERE id=103""",
            (),
        )
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()

    expected_stamp = cases["stamp"]
    assert failed_signature == {
        "archive_status": "failed",
        "archive_error": "synthetic archive failure",
        "updated_at": expected_stamp,
    }
    assert archived_signature == {
        "archive_status": "archived",
        "archive_error": "",
        "updated_at": expected_stamp,
    }
    assert active_link == {
        "status": "active",
        "updated_at": None,
        "signed_at": None,
        "signed_signature_id": None,
    }
    assert ordinary_contract == {"contract_status": "Sent", "updated_at": expected_stamp}
    assert transfer_contract == {
        "contract_status": "FullyExecuted",
        "updated_at": expected_stamp,
    }
    assert before_hash == after_first_hash == after_second_hash
    assert quick_check == "ok"
    assert integrity_check == "ok"
    assert fk_violations == 0

    return {
        "status": "PASS",
        "database": str(database_path),
        "production_derived": True,
        "fingerprint_before": before_hash,
        "fingerprint_after_first_initialize": after_first_hash,
        "fingerprint_after_second_initialize": after_second_hash,
        "historical_mutation_count": 0,
        "failed_with_artifacts": failed_signature,
        "already_archived": archived_signature,
        "active_link": active_link,
        "ordinary_contract": ordinary_contract,
        "transfer_contract": transfer_contract,
        "contract_199": contract_199,
        "signature_103": signature_103,
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "fk_violations": fk_violations,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    result = run(args.database)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

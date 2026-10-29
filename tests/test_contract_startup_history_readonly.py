import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from database import Database


class ContractStartupHistoryReadOnlyTests(unittest.TestCase):
    def _fingerprint(self, database):
        payload = {}
        for table in ("contracts", "contract_signing_links", "contract_signatures"):
            payload[table] = [
                dict(row)
                for row in database.all(f"SELECT * FROM {table} ORDER BY id")
            ]
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def test_repeated_initialize_preserves_historical_contract_signing_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "aaie_crm.db")
            database.initialize()
            stamp = "2026-09-05T09:30:00+10:00"

            for student_id, last_name in (
                ("STARTUPORD001", "Ordinary"),
                ("STARTUPTRF001", "Transfer"),
            ):
                database.execute(
                    """INSERT INTO students(
                       student_id,passport_last_name,passport_first_name,current_status,
                       created_date,updated_at,form_data_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (student_id, last_name, "Startup", "Active", stamp, stamp, "{}"),
                )

            ordinary_contract_id = database.execute(
                """INSERT INTO contracts(
                   student_id,contract_uid,contract_category,contract_type,contract_title,
                   contract_status,service_scope_json,generated_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "STARTUPORD001",
                    "STARTUP-ORDINARY-001",
                    "留学合同",
                    "LocalStudentApplicationContract",
                    "普通新学生合同",
                    "Sent",
                    "[]",
                    stamp,
                    stamp,
                ),
            )
            transfer_contract_id = database.execute(
                """INSERT INTO contracts(
                   student_id,contract_uid,contract_category,contract_type,contract_title,
                   contract_status,service_scope_json,generated_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "STARTUPTRF001",
                    "STARTUP-TRANSFER-001",
                    "转学合同",
                    "TransferApplicationContract",
                    "转学申请合同",
                    "FullyExecuted",
                    "[]",
                    stamp,
                    stamp,
                ),
            )

            ordinary_link_id = database.execute(
                """INSERT INTO contract_signing_links(
                   contract_id,student_id,token_hash,token_last_4,signing_url,status,
                   expires_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    ordinary_contract_id,
                    "STARTUPORD001",
                    "startup-ordinary-hash",
                    "hash",
                    "https://example.invalid/ordinary",
                    "active",
                    "2099-01-01T00:00:00+10:00",
                    stamp,
                ),
            )
            transfer_link_id = database.execute(
                """INSERT INTO contract_signing_links(
                   contract_id,student_id,token_hash,token_last_4,signing_url,status,
                   expires_at,created_at,updated_at,signed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    transfer_contract_id,
                    "STARTUPTRF001",
                    "startup-transfer-hash",
                    "hash",
                    "https://example.invalid/transfer",
                    "signed",
                    "2099-01-01T00:00:00+10:00",
                    stamp,
                    stamp,
                    stamp,
                ),
            )

            failed_signature_id = database.execute(
                """INSERT INTO contract_signatures(
                   contract_id,student_id,link_id,signed_at,signature_image_path,
                   signed_pdf_path,confirmation_pdf_path,confirmation_statement,status,
                   archive_status,archive_error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ordinary_contract_id,
                    "STARTUPORD001",
                    ordinary_link_id,
                    stamp,
                    "/isolated/signature-ordinary.png",
                    "/isolated/signed-ordinary.pdf",
                    "/isolated/confirmation-ordinary.pdf",
                    "confirmed",
                    "signed",
                    "failed",
                    "synthetic archive failure",
                    stamp,
                    stamp,
                ),
            )
            archived_signature_id = database.execute(
                """INSERT INTO contract_signatures(
                   contract_id,student_id,link_id,signed_at,signature_image_path,
                   signed_pdf_path,confirmation_pdf_path,confirmation_statement,status,
                   archive_status,archive_error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    transfer_contract_id,
                    "STARTUPTRF001",
                    transfer_link_id,
                    stamp,
                    "/isolated/signature-transfer.png",
                    "/isolated/signed-transfer.pdf",
                    "/isolated/confirmation-transfer.pdf",
                    "confirmed",
                    "signed",
                    "archived",
                    "",
                    stamp,
                    stamp,
                ),
            )
            database.execute(
                "UPDATE contract_signing_links SET signed_signature_id=? WHERE id=?",
                (archived_signature_id, transfer_link_id),
            )

            fingerprint_before = self._fingerprint(database)
            database.initialize()
            fingerprint_after_first_start = self._fingerprint(database)
            database.initialize()
            fingerprint_after_second_start = self._fingerprint(database)

            failed_signature = database.one(
                """SELECT archive_status,archive_error,updated_at
                   FROM contract_signatures WHERE id=?""",
                (failed_signature_id,),
            )
            archived_signature = database.one(
                """SELECT archive_status,archive_error,updated_at
                   FROM contract_signatures WHERE id=?""",
                (archived_signature_id,),
            )
            ordinary_link = database.one(
                """SELECT status,updated_at,signed_at,signed_signature_id
                   FROM contract_signing_links WHERE id=?""",
                (ordinary_link_id,),
            )
            ordinary_contract = database.one(
                "SELECT contract_status,updated_at FROM contracts WHERE id=?",
                (ordinary_contract_id,),
            )
            transfer_contract = database.one(
                "SELECT contract_status,updated_at FROM contracts WHERE id=?",
                (transfer_contract_id,),
            )

            self.assertEqual(dict(failed_signature), {
                "archive_status": "failed",
                "archive_error": "synthetic archive failure",
                "updated_at": stamp,
            })
            self.assertEqual(dict(archived_signature), {
                "archive_status": "archived",
                "archive_error": "",
                "updated_at": stamp,
            })
            self.assertEqual(dict(ordinary_link), {
                "status": "active",
                "updated_at": None,
                "signed_at": None,
                "signed_signature_id": None,
            })
            self.assertEqual(dict(ordinary_contract), {
                "contract_status": "Sent",
                "updated_at": stamp,
            })
            self.assertEqual(dict(transfer_contract), {
                "contract_status": "FullyExecuted",
                "updated_at": stamp,
            })
            self.assertEqual(fingerprint_after_first_start, fingerprint_before)
            self.assertEqual(fingerprint_after_second_start, fingerprint_before)


if __name__ == "__main__":
    unittest.main()

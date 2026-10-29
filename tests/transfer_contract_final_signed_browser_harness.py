#!/usr/bin/env python3
"""Serve a complete ordinary/Transfer signing fixture from an isolated DB clone."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timedelta

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["AAIE_CRM_DB_PERF_LOG"] = "0"
for key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET"):
    os.environ.pop(key, None)

import app
from database import Database
from security import password_hash


PASSWORD = "TransferFinalSigned!2026"
FORMAL_DB = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db").resolve()


class IsolatedNas:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def student_folder(self, student):
        folder = Path(student["folder_path"]).resolve()
        if self.root not in folder.parents:
            raise RuntimeError("student folder escaped isolated NAS root")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def category_folder(self, student, category):
        folder = self.student_folder(student) / str(category)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def require_student_file_readable(self, student, file_path):
        path = Path(file_path).resolve()
        if self.student_folder(student) not in path.parents or not path.is_file():
            raise RuntimeError("contract source is not an isolated readable student file")
        return path

    def status(self):
        return {
            "mounted": True,
            "mode": "isolated-browser-clone",
            "production_enabled": False,
            "production_locked": True,
            "root": str(self.root),
            "root_error": "",
            "root_exists": True,
            "readable": True,
            "writable": True,
            "ready": True,
        }


def _write_contract_pdf(target: Path, label: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf = app.canvas.Canvas(str(target), pagesize=(612, 792))
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(72, 724, label)
    pdf.setFont("Helvetica", 11)
    for index in range(14):
        pdf.drawString(72, 690 - index * 28, f"Authoritative contract term {index + 1}: synthetic browser acceptance only.")
    pdf.showPage()
    font = app.reportlab_chinese_font()
    pdf.setFont(font, 12)
    pdf.drawString(72, 690, "合同签署页")
    pdf.drawString(72, 626, "代理方（甲方盖章）：AAIE")
    pdf.drawString(72, 574, "授权代表：Sonny Zhang")
    pdf.drawString(72, 522, "甲方签署日期：2026年09月05日")
    pdf.drawString(326, 626, "委托方（乙方签字）：________________")
    pdf.drawString(326, 558, "法定代理人或监护人（签字）：________________")
    pdf.drawString(326, 490, "家长/乙方签署日期：____年____月____日")
    pdf.drawString(326, 438, "（乙方/监护人自行签署并填写日期）")
    pdf.showPage()
    pdf.save()


def _write_signing_assets(runtime_root: Path) -> None:
    signature = runtime_root / "assets" / "sonny_signature.png"
    stamp = runtime_root / "assets" / "aaie_company_stamp.png"
    signature.parent.mkdir(parents=True, exist_ok=True)
    signature_image = Image.new("RGBA", (300, 100), (255, 255, 255, 0))
    signature_draw = ImageDraw.Draw(signature_image)
    signature_draw.line((12, 62, 80, 24, 148, 70, 286, 26), fill=(14, 55, 120, 255), width=7)
    signature_image.save(signature)
    stamp_image = Image.new("RGBA", (180, 180), (255, 255, 255, 0))
    stamp_draw = ImageDraw.Draw(stamp_image)
    stamp_draw.ellipse((8, 8, 172, 172), outline=(190, 0, 0, 220), width=9)
    stamp_draw.text((47, 78), "AAIE", fill=(190, 0, 0, 220))
    stamp_image.save(stamp)
    app.SONNY_SIGNATURE_PATH = signature
    app.AAIE_COMPANY_STAMP_PATH = stamp


def _insert_student(connection, student_id: str, folder: Path, first_name: str) -> None:
    stamp = "2026-09-05T12:00:00+10:00"
    connection.execute(
        """INSERT INTO students(
           student_id,passport_last_name,passport_first_name,current_status,
           current_year_level,applying_year_level,folder_path,nas_status,
           created_date,updated_at,form_data_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            student_id,
            "Acceptance",
            first_name,
            "Active",
            "Year 8",
            "Year 9",
            str(folder),
            "ready",
            stamp,
            stamp,
            "{}",
        ),
    )


def _insert_contract(
    connection,
    *,
    student_id: str,
    contract_type: str,
    contract_uid: str,
    source_pdf: Path,
    cycle_id: int | None,
    token: str,
    parent_port: int,
    admin_user_id: int,
) -> tuple[int, int]:
    stamp = "2026-09-05T12:00:00+10:00"
    contract_id = connection.execute(
        """INSERT INTO contracts(
           student_id,contract_uid,contract_category,contract_type,contract_title,
           contract_status,contract_date,service_type,service_scope_json,
           parent_signer_name,parent_phone,parent_email,parent_address,
           contract_file_path,generated_by,generated_at,updated_at,application_cycle_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            student_id,
            contract_uid,
            "留学合同",
            contract_type,
            "AAIE 留学合同",
            "Sent",
            "2026-09-05",
            "留学咨询服务",
            "[]",
            "测试家长",
            "+61 400 000 000",
            "synthetic@example.test",
            "Isolated Browser Fixture",
            str(source_pdf),
            admin_user_id,
            stamp,
            stamp,
            cycle_id,
        ),
    ).lastrowid
    link_id = connection.execute(
        """INSERT INTO contract_signing_links(
           contract_id,student_id,token_hash,token_last_4,signing_url,status,
           expires_at,created_by_user_id,created_by_name,created_at,updated_at
        ) VALUES(?,?,?,?,?,'active',?,?,?,?,?)""",
        (
            contract_id,
            student_id,
            app.contract_signing_token_hash(token),
            token[-4:],
            f"http://127.0.0.1:{parent_port}/contract-sign/{token}",
            (datetime.now().astimezone() + timedelta(days=7)).isoformat(timespec="seconds"),
            admin_user_id,
            "Admin",
            stamp,
            stamp,
        ),
    ).lastrowid
    return int(contract_id), int(link_id)


def configure(runtime_root: Path, database_path: Path, parent_port: int) -> dict:
    runtime_root = runtime_root.resolve()
    database_path = database_path.resolve()
    if database_path == FORMAL_DB:
        raise RuntimeError("formal database is prohibited")
    if runtime_root not in database_path.parents:
        raise RuntimeError("database must stay inside isolated runtime")
    if not database_path.is_file():
        raise RuntimeError("isolated database clone is missing")

    storage = runtime_root / "storage"
    nas_root = runtime_root / "nas"
    storage.mkdir(parents=True, exist_ok=True)
    nas_root.mkdir(parents=True, exist_ok=True)
    app.DB_PATH = database_path
    app.PREVIEW_DIR = storage / "previews"
    app.UPLOAD_REVIEW_DIR = storage / "upload-staging"
    app.SCHOOL_PLAN_REPORT_DIR = storage / "school-plan-reports"
    app.SCHOOL_PLAN_CONFIRMATION_DIR = storage / "school-plan-confirmations"
    app.CONTRACT_SIGNING_DIR = storage / "contract-signing"
    app.LOG_DIR = runtime_root / "logs"
    app.SAFE_PRODUCTION_ACTIVE = True
    app.db = Database(database_path)
    app.nas = IsolatedNas(nas_root)
    app.CRMHandler.school_plan_wechat_oauth_enabled = lambda _self: False
    _write_signing_assets(runtime_root)

    ordinary_student_id = "TFS-ORD-20260905"
    transfer_student_id = "TFS-TRN-20260905"
    ordinary_token = "ordinary-final-signed-20260905"
    transfer_token = "transfer-final-signed-20260905"
    with app.db.transaction() as connection:
        admin = connection.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()
        admin_user_id = int(admin["id"])
        connection.execute(
            "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
            (password_hash(PASSWORD), admin_user_id),
        )
        ordinary_folder = nas_root / ordinary_student_id
        transfer_folder = nas_root / transfer_student_id
        _insert_student(connection, ordinary_student_id, ordinary_folder, "Ordinary")
        _insert_student(connection, transfer_student_id, transfer_folder, "Transfer")
        ordinary_cycle_id = connection.execute(
            """INSERT INTO application_cycles(
               cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,
               created_by_user_id,created_by_name,created_at,updated_by_user_id,
               updated_by_name,updated_at,education_stream
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "CYCLE-TFS-ORD-20260905",
                ordinary_student_id,
                "initial",
                1,
                "active",
                "2026-09-05T12:00:00+10:00",
                admin_user_id,
                "Admin",
                "2026-09-05T12:00:00+10:00",
                admin_user_id,
                "Admin",
                "2026-09-05T12:00:00+10:00",
                "school_education",
            ),
        ).lastrowid
        ordinary_source = ordinary_folder / "Forms" / "ordinary-source.pdf"
        transfer_source = transfer_folder / "Forms" / "transfer-source.pdf"
        _write_contract_pdf(ordinary_source, "ORDINARY NEW STUDENT CONTRACT")
        _write_contract_pdf(transfer_source, "TRANSFER APPLICATION CONTRACT")
        ordinary_contract_id, ordinary_link_id = _insert_contract(
            connection,
            student_id=ordinary_student_id,
            contract_type="NewInternationalStudentContract",
            contract_uid="TFS-ORDINARY-20260905",
            source_pdf=ordinary_source,
            cycle_id=int(ordinary_cycle_id),
            token=ordinary_token,
            parent_port=parent_port,
            admin_user_id=admin_user_id,
        )
        transfer_contract_id, transfer_link_id = _insert_contract(
            connection,
            student_id=transfer_student_id,
            contract_type="TransferApplicationContract",
            contract_uid="TFS-TRANSFER-20260905",
            source_pdf=transfer_source,
            cycle_id=None,
            token=transfer_token,
            parent_port=parent_port,
            admin_user_id=admin_user_id,
        )

    app.db.set_setting("auto_backup_enabled", "false", 1)
    with sqlite3.connect(database_path) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute(
            "SELECT count(*) FROM pragma_foreign_key_check"
        ).fetchone()[0]
        revenue_state = connection.execute(
            "SELECT status,cutover_at FROM revenue_module_state WHERE id=1"
        ).fetchone()
    return {
        "database": str(database_path),
        "runtime_root": str(runtime_root),
        "quick_check": quick_check,
        "foreign_key_violations": foreign_keys,
        "revenue_state": list(revenue_state),
        "username": "admin",
        "password": PASSWORD,
        "ordinary": {
            "student_id": ordinary_student_id,
            "contract_id": ordinary_contract_id,
            "link_id": ordinary_link_id,
            "token": ordinary_token,
        },
        "transfer": {
            "student_id": transfer_student_id,
            "contract_id": transfer_contract_id,
            "link_id": transfer_link_id,
            "token": transfer_token,
        },
    }


def serve(runtime_root: Path, database_path: Path, admin_port: int, parent_port: int) -> None:
    evidence = configure(runtime_root, database_path, parent_port)
    admin_server = app.ReusableThreadingHTTPServer(
        ("127.0.0.1", int(admin_port)), app.CRMHandler
    )
    admin_server.service_mode = "admin"
    parent_server = app.ReusableThreadingHTTPServer(
        ("127.0.0.1", int(parent_port)), app.CRMHandler
    )
    parent_server.service_mode = "parent"

    def shutdown(_signum, _frame):
        threading.Thread(target=admin_server.shutdown, daemon=True).start()
        threading.Thread(target=parent_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    admin_thread = threading.Thread(target=admin_server.serve_forever, daemon=True)
    admin_thread.start()
    print(json.dumps({
        "ready": True,
        "admin_url": f"http://127.0.0.1:{admin_port}",
        "parent_url": f"http://127.0.0.1:{parent_port}",
        **evidence,
    }, ensure_ascii=False), flush=True)
    try:
        parent_server.serve_forever()
    finally:
        parent_server.server_close()
        admin_server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--admin-port", type=int, required=True)
    parser.add_argument("--parent-port", type=int, required=True)
    args = parser.parse_args()
    serve(args.runtime_root, args.database, args.admin_port, args.parent_port)


if __name__ == "__main__":
    main()

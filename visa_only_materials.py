"""Material review and attachment storage for cases without a Student Profile."""

import hashlib
import sqlite3
from html import escape
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from visa_document_lifecycle import DuplicateVisaUpload
from visa_document_metadata import CATEGORIES
from visa_documents_candidate import register_document, save_extraction


class VisaOnlyMaterialsMixin:
    def visa_only_materials_ready(self):
        return bool(self._visa_runtime()["db"].one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visa_only_material_uploads'"
        ))

    def visa_only_materials_url(self, case_id):
        return f"/visa-management/cases/{int(case_id)}/applicant/material-batch-upload"

    def visa_only_material_file_rows(self, case_id, item_id):
        return self._visa_runtime()["db"].all(
            """SELECT d.id, d.original_name FROM visa_only_material_uploads u
               JOIN visa_case_documents d ON d.id=u.document_id AND d.visa_case_id=u.visa_case_id
               WHERE u.visa_case_id=? AND u.item_id=? AND d.archived_at IS NULL
               ORDER BY d.id""",
            (case_id, item_id),
        )

    def visa_only_material_review(self, case, documents, notices=()):
        message = "".join(f"<li>{escape(notice)}</li>" for notice in notices)
        if message:
            message = f'<ul class="notice" role="status">{message}</ul>'
        body = self.visa_material_batch_review_html("", case, documents)
        return self._visa_send("Smart Batch Upload Review", message + body)

    def visa_only_material_batch_upload(self, method, case):
        db = self._visa_runtime()["db"]
        if method == "GET":
            pending = db.all(
                """SELECT * FROM visa_only_material_uploads
                   WHERE visa_case_id=? AND document_id IS NULL ORDER BY id""",
                (case["id"],),
            )
            query = getattr(self, "query", {}) or {}
            if pending and query.get("review") == ["pending"]:
                return self.visa_only_material_review(case, pending)
            body = self.visa_material_batch_upload_page_html("", case)
            if pending:
                tab = self.visa_case_active_tab()
                href = self.visa_only_materials_url(case["id"]) + f"?review=pending&visa_tab={quote(tab)}"
                body = f'<p><a href="{escape(href, quote=True)}">继续确认 {len(pending)} 个待确认文件</a></p>' + body
            return self._visa_send("Smart Batch Upload", body)
        if method != "POST":
            return self.error_page("Method Not Allowed", 405)

        uploads = [upload for upload in self.files if upload.field_name in {"file", "files"} and upload.filename]
        if not uploads:
            return self.error_page("请选择要上传的文件。", 400)

        documents = {}
        notices = []
        for upload in uploads:
            name = Path(upload.filename).name
            try:
                row = self.visa_only_material_stage(case, upload, name)
                documents[row["id"]] = row
            except (ValueError, OSError, sqlite3.Error) as exc:
                notices.append(f"{name}：{exc}" if isinstance(exc, ValueError) else f"{name}：无法保存文件，请重试。")
        return self.visa_only_material_review(case, documents.values(), notices)

    def visa_only_material_stage(self, case, upload, name):
        if not name or not upload.data or not self.visa_material_file_allowed(upload):
            raise ValueError("文件为空或格式不受支持。")
        g = self._visa_runtime()
        digest = hashlib.sha256(upload.data).hexdigest()
        path = None
        created_file = False
        try:
            with g["db"].transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM visa_only_material_uploads WHERE visa_case_id=? AND sha256=?",
                    (case["id"], digest),
                ).fetchone()
                if existing:
                    if existing["document_id"] is not None:
                        raise ValueError("文件已存在于当前签证案件。")
                    self._visa_document_path(dict(case), dict(existing))
                    return existing
                try:
                    self._visa_check_upload(dict(case), upload.data, connection=connection)
                except DuplicateVisaUpload as exc:
                    raise ValueError("文件已存在于当前签证案件。") from exc

                self.visa_material_initialize_checklist(case, connection=connection)
                folder = self.visa_only_document_root(case) / "materials"
                folder = g["ensure_within"](folder, self.visa_only_document_root(case))
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (uuid4().hex + Path(name).suffix.lower())
                with path.open("xb") as stream:
                    created_file = True
                    stream.write(upload.data)
                return connection.execute(
                    """INSERT INTO visa_only_material_uploads(visa_case_id,original_name,file_path,sha256)
                       VALUES(?,?,?,?) RETURNING *""",
                    (case["id"], name, str(path), digest),
                ).fetchone()
        except Exception:
            if created_file:
                path.unlink(missing_ok=True)
            raise

    def visa_only_material_batch_confirm(self, method, case):
        if method != "POST":
            return self.error_page("Method Not Allowed", 405)
        g = self._visa_runtime()
        form_value = lambda name: g["form_text_value"](self.form, name)
        return_tab = self.visa_case_normalized_tab(form_value("return_tab"), "material_checklist")
        confirmed = 0
        try:
            with g["db"].transaction() as connection:
                ids = dict.fromkeys(int(value) for value in self.visa_material_batch_form_values("document_ids"))
                for upload_id in ids:
                    item_id = form_value(f"final_item_{upload_id}")
                    upload = connection.execute(
                        "SELECT * FROM visa_only_material_uploads WHERE id=? AND visa_case_id=?",
                        (upload_id, case["id"]),
                    ).fetchone()
                    if not upload:
                        raise ValueError("待确认文件不属于当前 Visa Case。")
                    if not item_id or upload["document_id"] is not None:
                        continue
                    item = connection.execute(
                        "SELECT * FROM visa_checklist_items WHERE id=? AND visa_case_id=?",
                        (int(item_id), case["id"]),
                    ).fetchone()
                    if not item:
                        raise ValueError("材料项不属于当前 Visa Case。")
                    self.visa_only_material_confirm_item(
                        connection, case, upload, item, form_value(f"result_type_{upload_id}"),
                    )
                    confirmed += 1
        except (ValueError, TypeError) as exc:
            return self.error_page(str(exc), 400)
        except (OSError, sqlite3.Error):
            return self.error_page("材料确认失败，待确认文件仍保留，请重试。", 409)

        from visa_material_workspace_ui import material_upload_target
        target = material_upload_target(
            self.form, {}, self.visa_material_batch_available_items(case["id"]),
        ) if form_value("target_material_item") else ""
        prefix = {'material_checklist':'material-checklist', 'visa_result':'visa-result'}.get(return_tab)
        anchor = f"{prefix}-item-{target}" if target and prefix else ""
        location = self.visa_case_return_path("", case["id"], return_tab, anchor)
        return self.flash_redirect(location, f"成功挂载 {confirmed} 个文件。")

    def visa_only_material_confirm_item(self, connection, case, upload, item, manual_type):
        g = self._visa_runtime()
        path = self._visa_document_path(dict(case), dict(upload))
        data = path.read_bytes()
        title = self.visa_material_item_title(item).lower()
        result_item = "grant" in title and "refusal" in title
        if manual_type and (not result_item or manual_type not in {"grant_letter", "refusal_letter"}):
            raise ValueError("请在对应的签证结果材料项确认下签或拒签信。")
        category = manual_type or self._visa_result_type(item, upload["original_name"], data, batch=True)
        if result_item and category not in {"grant_letter", "refusal_letter", "withdrawal_confirmation"}:
            raise ValueError("请选择文件类型，确认是下签信或拒签信。")
        version = connection.execute(
            """SELECT COALESCE(MAX(version),0)+1 FROM visa_case_documents
               WHERE visa_case_id=? AND original_name=?""",
            (case["id"], upload["original_name"]),
        ).fetchone()[0]
        actor = {"actor_user_id": self.user["id"], "occurred_at": g["now_iso"]()}
        document = register_document(
            connection, case["id"], file_path=str(path), original_name=upload["original_name"],
            version=version, sha256=upload["sha256"], category=category if category in CATEGORIES else "other",
            **actor,
        )
        connection.execute(
            "UPDATE visa_only_material_uploads SET item_id=?,document_id=? WHERE id=?",
            (item["id"], document["id"], upload["id"]),
        )
        self.visa_material_mark_item_uploaded_if_missing(case["id"], item["id"], connection=connection)
        if category != "other":
            text = self._visa_read_text(dict(case), document)
            if text:
                save_extraction(connection, case["id"], document["id"], text=text, **actor)

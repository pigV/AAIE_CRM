"""Private, immutable upload staging for Revenue documents."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
from typing import Iterable
import zipfile


MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
}


@dataclass(frozen=True)
class StagedRevenueUpload:
    path: Path
    original_filename: str
    sha256: str
    mime_type: str
    size_bytes: int
    created: bool


ACCEPTED_UPLOAD_MIME_TYPES = {
    ".pdf": {"application/pdf", "application/x-pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg", "image/jpg"},
    ".jpeg": {"image/jpeg", "image/jpg"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    ".csv": {"text/csv", "application/csv", "text/plain"},
}


def _validate_upload_identity(extension: str, content_type: str, payload: bytes) -> None:
    supplied = str(content_type or "").split(";", 1)[0].strip().lower()
    if supplied and supplied != "application/octet-stream" and supplied not in ACCEPTED_UPLOAD_MIME_TYPES[extension]:
        raise ValueError("文件类型与扩展名不一致。")
    if extension == ".pdf" and not payload.startswith(b"%PDF-"):
        raise ValueError("PDF 文件签名无效。")
    if extension == ".png" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG 文件签名无效。")
    if extension in {".jpg", ".jpeg"} and not (
        payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")
    ):
        raise ValueError("JPEG 文件签名无效。")
    if extension == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("DOCX 文件签名无效。") from exc
        if "[Content_Types].xml" not in names or not any(
            name.startswith("word/") for name in names
        ):
            raise ValueError("DOCX 文件结构无效。")
    if extension == ".csv":
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV 文件必须使用 UTF-8 编码。") from exc
        if "\x00" in text:
            raise ValueError("CSV 文件内容无效。")


def stage_revenue_upload(
    root: Path,
    upload,
    allowed_extensions: Iterable[str],
    *,
    max_bytes: int = 20 * 1024 * 1024,
) -> StagedRevenueUpload:
    filename = str(getattr(upload, "filename", "") or "")
    if (
        not filename
        or "\x00" in filename
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("请选择安全的文件名。")
    extension = Path(filename).suffix.lower()
    allowed = {str(item).lower() for item in allowed_extensions}
    if extension not in allowed or extension not in MIME_BY_EXTENSION:
        raise ValueError("不支持此文件类型。")
    payload = getattr(upload, "data", None)
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("上传文件不能为空。")
    if len(payload) > int(max_bytes):
        raise ValueError("上传文件超过大小限制。")
    _validate_upload_identity(extension, getattr(upload, "content_type", ""), payload)

    from revenue.secure_storage import write_immutable
    digest = hashlib.sha256(payload).hexdigest()
    target, created = write_immutable(root, f"{digest}{extension}", payload)
    return StagedRevenueUpload(
        path=target,
        original_filename=filename,
        sha256=digest,
        mime_type=MIME_BY_EXTENSION[extension],
        size_bytes=len(payload),
        created=created,
    )


def discard_unregistered_upload(staged: StagedRevenueUpload) -> None:
    """Keep published secure objects: a concurrent request may have registered one.

    No financial row is created by retaining an inert object. Shared published
    files must never be removed by request-local transaction rollback.
    """
    del staged


def parse_statement_csv(payload: bytes, *, max_rows: int = 5000) -> list[dict]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Statement CSV 必须使用 UTF-8 编码。") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or "gross_commission_cents" not in reader.fieldnames:
        raise ValueError("Statement CSV 缺少 gross_commission_cents 栏位。")
    rows = []
    for line_number, raw in enumerate(reader, start=2):
        if len(rows) >= max_rows:
            raise ValueError("Statement CSV 超过行数限制。")
        cents_text = str(raw.get("gross_commission_cents") or "").strip()
        if not cents_text.isdigit():
            raise ValueError(f"Statement 第 {line_number} 行金额必须为整数分。")
        rows.append({
            "external_reference": str(raw.get("external_reference") or "").strip() or None,
            "student_reference": str(raw.get("student_reference") or "").strip() or None,
            "student_name_text": str(raw.get("student_name_text") or "").strip() or None,
            "program_text": str(raw.get("program_text") or "").strip() or None,
            "gross_commission_cents": int(cents_text),
        })
    if not rows:
        raise ValueError("Statement CSV 没有数据行。")
    return rows

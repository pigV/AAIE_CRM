"""Manual invoice lines, scoped to the current school payer."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json

from revenue.money import apply_basis_points

MAX_LINES = 20
DEFAULT_PAYMENT_TERMS = 15
PAYMENT_TERMS = {0, 7, 15, 30}
FIELDS = ("product", "description", "student_id", "quantity", "unit_price", "gst")


def manual_students(connection, school_id):
    from revenue.read_models import RevenueReadModels

    return {
        row["student_id"]: row["student_name"]
        for row in RevenueReadModels(connection).school_claim_components(school_id)
    }


def student_invoice_label(connection, student_id, name=None, *, missing_birthday=False):
    if not student_id:
        return ""
    if name is None:
        from revenue.read_models import RevenueReadModels

        name = RevenueReadModels(connection)._student_display_name(student_id)
    row = (connection.execute("SELECT date_of_birth FROM students WHERE student_id=?", (student_id,)).fetchone()
           if any(column[1] == "date_of_birth" for column in connection.execute("PRAGMA table_info(students)")) else None)
    birthday = str(row[0] or "").strip() if row else ""
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            date_label = datetime.strptime(birthday, pattern).strftime("%d/%m/%Y")
            return f"{name} · {date_label}"
        except ValueError:
            continue
    return f"{name} · 生日未录入" if missing_birthday else name


def parse_manual_form(form):
    if "manual_lines" in form:
        try:
            rows = json.loads(form["manual_lines"])
        except (TypeError, ValueError):
            raise ValueError("手工明细格式无效。") from None
        if not isinstance(rows, list):
            raise ValueError("手工明细格式无效。")
        return rows
    rows = []
    for index in range(MAX_LINES):
        row = {field: form.get(f"manual_{field}_{index}", "") for field in FIELDS}
        if any(row[field] for field in ("product", "description", "student_id", "unit_price")):
            rows.append(row)
    return rows


def prepare_manual_lines(connection, school_id, rows):
    if not rows:
        return []
    if not school_id or len(rows) > MAX_LINES:
        raise ValueError("每张发票最多可添加 20 条手工明细。")
    students = manual_students(connection, school_id) if any(
        isinstance(row, dict) and row.get("student_id") for row in rows
    ) else {}
    lines = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS) or not all(
            isinstance(value, str) for value in row.values()
        ):
            raise ValueError("手工明细格式无效。")
        product = row["product"].strip()
        description = row["description"].strip()
        student_id = row["student_id"].strip()
        if not product or len(product) > 100 or len(description) > 300:
            raise ValueError("请填写手工项目，项目最多 100 字，说明最多 300 字。")
        if student_id and student_id not in students:
            raise ValueError("手工明细只能关联当前学校的学生。")
        try:
            quantity = int(row["quantity"])
            price = Decimal(row["unit_price"])
            if not 1 <= quantity <= 1000 or not price.is_finite() or not 0 < price <= 1_000_000:
                raise ValueError()
            unit_cents = price * 100
            if unit_cents != unit_cents.to_integral_value():
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError("请输入有效的正整数数量，以及最多两位小数的正数单价。") from None
        if row["gst"] not in {"taxable", "no_gst"}:
            raise ValueError("请为手工明细选择 GST。")
        student_label = student_invoice_label(connection, student_id, students.get(student_id)) if student_id else ""
        description = " · ".join(filter(None, (student_label, description)))
        net = quantity * int(unit_cents)
        gst = apply_basis_points(net, 1000) if row["gst"] == "taxable" else 0
        lines.append({
            "product": product, "description": description, "student_id": student_id,
            "student_name": student_label, "quantity": quantity,
            "unit_cents": int(unit_cents), "net_cents": net, "gst_cents": gst, "gross_cents": net + gst,
            "gst_mode": "exclusive" if row["gst"] == "taxable" else "none",
            "claim_uid": "", "claim_number": "", "rate": "",
        })
    return lines

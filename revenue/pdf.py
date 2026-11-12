"""Deterministic PDF renderers over immutable financial snapshots."""

from __future__ import annotations

from hashlib import sha256
import base64
from html import escape
from pathlib import Path
from io import BytesIO
from revenue.money import commission_gst_indicator
from revenue.secure_storage import write_immutable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


pdfmetrics.registerFont(TTFont(
    "RevenueUnicode", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
))


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}${value // 100:,}.{value % 100:02d}"


def _currency_money(cents: int, currency_code: str) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}{str(currency_code).upper()} {value // 100:,}.{value % 100:02d}"


class _InvariantCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        kwargs["invariant"] = 1
        kwargs["pageCompression"] = 1
        super().__init__(*args, **kwargs)


def _render(snapshot: dict, output_path: str | Path, title: str, party_label: str,
            party_value: str, lines: list[dict], total_rows: list[tuple[str, int]]) -> str:
    output = BytesIO()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "RevenueBody", parent=styles["BodyText"], fontName="RevenueUnicode",
        fontSize=9, leading=12, textColor=colors.HexColor("#263247"),
    )
    heading = ParagraphStyle(
        "RevenueHeading", parent=styles["Title"], fontName="RevenueUnicode",
        fontSize=21, leading=25, textColor=colors.HexColor("#17324D"),
    )
    right = ParagraphStyle("RevenueRight", parent=body, alignment=TA_RIGHT)
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=17 * mm, bottomMargin=17 * mm,
        title=f"{title} {snapshot['document_number']}",
        author=snapshot["issuer_name"],
    )
    story = [
        Paragraph(escape(snapshot["issuer_name"]), heading),
        Paragraph(f"ABN: {escape(snapshot['issuer_abn'])}", body),
        Spacer(1, 6 * mm),
        Table([
            [Paragraph(f"<b>{escape(title)}</b>", heading),
             Paragraph(f"<b>{escape(snapshot['document_number'])}</b><br/>"
                       f"Date: {escape(snapshot['issue_date'])}", right)],
            [Paragraph(f"<b>{escape(party_label)}:</b><br/>{escape(party_value)}", body),
             Paragraph(f"Currency: {escape(snapshot.get('currency_code', 'AUD'))}", right)],
        ], colWidths=[105 * mm, 52 * mm], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#B7C3D0")),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 1), (-1, 1), 7),
        ])),
        Spacer(1, 6 * mm),
    ]
    if snapshot.get("test_notice"):
        story.extend([Paragraph(f"<b>{escape(snapshot['test_notice'])}</b>", body), Spacer(1, 5 * mm)])
    if lines:
        data = [[Paragraph("Description", body), Paragraph("Net", right),
                 Paragraph("GST", right), Paragraph("Total", right)]]
        for item in lines:
            gross = int(item["gross_cents"])
            net = int(item.get("net_cents", gross - int(item.get("gst_cents", 0))))
            gst = int(item.get("gst_cents", gross - net))
            data.append([
                Paragraph(escape(str(item["description"])), body),
                Paragraph(_money(net), right), Paragraph(_money(gst), right),
                Paragraph(_money(gross), right),
            ])
        story.append(Table(data, repeatRows=1, colWidths=[91 * mm, 22 * mm, 22 * mm, 22 * mm],
                           style=TableStyle([
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0F5")),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D2DAE2")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                           ])))
        story.append(Spacer(1, 5 * mm))
    total_data = [[Paragraph(f"<b>{escape(label)}</b>", right),
                   Paragraph(f"<b>{_money(value)}</b>", right)]
                  for label, value in total_rows]
    story.append(Table(total_data, colWidths=[120 * mm, 37 * mm], style=TableStyle([
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#17324D")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ])))
    story.extend([
        Spacer(1, 9 * mm),
        Paragraph("Generated from an immutable Revenue & Commission snapshot. Corrections are issued as separate adjustment documents.", body),
    ])

    def footer(pdf_canvas, doc):
        pdf_canvas.saveState()
        pdf_canvas.setFont("Helvetica", 8)
        pdf_canvas.setFillColor(colors.HexColor("#5F6B7A"))
        pdf_canvas.drawString(18 * mm, 10 * mm, snapshot["document_number"])
        pdf_canvas.drawRightString(192 * mm, 10 * mm, f"Page {doc.page}")
        pdf_canvas.restoreState()

    document.build(story, canvasmaker=_InvariantCanvas,
                   onFirstPage=footer, onLaterPages=footer)
    payload = output.getvalue()
    if output_path is None:
        return payload
    path = Path(output_path)
    write_immutable(path.parent, path.name, payload)
    return sha256(payload).hexdigest()


def render_invoice_pdf(snapshot: dict, output_path: str | Path) -> str:
    return _render(
        snapshot, output_path, "Tax Invoice", "Bill to", snapshot["bill_to"],
        snapshot["lines"], [
            ("Net", snapshot["net_cents"]), ("GST", snapshot["gst_cents"]),
            ("Total", snapshot["gross_cents"]),
        ],
    )


def render_toorak_invoice_pdf(snapshot: dict) -> bytes:
    data = {**snapshot, "test_notice": "TEST ONLY - Not for submission or payment. Receipt date and due date pending."}
    return _render(data, None, "Commission Invoice", "Bill to", data["bill_to"], data["lines"], [
        ("Net", data["net_cents"]), ("GST", data["gst_cents"]), ("Total", data["gross_cents"]),
    ])


def render_configured_commission_invoice(snapshot: dict, layout: dict) -> bytes:
    if layout.get("preset")=="invoice_1762":
        from revenue.invoice_1762_pdf import render_invoice_1762
        reference=render_invoice_1762(snapshot,layout,_InvariantCanvas)
        if reference is not None:
            return reference
    style=layout["style"]
    output=BytesIO()
    accent=colors.HexColor(style["accent"])
    font="RevenueUnicode" if style["font"]=="unicode" else "Times-Roman"
    if font=="Times-Roman":
        try:
            str((snapshot,layout)).encode("cp1252")
        except UnicodeEncodeError:
            font="RevenueUnicode"
    size=style["size"]
    margin=style["margin"]
    width=A4[0]-2*margin-12
    spacing=style["spacing"]
    align={"left":TA_LEFT,"center":TA_CENTER,"right":TA_RIGHT}[style["alignment"]]
    normal=ParagraphStyle("CommissionBody",fontName=font,fontSize=size,leading=size+4,
                          textColor=colors.HexColor("#263247"),alignment=align)
    heading=ParagraphStyle("CommissionTitle",parent=normal,fontSize=size+8,leading=size+12,textColor=accent)
    issuer_name_size=style.get("issuer_name_size",size+8)
    issuer_text_size=style.get("issuer_text_size",size)
    issuer_name=ParagraphStyle("CommissionIssuerName",parent=heading,fontSize=issuer_name_size,leading=issuer_name_size+4)
    issuer_text=ParagraphStyle("CommissionIssuerText",parent=normal,fontSize=issuer_text_size,leading=issuer_text_size+4)
    small=ParagraphStyle("CommissionNotice",parent=normal,fontSize=9,leading=13,textColor=colors.HexColor("#922438"))
    right=ParagraphStyle("CommissionAmount",parent=normal,alignment=TA_RIGHT)
    test_only = snapshot.get("test_only",True)
    draft = snapshot.get("draft_notice") or snapshot["document_number"]=="DRAFT"
    mark = "TEST ONLY" if test_only else "DRAFT" if draft else ""
    document=SimpleDocTemplate(output,pagesize=A4,leftMargin=margin,rightMargin=margin,topMargin=margin,
                               bottomMargin=margin,title=f"{mark} Commission invoice {snapshot['document_number']}".strip())
    values=dict(snapshot)
    values.update(invoice_title="Tax Invoice",please_pay=_currency_money(snapshot["gross_cents"],"AUD"),
                  total_due=_currency_money(snapshot["gross_cents"],"AUD"),
                  due_date=snapshot.get("due_date") or "Pending school receipt confirmation",
                  payment_terms=snapshot.get("payment_terms") or "Payment within 30 days of school receipt of a valid invoice.")
    for key in ("net_cents","gst_cents","gross_cents"):
        values[key]=_currency_money(snapshot[key],"AUD")
    story=[]
    if mark:
        story.extend([Paragraph(f"<b>{mark}</b> · Sample for layout review. Do not submit or pay.",small),
                      Spacer(1,spacing)])
    for block in layout["blocks"]:
        kind=block["kind"]
        if block["heading"]:
            story.append(Paragraph(escape(block["heading"]),heading))
        if kind=="issuer" and layout.get("logo"):
            logo=Image(BytesIO(base64.b64decode(layout["logo"])))
            scale=min(1,125/logo.drawWidth,54/logo.drawHeight)
            logo.drawWidth*=scale
            logo.drawHeight*=scale
            logo.hAlign=style["alignment"].upper()
            story.append(logo)
        if kind == "footer":
            if layout["footer"]:
                story.append(Paragraph(escape(layout["footer"]), normal))
            if note := snapshot.get("customer_note"):
                story.append(Paragraph("Customer note: " + escape(note).replace("\n", "<br/>"), normal))
        elif kind=="items":
            fields=block["fields"]
            grid=[[Paragraph(escape(f["label"] or f["key"].title()),normal) for f in fields]]
            weights={"product":1.5,"description":2.65,"quantity":0.6,"rate":1.0,"gst":0.8,"amount":1.0}
            for line in snapshot["lines"]:
                quantity=int(line.get("quantity",1))
                row={"product":"School recruitment commission","description":line["description"],
                     "quantity":quantity,"rate":_money(line["net_cents"]//quantity),
                     "gst":commission_gst_indicator(line["gst_cents"], line.get("gst_mode") or snapshot.get("gst_mode")),
                     "amount":_money(line["net_cents"])}
                grid.append([Paragraph(escape(str(row[f["key"]])),right if f["key"] in {"rate","amount"} else normal)
                             for f in fields])
            columns=[width*weights[f["key"]]/sum(weights.values()) for f in fields]
            story.append(Table(grid,colWidths=columns,repeatRows=1,style=TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E9EFF5")),
                ("LINEBELOW",(0,0),(-1,0),1,accent),("VALIGN",(0,0),(-1,-1),"TOP"),
                ("BOTTOMPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),6),
            ])))
        else:
            for field in block["fields"]:
                key,label=field["key"],field["label"]
                value=escape(str(values.get(key) or "—")).replace("\n","<br/>")
                prefix=f"<b>{escape(label)}:</b> " if label else ""
                field_style = heading if key in {"issuer_name","invoice_title"} else normal
                if kind == "issuer" and key != "invoice_title":
                    field_style = issuer_name if key == "issuer_name" else issuer_text
                story.append(Paragraph(prefix+value,field_style))
        story.append(Spacer(1,spacing))
    def page_notice(pdf_canvas, doc):
        pdf_canvas.saveState()
        pdf_canvas.setFillColor(colors.HexColor("#922438"))
        pdf_canvas.setFont("Helvetica-Bold",8)
        if mark:
            pdf_canvas.drawString(margin,margin/2,mark)
        pdf_canvas.drawRightString(A4[0]-margin,margin/2,f"{snapshot['document_number']}  |  Page {doc.page}")
        pdf_canvas.restoreState()

    document.build(story,canvasmaker=_InvariantCanvas,onFirstPage=page_notice,onLaterPages=page_notice)
    return output.getvalue()


def _render_client_receipt(snapshot: dict, output_path: str | Path) -> str:
    output = BytesIO()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ClientReceiptBody", parent=styles["BodyText"], fontName="RevenueUnicode",
        fontSize=9, leading=12, textColor=colors.HexColor("#263247"),
    )
    heading = ParagraphStyle(
        "ClientReceiptHeading", parent=styles["Title"], fontName="RevenueUnicode",
        fontSize=20, leading=24, textColor=colors.HexColor("#17324D"),
    )
    right = ParagraphStyle("ClientReceiptRight", parent=body, alignment=TA_RIGHT)
    number = str(snapshot["receipt_number"])
    issued_date = str(snapshot["issued_at"])[:10]
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=17 * mm, bottomMargin=17 * mm,
        title=f"Receipt {number}", author=str(snapshot["issuer_name"]),
    )
    rows = [
        ("Student / 学生", f"{snapshot['student_name']} · {snapshot['student_number']}"),
        ("Application Cycle / 申请周期", snapshot["application_cycle_label"]),
        ("Contract / 合同", snapshot["contract_reference"]),
        ("Fee Type / 收费类型", snapshot["fee_type"]),
        ("Payment Date / 收款日期", snapshot["payment_date"]),
        ("Payment Method / 支付方式", snapshot["payment_method_label"]),
        (
            "Actual Amount / 实收金额",
            _currency_money(snapshot["actual_amount_cents"], snapshot["actual_currency_code"]),
        ),
        (
            "Allocated Base / 本次服务费",
            _currency_money(snapshot["allocated_base_cents"], "AUD"),
        ),
        ("GST", _currency_money(snapshot["gst_cents"], "AUD")),
        (
            "AUD Gross Charge / 本次澳元合计",
            _currency_money(snapshot["gross_charge_aud_cents"], "AUD"),
        ),
        ("Transaction Reference / 交易编号", snapshot.get("transaction_reference") or "—"),
        ("Issued Date / 签发日期", issued_date),
    ]
    story = [
        Paragraph(escape(str(snapshot["issuer_name"])), heading),
        Paragraph(f"ABN: {escape(str(snapshot['issuer_abn']))}", body),
        Spacer(1, 6 * mm),
        Table([
            [Paragraph("<b>Receipt / 收据</b>", heading),
             Paragraph(f"<b>{escape(number)}</b><br/>{escape(issued_date)}", right)],
        ], colWidths=[105 * mm, 52 * mm], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.7, colors.HexColor("#B7C3D0")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ])),
        Spacer(1, 6 * mm),
        Table(
            [[Paragraph(f"<b>{escape(label)}</b>", body),
              Paragraph(escape(str(value)), body)] for label, value in rows],
            colWidths=[62 * mm, 95 * mm],
            style=TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D2DAE2")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF0F5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]),
        ),
        Spacer(1, 9 * mm),
        Paragraph(
            "This Receipt is rendered from an immutable Revenue snapshot. / 本收据依据签发时的不可变财务快照生成。",
            body,
        ),
    ]
    document.build(story, canvasmaker=_InvariantCanvas)
    payload = output.getvalue()
    if output_path is None:
        return payload
    path = Path(output_path)
    write_immutable(path.parent, path.name, payload)
    return sha256(payload).hexdigest()


def render_receipt_pdf(snapshot: dict, output_path: str | Path) -> str:
    if "student_number" in snapshot:
        return _render_client_receipt(snapshot, output_path)
    lines = [{"description": f"Payment reference: {snapshot['payment_reference']}",
              "net_cents": snapshot["amount_cents"], "gst_cents": 0,
              "gross_cents": snapshot["amount_cents"]}]
    return _render(
        snapshot, output_path, "Receipt", "Received from", snapshot["received_from"],
        lines, [("Amount received", snapshot["amount_cents"])],
    )


def render_claim_pdf(snapshot: dict, output_path: str | Path) -> str:
    return _render(
        snapshot, output_path, "Commission Claim", "Claim to", snapshot["claim_to"],
        snapshot["lines"], [
            ("Net", snapshot["net_cents"]), ("GST", snapshot["gst_cents"]),
            ("Total", snapshot["gross_cents"]),
        ],
    )


def render_statement_pdf(snapshot: dict, output_path: str | Path) -> str:
    lines = []
    for item in snapshot["lines"]:
        lines.append({
            "description": item["description"],
            "net_cents": item["gross_cents"],
            "gst_cents": -item["share_cents"],
            "gross_cents": item["payable_cents"],
        })
    return _render(
        snapshot, output_path, "Master Agent Statement", "Master Agent",
        snapshot["master_agent"], lines, [
            ("Gross commission", snapshot["total_cents"]),
            ("Master Agent share", -snapshot["share_cents"]),
            ("Payable to AAIE", snapshot["payable_cents"]),
        ],
    )

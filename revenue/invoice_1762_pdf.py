"""Letter-sized commission invoice preset based on the supplied Invoice 1762."""

import base64
from datetime import date
from html import escape
from io import BytesIO

from revenue.money import commission_gst_indicator

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph, Table, TableStyle


REFERENCE_ORDER = ("issuer","invoice","bill_to","items","totals","footer","payment")


def _number(cents):
    dollars, remainder = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    return f"{sign}{dollars:,}.{remainder:02d}"


def render_invoice_1762(snapshot, layout, canvas_class):
    """Return None when custom content requires the existing paginated renderer."""
    style=layout["style"]
    size=style["size"]
    margin=style["margin"]
    spacing=style["spacing"]
    width,height=LETTER
    right=width-margin
    area=right-margin
    blue=colors.HexColor(style["accent"])
    light=colors.HexColor("#D9E8F1")
    line=colors.HexColor("#A8A8A8")
    dark=colors.HexColor("#151515")
    font="RevenueUnicode" if style["font"]=="unicode" else "Times-Roman"
    if font=="Times-Roman":
        try:
            str((snapshot,layout)).encode("cp1252")
        except UnicodeEncodeError:
            font="RevenueUnicode"
    align={"left":TA_LEFT,"center":TA_CENTER,"right":TA_RIGHT}[style["alignment"]]
    normal=ParagraphStyle("ReferenceNormal",fontName=font,fontSize=size,leading=size+4,
                          textColor=dark,alignment=align)
    left=ParagraphStyle("ReferenceLeft",parent=normal,alignment=TA_LEFT)
    issuer_name_size=style.get("issuer_name_size",size)
    issuer_text_size=style.get("issuer_text_size",size)
    issuer_name=ParagraphStyle("ReferenceIssuerName",parent=left,fontSize=issuer_name_size,leading=issuer_name_size+4)
    issuer_text=ParagraphStyle("ReferenceIssuerText",parent=left,fontSize=issuer_text_size,leading=issuer_text_size+4)
    heading=ParagraphStyle("ReferenceTitle",parent=left,fontSize=size+7,leading=size+10,textColor=blue)
    right_style=ParagraphStyle("ReferenceRight",parent=normal,alignment=TA_RIGHT)
    small=ParagraphStyle("ReferenceFooter",parent=left,fontSize=max(8,size-2),leading=max(10,size+1))
    card=ParagraphStyle("ReferenceCard",parent=normal,alignment=TA_CENTER,textColor=blue)
    pay_card=ParagraphStyle("ReferencePay",parent=card,textColor=colors.white)
    block={b["kind"]:b for b in layout["blocks"]}
    values=dict(snapshot)
    values.update(invoice_title="Tax Invoice",please_pay="A$" + _number(snapshot["gross_cents"]),
                  total_due="A$" + _number(snapshot["gross_cents"]),
                  due_date=(date.fromisoformat(snapshot["due_date"]).strftime("%d/%m/%Y") if snapshot.get("due_date")
                            else "Pending receipt confirmation"),
                  issue_date=date.fromisoformat(snapshot["issue_date"]).strftime("%d/%m/%Y"),
                  payment_terms=snapshot.get("payment_terms") or "Payment within 30 days of school receipt of a valid invoice.")
    for key in ("net_cents","gst_cents","gross_cents"):
        values[key]=_number(snapshot[key])

    output=BytesIO()
    c=canvas_class(output,pagesize=LETTER)
    test_only = snapshot.get("test_only",True)
    draft = snapshot.get("draft_notice") or snapshot["document_number"]=="DRAFT"
    mark = "TEST ONLY" if test_only else "DRAFT" if draft else ""
    c.setTitle(f"{mark} Commission invoice {snapshot['document_number']}".strip())

    def p(text,x,top,w,fmt):
        item=Paragraph(text,fmt)
        _,h=item.wrap(w,height)
        item.drawOn(c,x,top-h)
        return top-h

    def plain(value):
        return escape(str(value)).replace("\n","<br/>")

    def label(key,fields):
        return next((f["label"] for f in fields if f["key"]==key),"")

    def show_fields(fields,x,top,w,fmt,bold_first=False,name_style=None):
        for index,f in enumerate(fields):
            key=f["key"]
            prefix=plain(f["label"])+": " if f["label"] else ""
            text=prefix+plain(values.get(key) or "—")
            field_style = name_style if name_style is not None and key=="issuer_name" else fmt
            top=p(f"<b>{text}</b>" if bold_first and index==0 else text,x,top,w,field_style)
        return top

    if mark:
        c.setFillColor(colors.HexColor("#922438"))
        c.setFont("Helvetica-Bold",8)
        c.drawString(margin,height-18,mark+" - Sample for layout review. Do not submit or pay.")

    top=height-max(margin,38)
    issuer=block["issuer"]
    if issuer["heading"]:
        top_issuer=p(plain(issuer["heading"]),margin+6,top,area*.28,left)
    else:
        top_issuer=top
    issuer_end=show_fields(issuer["fields"],margin+6,top_issuer,area*.28,issuer_text,
                           bold_first=True,name_style=issuer_name)
    invoice=block["invoice"]
    headline_keys={"invoice_title","document_number"}
    cards={"issue_date","please_pay","due_date"}
    title="  ".join(str(values[f["key"]]) for f in invoice["fields"] if f["key"] in headline_keys)
    title_bottom=p(plain(title),margin+area*.30,top,area*.46,heading) if title else top
    if invoice["heading"]:
        title_bottom=p(plain(invoice["heading"]),margin+area*.30,title_bottom,area*.46,left)
    title_bottom=show_fields([f for f in invoice["fields"] if f["key"] not in headline_keys|cards],
                             margin+area*.30,title_bottom,area*.46,left)
    logo_bottom=top
    if layout.get("logo"):
        logo=ImageReader(BytesIO(base64.b64decode(layout["logo"])))
        lw,lh=logo.getSize()
        scale=min((area*.20)/lw,99/lh)
        lw,lh=lw*scale,lh*scale
        logo_bottom=top-lh
        c.drawImage(logo,right-lw-7,logo_bottom,width=lw,height=lh,mask="auto")

    divider=min(height-156,issuer_end-spacing,title_bottom-spacing,logo_bottom-spacing)
    c.setStrokeColor(blue)
    c.setLineWidth(1)
    c.line(margin,divider,right,divider)
    bill=block["bill_to"]
    bill_top=divider-3
    if bill["heading"]:
        bill_top=p("<b>"+plain(bill["heading"].upper())+"</b>",margin,bill_top,area*.43,left)
    bill_end=show_fields(bill["fields"],margin,bill_top,area*.43,left)
    card_fields=[f for f in invoice["fields"] if f["key"] in cards]
    card_end=divider
    if card_fields:
        group_x=margin+area*.51
        card_width=(right-group_x)/len(card_fields)
        card_end=divider-73
        for i,f in enumerate(card_fields):
            x=group_x+i*card_width
            is_pay=f["key"]=="please_pay"
            c.setFillColor(blue if is_pay else light)
            c.rect(x,card_end,card_width,70,fill=1,stroke=0)
            text=plain((f["label"] or f["key"]).upper())+"<br/>"
            value=plain(values[f["key"]])
            p(text+("<b>"+value+"</b>" if is_pay else value),x+5,divider-21,card_width-10,
              pay_card if is_pay else card)
        c.setStrokeColor(blue)
        c.setLineWidth(.5)
        c.line(group_x,card_end,right,card_end)

    items=block["items"]
    table_top=min(height-279,bill_end-2*spacing,card_end-2*spacing)
    if items["heading"]:
        table_top=p(plain(items["heading"]),margin,table_top,area,left)
    fields=items["fields"]
    weights={"product":.185,"description":.315,"quantity":.055,"rate":.12,"gst":.185,"amount":.14}
    right_columns={"quantity","rate","gst","amount"}
    right_small=ParagraphStyle("ReferenceColumn",parent=small,alignment=TA_RIGHT)
    grid=[[Paragraph("<b>"+plain((f["label"] or f["key"]).upper())+"</b>",
                     right_small if f["key"] in right_columns else small) for f in fields]]
    for item in snapshot["lines"]:
        quantity=int(item.get("quantity",1))
        row={"product":"School recruitment commission","description":item["description"],
             "quantity":quantity,"rate":_number(item["net_cents"] // quantity),
             "gst":commission_gst_indicator(item["gst_cents"], item.get("gst_mode") or snapshot.get("gst_mode")),
             "amount":_number(item["net_cents"])}
        grid.append([Paragraph(plain(row[f["key"]]),right_style if f["key"] in right_columns else left)
                     for f in fields])
    table=Table(grid,colWidths=[area*weights[f["key"]] for f in fields],repeatRows=1,style=TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),("LINEABOVE",(0,0),(-1,0),.5,line),
        ("LINEBELOW",(0,0),(-1,0),.5,line),("LINEBELOW",(0,-1),(-1,-1),.5,line),
        ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),4),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),4),
    ]))
    _,table_h=table.wrap(area,height)
    table.drawOn(c,margin,table_top-table_h)

    total_x=margin+area*.53
    total_w=right-total_x
    y=table_top-table_h-spacing
    totals=block["totals"]
    if totals["heading"]:
        y=p(plain(totals["heading"]),total_x,y,total_w,left)
    for f in totals["fields"]:
        key=f["key"]
        final=key=="total_due"
        if final:
            y-=spacing
            c.setStrokeColor(line)
            c.line(total_x,y,right,y)
            y-=4
        label_style=ParagraphStyle("ReferenceTotalLabel",parent=left,textColor=blue)
        end_label=p(plain((f["label"] or key).upper()),total_x+5,y,total_w*.57,label_style)
        number_style=ParagraphStyle("ReferenceFinal",parent=right_style,textColor=blue,fontSize=size+5,
                                   leading=size+9) if final else right_style
        end_number=p(plain(values.get(key) or "—"),total_x+total_w*.56,y,total_w*.44,number_style)
        y=min(end_label,end_number)
        if final:
            c.setStrokeColor(blue)
            c.line(total_x,y-5,right,y-5)
            y-=9
    footer=block["footer"]
    if footer["heading"]:
        y=p(plain(footer["heading"]),total_x,y,total_w,right_style)
    if layout["footer"]:
        thanks=ParagraphStyle("ReferenceThanks",parent=right_style,textColor=blue)
        y=p(plain(layout["footer"]),total_x,y-spacing,total_w,thanks)

    if note := snapshot.get("customer_note"):
        y = p("Customer note: " + plain(note), margin, y - spacing, area, left)

    payment=block["payment"]
    bank_top=max(108,margin+len(payment["fields"])*(size+2)+25)
    if y<bank_top+spacing:
        return None
    if payment["heading"]:
        bank_top=p(plain(payment["heading"])+":",margin,bank_top,area,small)-spacing
    bank_end=show_fields(payment["fields"],margin,bank_top,area,small)
    if bank_end<20:
        return None
    if mark:
        c.setFillColor(colors.HexColor("#922438"))
        c.setFont("Helvetica-Bold",7)
        c.drawRightString(right,10,mark+"  |  "+snapshot["document_number"])
    c.showPage()
    c.save()
    return output.getvalue()

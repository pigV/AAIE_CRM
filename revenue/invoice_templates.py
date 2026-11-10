"""Versioned layout and approved financial profile for school commission invoices."""

from copy import deepcopy
import base64
from io import BytesIO
from pathlib import Path
import json
import re

from PIL import Image

from revenue.pdf import render_configured_commission_invoice
from revenue.invoice_1762_pdf import REFERENCE_ORDER


LAYOUT_SOURCE = "commission_invoice_template"
PROFILE_SOURCE = "commission_invoice_test_finance_profile"
PUBLICATION = "commission_invoice_template_published"
ROLLBACK = "commission_invoice_template_rolled_back"
RENAME = "commission_invoice_template_renamed"
VERSION_NAME_LIMIT = 80
ISSUER_FONT_BOUNDS = (8, 18)
FORMAL_LAYOUT_SOURCE = "school_commission_formal_invoice_template"
FORMAL_PROFILE_SOURCE = "school_commission_formal_finance_profile"
FORMAL_PROFILE_APPROVED = "school_commission_formal_finance_approved"
TEST_PROFILE_SAVED = "commission_invoice_test_profile_saved"
FORMAL_INVOICE_KIND = "school_commission_invoice"
TEST_INVOICE_KIND = "toorak_test_invoice"

FIELD_GROUPS = {
    "issuer": ("issuer_name","issuer_address","issuer_email","issuer_website","issuer_abn"),
    "invoice": ("invoice_title","document_number","issue_date","please_pay","due_date"),
    "bill_to": ("bill_to","bill_to_address","bill_to_phone","bill_to_email"),
    "totals": ("net_cents","gst_cents","gross_cents","total_due"),
    "payment": ("account_name","bank_address","bank_name","bsb","account_number","swift","payment_terms"),
}
COLUMNS = ("product","description","quantity","rate","gst","amount")
LABELS = {
    "issuer_name":"", "issuer_address":"", "issuer_email":"Email", "issuer_website":"Website",
    "issuer_abn":"ABN", "invoice_title":"", "document_number":"Invoice no.", "issue_date":"Date",
    "please_pay":"Please Pay", "due_date":"Due Date", "bill_to":"", "bill_to_address":"", "bill_to_phone":"Tel", "bill_to_email":"Email",
    "net_cents":"Subtotal", "gst_cents":"GST Total", "gross_cents":"Total", "total_due":"Total Due",
    "account_name":"Account Name", "bank_address":"Bank Address", "bank_name":"Bank Name",
    "bsb":"BSB", "account_number":"Account Number", "swift":"SWIFT Code", "payment_terms":"Payment Terms",
    "product":"Product / Service","description":"Description","quantity":"Qty","rate":"Rate",
    "gst":"GST","amount":"Amount","claim_number":"Claim","student_id":"Student reference",
    "currency_code":"Currency",
}
FIELD_LABELS = {key:label or key.replace("_"," ").title() for key,label in LABELS.items() if key not in COLUMNS}
HEADINGS = {"issuer":"", "invoice":"", "bill_to":"Invoice To", "items":"",
            "totals":"", "payment":"How To Pay", "footer":""}
PROFILE_DEFAULT = {
    "issuer_name":"Aus-Asia International Education Alliance",
    "issuer_address":"Suite 1113, 401 Docklands Drive, Docklands VIC 3008",
    "issuer_email":"finance@example.test", "issuer_website":"www.aaie.com.au",
    "issuer_abn":"Pending Finance confirmation",
    "account_name":"Aus-Asia International Education Alliance Pty Ltd",
    "bank_address":"833 Collins Street, Docklands VIC 3008", "bank_name":"ANZ (test reference)",
    "bsb":"Pending Finance confirmation", "account_number":"Pending Finance confirmation",
    "swift":"Pending Finance confirmation",
}


FORMAL_PROFILE_FIELDS = (*PROFILE_DEFAULT, "bill_to_address")


def _formal_profile(values):
    fields = {field:_text(values.get(field),180) for field in FORMAL_PROFILE_FIELDS}
    placeholders = re.compile(r"pending|待确认|placeholder|test fixture|^n/?a$|^tbc$|^tbd$", re.I)
    if any(not value or placeholders.search(value) for value in fields.values()):
        raise ValueError("正式财务资料必须完整，不能使用待确认或占位内容。")
    checks = {
        "issuer_abn":r"\d{11}",
        "bsb":r"\d{6}",
        "account_number":r"\d{5,16}",
        "swift":r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?",
    }
    for key, pattern in checks.items():
        if not re.fullmatch(pattern,re.sub(r"[ -]","",fields[key]).upper()):
            raise ValueError(f"{FIELD_LABELS[key]} 格式不正确。")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",fields["issuer_email"]):
        raise ValueError("财务邮箱格式不正确。")
    return fields


def school_test_billing_address(school_name):
    if school_name.strip().casefold()=="toorak college":
        return "Old Mornington Road, PO Box 150, Mt Eliza VIC 3930"
    return "Pending school confirmation"


def default_layout():
    logo = Path(__file__).with_name("assets") / "invoice-1762-logo.png"
    return {
        "preset":"invoice_1762",
        "blocks":[{"kind":kind,"heading":HEADINGS[kind],"fields":[{"key":key,"label":LABELS[key]}
                   for key in FIELD_GROUPS.get(kind, COLUMNS if kind=="items" else ())]}
                  for kind in REFERENCE_ORDER],
        "style":{"accent":"#36749C","font":"unicode","size":10,"spacing":10,"alignment":"left","margin":18},
        "footer":"THANK YOU.", "logo":base64.b64encode(logo.read_bytes()).decode("ascii"),
    }


def _text(value, maximum):
    if not isinstance(value,str) or len(value)>maximum or any(ord(char)<32 and char!="\n" for char in value):
        raise ValueError("模板文字格式或长度不适用。")
    return value.strip()


def logo_data(payload):
    if not payload or len(payload)>100_000:
        raise ValueError("Logo 须为不超过 100 KB 的 PNG 或 JPEG。")
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format not in {"PNG","JPEG"} or image.width>1200 or image.height>1200:
                raise ValueError("Logo 须为 PNG 或 JPEG，尺寸不超过 1200 × 1200。")
            output=BytesIO()
            converted=image.convert("RGBA")
            converted.thumbnail((480,240))
            converted.save(output,format="PNG")
    except (OSError,Image.DecompressionBombError) as exc:
        raise ValueError("Logo 文件无法读取。") from exc
    data=output.getvalue()
    if len(data)>140_000:
        raise ValueError("Logo 转换后过大。")
    return base64.b64encode(data).decode("ascii")


def validate_layout(layout, logo=None):
    if not isinstance(layout,dict) or not isinstance(layout.get("style"),dict):
        raise ValueError("模板结构不正确。")
    style=layout["style"]
    accent=style.get("accent")
    if not isinstance(accent,str) or not re.fullmatch(r"#[0-9a-fA-F]{6}",accent):
        raise ValueError("请选择有效的颜色。")
    channels=[int(accent[i:i+2],16)/255 for i in (1,3,5)]
    linear=[c/12.92 if c<=0.04045 else ((c+0.055)/1.055)**2.4 for c in channels]
    luminance=sum(channel*weight for channel,weight in zip(linear,(0.2126,0.7152,0.0722)))
    if 1.05/(luminance+0.05)<4.5:
        raise ValueError("强调色过浅，请选择在白色背景上清晰的深色。")
    if style.get("font") not in ("unicode","serif") or style.get("alignment") not in ("left","center","right"):
        raise ValueError("字体或对齐方式不适用。")
    preset=layout.get("preset","block_flow")
    if preset not in ("invoice_1762","block_flow"):
        raise ValueError("请选择支持的版式预设。")
    bounds={"size":(8,14),"spacing":(4,24),"margin":(16,64)}
    for key,(low,high) in bounds.items():
        if type(style.get(key)) is not int or not low<=style[key]<=high:
            raise ValueError("字号、间距或页边距超出支持范围。")
    issuer_fonts = {}
    for key in ("issuer_name_size", "issuer_text_size"):
        if key in style:
            value = style[key]
            if type(value) is not int or not ISSUER_FONT_BOUNDS[0] <= value <= ISSUER_FONT_BOUNDS[1]:
                raise ValueError("抬头字号须为 8 至 18 pt 的整数。")
            issuer_fonts[key] = value
    blocks=layout.get("blocks")
    if not isinstance(blocks,list) or len(blocks)!=len(HEADINGS):
        raise ValueError("模板必须包含七个发票区块。")
    result=[]
    seen=set()
    data_keys=set()
    for block in blocks:
        kind=block.get("kind") if isinstance(block,dict) else None
        if not isinstance(kind,str) or kind not in HEADINGS or kind in seen:
            raise ValueError("发票区块缺失或重复。")
        seen.add(kind)
        fields=block.get("fields")
        if not isinstance(fields,list) or len(fields)>16:
            raise ValueError("一个区块最多包含 16 个字段。")
        valid=COLUMNS if kind=="items" else FIELD_LABELS if kind!="footer" else ()
        normalized=[]
        used=set()
        for field in fields:
            key=field.get("key") if isinstance(field,dict) else None
            if not isinstance(key,str) or key not in valid or key in used:
                raise ValueError("存在未知或重复字段。")
            used.add(key)
            normalized.append({"key":key,"label":_text(field.get("label",""),50)})
        if kind=="items" and used!=set(COLUMNS):
            raise ValueError("佣金明细必须保留全部六列。")
        data_keys.update(used)
        result.append({"kind":kind,"heading":_text(block.get("heading",""),50),"fields":normalized})
    mandatory=set().union(*FIELD_GROUPS.values()) - {"bill_to_phone", "bill_to_email"}
    if not mandatory.issubset(data_keys):
        raise ValueError("发票必填内容缺失："+"、".join(FIELD_LABELS[k] for k in sorted(mandatory-data_keys)))
    if preset=="invoice_1762" and tuple(block["kind"] for block in result)!=REFERENCE_ORDER:
        preset="block_flow"
    return {
        "preset":preset,"blocks":result,"style":{**{k:style[k] for k in (*bounds,"accent","font","alignment")},**issuer_fonts},
        "footer":_text(layout.get("footer",""),240),"logo":logo,
    }


def _latest_event(connection, actions, source):
    marks=",".join("?" for _ in actions)
    row=connection.execute(
        f"SELECT after_json FROM revenue_audit_events WHERE aggregate_uid=? AND action IN ({marks}) ORDER BY id DESC LIMIT 1",
        (source,*actions),
    ).fetchone()
    return json.loads(row[0]) if row else {}


def _sources(formal, entity_id, simulation):
    if not formal:
        return LAYOUT_SOURCE,PROFILE_SOURCE
    if type(entity_id) is not int or entity_id<=0:
        raise ValueError("正式发票模板和财务资料须绑定到已授权的学校付款主体。")
    scope = f"{entity_id}:{'simulation' if simulation else 'production'}"
    return f"{FORMAL_LAYOUT_SOURCE}:{scope}",f"{FORMAL_PROFILE_SOURCE}:{scope}"


def active_template_uid(connection, *, formal=False, entity_id=None, simulation=True):
    source,_ = _sources(formal,entity_id,simulation)
    return _latest_event(connection,(PUBLICATION,ROLLBACK),source).get("template_uid","")


def invoice_origin_predicate():
    """Match invoice alias i to one origin using its immutable Claim evaluations."""
    return """(?=0 OR (EXISTS (
                 SELECT 1 FROM revenue_invoice_lines ol WHERE ol.invoice_id=i.id)
               AND NOT EXISTS (
                 SELECT 1 FROM revenue_invoice_lines ol WHERE ol.invoice_id=i.id
                   AND NOT EXISTS (
                     SELECT 1 FROM revenue_claims oc
                     JOIN revenue_claim_eligibility_evaluations oe ON oe.claim_id=oc.id
                    WHERE ol.source_kind='claim' AND oc.uid=ol.source_uid AND oe.eligible=1
                      AND json_extract(oe.authoritative_facts_json,'$.school_commission_formal')=1
                      AND json_extract(oe.authoritative_facts_json,'$.school_commission_simulation')=?))))"""


def _version(connection, uid, source):
    row=connection.execute(
        "SELECT * FROM revenue_rule_snapshots WHERE uid=? AND source_rule_uid=? AND rule_kind='commission'",
        (uid,source),
    ).fetchone()
    if row is None:
        raise ValueError("所选模板或测试财务资料版本不存在。")
    return {**dict(row),"value":json.loads(row["canonical_json"])["value"]}


def financial_profile(connection, *, formal=False, entity_id=None, simulation=True):
    _,source = _sources(formal,entity_id,simulation)
    action = FORMAL_PROFILE_APPROVED if formal else TEST_PROFILE_SAVED
    uid = _latest_event(connection,(action,),source).get("profile_uid")
    if uid:
        return _version(connection,uid,source)["value"],uid
    default = dict.fromkeys(FORMAL_PROFILE_FIELDS,"") if formal else deepcopy(PROFILE_DEFAULT)
    return default,None


def current_invoice_template_revisions(connection, *, formal=False, entity_id=None, simulation=True,
                                       include_billing=False):
    revisions = (active_template_uid(connection,formal=formal,entity_id=entity_id,simulation=simulation),
                 financial_profile(connection,formal=formal,entity_id=entity_id,simulation=simulation)[1] or "")
    if formal and include_billing:
        from revenue.invoice_billing_details import billing_details

        billing = billing_details(connection, entity_id, simulation)
        revisions += (billing["revision"], billing["settings_revision"])
    return revisions


def _bill_to_layout(layout, data):
    """Let already published Invoice 1762 layouts display newly configured contact fields."""
    if layout.get("preset") != "invoice_1762":
        return layout
    selected = {field["key"] for block in layout["blocks"] for field in block["fields"]}
    missing = [{"key": key, "label": LABELS[key]} for key in FIELD_GROUPS["bill_to"]
               if data.get(key) and key not in selected]
    if not missing:
        return layout
    layout = deepcopy(layout)
    block = next(block for block in layout["blocks"] if block["kind"] == "bill_to")
    block["fields"].extend(missing)
    return layout


def render_with_active_template(connection, snapshot, *, formal=False, simulation=True, entity_id=None):
    layout_source,profile_source = _sources(formal,entity_id,simulation)
    uid = active_template_uid(connection,formal=formal,entity_id=entity_id,simulation=simulation)
    profile,profile_uid = financial_profile(connection,formal=formal,entity_id=entity_id,simulation=simulation)
    if formal and (not uid or not profile_uid):
        raise ValueError("正式签发须先发布发票模板并批准完整的开票及收款资料。")
    if not uid:
        return None
    layout = _version(connection,uid,layout_source)["value"]
    data = {**snapshot,**profile}
    if formal:
        from revenue.invoice_billing_details import bill_to_fields

        _formal_profile(profile)
        fields = snapshot.get("invoice_bill_to")
        data.update(bill_to_fields(connection, entity_id, simulation) if fields is None else fields)
        if data.get("bill_to_phone") or data.get("bill_to_email"):
            layout = _bill_to_layout(layout, data)
        approved = _latest_event(connection,(FORMAL_PROFILE_APPROVED,),profile_source)
        if not simulation and approved.get("simulation") is not False:
            raise ValueError("正式签发须使用当前环境独立批准的开票资料。")
    else:
        data["bill_to_address"] = school_test_billing_address(snapshot["bill_to"])
    data["test_only"] = not formal or simulation
    return render_configured_commission_invoice(data,layout),uid,profile_uid


class CommissionInvoiceTemplates:
    def __init__(self,service,actor,role, *, formal=False, simulation=True, entity_id=None):
        if role!="Admin":
            raise PermissionError("Commission invoice templates require Admin")
        self.service=service
        self.repository=service.repository
        self.connection=service.connection
        self.actor=actor
        self.formal=formal
        self.simulation=simulation
        self.entity_id=entity_id
        self.layout_source,self.profile_source=_sources(formal,entity_id,simulation)
        self.profile_fields=FORMAL_PROFILE_FIELDS if formal else tuple(PROFILE_DEFAULT)
        self.invoice_kind=FORMAL_INVOICE_KIND if formal else TEST_INVOICE_KIND

    def state(self, selected=""):
        rows=[dict(row) for row in self.connection.execute(
            """SELECT s.uid,s.created_at,
                      COALESCE((SELECT json_extract(a.after_json,'$.name')
                                FROM revenue_audit_events a
                                WHERE a.aggregate_uid=s.uid AND a.action=?
                                ORDER BY a.id DESC LIMIT 1),
                               json_extract(s.canonical_json,'$.version_name'),'') AS name
                 FROM revenue_rule_snapshots s WHERE s.source_rule_uid=? ORDER BY s.id DESC""",
            (RENAME,self.layout_source),
        )]
        active=active_template_uid(self.connection,formal=self.formal,entity_id=self.entity_id,simulation=self.simulation)
        uid=selected or (rows[0]["uid"] if rows else "")
        published={row[0] for row in self.connection.execute(
            "SELECT json_extract(after_json,'$.template_uid') FROM revenue_audit_events WHERE action=? AND aggregate_uid=?",
            (PUBLICATION,self.layout_source),
        )}
        samples=[dict(row) for row in self.connection.execute(
            """SELECT i.uid,i.invoice_number,i.gross_cents FROM revenue_invoices i
                 JOIN revenue_documents d ON d.id=i.document_id
                WHERE i.document_kind='direct_commission_invoice' AND i.status='issued'
                  AND d.document_kind=? AND (? IS NULL OR i.revenue_entity_id=?) AND """
            + invoice_origin_predicate() + " ORDER BY i.id",
            (self.invoice_kind,self.entity_id,self.entity_id,self.formal,self.simulation),
        )]
        layout=_version(self.connection,uid,self.layout_source)["value"] if uid else default_layout()
        profile,profile_uid=financial_profile(self.connection,formal=self.formal,entity_id=self.entity_id,simulation=self.simulation)
        return {"layout":layout,"versions":rows,"active_uid":active,"selected_uid":uid,
                "published":published,"samples":samples,"profile":profile,"profile_uid":profile_uid or "","formal":self.formal}

    def save(self, layout, key, *, base_uid="", logo=None, remove_logo=False, name=""):
        name = self._version_name(name)
        base=_version(self.connection,base_uid,self.layout_source)["value"] if base_uid else default_layout()
        stored=base.get("logo")
        if stored is None and base.get("preset")!="invoice_1762" and isinstance(layout,dict) and layout.get("preset")=="invoice_1762":
            stored=default_layout()["logo"]
        approved_logo=logo_data(logo) if logo is not None else None if remove_logo else stored
        normalized=validate_layout(layout,approved_logo)

        snapshot = {"kind":self.layout_source,"value":normalized}
        if name:
            snapshot["version_name"] = name

        def operation():
            return self.service._rule_snapshot("commission",self.layout_source,snapshot,self.actor)

        return self.repository.command(
            idempotency_key=key,command_type=("school_commission_formal_template_saved" if self.formal
                                             else "commission_invoice_template_saved"),
            aggregate_type="invoice_template",
            payload=snapshot if name else normalized,actor_user_id=self.actor,operation=operation,
        )

    @staticmethod
    def _version_name(value):
        name = _text(value, VERSION_NAME_LIMIT)
        if any(char in value for char in ("\r", "\n")):
            raise ValueError("模板版本名称须填写在一行内。")
        return name

    def rename(self, uid, name, key):
        name = self._version_name(name)
        if not name:
            raise ValueError("请输入模板版本名称。")

        _version(self.connection,uid,self.layout_source)

        def operation():
            return {"uid":uid,"name":name}

        return self.repository.command(
            idempotency_key=key,command_type=RENAME,aggregate_type="invoice_template",
            payload={"uid":uid,"name":name},actor_user_id=self.actor,operation=operation,
        )

    def save_profile(self, values, key, *, expected_uid, approved=False):
        if self.formal:
            if approved is not True:
                raise ValueError("请确认财务已核准本版本的开票及收款资料。")
            normalized = _formal_profile(values)
            action = FORMAL_PROFILE_APPROVED
        else:
            normalized = {field:_text(values.get(field),180) for field in PROFILE_DEFAULT}
            if not all(normalized.values()):
                raise ValueError("测试财务资料请填写明确值或待确认占位。")
            action = TEST_PROFILE_SAVED

        def operation():
            if (financial_profile(self.connection,formal=self.formal,entity_id=self.entity_id,simulation=self.simulation)[1] or "")!=expected_uid:
                raise ValueError("测试财务资料已被更新，请刷新后重试。")
            result=self.service._rule_snapshot("commission",self.profile_source,
                   {"kind":self.profile_source,"value":normalized},self.actor)
            return {"uid":self.profile_source,"profile_uid":result["uid"],
                    **({"approved_by":self.actor,"simulation":self.simulation,"entity_id":self.entity_id} if self.formal else {})}

        return self.repository.command(
            idempotency_key=key,command_type=action,aggregate_type="invoice_template",
            payload={"profile":normalized,"expected_uid":expected_uid},actor_user_id=self.actor,operation=operation,
        )

    def publish(self,uid,expected_active,key,rollback=False):
        def operation():
            if active_template_uid(self.connection,formal=self.formal,entity_id=self.entity_id,simulation=self.simulation)!=expected_active:
                raise ValueError("已发布版本发生变化，请刷新后重试。")
            layout=_version(self.connection,uid,self.layout_source)["value"]
            validate_layout(layout,layout.get("logo"))
            if rollback and not self.connection.execute(
                """SELECT 1 FROM revenue_audit_events WHERE action=? AND aggregate_uid=?
                    AND json_extract(after_json,'$.template_uid')=?""",(PUBLICATION,self.layout_source,uid),
            ).fetchone():
                raise ValueError("仅可回退到曾发布的模板版本。")
            return {"uid":self.layout_source,"template_uid":uid,"previous_uid":expected_active}

        return self.repository.command(
            idempotency_key=key,command_type=ROLLBACK if rollback else PUBLICATION,aggregate_type="invoice_template",
            payload={"template_uid":uid,"expected_active":expected_active},actor_user_id=self.actor,operation=operation,
        )

    def preview_sample(self, template_uid, *, authority_name=None):
        if not self.formal:
            raise ValueError("请选择正式发票模板。")
        layout = (_version(self.connection,template_uid,self.layout_source)["value"]
                  if template_uid else default_layout())
        profile,uid = financial_profile(self.connection,formal=True,entity_id=self.entity_id,
                                        simulation=self.simulation)
        if not uid:
            profile = {**PROFILE_DEFAULT,"bill_to_address":"3 Example Street, Melbourne VIC 3000"}
        lines = [
            {"description":"DEMO-STUDENT-A | DEMO-CLAIM-A | first-year local tuition commission (5%)",
             "net_cents":50000,"gst_cents":5000,"gross_cents":55000},
            {"description":"DEMO-STUDENT-B | DEMO-CLAIM-B | first-year international tuition commission (17%)",
             "net_cents":170000,"gst_cents":17000,"gross_cents":187000},
        ]
        if authority_name:
            lines = [{**line, "description": f"DEMO-STUDENT-{i} | Commission sample"}
                     for i, line in enumerate(lines, 1)]
        amounts = {key:sum(line[key] for line in lines) for key in ("net_cents","gst_cents","gross_cents")}
        sample = {**profile,**amounts,"document_number":"DRAFT","issue_date":"2026-11-07","test_only":True,
                  "bill_to":f"{authority_name or 'Toorak College'} (synthetic)","claim_number":"DEMO-CLAIM-A\nDEMO-CLAIM-B",
                  "student_id":"DEMO-STUDENT-A\nDEMO-STUDENT-B","lines":lines}
        return render_configured_commission_invoice(sample,layout)

    def preview(self,invoice_uid,template_uid):
        invoice=self.connection.execute(
            """SELECT i.*,e.legal_name FROM revenue_invoices i
               JOIN revenue_documents d ON d.id=i.document_id
               JOIN revenue_entities e ON e.id=i.revenue_entity_id
               WHERE i.uid=? AND i.document_kind='direct_commission_invoice' AND i.status='issued'
                 AND d.document_kind=? AND (? IS NULL OR i.revenue_entity_id=?) AND """ + invoice_origin_predicate(),
            (invoice_uid,self.invoice_kind,self.entity_id,self.entity_id,self.formal,self.simulation),
        ).fetchone()
        if invoice is None:
            raise ValueError("请选择已签发的 Toorak 测试佣金发票。")
        lines=self.connection.execute(
            """SELECT il.*,c.claim_number FROM revenue_invoice_lines il
                 JOIN revenue_claims c ON c.uid=il.source_uid
                WHERE il.invoice_id=? AND il.source_kind='claim' ORDER BY il.sort_order,il.id""",
            (invoice["id"],),
        ).fetchall()
        if not lines:
            raise ValueError("测试发票缺少 Claim 明细。")
        data=dict(invoice)
        profile,_=financial_profile(self.connection,formal=self.formal,entity_id=self.entity_id,simulation=self.simulation)
        layout=_version(self.connection,template_uid,self.layout_source)["value"] if template_uid else default_layout()
        return render_configured_commission_invoice({
            **data,**profile,"document_number":data["invoice_number"],"bill_to":data["legal_name"],
            "bill_to_address":profile["bill_to_address"] if self.formal else school_test_billing_address(data["legal_name"]),
            "test_only":not self.formal or self.simulation,"draft_notice":self.formal,
            "claim_number":"\n".join(line["claim_number"] for line in lines),
            "student_id":"\n".join(line["student_id"] for line in lines),
            "payment_terms":"Payment due date is confirmed after school receipt.",
            "lines":[dict(line) for line in lines],
        },layout)

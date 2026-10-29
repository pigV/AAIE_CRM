"""Commercial Case operational workbench read model.

This module is intentionally query-only.  Every mutation remains in
``RevenueUIActions`` and the Revenue domain/service layer.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from revenue.read_models import RevenueReadModels, exception_code_label


class RevenueWorkbench:
    SECTION_DEFINITIONS = (
        ("student_application", "学生与申请"),
        ("client_billing", "客户收费"),
        ("components", "入学组成"),
        ("tuition_commencement", "学费与实际入学"),
        ("direct_commission", "直接佣金"),
        ("master_agent", "一级代理"),
        ("invoice_payment_receipt", "收款、收据与历史发票"),
        ("documents_evidence", "文件与证据"),
        ("exceptions", "异常与待处理"),
        ("timeline_audit", "时间线与审计"),
    )

    ACTIONS = (
        ("客户收费", None),
        ("直接佣金", "/revenue/direct-commission#revenue-action"),
        ("一级代理", "/revenue/master-agent#revenue-action"),
        ("合同与规则", "/revenue/contracts#revenue-action"),
        ("对账", "/revenue/reconciliation"),
    )

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def load(self, case_uid: str) -> dict[str, Any]:
        case_row = self.connection.execute(
            "SELECT * FROM revenue_commercial_cases WHERE uid=?", (case_uid,)
        ).fetchone()
        if not case_row:
            raise LookupError("Revenue Commercial Case not found")
        case_projection = RevenueReadModels(self.connection)._case_projection(case_row)
        case = dict(case_row)
        case_id = int(case["id"])
        cycle_id = int(case["application_cycle_id"])
        student_id = str(case["student_id"])
        cycle = self._one("SELECT * FROM application_cycles WHERE id=?", (cycle_id,))
        components = self._rows(
            "SELECT * FROM revenue_enrolment_components WHERE commercial_case_id=? ORDER BY id",
            (case_id,),
        )
        component_ids = [int(row["id"]) for row in components]
        tuition = self._by_ids(
            "revenue_tuition_facts", "enrolment_component_id", component_ids
        )
        commencement = self._by_ids(
            "revenue_commencement_facts", "enrolment_component_id", component_ids
        )
        commissions = self._rows(
            "SELECT * FROM revenue_commission_lines WHERE commercial_case_id=? ORDER BY id",
            (case_id,),
        )
        line_ids = [int(row["id"]) for row in commissions]
        statements = self._statement_items(line_ids)
        invoices = self._rows(
            "SELECT * FROM revenue_invoices WHERE commercial_case_id=? ORDER BY id",
            (case_id,),
        )
        fee_items = self._client_fee_items(case_id)
        payments = self._case_payments(case_id)
        payment_ids = [int(row["id"]) for row in payments]
        receipts = self._receipts(payment_ids)
        contract = self._one(
            """SELECT id,contract_status,contract_date,fully_executed_at
                 FROM contracts
                WHERE student_id=? AND application_cycle_id=?
                  AND contract_status='FullyExecuted'
                ORDER BY fully_executed_at DESC,id DESC LIMIT 1""",
            (student_id, cycle_id),
        )
        exceptions = self._rows(
            """SELECT * FROM revenue_exceptions
                WHERE aggregate_uid=? OR (aggregate_type='commercial_case' AND aggregate_uid=?)
                ORDER BY id DESC""",
            (case_uid, case_uid),
        )
        exceptions = [self._exception_projection(item) for item in exceptions]
        documents = self._case_documents(case_id, student_id)
        audit = self._rows(
            """SELECT * FROM revenue_audit_events
                WHERE aggregate_uid=? OR aggregate_uid IN (
                    SELECT uid FROM revenue_enrolment_components WHERE commercial_case_id=?
                ) ORDER BY id DESC LIMIT 100""",
            (case_uid, case_id),
        )
        business_summary = {
            "student_name": case_projection["student_name"],
            "student_id": student_id,
            "classification": case_projection["classification"],
            "business_type": case_projection["business_type"],
            "application_cycle": self._cycle_business_label(cycle),
            "institution": case_projection["institution"],
            "intake": case_projection["intake"],
            "billing_status": case_projection["billing_status"],
            "commission_status": case_projection["commission_status"],
            "case_status": case_projection["status"],
        }
        contract_summary = self._contract_summary(contract)
        scoped_billing_url = f"/revenue/cases/{quote(case_uid, safe='')}/billing"
        client_billing_summary = [
            self._fee_item_summary(item, scoped_billing_url) for item in fee_items
        ]
        payment_history = [self._payment_summary(item) for item in payments]
        receipt_records = [self._receipt_summary(item) for item in receipts]
        evidence_records = [
            {
                "label": self._document_label(item),
                "view_url": item.get("protected_view_url"),
                "download_url": item.get("protected_download_url"),
                "protected_view_url": item.get("protected_view_url"),
            }
            for item in documents
            if item.get("protected_view_url")
        ]
        section_rows = {
            "student_application": [business_summary],
            "client_billing": client_billing_summary,
            "components": components,
            "tuition_commencement": tuition + commencement,
            "direct_commission": commissions,
            "master_agent": statements,
            # Keep historical invoices in the authorized read model for audit
            # compatibility. The business renderer intentionally presents only
            # Payment/Receipt summaries and keeps invoice rows in audit details.
            "invoice_payment_receipt": invoices + payment_history + receipt_records,
            "documents_evidence": evidence_records,
            "exceptions": exceptions,
            "timeline_audit": audit,
        }
        return {
            "schema_version": "revenue.v1",
            "case": case,
            "cycle": cycle,
            "title": "收费与佣金档案",
            "classification": "现有 AAIE K–12" if case["existing_aaie_k12"] else "新客户",
            "business_summary": business_summary,
            "contract_summary": contract_summary,
            "client_billing_summary": client_billing_summary,
            "payment_history": payment_history,
            "receipt_records": receipt_records,
            "evidence_records": evidence_records,
            "technical_information": {
                "case": case,
                "cycle": cycle or {},
                "fee_items": fee_items,
                "payments": payments,
                "receipts": receipts,
                "historical_invoices": invoices,
            },
            "context_links": self._context_links(case, cycle, components),
            "sections": [
                {"key": key, "title": title, "rows": section_rows[key]}
                for key, title in self.SECTION_DEFINITIONS
            ],
            "actions": [
                {
                    "kind": "link",
                    "label": label,
                    "href": scoped_billing_url if label == "客户收费" else href,
                }
                for label, href in self.ACTIONS
            ],
            "operations": {
                "components": [
                    {
                        "uid": str(component["uid"]),
                        "label": self._component_label(component),
                    }
                    for component in components
                    if component.get("status") == "active"
                ],
                "evidence_documents": [
                    {"id": int(document["id"]), "label": self._document_label(document)}
                    for document in documents
                    if document.get("uid") and document.get("id")
                ],
            },
        }

    @classmethod
    def _contract_summary(cls, contract):
        if not contract:
            return {
                "status_code": "missing",
                "status_label": "未关联已签服务合同",
                "fully_executed_display": "—",
            }
        return {
            "status_code": str(contract.get("contract_status") or ""),
            "status_label": "已签署完成",
            "fully_executed_display": cls._melbourne_datetime(contract.get("fully_executed_at")),
        }

    @staticmethod
    def _melbourne_datetime(value):
        if not value:
            return "—"
        normalized = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
            local = parsed.astimezone(ZoneInfo("Australia/Melbourne"))
        except (ValueError, TypeError):
            return "—"
        return f"{local.day} {local.strftime('%b %Y, %I:%M %p').lstrip('0')}"

    @staticmethod
    def _fee_item_summary(item, billing_url="/revenue/billing"):
        uid = str(item["uid"])
        return {
            "fee_item_uid": uid,
            "description": str(item.get("description") or "基础服务费"),
            "final_base_cents": int(item["final_base_cents"]),
            "base_received_cents": int(item["base_received_cents"]),
            "gst_collected_cents": int(item["gst_collected_cents"]),
            "outstanding_base_cents": int(item["outstanding_base_cents"]),
            "payment_status": str(item["payment_status"]),
            "add_payment_href": f"{billing_url}?item={quote(uid, safe='')}#revenue-action",
        }

    @classmethod
    def _payment_summary(cls, item):
        method_labels = {
            "alipay": "支付宝",
            "wechat": "微信",
            "au_personal_account": "澳洲私人账号",
            "au_company_account": "澳洲公司账号",
        }
        method_code = str(item.get("client_payment_method") or "")
        return {
            "date_display": cls._melbourne_date(item.get("received_date")),
            "method_label": method_labels.get(method_code, method_code or "其他方式"),
            "currency_code": str(item.get("currency_code") or "AUD"),
            "actual_amount_cents": int(item.get("amount_cents") or 0),
            "allocated_base_cents": int(item.get("client_base_received_cents") or 0),
            "gst_cents": int(item.get("client_gst_cents") or 0),
            "evidence_view_url": item.get("evidence_view_url"),
            "receipt_number": item.get("receipt_number"),
            "receipt_view_url": item.get("receipt_view_url"),
        }

    @classmethod
    def _receipt_summary(cls, item):
        return {
            "receipt_number": str(item.get("receipt_number") or "—"),
            "amount_cents": int(item.get("amount_cents") or 0),
            "currency_code": str(item.get("currency_code") or "AUD"),
            "issued_at_display": cls._melbourne_datetime(item.get("issued_at")),
            "view_url": item.get("protected_view_url"),
            "receipt_view_url": item.get("protected_view_url"),
            "download_url": item.get("protected_download_url"),
        }

    @staticmethod
    def _melbourne_date(value):
        if not value:
            return "—"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Australia/Melbourne"))
            return f"{parsed.day} {parsed.strftime('%b %Y')}"
        except (ValueError, TypeError):
            return "—"

    @staticmethod
    def _cycle_business_label(cycle):
        if not cycle:
            return "未关联"
        cycle_type = {
            "initial": "首次申请",
            "transfer": "转学申请",
            "progression": "升学申请",
        }.get(str(cycle.get("cycle_type") or ""), "其他申请")
        status = {
            "active": "进行中",
            "closed": "已关闭",
            "cancelled": "已取消",
            "on_hold": "已暂停",
        }.get(str(cycle.get("status") or ""), str(cycle.get("status") or "未设置"))
        return f"{cycle_type} · {status}"

    @staticmethod
    def _exception_projection(item):
        projected = {
            "异常原因": exception_code_label(item.get("exception_code")),
            "状态": item.get("status"),
            "说明": item.get("message"),
            "技术信息": item.get("exception_code"),
        }
        return {key: value for key, value in projected.items() if value not in (None, "")}

    @staticmethod
    def _component_label(component):
        kind = {
            "school": "学校课程",
            "higher_education": "高等教育课程",
            "package_segment": "Package 课程段",
        }.get(component.get("component_kind"), "入学组成")
        source = {
            "school_enrolment": "学校在读记录",
            "final_acceptance": "最终接受",
            "higher_education_application": "高等教育申请",
            "higher_education_package_segment": "高等教育课程段",
            "coe_language": "COE 语言课程",
        }.get(component.get("source_kind"), "权威业务记录")
        return f"{kind} · {source} #{component.get('source_id')}"

    @staticmethod
    def _document_label(document):
        return str(
            document.get("original_name")
            or document.get("document_type")
            or document.get("document_kind")
            or document.get("uid")
        )

    def _context_links(self, case, cycle, components):
        student_id = quote(str(case["student_id"]), safe="")
        cycle_id = int(case["application_cycle_id"])
        links = [
            {"kind": "student", "label": "学生档案", "href": f"/students/{student_id}"},
            {"kind": "cycle", "label": "Application Cycle", "href": f"/students/{student_id}?tab=application-workflow#cycle-{cycle_id}"},
            {"kind": "contract", "label": "客户合同", "href": f"/contracts?student_id={student_id}"},
            {"kind": "offer", "label": "Offer", "href": f"/students/{student_id}?tab=application-workflow#offers"},
            {"kind": "coe", "label": "COE", "href": f"/students/{student_id}?tab=application-workflow#coe"},
            {"kind": "visa", "label": "Visa", "href": f"/students/{student_id}?tab=visa"},
        ]
        for component in components:
            enrolment_id = component.get("school_enrolment_id")
            if enrolment_id:
                row = self._one(
                    "SELECT school_id FROM student_school_enrolments WHERE id=?",
                    (enrolment_id,),
                )
                if row and row.get("school_id"):
                    links.append({
                        "kind": "school", "label": "学校",
                        "href": f"/schools/{int(row['school_id'])}",
                    })
            if component.get("higher_education_application_id"):
                links.append({
                    "kind": "higher_education", "label": "高等教育申请",
                    "href": f"/higher-education?cycle={cycle_id}",
                })
        seen = set()
        return [link for link in links if not (link["href"] in seen or seen.add(link["href"]))]

    def _statement_items(self, line_ids):
        if not line_ids:
            return []
        marks = ",".join("?" for _ in line_ids)
        return self._rows(
            f"""SELECT i.*,m.match_status,m.matched_cents
                  FROM revenue_statement_matches m
                  JOIN revenue_master_agent_statement_items i ON i.id=m.statement_item_id
                 WHERE m.commission_line_id IN ({marks}) ORDER BY i.id""",
            tuple(line_ids),
        )

    def _case_payments(self, case_id):
        return self._rows(
            """SELECT DISTINCT p.*,
                      ca.allocated_base_cents AS client_base_received_cents,
                      ca.gst_cents AS client_gst_cents,
                      ca.method_code_snapshot AS client_payment_method,
                      ed.uid AS evidence_document_uid,
                      CASE WHEN ed.uid IS NOT NULL
                           THEN '/revenue/documents/' || ed.uid || '/view' END AS evidence_view_url,
                      r.receipt_number,
                      rd.uid AS receipt_document_uid,
                      CASE WHEN rd.uid IS NOT NULL
                           THEN '/revenue/documents/' || rd.uid || '/view' END AS receipt_view_url
                 FROM revenue_payments p
            LEFT JOIN revenue_client_payment_allocations ca ON ca.payment_id=p.id
            LEFT JOIN revenue_fee_items cf ON cf.id=ca.fee_item_id
            LEFT JOIN revenue_documents ed ON ed.id=p.evidence_document_id
            LEFT JOIN revenue_receipts r ON r.payment_id=p.id
            LEFT JOIN revenue_documents rd ON rd.id=r.document_id
            LEFT JOIN revenue_payment_allocations a ON a.payment_id=p.id
            LEFT JOIN revenue_invoice_lines il ON il.id=a.invoice_line_id
            LEFT JOIN revenue_invoices i ON i.id=il.invoice_id
            LEFT JOIN revenue_commission_lines c ON c.id=a.commission_line_id
                WHERE cf.commercial_case_id=? OR i.commercial_case_id=?
                   OR c.commercial_case_id=? ORDER BY p.id""",
            (case_id, case_id, case_id),
        )

    def _client_fee_items(self, case_id):
        rows = self._rows(
            """SELECT f.*,
                      COALESCE((SELECT SUM(da.allocated_cents)
                                  FROM revenue_discount_allocations da
                                 WHERE da.fee_item_id=f.id),0) discount_cents,
                      COALESCE((SELECT SUM(a.allocated_base_cents)
                                  FROM revenue_client_payment_allocations a
                                 WHERE a.fee_item_id=f.id),0) base_received_cents,
                      COALESCE((SELECT SUM(a.gst_cents)
                                  FROM revenue_client_payment_allocations a
                                 WHERE a.fee_item_id=f.id),0) gst_collected_cents
                 FROM revenue_fee_items f
                WHERE f.commercial_case_id=? AND f.status!='cancelled' ORDER BY f.id""",
            (case_id,),
        )
        for row in rows:
            final_base = int(row["net_cents"])
            received = int(row["base_received_cents"])
            row.update({
                "standard_base_cents": final_base + int(row["discount_cents"]),
                "final_base_cents": final_base,
                "outstanding_base_cents": max(0, final_base - received),
                "payment_status": (
                    "已收清" if received >= final_base else
                    "部分收款" if received else "待收款"
                ),
            })
        return rows

    def _case_documents(self, case_id, student_id):
        rows = self._rows(
            """SELECT DISTINCT d.* FROM revenue_documents d
                LEFT JOIN revenue_invoices i ON i.document_id=d.id
                LEFT JOIN revenue_claims c ON c.document_id=d.id
                LEFT JOIN revenue_claim_allocations ca ON ca.claim_id=c.id
                LEFT JOIN revenue_commission_instalments ci ON ci.id=ca.commission_instalment_id
                LEFT JOIN revenue_commission_lines cl ON cl.id=ci.commission_line_id
                LEFT JOIN revenue_payments p ON p.evidence_document_id=d.id
                LEFT JOIN revenue_client_payment_allocations cpa ON cpa.payment_id=p.id
                LEFT JOIN revenue_fee_items cf ON cf.id=cpa.fee_item_id
                LEFT JOIN revenue_receipts r ON r.document_id=d.id
                LEFT JOIN revenue_payments rp ON rp.id=r.payment_id
                LEFT JOIN revenue_client_payment_allocations rpa ON rpa.payment_id=rp.id
                LEFT JOIN revenue_fee_items rf ON rf.id=rpa.fee_item_id
               WHERE i.commercial_case_id=? OR cl.commercial_case_id=?
                  OR cf.commercial_case_id=? OR rf.commercial_case_id=? ORDER BY d.id""",
            (case_id, case_id, case_id, case_id),
        )
        for row in rows:
            if row.get("uid"):
                row["protected_view_url"] = f"/revenue/documents/{row['uid']}/view"
                row["protected_download_url"] = f"/revenue/documents/{row['uid']}/download"
        if self._table_exists("student_documents"):
            rows.extend(self._rows(
                """SELECT id,document_type,original_name,file_path,uploaded_at,is_active
                     FROM student_documents WHERE student_id=? ORDER BY id""",
                (student_id,),
            ))
        for row in rows:
            row.pop("storage_path", None)
            row.pop("file_path", None)
        return rows

    def _receipts(self, payment_ids):
        if not payment_ids:
            return []
        marks = ",".join("?" for _ in payment_ids)
        return self._rows(
            f"""SELECT r.*,d.uid AS document_uid,
                       '/revenue/documents/' || d.uid || '/view' AS protected_view_url,
                       '/revenue/documents/' || d.uid || '/download' AS protected_download_url
                  FROM revenue_receipts r
                  JOIN revenue_documents d ON d.id=r.document_id
                 WHERE r.payment_id IN ({marks}) ORDER BY r.id""",
            tuple(payment_ids),
        )

    def _by_ids(self, table, column, identifiers):
        if not identifiers:
            return []
        marks = ",".join("?" for _ in identifiers)
        return self._rows(
            f'SELECT * FROM "{table}" WHERE "{column}" IN ({marks}) ORDER BY id',
            tuple(identifiers),
        )

    def _rows(self, sql, parameters=()):
        return [dict(row) for row in self.connection.execute(sql, parameters).fetchall()]

    def _one(self, sql, parameters=()):
        row = self.connection.execute(sql, parameters).fetchone()
        return dict(row) if row else None

    def _table_exists(self, name):
        return bool(self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone())

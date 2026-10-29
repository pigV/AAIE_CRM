"""Centralized, read-only presentation projections for Revenue & Commission V1.

This module is the UI adapter boundary.  It may aggregate authoritative Revenue
records for display, but it never mutates business data and never reimplements
fee, commission, share, ledger, or reconciliation rules.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sqlite3
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from revenue.operational_visibility import RevenueOperationalVisibility
from revenue.urls import case_url, cases_url, revenue_home_url


METRIC_DEFINITIONS = (
    ("open_cases", "收费与佣金档案", "/revenue/cases?filter=open"),
    ("client_receivable_cents", "客户应收", "/revenue/billing?filter=outstanding"),
    ("commission_forecast_cents", "佣金预测", "/revenue/direct-commission?filter=forecast"),
    ("claimable_commission_cents", "可申领佣金", "/revenue/direct-commission?filter=claimable"),
    ("unmatched_statement_items", "待匹配流水", "/revenue/master-agent?filter=unmatched"),
    ("open_exceptions", "待处理异常", "/revenue/reconciliation?filter=open-exceptions"),
)

WORKSPACE_DEFINITIONS = {
    "cases": {
        "title": "学生收费与佣金档案",
        "description": "按学生申请、转学或升学周期，统一管理客户收费、学校佣金、发票、收款和对账。",
        "empty_message": "没有符合当前搜索或筛选条件的学生。",
        "columns": (
            ("student_name", "学生"), ("student_id", "学生编号"),
            ("classification", "客户分类"), ("business_type", "业务类型"),
            ("institution", "院校"), ("intake", "入学期"),
            ("billing_status", "收费状态"), ("commission_status", "佣金状态"),
            ("status", "档案状态"), ("updated_at_display", "最后更新"),
        ),
        "filters": (("all", "全部"), ("open", "进行中"), ("on_hold", "已暂停"), ("closed", "已关闭")),
    },
    "billing": {
        "title": "客户收费",
        "description": "按收费项目查看基础服务费、客户收款分配、付款证据与收据。",
        "empty_message": "暂无客户收费项目。FullyExecuted 合同进入 Revenue 后会建立正式收费项目。",
        "columns": (
            ("student_name", "学生"), ("student_id", "学生编号"),
            ("fee_type", "收费项目"), ("standard_base_cents", "标准基础费"),
            ("discount_cents", "折扣"), ("final_base_cents", "最终基础费（未含 GST）"),
            ("base_received_cents", "已收基础费"), ("gst_collected_cents", "已收 GST"),
            ("outstanding_base_cents", "未收基础费"), ("status", "收费状态"),
        ),
        "filters": (("all", "全部"), ("outstanding", "待收款"), ("settled", "已收清")),
    },
    "direct-commission": {
        "title": "直接佣金",
        "description": "查看实际入学、结构化学费、佣金计算、申领与追回。",
        "empty_message": "暂无直接佣金记录。",
        "columns": (
            ("student_id", "学生"), ("entity_name", "院校"),
            ("gross_cents", "佣金总额"), ("claimable_cents", "可申领"),
            ("status", "状态"), ("calculated_at", "计算时间"),
        ),
        "filters": (("all", "全部"), ("missing-commencement", "待确认实际入学"), ("missing-tuition", "待补充学费"), ("forecast", "预测"), ("claimable", "可申领"), ("claimed", "已申领"), ("paid", "已收款"), ("outstanding", "未结"), ("adjustments", "调整"), ("clawback", "追回")),
    },
    "master-agent": {
        "title": "一级代理",
        "description": "查看对账单、分成金额、匹配建议、管理员确认与争议。",
        "empty_message": "暂无一级代理对账单流水。",
        "columns": (
            ("statement_reference", "对账单"), ("student_reference", "学生参考"),
            ("program", "课程"), ("gross_cents", "总佣金"),
            ("share_cents", "分成"), ("payable_cents", "应付"),
            ("status", "匹配状态"),
        ),
        "filters": (("all", "全部"), ("unmatched", "待匹配"), ("needs-review", "待审核"), ("confirmed", "已确认"), ("disputed", "争议")),
    },
    "contracts": {
        "title": "佣金合同",
        "description": "查看不可原位覆盖的合同版本、智能提取建议与管理员激活状态。",
        "empty_message": "暂无收入与佣金合同版本。",
        "columns": (
            ("entity_name", "合作方"), ("entity_type", "机构类型"),
            ("state", "州"), ("city", "城市"), ("contract_kind", "合同类型"),
            ("version", "版本"), ("effective_from", "生效日期"),
            ("parse_status", "智能提取"), ("status", "合同状态"),
        ),
        "filters": (("all", "全部"), ("draft", "草稿"), ("under_review", "待审核"), ("active", "当前有效"), ("expiring", "即将到期"), ("expired", "已到期")),
    },
    "reconciliation": {
        "title": "对账与异常",
        "description": "集中处理异常、争议与仅供管理员确认的对账建议。",
        "empty_message": "暂无待处理对账事项。",
        "columns": (
            ("kind", "事项类型"), ("reference", "参考"),
            ("description", "说明"), ("amount_cents", "金额"),
            ("status", "状态"), ("occurred_at", "发生时间"),
        ),
        "filters": (("all", "全部"), ("open-exceptions", "异常"), ("unallocated", "未分配收款"), ("remittance", "汇款"), ("suggested", "待确认建议"), ("completed", "已完成"), ("disputed", "争议")),
    },
}

WORKSPACE_TABS = (
    ("cases", "收费与佣金档案"),
    ("billing", "客户收费"),
    ("direct-commission", "直接佣金"),
    ("master-agent", "一级代理"),
    ("contracts", "佣金合同"),
    ("reconciliation", "对账与异常"),
)

EXCEPTION_CODE_LABELS = {
    "MISSING_EVIDENCE": "缺少权威证据",
    "STATEMENT_REVIEW": "对账单审核",
    "AMOUNT_MISMATCH": "金额不一致",
    "missing_student_authority": "缺少学生权威关联",
    "ambiguous_engagement_contract": "存在多份已签服务合同，请确认有效合同",
    "missing_engagement_contract": "缺少已签服务合同",
    "missing_fee_rule": "缺少适用收费规则",
    "unresolved_fee_classification": "收费类型待确认",
    "missing_he_application": "缺少高等教育申请资料",
    "missing_revenue_entity": "缺少佣金结算机构",
    "missing_institution_contract": "缺少机构佣金合同",
    "missing_commission_rule": "缺少佣金规则",
    "missing_tuition": "缺少结构化学费",
    "missing_structured_tuition": "缺少结构化学费",
    "payment_method_ambiguity": "支付方式待确认",
}


def exception_code_label(code: Any) -> str:
    """Return the Admin-facing business label while preserving the source code separately."""
    return EXCEPTION_CODE_LABELS.get(str(code), "其他待处理事项")


def melbourne_datetime_display(value: Any) -> str:
    """Format an authoritative timestamp for the Melbourne Admin UI."""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(ZoneInfo("Australia/Melbourne"))
    return local.strftime("%d/%m/%Y %H:%M")


class RevenueReadModels:
    """Build Chinese, API-ready view data from authoritative tables."""

    def __init__(self, connection: sqlite3.Connection, *, case_surface: str = "desktop"):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.visibility = RevenueOperationalVisibility(connection)
        self.case_surface = case_surface
        self.cases_href = cases_url(case_surface)
        self.revenue_home_href = revenue_home_url(case_surface)
        self._column_cache: dict[str, set[str]] = {}
        self._student_name_cache: dict[str, str] = {}

    def dashboard(self) -> dict[str, Any]:
        values = {
            "open_cases": self._scalar(
                "SELECT COUNT(*) FROM revenue_commercial_cases c "
                "WHERE c.status='open' AND " + self.visibility.case_predicate("c")
            ),
            "client_receivable_cents": self._client_receivable_cents(),
            "commission_forecast_cents": self._latest_forecast_cents(),
            "claimable_commission_cents": self._scalar(
                """SELECT COALESCE(SUM(i.eligible_cents-i.claimed_cents),0)
                     FROM revenue_commission_instalments i
                     JOIN revenue_commission_lines l ON l.id=i.commission_line_id
                     JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                    WHERE i.status IN ('eligible','partial')
                      AND i.eligible_cents>i.claimed_cents AND """
                + self.visibility.case_predicate("c")
            ),
            "unmatched_statement_items": self._unmatched_statement_items(),
            "open_exceptions": self._scalar(
                "SELECT COUNT(*) FROM revenue_exceptions e WHERE e.status='open' AND "
                + self.visibility.operational_exception_predicate("e")
            ),
        }
        data: dict[str, Any] = {
            "schema_version": "revenue.v1",
            **{key: int(value) for key, value in values.items()},
            "metrics": [
                {
                    "key": key,
                    "label": label,
                    "value": int(values[key]),
                    "href": (
                        f"{self.cases_href}?filter=open" if key == "open_cases" else href
                    ),
                }
                for key, label, href in METRIC_DEFINITIONS
            ],
            "pending_items": self._pending_items(values),
            "recent_cases": self._recent_cases(),
            "quick_actions": self._dashboard_quick_actions(),
            "workspace_links": [
                {
                    "key": key,
                    "label": label,
                    "href": self.cases_href if key == "cases" else f"/revenue/{key}",
                }
                for key, label in WORKSPACE_TABS
            ],
            "cases_href": self.cases_href,
            "revenue_home_href": self.revenue_home_href,
        }
        return data

    def contextual_case_href(self, source_kind: str, source_id: Any) -> str | None:
        """Resolve a core authoritative record to its Revenue case without copying it."""
        kind = str(source_kind or "")
        cycle_id = None
        student_id = None
        if kind == "student":
            student_id = str(source_id)
        elif kind == "cycle":
            cycle_id = int(source_id)
        elif kind == "contract":
            row = self.connection.execute(
                "SELECT application_cycle_id,student_id FROM contracts WHERE id=?",
                (int(source_id),),
            ).fetchone()
            if row:
                cycle_id, student_id = row[0], row[1]
        elif kind in {"offer", "coe", "visa"}:
            table = {"offer": "offer_received_events", "coe": "coe_oshc", "visa": "visa_cases"}[kind]
            row = self.connection.execute(
                f'SELECT application_cycle_id FROM "{table}" WHERE id=?',
                (int(source_id),),
            ).fetchone()
            cycle_id = row[0] if row else None
        elif kind == "higher_education":
            row = self.connection.execute(
                "SELECT application_cycle_id FROM higher_education_applications WHERE id=?",
                (int(source_id),),
            ).fetchone()
            cycle_id = row[0] if row else None
        elif kind == "school":
            row = self.connection.execute(
                """SELECT c.uid FROM revenue_commercial_cases c
                    JOIN revenue_enrolment_components ec ON ec.commercial_case_id=c.id
                    JOIN student_school_enrolments se ON se.id=ec.school_enrolment_id
                   WHERE se.school_id=? AND """
                + self.visibility.case_predicate("c")
                + " ORDER BY c.id DESC LIMIT 1",
                (int(source_id),),
            ).fetchone()
            return case_url(str(row[0]), self.case_surface) if row else None
        else:
            raise ValueError("unknown authoritative Revenue context")
        if cycle_id is not None:
            row = self.connection.execute(
                "SELECT c.uid FROM revenue_commercial_cases c "
                "WHERE c.application_cycle_id=? AND "
                + self.visibility.case_predicate("c")
                + " ORDER BY c.id DESC LIMIT 1",
                (int(cycle_id),),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT c.uid FROM revenue_commercial_cases c "
                "WHERE c.student_id=? AND "
                + self.visibility.case_predicate("c")
                + " ORDER BY c.id DESC LIMIT 1",
                (student_id,),
            ).fetchone()
        return case_url(str(row[0]), self.case_surface) if row else None

    def student_context(self, student_id: str) -> dict[str, Any]:
        """List Revenue cases through their authoritative Application Cycles."""
        rows = self.connection.execute(
            """SELECT c.uid case_uid,c.application_cycle_id,c.status case_status,
                      c.opened_at,c.closed_at,a.cycle_type,a.education_stream,
                      a.status cycle_status
                 FROM revenue_commercial_cases c
                 JOIN application_cycles a ON a.id=c.application_cycle_id
                WHERE c.student_id=? AND """
            + self.visibility.case_predicate("c")
            + " ORDER BY a.id DESC",
            (str(student_id),),
        ).fetchall()
        return {
            "student_id": str(student_id),
            "rows": [
                {
                    "case_uid": str(row["case_uid"]),
                    "application_cycle_id": int(row["application_cycle_id"]),
                    "cycle_type": str(row["cycle_type"]),
                    "education_stream": str(row["education_stream"]),
                    "cycle_status": str(row["cycle_status"]),
                    "case_status": self._case_status_label(str(row["case_status"])),
                    "updated_at_display": melbourne_datetime_display(
                        row["closed_at"] or row["opened_at"]
                    ),
                    "href": case_url(str(row["case_uid"]), self.case_surface),
                }
                for row in rows
            ],
        }

    def _dashboard_quick_actions(self) -> list[dict[str, Any]]:
        actions = [
            {
                "key": "review_outstanding",
                "label": "查看待收客户费用",
                "description": "按收费项目查看尚未收清的基础服务费。",
                "href": "/revenue/billing?filter=outstanding",
                "mobile_safe": True,
            },
            {
                "key": "review_unmatched",
                "label": "查看待匹配流水",
                "description": "查看尚未匹配的一级代理对账单流水。",
                "href": "/revenue/master-agent?filter=unmatched",
                "mobile_safe": True,
            },
            {
                "key": "review_exceptions",
                "label": "查看待处理异常",
                "description": "查看待管理员处理的异常和差异。",
                "href": "/revenue/reconciliation?filter=open-exceptions",
                "mobile_safe": True,
            },
        ]
        mutation_definitions = (
            ("billing", "record_client_payment", "记录客户收款", "进入已闭环的收款、分配与分类账操作。"),
            ("direct-commission", "issue_direct_claim", "签发佣金申领", "进入已确认可申领期次的正式申领操作。"),
            ("master-agent", "import_statement", "导入对账单", "进入已激活一级代理分成规则对应的导入操作。"),
            ("contracts", "register_contract", "登记佣金合同", "进入不可原位覆盖的草稿合同版本登记操作。"),
        )
        for workspace, key, label, description in mutation_definitions:
            if self._workspace_action(workspace):
                actions.append({
                    "key": key,
                    "label": label,
                    "description": description,
                    "href": f"/revenue/{workspace}#revenue-action",
                    "mobile_safe": False,
                })
        return actions

    def workspace(
        self, name: str, filters: dict[str, Any] | None = None, *, case_uid: str = ""
    ) -> dict[str, Any]:
        if name not in WORKSPACE_DEFINITIONS:
            raise ValueError("unknown Revenue workspace")
        parameters = filters or {}
        raw_filter = parameters.get("filter", "all")
        if isinstance(raw_filter, (list, tuple)):
            raw_filter = raw_filter[0] if raw_filter else "all"
        active_filter = str(raw_filter or "all")
        definition = WORKSPACE_DEFINITIONS[name]
        allowed_filters = {key for key, _label in definition["filters"]}
        if active_filter not in allowed_filters:
            active_filter = "all"
        loaders = {
            "cases": self._case_rows,
            "billing": self._billing_rows,
            "direct-commission": self._direct_commission_rows,
            "master-agent": self._master_agent_rows,
            "reconciliation": self._reconciliation_rows,
        }
        student_page = self._student_page(active_filter, parameters) if name == "cases" else {}
        case_scope = None
        if case_uid:
            if name != "billing":
                raise ValueError("case scope is supported only for billing")
            case_scope_row = self.connection.execute(
                """SELECT c.uid,c.student_id
                     FROM revenue_commercial_cases c
                    WHERE c.uid=?""",
                (case_uid,),
            ).fetchone()
            if not case_scope_row:
                raise LookupError("Revenue Commercial Case not found")
            case_scope = {
                "case_uid": str(case_scope_row["uid"]),
                "student_id": str(case_scope_row["student_id"]),
                "student_name": self._student_display_name(
                    str(case_scope_row["student_id"])
                ),
                "case_href": f"/revenue/cases/{quote(str(case_scope_row['uid']), safe='')}",
                "billing_href": f"/revenue/cases/{quote(str(case_scope_row['uid']), safe='')}/billing",
            }
        rows = student_page["rows"] if name == "cases" else (
            self._contract_rows(active_filter, parameters)
            if name == "contracts" else (
                self._billing_rows(active_filter, case_uid=case_uid)
                if name == "billing" else loaders[name](active_filter)
            )
        )
        selected = self._selection(parameters)
        if selected:
            for row in rows:
                row["selected"] = selected in {str(row.get("uid") or ""), str(row.get("reference") or "")}
        action = self._workspace_action(name, selected=selected, case_uid=case_uid)
        result = {
            **student_page,
            "schema_version": "revenue.v1",
            "name": name,
            "title": definition["title"],
            "description": definition["description"],
            "active_filter": active_filter,
            "tabs": [
                {
                    "key": key,
                    "label": label,
                    "href": self.cases_href if key == "cases" else f"/revenue/{key}",
                    "active": key == name,
                }
                for key, label in WORKSPACE_TABS
            ],
            "filters": [
                {
                    "key": key,
                    "label": label,
                    "href": (
                        case_scope["billing_href"] if case_scope else (
                            self.cases_href if name == "cases" else f"/revenue/{name}"
                        )
                    ) + "?" + urlencode({
                        "filter": key,
                        **({"q": self._student_parameter(parameters, "q")}
                           if name == "cases" and self._student_parameter(parameters, "q") else {}),
                    }),
                    "active": key == active_filter,
                }
                for key, label in definition["filters"]
            ],
            "columns": [
                {"key": key, "label": label, "kind": "money" if key.endswith("_cents") else "text"}
                for key, label in definition["columns"]
            ],
            "rows": rows,
            "empty_message": definition["empty_message"],
            "coverage_cards": [] if case_scope else self._workspace_coverage(name),
            "search_controls": (
                {"q": self._student_parameter(parameters, "q")}
                if name == "cases" else
                self._contract_search_controls(parameters) if name == "contracts" else {}
            ),
            "action": action,
            "action_unavailable_message": self._action_unavailable_message(name),
            "cases_href": self.cases_href,
            "revenue_home_href": self.revenue_home_href,
        }
        if case_scope:
            result["student_context"] = case_scope
            result["title"] = "当前学生客户收费"
            result["description"] = (
                f"{case_scope['student_name']} · {case_scope['student_id']}"
            )
            result["tabs"] = []
        return result

    def _workspace_coverage(self, name: str) -> list[dict[str, Any]]:
        visible_case = self.visibility.case_predicate("c")
        definitions: dict[str, tuple[tuple[str, str, str | None], ...]] = {
            "cases": (
                ("在办收费与佣金档案", "SELECT COUNT(*) FROM revenue_commercial_cases c WHERE c.status='open' AND " + visible_case, "/revenue/cases?filter=open"),
                ("现有 AAIE K–12", "SELECT COUNT(*) FROM revenue_commercial_cases c WHERE c.existing_aaie_k12=1 AND " + visible_case, None),
                ("新客户", "SELECT COUNT(*) FROM revenue_commercial_cases c WHERE c.existing_aaie_k12=0 AND " + visible_case, None),
            ),
            "billing": (
                ("收费项目", "SELECT COUNT(*) FROM revenue_fee_items f JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id WHERE f.status!='cancelled' AND " + visible_case, None),
                ("待收收费项目", "SELECT COUNT(*) FROM revenue_fee_items f JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id WHERE f.status!='cancelled' AND f.net_cents>COALESCE((SELECT SUM(a.allocated_base_cents) FROM revenue_client_payment_allocations a WHERE a.fee_item_id=f.id),0) AND " + visible_case, "/revenue/billing?filter=outstanding"),
                ("客户收款", "SELECT COUNT(*) FROM revenue_client_payment_allocations a JOIN revenue_fee_items f ON f.id=a.fee_item_id JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id WHERE " + visible_case, None),
                ("收据", "SELECT COUNT(*) FROM revenue_receipts", None),
                ("退款", "SELECT COUNT(*) FROM revenue_refunds", None),
                ("收费规则", "SELECT COUNT(*) FROM revenue_fee_rule_versions", None),
            ),
            "direct-commission": (
                ("预计佣金", "SELECT COUNT(*) FROM revenue_forecast_lines f JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id WHERE f.forecast_run_id=(SELECT id FROM revenue_forecast_runs WHERE status='completed' ORDER BY id DESC LIMIT 1) AND " + visible_case, "/revenue/direct-commission?filter=forecast"),
                ("待申领", "SELECT COUNT(DISTINCT i.commission_line_id) FROM revenue_commission_instalments i JOIN revenue_commission_lines l ON l.id=i.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE i.status IN ('eligible','partial') AND i.eligible_cents>i.claimed_cents AND " + visible_case, "/revenue/direct-commission?filter=claimable"),
                ("已申领", "SELECT COUNT(DISTINCT i.commission_line_id) FROM revenue_commission_instalments i JOIN revenue_commission_lines l ON l.id=i.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE i.claimed_cents>0 AND " + visible_case, "/revenue/direct-commission?filter=claimed"),
                ("已收款", "SELECT COUNT(DISTINCT i.commission_line_id) FROM revenue_commission_instalments i JOIN revenue_commission_lines l ON l.id=i.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE i.status='paid' AND " + visible_case, "/revenue/direct-commission?filter=paid"),
                ("未结", "SELECT COUNT(*) FROM revenue_commission_lines l JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE l.status NOT IN ('paid','closed') AND " + visible_case, "/revenue/direct-commission?filter=outstanding"),
                ("调整与追回", "SELECT (SELECT COUNT(*) FROM revenue_commission_adjustments a JOIN revenue_commission_lines l ON l.id=a.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE " + visible_case + ")+(SELECT COUNT(*) FROM revenue_clawbacks cb JOIN revenue_commission_lines l ON l.id=cb.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE " + visible_case + ")", "/revenue/direct-commission?filter=adjustments"),
            ),
            "master-agent": (
                ("对账单", "SELECT COUNT(*) FROM revenue_master_agent_statements", "/revenue/master-agent?filter=all"),
                ("待匹配", "SELECT COUNT(*) FROM revenue_master_agent_statement_items i WHERE NOT EXISTS(SELECT 1 FROM revenue_statement_matches m WHERE m.statement_item_id=i.id AND m.match_status='confirmed')", "/revenue/master-agent?filter=unmatched"),
                ("待审核", "SELECT COUNT(*) FROM revenue_master_agent_statement_items i WHERE NOT EXISTS(SELECT 1 FROM revenue_statement_matches m WHERE m.statement_item_id=i.id AND m.match_status IN ('confirmed','excluded'))", "/revenue/master-agent?filter=needs-review"),
                ("有争议", "SELECT COUNT(*) FROM revenue_statement_matches m JOIN revenue_commission_lines l ON l.id=m.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE m.match_status='disputed' AND " + visible_case, "/revenue/master-agent?filter=disputed"),
                ("已确认", "SELECT COUNT(*) FROM revenue_statement_matches m JOIN revenue_commission_lines l ON l.id=m.commission_line_id JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id WHERE m.match_status='confirmed' AND " + visible_case, "/revenue/master-agent?filter=confirmed"),
                ("一级代理发票", "SELECT COUNT(*) FROM revenue_invoices WHERE document_kind='master_agent_invoice'", None),
                ("收款与汇款", "SELECT COUNT(*) FROM revenue_payments WHERE counterparty_kind='master_agent'", None),
                ("对账", "SELECT COUNT(*) FROM revenue_reconciliation_runs WHERE reconciliation_kind='master_agent_statement'", None),
            ),
            "contracts": (
                ("学校 / 机构合同", "SELECT COUNT(*) FROM revenue_contract_groups g JOIN revenue_entities e ON e.id=g.revenue_entity_id WHERE e.entity_type IN ('school','higher_education_provider')", None),
                ("一级代理合同", "SELECT COUNT(*) FROM revenue_contract_groups g JOIN revenue_entities e ON e.id=g.revenue_entity_id WHERE e.entity_type='master_agent'", None),
                ("当前有效", "SELECT COUNT(*) FROM revenue_contract_versions WHERE status='active'", "/revenue/contracts?filter=active"),
                ("智能提取待审核", "SELECT COUNT(*) FROM revenue_contract_parse_runs WHERE status IN ('pending','suggested')", "/revenue/contracts?filter=under_review"),
            ),
            "reconciliation": (
                ("待匹配收款", "SELECT COUNT(*) FROM revenue_payments WHERE status='confirmed' AND allocated_cents<amount_cents", "/revenue/reconciliation?filter=unallocated"),
                ("汇款", "SELECT COUNT(*) FROM revenue_payments WHERE counterparty_kind='master_agent'", "/revenue/reconciliation?filter=remittance"),
                ("智能匹配建议", "SELECT COUNT(*) FROM revenue_reconciliation_suggestions s WHERE s.review_status='pending' AND " + self.visibility.operational_suggestion_predicate("s"), "/revenue/reconciliation?filter=suggested"),
                ("未分配收款", "SELECT COUNT(*) FROM revenue_payments WHERE status='confirmed' AND allocated_cents<amount_cents", "/revenue/reconciliation?filter=unallocated"),
                ("已完成对账", "SELECT COUNT(*) FROM revenue_reconciliation_runs WHERE status='confirmed'", "/revenue/reconciliation?filter=completed"),
                ("差异与争议", "SELECT COUNT(*) FROM revenue_disputes WHERE status='open'", "/revenue/reconciliation?filter=disputed"),
            ),
        }
        cards = [
            {"label": label, "count": self._scalar(sql), "href": href}
            for label, sql, href in definitions[name]
        ]
        if name == "cases":
            for card in cards:
                if card["href"] == "/revenue/cases?filter=open":
                    card["href"] = f"{self.cases_href}?filter=open"
        return cards

    @staticmethod
    def _contract_search_controls(parameters: dict[str, Any]) -> dict[str, str]:
        return {
            key: RevenueReadModels._first_parameter(parameters, key)
            for key in ("q", "institution_type", "state", "city")
        }

    @staticmethod
    def _first_parameter(parameters: dict[str, Any], key: str) -> str:
        value = parameters.get(key, "")
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value or "").strip()

    def _workspace_action(
        self, name: str, *, selected: str = "", case_uid: str = ""
    ) -> dict[str, Any] | None:
        if name == "contracts":
            entities = [
                {"uid": str(row["uid"]), "label": str(row["legal_name"])}
                for row in self.connection.execute(
                    """SELECT uid,legal_name FROM revenue_entities
                        WHERE status='active' ORDER BY legal_name,id"""
                )
            ]
            pending_extractions = [dict(row) for row in self.connection.execute(
                """SELECT x.uid,v.uid contract_version_uid,e.legal_name entity_name,
                          x.field_name,x.suggested_value,x.evidence_text,
                          x.confidence_basis_points
                     FROM revenue_contract_extractions x
                     JOIN revenue_contract_parse_runs p ON p.id=x.parse_run_id
                     JOIN revenue_contract_versions v ON v.id=p.contract_version_id
                     JOIN revenue_contract_groups g ON g.id=v.contract_group_id
                     JOIN revenue_entities e ON e.id=g.revenue_entity_id
                    WHERE x.review_status='pending'
                      AND p.status='suggested'
                      AND v.status='under_review'
                 ORDER BY x.id"""
            )]
            manual_contracts = [dict(row) for row in self.connection.execute(
                """SELECT v.uid,e.legal_name label
                     FROM revenue_contract_versions v
                     JOIN revenue_contract_groups g ON g.id=v.contract_group_id
                     JOIN revenue_entities e ON e.id=g.revenue_entity_id
                    WHERE v.status IN ('draft','under_review')
                 ORDER BY v.created_at DESC,v.id DESC"""
            )]
            activatable_contracts = [dict(row) for row in self.connection.execute(
                """SELECT v.uid,e.legal_name label
                     FROM revenue_contract_versions v
                     JOIN revenue_contract_groups g ON g.id=v.contract_group_id
                     JOIN revenue_entities e ON e.id=g.revenue_entity_id
                    WHERE v.status='under_review'
                      AND EXISTS(
                          SELECT 1 FROM revenue_contract_extractions x
                          JOIN revenue_contract_parse_runs p ON p.id=x.parse_run_id
                           WHERE p.contract_version_id=v.id
                             AND (x.field_name LIKE '%_cents'
                                  OR x.field_name LIKE '%_basis_points')
                      )
                      AND NOT EXISTS(
                          SELECT 1 FROM revenue_contract_extractions x
                          JOIN revenue_contract_parse_runs p ON p.id=x.parse_run_id
                           WHERE p.contract_version_id=v.id
                             AND (x.field_name LIKE '%_cents'
                                  OR x.field_name LIKE '%_basis_points')
                             AND (x.review_status NOT IN ('confirmed','corrected')
                                  OR x.confirmed_value IS NULL OR x.confirmed_value='')
                      )
                 ORDER BY v.created_at DESC,v.id DESC"""
            )]
            if not entities and not pending_extractions and not manual_contracts:
                return None
            return {
                "kind": "contract_management",
                "entities": entities,
                "pending_extractions": pending_extractions,
                "manual_contracts": manual_contracts,
                "activatable_contracts": activatable_contracts,
            }
        if name == "billing":
            # A scoped workspace continues an already-authorized Revenue Case,
            # including immutable history whose Student is no longer operational.
            # Global billing remains limited to the current Student universe.
            case_predicate = (
                "c.uid=?" if case_uid else self.visibility.case_predicate("c")
            )
            case_parameters = (case_uid,) if case_uid else ()
            payment_options = []
            for row in self.connection.execute(
                """SELECT f.uid,f.student_id,f.description,f.net_cents,
                          COALESCE((SELECT SUM(a.allocated_base_cents)
                                      FROM revenue_client_payment_allocations a
                                     WHERE a.fee_item_id=f.id),0) base_received_cents
                     FROM revenue_fee_items f
                    JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                   WHERE f.status!='cancelled'
                      AND """ + case_predicate + """
                 ORDER BY f.created_at,f.id""",
                case_parameters,
            ):
                outstanding = max(0, int(row["net_cents"]) - int(row["base_received_cents"]))
                if outstanding:
                    payment_options.append({
                        "fee_item_uid": str(row["uid"]),
                        "label": f"{row['student_id']} · {row['description']}",
                        "outstanding_base_cents": outstanding,
                        "selected": str(row["uid"]) == str(selected),
                    })
            payment_methods = []
            expected_currencies = {
                "alipay": "CNY",
                "wechat": "CNY",
                "au_personal_account": "AUD",
                "au_company_account": "AUD",
            }
            for row in self.connection.execute(
                """SELECT m.method_code,m.display_name,r.gst_rate_basis_points
                     FROM revenue_payment_methods m
                     JOIN revenue_payment_method_rule_versions r ON r.payment_method_id=m.id
                    WHERE m.status='active' AND r.status='active'
                      AND m.method_code IN ('alipay','wechat','au_personal_account','au_company_account')
                      AND NOT EXISTS(
                          SELECT 1 FROM revenue_payment_method_rule_versions other
                           WHERE other.payment_method_id=m.id AND other.status='active'
                             AND other.id<>r.id
                      )
                    ORDER BY m.id"""
            ):
                code = str(row["method_code"])
                payment_methods.append({
                    "code": code,
                    "label": {
                        "au_personal_account": "澳洲私人账号",
                        "au_company_account": "澳洲公司账号",
                    }.get(code, str(row["display_name"])),
                    "currency_code": expected_currencies[code],
                    "gst_rate_basis_points": int(row["gst_rate_basis_points"]),
                })
            receipt_options = [dict(row) for row in self.connection.execute(
                """SELECT p.uid payment_uid,f.student_id,f.description,p.received_date,
                          p.currency_code,p.amount_cents
                     FROM revenue_payments p
                     JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
                     JOIN revenue_fee_items f ON f.id=a.fee_item_id
                     JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                LEFT JOIN revenue_receipts r ON r.payment_id=p.id
                    WHERE r.id IS NULL AND """ + case_predicate + """
                 ORDER BY p.received_date DESC,p.id DESC""",
                case_parameters,
            )]
            payment_history = [dict(row) for row in self.connection.execute(
                """SELECT p.uid payment_uid,f.student_id,f.description,p.received_date,
                          p.currency_code,p.amount_cents,a.allocated_base_cents,a.gst_cents,
                          a.method_code_snapshot,d.uid evidence_document_uid,
                          r.receipt_number,rd.uid receipt_document_uid
                     FROM revenue_payments p
                     JOIN revenue_client_payment_allocations a ON a.payment_id=p.id
                     JOIN revenue_fee_items f ON f.id=a.fee_item_id
                     JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                LEFT JOIN revenue_documents d ON d.id=p.evidence_document_id
                LEFT JOIN revenue_receipts r ON r.payment_id=p.id
                LEFT JOIN revenue_documents rd ON rd.id=r.document_id
                    WHERE """ + case_predicate + """
                 ORDER BY p.received_date DESC,p.id DESC LIMIT 100""",
                case_parameters,
            )]
            if payment_options and not payment_methods:
                payment_options = []
            if payment_options or receipt_options or payment_history:
                return {
                    "kind": "billing_operations",
                    "payment_action": (
                        f"/revenue/cases/{quote(case_uid, safe='')}/billing/payment"
                        if case_uid else "/revenue/billing/payment"
                    ),
                    "receipt_action": (
                        f"/revenue/cases/{quote(case_uid, safe='')}/billing/receipt"
                        if case_uid else "/revenue/billing/receipt"
                    ),
                    "payment_options": payment_options,
                    "payment_methods": payment_methods,
                    "receipt_options": receipt_options,
                    "payment_history": payment_history,
                }
            return None
        if name == "direct-commission":
            claims = [
                {
                    "commission_line_uid": str(row["line_uid"]),
                    "instalment_no": int(row["instalment_no"]),
                    "label": f"{row['student_id']} · 第 {row['instalment_no']} 期",
                    "claimable_cents": int(row["eligible_cents"]) - int(row["claimed_cents"]),
                }
                for row in self.connection.execute(
                    """SELECT l.uid line_uid,c.student_id,i.instalment_no,
                              i.eligible_cents,i.claimed_cents
                         FROM revenue_commission_instalments i
                         JOIN revenue_commission_lines l ON l.id=i.commission_line_id
                         JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                        WHERE i.status IN ('eligible','partial')
                          AND i.eligible_cents>i.claimed_cents
                          AND """ + self.visibility.case_predicate("c") + """
                     ORDER BY i.created_at,i.id"""
                )
            ]
            payments = []
            for row in self.connection.execute(
                """SELECT l.uid line_uid,e.uid entity_uid,c.student_id,l.gross_cents,
                          COALESCE((SELECT SUM(a.claimed_cents)
                            FROM revenue_claim_allocations a
                            JOIN revenue_commission_instalments i ON i.id=a.commission_instalment_id
                           WHERE i.commission_line_id=l.id),0) claimed_cents,
                          COALESCE((SELECT SUM(pa.allocated_cents)
                            FROM revenue_payment_allocations pa
                           WHERE pa.commission_line_id=l.id),0) allocated_cents
                     FROM revenue_commission_lines l
                     JOIN revenue_entities e ON e.id=l.revenue_entity_id
                     JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                    WHERE """ + self.visibility.case_predicate("c") + """
                 ORDER BY l.id"""
            ):
                outstanding = int(row["claimed_cents"]) - int(row["allocated_cents"])
                if outstanding > 0:
                    payments.append({
                        "commission_line_uid": str(row["line_uid"]),
                        "institution_entity_uid": str(row["entity_uid"]),
                        "label": f"{row['student_id']} · 机构佣金收款",
                        "outstanding_cents": outstanding,
                    })
            unbilled_claims = [dict(row) for row in self.connection.execute(
                """SELECT claim.uid,claim.claim_number,claim.gross_cents
                     FROM revenue_claims claim
                    WHERE claim.claim_kind='direct' AND claim.status='issued'
                      AND EXISTS(
                          SELECT 1 FROM revenue_claim_allocations allocation
                          JOIN revenue_commission_instalments instalment
                            ON instalment.id=allocation.commission_instalment_id
                          JOIN revenue_commission_lines line
                            ON line.id=instalment.commission_line_id
                          JOIN revenue_commercial_cases c
                            ON c.id=line.commercial_case_id
                           WHERE allocation.claim_id=claim.id AND """
                + self.visibility.case_predicate("c")
                + """
                      )
                      AND NOT EXISTS(
                          SELECT 1 FROM revenue_invoice_lines l
                           WHERE l.source_kind='claim' AND l.source_uid=claim.uid
                      ) ORDER BY claim.id"""
            )]
            if claims or payments or unbilled_claims:
                return {"kind": "direct_operations", "claims": claims,
                        "payments": payments, "unbilled_claims": unbilled_claims}
            return None
        if name == "master-agent":
            pairs = [
                {
                    "master_agent_entity_uid": str(row["master_uid"]),
                    "provider_entity_uid": str(row["provider_uid"]),
                    "label": f"{row['master_name']} · {row['provider_name']}",
                }
                for row in self.connection.execute(
                    """SELECT m.uid master_uid,m.legal_name master_name,
                              p.uid provider_uid,p.legal_name provider_name
                         FROM revenue_master_agent_share_rule_versions r
                         JOIN revenue_entities m ON m.id=r.master_agent_entity_id
                         JOIN revenue_entities p ON p.id=r.provider_entity_id
                        WHERE r.status='active'
                     ORDER BY m.legal_name,p.legal_name,r.id DESC"""
                )
            ]
            suggestions = [dict(row) for row in self.connection.execute(
                """SELECT s.uid,s.suggested_amount_cents
                     FROM revenue_reconciliation_suggestions s
                     JOIN revenue_reconciliation_runs r ON r.id=s.reconciliation_run_id
                    WHERE r.reconciliation_kind='master_agent_statement'
                      AND s.review_status='pending'
                      AND """
                + self.visibility.operational_suggestion_predicate("s")
                + """
                 ORDER BY s.id"""
            )]
            invoice_items = [dict(row) for row in self.connection.execute(
                """SELECT i.uid,i.student_reference,i.payable_cents
                     FROM revenue_master_agent_statement_items i
                    WHERE EXISTS(
                          SELECT 1 FROM revenue_statement_matches m
                          JOIN revenue_commission_lines l ON l.id=m.commission_line_id
                          JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                           WHERE m.statement_item_id=i.id AND m.match_status='confirmed'
                             AND """ + self.visibility.case_predicate("c") + """
                      )
                      AND NOT EXISTS(SELECT 1 FROM revenue_invoice_lines l
                                     WHERE l.source_kind='master_agent_statement_item'
                                       AND l.source_uid=i.uid)
                 ORDER BY i.id"""
            )]
            payments = []
            for row in self.connection.execute(
                """SELECT i.uid item_uid,e.uid master_uid,i.student_reference,i.payable_cents,
                          COALESCE((SELECT SUM(a.allocated_cents)
                            FROM revenue_payment_allocations a
                           WHERE a.statement_item_uid=i.uid),0) allocated_cents
                     FROM revenue_master_agent_statement_items i
                     JOIN revenue_master_agent_statements s ON s.id=i.statement_id
                     JOIN revenue_entities e ON e.id=s.master_agent_entity_id
                    WHERE EXISTS(
                          SELECT 1 FROM revenue_statement_matches m
                          JOIN revenue_commission_lines l ON l.id=m.commission_line_id
                          JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                           WHERE m.statement_item_id=i.id AND m.match_status='confirmed'
                             AND """ + self.visibility.case_predicate("c") + """
                      )
                 ORDER BY i.id"""
            ):
                outstanding = int(row["payable_cents"]) - int(row["allocated_cents"])
                if outstanding > 0:
                    payments.append({**dict(row), "outstanding_cents": outstanding})
            if pairs or suggestions or invoice_items or payments:
                return {"kind": "master_agent_operations", "pairs": pairs,
                        "suggestions": suggestions, "invoice_items": invoice_items,
                        "payments": payments}
            return None
        return None

    @staticmethod
    def _action_unavailable_message(name: str) -> str:
        return {
            "contracts": "建立收入业务主体后即可登记新的合同版本。",
            "billing": "存在已绑定 FullyExecuted 合同且尚未收清的收费项目后即可记录客户收款。",
            "direct-commission": "仅当管理员已确认可申领期次后才显示申领操作。",
            "master-agent": "激活一级代理分成规则后才可导入对应对账单。",
            "cases": "收费与佣金档案由权威申请周期自动建立，不提供手工建档。",
            "reconciliation": "对账建议只能由现有对账单或收款工作流产生。",
        }[name]

    @staticmethod
    def _selection(parameters: dict[str, Any]) -> str:
        for key in ("case", "invoice", "commission", "component", "item", "contract", "record"):
            value = parameters.get(key)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            if value:
                return str(value)
        return ""

    @staticmethod
    def _student_parameter(parameters: dict[str, Any], key: str) -> str:
        value = parameters.get(key, "")
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value or "").strip()

    def _student_page(self, active_filter: str, parameters: dict[str, Any]) -> dict[str, Any]:
        # Student Master defines membership. Case status only filters this view;
        # no case is created, selected as current, or discarded from history.
        where = self.visibility.student_predicate("s")
        values: list[Any] = []
        # Match the existing display-name adapter for identifier-only fixtures.
        has_names = {"passport_first_name", "passport_last_name"}.issubset(self._columns("students"))
        name_columns = ("s.passport_first_name,s.passport_last_name" if has_names else
                        "NULL AS passport_first_name,NULL AS passport_last_name")
        student_select = "SELECT s.student_id," + name_columns + " FROM students s WHERE "
        q = self._student_parameter(parameters, "q")
        if q:
            expressions = ["s.student_id"]
            if has_names:
                expressions += ["s.passport_first_name", "s.passport_last_name",
                "TRIM(COALESCE(s.passport_first_name,'') || ' ' || COALESCE(s.passport_last_name,''))",
                "TRIM(COALESCE(s.passport_last_name,'') || ' ' || COALESCE(s.passport_first_name,''))"]
            # Treat user wildcards literally; values always remain SQL parameters.
            pattern = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            where += " AND (" + " OR ".join(e + " LIKE ? ESCAPE '!'" for e in expressions) + ")"
            values.extend([pattern] * len(expressions))
        if active_filter != "all":
            where += " AND EXISTS(SELECT 1 FROM revenue_commercial_cases sc WHERE sc.student_id=s.student_id AND sc.status=?)"
            values.append(active_filter)
        total = self.connection.execute("SELECT COUNT(*) FROM students s WHERE " + where, values).fetchone()[0]
        page_size = 25
        pages = max(1, (total + page_size - 1) // page_size)
        try:
            page = max(1, min(pages, int(self._student_parameter(parameters, "page") or "1")))
        except ValueError:
            page = 1
        students = self.connection.execute(
            student_select + where
            + " ORDER BY s.student_id LIMIT ? OFFSET ?", [*values, page_size, (page-1)*page_size]).fetchall()
        cases_by_student: dict[str, list] = {str(s["student_id"]): [] for s in students}
        if cases_by_student:
            marks = ",".join("?" for _ in cases_by_student)
            for case in self.connection.execute(
                    "SELECT * FROM revenue_commercial_cases WHERE student_id IN (" + marks + ") ORDER BY application_cycle_id DESC,id DESC",
                    list(cases_by_student)):
                cases_by_student[str(case["student_id"])].append(case)
        rows = []
        for student in students:
            sid = str(student["student_id"])
            cases = cases_by_student[sid]
            name = " ".join(str(student[k] or "").strip() for k in ("passport_first_name", "passport_last_name")).strip() or "姓名待关联"
            self._student_name_cache[sid] = name
            if len(cases) == 1:
                row = self._case_projection(cases[0])
            else:
                row = {"uid": sid, "student_id": sid, "student_name": name,
                       "status": "未建立 / Not Yet Created" if not cases else f"{len(cases)} 个申请周期档案",
                       "href": self.cases_href + "?" + urlencode({"student": sid})}
            rows.append(row)
        def link(number):
            return self.cases_href + "?" + urlencode({"q": q, "filter": active_filter, "page": number})
        detail = None
        selected = self._student_parameter(parameters, "student")
        if selected:
            student = self.connection.execute(
                student_select + "s.student_id=? AND "
                + self.visibility.student_predicate("s"), (selected,)).fetchone()
            if student:
                case_rows = self.connection.execute(
                    "SELECT * FROM revenue_commercial_cases WHERE student_id=? ORDER BY application_cycle_id DESC,id DESC", (selected,)).fetchall()
                detail = {"student_id": selected, "student_name": self._student_display_name(selected),
                          "state": "found", "cases": [self._case_projection(c) for c in case_rows]}
            else:
                detail = {"state": "not_found", "cases": []}
        return {"rows": rows, "student_detail": detail, "pagination": {
            "total": total, "page": page, "pages": pages, "page_size": page_size,
            "previous": link(page-1) if page > 1 else None,
            "next": link(page+1) if page < pages else None}}

    def _case_rows(self, active_filter: str) -> list[dict[str, Any]]:
        return self._student_page(active_filter, {})["rows"]

    def _billing_rows(
        self, active_filter: str, *, case_uid: str = ""
    ) -> list[dict[str, Any]]:
        case_predicate = (
            "c.uid=?" if case_uid else self.visibility.case_predicate("c")
        )
        case_parameters = (case_uid,) if case_uid else ()
        rows = self.connection.execute(
            """SELECT f.id,f.uid,f.student_id,f.description,f.net_cents,
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
                 JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                WHERE f.status!='cancelled' AND """ + case_predicate + """
             ORDER BY f.created_at DESC,f.id DESC LIMIT 100""",
            case_parameters,
        ).fetchall()
        projected = []
        for row in rows:
            final_base = int(row["net_cents"])
            received = int(row["base_received_cents"])
            outstanding = max(0, final_base - received)
            if active_filter == "outstanding" and outstanding == 0:
                continue
            if active_filter == "settled" and outstanding != 0:
                continue
            projected.append({
                "uid": str(row["uid"]),
                "student_name": self._student_display_name(str(row["student_id"])),
                "student_id": str(row["student_id"]),
                "fee_type": str(row["description"]),
                "standard_base_cents": final_base + int(row["discount_cents"]),
                "discount_cents": int(row["discount_cents"]),
                "final_base_cents": final_base,
                "base_received_cents": received,
                "gst_collected_cents": int(row["gst_collected_cents"]),
                "outstanding_base_cents": outstanding,
                "status": (
                    "已收清" if outstanding == 0 else
                    "部分收款" if received > 0 else "待收款"
                ),
                "href": (
                    f"/revenue/cases/{quote(case_uid, safe='')}/billing"
                    if case_uid else "/revenue/billing"
                ) + f"?item={quote(str(row['uid']), safe='')}",
            })
        return projected

    def _direct_commission_rows(self, active_filter: str) -> list[dict[str, Any]]:
        if active_filter in {"missing-commencement", "missing-tuition"}:
            return self._missing_direct_fact_rows(active_filter)
        if active_filter == "forecast":
            return self._forecast_rows()
        rows = self.connection.execute(
            """SELECT l.uid,c.student_id,e.legal_name entity_name,l.gross_cents,
                      l.status,l.calculated_at,
                      COALESCE((SELECT SUM(eligible_cents-claimed_cents)
                                  FROM revenue_commission_instalments i
                                 WHERE i.commission_line_id=l.id
                                   AND i.status IN ('eligible','partial')
                                   AND i.eligible_cents>i.claimed_cents),0) claimable_cents,
                      COALESCE((SELECT SUM(claimed_cents)
                                  FROM revenue_commission_instalments i
                                 WHERE i.commission_line_id=l.id),0) claimed_cents,
                      EXISTS(SELECT 1 FROM revenue_commission_instalments i
                              WHERE i.commission_line_id=l.id AND i.status='paid') has_paid,
                      EXISTS(SELECT 1 FROM revenue_clawbacks cb
                              WHERE cb.commission_line_id=l.id AND cb.status='open') has_clawback,
                      EXISTS(SELECT 1 FROM revenue_commission_adjustments a
                              WHERE a.commission_line_id=l.id) has_adjustment
                 FROM revenue_commission_lines l
                 JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                 JOIN revenue_entities e ON e.id=l.revenue_entity_id
                WHERE """ + self.visibility.case_predicate("c") + """
             ORDER BY l.calculated_at DESC,l.id DESC LIMIT 100"""
        ).fetchall()
        labels = {
            "forecast": "预测", "accrued": "已确认", "claimable": "可申领",
            "claimed": "已申领", "paid": "已收款", "closed": "已关闭",
        }
        result = []
        for row in rows:
            effective_status = (
                "paid" if row["has_paid"] else
                "claimed" if int(row["claimed_cents"]) > 0 else
                "claimable" if int(row["claimable_cents"]) > 0 else
                str(row["status"])
            )
            if active_filter == "clawback":
                if not row["has_clawback"]:
                    continue
            elif active_filter == "adjustments":
                if not row["has_adjustment"] and not row["has_clawback"]:
                    continue
            elif active_filter == "outstanding":
                if effective_status in {"paid", "closed"}:
                    continue
            elif active_filter == "claimable":
                if int(row["claimable_cents"]) <= 0:
                    continue
            elif active_filter == "claimed":
                if int(row["claimed_cents"]) <= 0:
                    continue
            elif active_filter == "paid":
                if not row["has_paid"]:
                    continue
            elif active_filter != "all" and effective_status != active_filter:
                continue
            result.append({
                "uid": str(row["uid"]), "student_id": str(row["student_id"]),
                "entity_name": str(row["entity_name"]), "gross_cents": int(row["gross_cents"]),
                "claimable_cents": int(row["claimable_cents"]),
                "status": labels.get(effective_status, effective_status),
                "calculated_at": str(row["calculated_at"]),
                "href": f"/revenue/direct-commission?commission={quote(str(row['uid']), safe='')}",
            })
        return result

    def _forecast_rows(self) -> list[dict[str, Any]]:
        run = self.connection.execute(
            """SELECT id,completed_at FROM revenue_forecast_runs
                WHERE status='completed' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if not run:
            return []
        rows = self.connection.execute(
            """SELECT f.uid,f.commission_cents,f.expected_date,c.student_id,ec.*
                 FROM revenue_forecast_lines f
                 JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                 JOIN revenue_enrolment_components ec ON ec.id=f.enrolment_component_id
                WHERE f.forecast_run_id=?
                  AND """ + self.visibility.case_predicate("c") + """
             ORDER BY f.created_at DESC,f.id DESC LIMIT 100""",
            (run["id"],),
        ).fetchall()
        return [{
            "uid": str(row["uid"]),
            "student_id": str(row["student_id"]),
            "entity_name": self._component_institution(row),
            "gross_cents": int(row["commission_cents"]),
            "claimable_cents": 0,
            "status": "预测",
            "calculated_at": str(row["expected_date"] or run["completed_at"] or "—"),
            "href": f"/revenue/direct-commission?commission={quote(str(row['uid']), safe='')}",
        } for row in rows]

    def _missing_direct_fact_rows(self, active_filter: str) -> list[dict[str, Any]]:
        if active_filter == "missing-commencement":
            missing_sql = """NOT EXISTS(
                SELECT 1 FROM revenue_commencement_facts f
                 WHERE f.enrolment_component_id=ec.id AND f.status='confirmed'
            )"""
            status = "待确认实际入学"
        else:
            missing_sql = """NOT EXISTS(
                SELECT 1 FROM revenue_tuition_facts f
                 WHERE f.enrolment_component_id=ec.id AND f.status='active'
            )"""
            status = "待补充学费结构"
        components = self.connection.execute(
            f"""SELECT ec.*,cc.student_id
                   FROM revenue_enrolment_components ec
                  JOIN revenue_commercial_cases cc ON cc.id=ec.commercial_case_id
                  WHERE ec.status='active' AND {missing_sql}
                    AND {self.visibility.case_predicate('cc')}
               ORDER BY ec.created_at DESC,ec.id DESC LIMIT 100"""
        ).fetchall()
        return [
            {
                "uid": str(row["uid"]),
                "student_id": str(row["student_id"]),
                "entity_name": self._component_institution(row),
                "gross_cents": 0,
                "claimable_cents": 0,
                "status": status,
                "calculated_at": "—",
                "href": f"/revenue/direct-commission?component={quote(str(row['uid']), safe='')}",
            }
            for row in components
        ]

    def _master_agent_rows(self, active_filter: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT i.uid,s.statement_reference,i.student_reference,i.program_text,
                      i.gross_commission_cents,i.share_cents,i.payable_cents,
                      COALESCE((SELECT match_status FROM revenue_statement_matches m
                                WHERE m.statement_item_id=i.id ORDER BY m.id DESC LIMIT 1),'unmatched') match_status
                 FROM revenue_master_agent_statement_items i
                 JOIN revenue_master_agent_statements s ON s.id=i.statement_id
                WHERE NOT EXISTS(
                          SELECT 1 FROM revenue_statement_matches current_match
                           WHERE current_match.statement_item_id=i.id
                             AND current_match.match_status='confirmed'
                      )
                   OR EXISTS(
                          SELECT 1 FROM revenue_statement_matches current_match
                          JOIN revenue_commission_lines current_line
                            ON current_line.id=current_match.commission_line_id
                          JOIN revenue_commercial_cases c
                            ON c.id=current_line.commercial_case_id
                           WHERE current_match.statement_item_id=i.id
                             AND current_match.match_status='confirmed'
                             AND """ + self.visibility.case_predicate("c") + """
                      )
             ORDER BY s.imported_at DESC,i.line_number LIMIT 200"""
        ).fetchall()
        labels = {
            "unmatched": "待匹配", "confirmed": "已确认", "excluded": "已排除",
            "disputed": "争议",
        }
        return [
            {
                "uid": str(row["uid"]), "statement_reference": str(row["statement_reference"]),
                "student_reference": str(row["student_reference"] or "—"),
                "program": str(row["program_text"] or "—"),
                "gross_cents": int(row["gross_commission_cents"]),
                "share_cents": int(row["share_cents"]), "payable_cents": int(row["payable_cents"]),
                "status": labels.get(str(row["match_status"]), str(row["match_status"])),
                "href": f"/revenue/master-agent?item={quote(str(row['uid']), safe='')}",
            }
            for row in rows
            if (
                active_filter == "all"
                or str(row["match_status"]) == active_filter
                or (
                    active_filter == "needs-review"
                    and str(row["match_status"]) in {"unmatched", "disputed"}
                )
            )
        ]

    def _contract_rows(
        self, active_filter: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT v.uid,e.id entity_id,e.entity_type,e.legal_name entity_name,
                      g.contract_kind,v.version_no,v.effective_from,v.effective_to,v.status,
                      COALESCE((SELECT status FROM revenue_contract_parse_runs p
                                WHERE p.contract_version_id=v.id ORDER BY p.id DESC LIMIT 1),'not_started') parse_status
                 FROM revenue_contract_versions v
                 JOIN revenue_contract_groups g ON g.id=v.contract_group_id
                 JOIN revenue_entities e ON e.id=g.revenue_entity_id
             ORDER BY v.created_at DESC,v.id DESC LIMIT 100"""
        ).fetchall()
        contract_labels = {
            "draft": "草稿", "under_review": "待审核", "active": "已激活",
            "expired": "已到期", "superseded": "已取代",
        }
        parse_labels = {
            "not_started": "未提取", "pending": "处理中", "suggested": "待审核建议",
            "failed": "提取失败", "reviewed": "已审核",
        }
        kind_labels = {"direct_commission": "直接佣金", "master_agent": "一级代理", "other": "其他"}
        type_labels = {
            "school": "学校", "higher_education_provider": "高等教育机构",
            "master_agent": "一级代理", "other": "其他",
        }
        search = self._contract_search_controls(parameters or {})
        today = date.today()
        expiring_by = today + timedelta(days=90)
        result = []
        for row in rows:
            effective_to = str(row["effective_to"] or "")
            if active_filter == "expiring":
                if row["status"] != "active" or not effective_to or not (str(today) <= effective_to <= str(expiring_by)):
                    continue
            elif active_filter == "expired":
                if row["status"] != "expired" and (not effective_to or effective_to >= str(today)):
                    continue
            elif active_filter == "under_review":
                if (
                    str(row["status"]) != "under_review"
                    and str(row["parse_status"]) not in {"pending", "suggested"}
                ):
                    continue
            elif active_filter != "all" and str(row["status"]) != active_filter:
                continue
            state, city = self._entity_authoritative_location(int(row["entity_id"]))
            projected = {
                "uid": str(row["uid"]), "entity_name": str(row["entity_name"]),
                "entity_type": type_labels.get(str(row["entity_type"]), str(row["entity_type"])),
                "entity_type_key": str(row["entity_type"]), "state": state, "city": city,
                "contract_kind": kind_labels.get(str(row["contract_kind"]), str(row["contract_kind"])),
                "version": f"V{int(row['version_no'])}", "effective_from": str(row["effective_from"]),
                "parse_status": parse_labels.get(str(row["parse_status"]), str(row["parse_status"])),
                "status": contract_labels.get(str(row["status"]), str(row["status"])),
                "href": f"/revenue/contracts?contract={quote(str(row['uid']), safe='')}",
            }
            haystack = f"{projected['entity_name']} {projected['contract_kind']}".casefold()
            if search["q"] and search["q"].casefold() not in haystack:
                continue
            if search["institution_type"] and search["institution_type"] != projected["entity_type_key"]:
                continue
            if search["state"] and search["state"].casefold() not in state.casefold():
                continue
            if search["city"] and search["city"].casefold() not in city.casefold():
                continue
            result.append(projected)
        return result

    def _entity_authoritative_location(self, entity_id: int) -> tuple[str, str]:
        link = self.connection.execute(
            """SELECT school_id,higher_education_provider_id
                 FROM revenue_entity_provider_links
                WHERE revenue_entity_id=?
             ORDER BY CASE WHEN effective_to IS NULL THEN 0 ELSE 1 END,id DESC LIMIT 1""",
            (entity_id,),
        ).fetchone()
        if not link:
            return "—", "—"
        if link["school_id"] and self._table_exists("schools"):
            return self._source_location("schools", int(link["school_id"]))
        if link["higher_education_provider_id"] and self._table_exists("higher_education_providers"):
            return self._source_location(
                "higher_education_providers", int(link["higher_education_provider_id"])
            )
        return "—", "—"

    def _source_location(self, table: str, source_id: int) -> tuple[str, str]:
        columns = self._columns(table)
        state_column = next(
            (column for column in ("state", "campus_state", "address_state") if column in columns), None
        )
        city_column = next(
            (column for column in ("city", "suburb", "campus_city", "address_city") if column in columns), None
        )
        if not state_column and not city_column:
            return "—", "—"
        selections = [state_column or "NULL", city_column or "NULL"]
        row = self.connection.execute(
            f"SELECT {selections[0]},{selections[1]} FROM {table} WHERE id=?", (source_id,)
        ).fetchone()
        return (
            str(row[0]) if row and row[0] else "—",
            str(row[1]) if row and row[1] else "—",
        )

    def _reconciliation_rows(self, active_filter: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if active_filter in {"all", "open-exceptions"}:
            rows.extend({
                "uid": str(row["uid"]), "kind": "异常",
                "exception_code": str(row["exception_code"]),
                "reference": exception_code_label(row["exception_code"]),
                "description": str(row["message"]), "amount_cents": None,
                "status": "待处理" if row["status"] == "open" else "已处理",
                "occurred_at": str(row["opened_at"]),
                "href": f"/revenue/reconciliation?record={quote(str(row['uid']), safe='')}",
            } for row in self.connection.execute(
                "SELECT e.* FROM revenue_exceptions e WHERE e.status='open' AND "
                + self.visibility.operational_exception_predicate("e")
                + " ORDER BY e.opened_at DESC,e.id DESC"
            ))
        if active_filter in {"all", "disputed"}:
            rows.extend({
                "uid": str(row["uid"]), "kind": "争议", "reference": str(row["dispute_code"]),
                "description": str(row["description"]), "amount_cents": int(row["disputed_cents"]),
                "status": "待处理" if row["status"] == "open" else "已处理",
                "occurred_at": str(row["opened_at"]),
                "href": f"/revenue/reconciliation?record={quote(str(row['uid']), safe='')}",
            } for row in self.connection.execute(
                "SELECT * FROM revenue_disputes WHERE status='open' ORDER BY opened_at DESC,id DESC"
            ))
        if active_filter in {"all", "suggested"}:
            rows.extend({
                "uid": str(row["uid"]), "kind": "对账建议", "reference": str(row["source_item_uid"]),
                "description": f"建议匹配至 {row['suggested_target_kind']} · 仅供管理员确认",
                "amount_cents": int(row["suggested_amount_cents"]), "status": "待确认",
                "occurred_at": "—",
                "href": f"/revenue/reconciliation?record={quote(str(row['uid']), safe='')}",
            } for row in self.connection.execute(
                """SELECT s.* FROM revenue_reconciliation_suggestions s
                    WHERE s.review_status='pending' AND """
                + self.visibility.operational_suggestion_predicate("s")
                + " ORDER BY s.id DESC"
            ))
        if active_filter in {"all", "unallocated"}:
            rows.extend({
                "uid": str(row["uid"]), "kind": "未分配收款",
                "reference": str(row["external_reference"] or row["uid"]),
                "description": "已确认收款尚未完成分配",
                "amount_cents": int(row["amount_cents"]) - int(row["allocated_cents"]),
                "status": "待匹配", "occurred_at": str(row["received_date"]),
                "href": f"/revenue/reconciliation?record={quote(str(row['uid']), safe='')}",
            } for row in self.connection.execute(
                """SELECT * FROM revenue_payments
                    WHERE status='confirmed' AND allocated_cents<amount_cents
                 ORDER BY received_date DESC,id DESC"""
            ))
        if active_filter in {"all", "remittance"}:
            rows.extend({
                "uid": str(row["uid"]), "kind": "汇款",
                "reference": str(row["external_reference"] or row["uid"]),
                "description": "一级代理收款或汇款",
                "amount_cents": int(row["amount_cents"]),
                "status": "已确认" if row["status"] == "confirmed" else "已冲销",
                "occurred_at": str(row["received_date"]),
                "href": f"/revenue/reconciliation?record={quote(str(row['uid']), safe='')}",
            } for row in self.connection.execute(
                """SELECT * FROM revenue_payments
                    WHERE counterparty_kind='master_agent'
                 ORDER BY received_date DESC,id DESC"""
            ))
        if active_filter in {"all", "completed"}:
            rows.extend({
                "uid": str(row["uid"]), "kind": "已完成对账",
                "reference": str(row["reconciliation_kind"]),
                "description": "管理员已确认对账结果",
                "amount_cents": None, "status": "已完成",
                "occurred_at": str(row["confirmed_at"] or row["created_at"]),
                "href": f"/revenue/reconciliation?record={quote(str(row['uid']), safe='')}",
            } for row in self.connection.execute(
                """SELECT * FROM revenue_reconciliation_runs
                    WHERE status='confirmed' ORDER BY confirmed_at DESC,id DESC"""
            ))
        return rows[:200]

    def _pending_items(self, values: dict[str, int]) -> list[dict[str, Any]]:
        missing_commencement = self._scalar(
            """SELECT COUNT(*) FROM revenue_enrolment_components component
                JOIN revenue_commercial_cases c ON c.id=component.commercial_case_id
                WHERE component.status='active'
                  AND NOT EXISTS(
                    SELECT 1 FROM revenue_commencement_facts f
                     WHERE f.enrolment_component_id=component.id AND f.status='confirmed'
                  ) AND """ + self.visibility.case_predicate("c")
        )
        missing_tuition = self._scalar(
            """SELECT COUNT(*) FROM revenue_enrolment_components component
                JOIN revenue_commercial_cases c ON c.id=component.commercial_case_id
                WHERE component.status='active'
                  AND NOT EXISTS(
                    SELECT 1 FROM revenue_tuition_facts f
                     WHERE f.enrolment_component_id=component.id AND f.status='active'
                  ) AND """ + self.visibility.case_predicate("c")
        )
        claimable = self._scalar(
            """SELECT COUNT(*) FROM revenue_commission_instalments i
                JOIN revenue_commission_lines l ON l.id=i.commission_line_id
                JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                WHERE i.status IN ('eligible','partial')
                  AND i.eligible_cents>i.claimed_cents AND """
            + self.visibility.case_predicate("c")
        )
        disputes = self._scalar(
            "SELECT COUNT(*) FROM revenue_disputes WHERE status='open'"
        )
        clawbacks = self._scalar(
            """SELECT COUNT(*) FROM revenue_clawbacks cb
                JOIN revenue_commission_lines l ON l.id=cb.commission_line_id
                JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
               WHERE cb.status='open' AND """
            + self.visibility.case_predicate("c")
        )
        return [
            self._pending(
                "open_exceptions", "待处理异常", values["open_exceptions"],
                "/revenue/reconciliation?filter=open-exceptions",
            ),
            self._pending(
                "missing_commencement", "待确认实际入学", missing_commencement,
                "/revenue/direct-commission?filter=missing-commencement",
            ),
            self._pending(
                "missing_tuition", "待补充学费结构", missing_tuition,
                "/revenue/direct-commission?filter=missing-tuition",
            ),
            self._pending(
                "claimable_unclaimed", "可申领但未申领", claimable,
                "/revenue/direct-commission?filter=claimable",
            ),
            self._pending(
                "unmatched_statements", "待匹配一级代理流水",
                values["unmatched_statement_items"],
                "/revenue/master-agent?filter=unmatched",
            ),
            self._pending(
                "open_disputes", "待处理争议", disputes,
                "/revenue/master-agent?filter=disputed",
            ),
            self._pending(
                "open_clawbacks", "待处理追回", clawbacks,
                "/revenue/direct-commission?filter=clawback",
            ),
        ]

    @staticmethod
    def _pending(category: str, label: str, count: int, href: str) -> dict[str, Any]:
        return {
            "category": category,
            "label": label,
            "count": int(count),
            "href": href,
        }

    def _recent_cases(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id,uid,application_cycle_id,student_id,status,
                      existing_aaie_k12,opened_at,closed_at
                 FROM revenue_commercial_cases c
                WHERE """ + self.visibility.case_predicate("c") + """
             ORDER BY COALESCE(c.closed_at,c.opened_at) DESC,c.id DESC
                LIMIT 10"""
        ).fetchall()
        return [self._case_projection(row) for row in rows]

    def _case_projection(self, row: sqlite3.Row) -> dict[str, Any]:
        case_id = int(row["id"])
        uid = str(row["uid"])
        institution, intake, business_type = self._case_authoritative_context(
            case_id, int(row["application_cycle_id"])
        )
        return {
            "uid": uid,
            "student_name": self._student_display_name(str(row["student_id"])),
            "student_id": str(row["student_id"]),
            "classification": "现有 AAIE K–12" if row["existing_aaie_k12"] else "新客户",
            "business_type": business_type,
            "status": self._case_status_label(str(row["status"])),
            "institution": institution,
            "intake": intake,
            "billing_status": self._billing_status(case_id),
            "commission_status": self._commission_status(case_id),
            "updated_at": row["closed_at"] or row["opened_at"],
            "updated_at_display": melbourne_datetime_display(row["closed_at"] or row["opened_at"]),
            "href": case_url(uid, self.case_surface),
        }

    def _student_display_name(self, student_id: str) -> str:
        if student_id in self._student_name_cache:
            return self._student_name_cache[student_id]
        columns = self._columns("students")
        required = {"student_id", "passport_first_name", "passport_last_name"}
        if not required.issubset(columns):
            self._student_name_cache[student_id] = "姓名待关联"
            return "姓名待关联"
        row = self.connection.execute(
            """SELECT passport_first_name,passport_last_name
                 FROM students WHERE student_id=?""",
            (student_id,),
        ).fetchone()
        name = ""
        if row:
            name = " ".join(
                str(part or "").strip()
                for part in (row["passport_first_name"], row["passport_last_name"])
                if str(part or "").strip()
            )
        result = name or "姓名待关联"
        self._student_name_cache[student_id] = result
        return result

    def _case_authoritative_context(self, case_id: int, cycle_id: int) -> tuple[str, str, str]:
        component = self.connection.execute(
            """SELECT * FROM revenue_enrolment_components
                WHERE commercial_case_id=?
             ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,id
                LIMIT 1""",
            (case_id,),
        ).fetchone()
        institution = "未关联"
        if component:
            institution = self._component_institution(component)
        cycle = self.connection.execute(
            "SELECT * FROM application_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        return institution, self._cycle_intake(cycle), self._cycle_business_type(cycle)

    @staticmethod
    def _cycle_business_type(cycle: sqlite3.Row | None) -> str:
        if not cycle:
            return "未分类"
        keys = set(cycle.keys())
        stream = str(cycle["education_stream"] or "") if "education_stream" in keys else ""
        if stream == "school_education":
            return "K–12 学校教育"
        if stream == "higher_education":
            return "高等教育"
        cycle_type = str(cycle["cycle_type"] or "") if "cycle_type" in keys else ""
        return {
            "transfer": "转学申请",
            "progression": "升学申请",
            "initial": "首次申请",
        }.get(cycle_type, "其他教育服务")

    def _component_institution(self, component: sqlite3.Row) -> str:
        if component["school_enrolment_id"]:
            enrolment = self.connection.execute(
                "SELECT * FROM student_school_enrolments WHERE id=?",
                (component["school_enrolment_id"],),
            ).fetchone()
            if enrolment and "school_id" in enrolment.keys():
                name = self._entity_display("schools", enrolment["school_id"])
                return name or f"学校 #{enrolment['school_id']}"
        if component["higher_education_application_id"]:
            application = self.connection.execute(
                "SELECT * FROM higher_education_applications WHERE id=?",
                (component["higher_education_application_id"],),
            ).fetchone()
            if application:
                for column in ("provider_id", "higher_education_provider_id"):
                    if column in application.keys() and application[column]:
                        name = self._entity_display(
                            "higher_education_providers", application[column]
                        )
                        return name or f"院校 #{application[column]}"
        return "未关联"

    def _entity_display(self, table: str, entity_id: Any) -> str | None:
        if not entity_id or not self._table_exists(table):
            return None
        columns = self._columns(table)
        display_column = next(
            (column for column in ("display_name", "school_name", "provider_name", "name", "legal_name")
             if column in columns),
            None,
        )
        if not display_column:
            return None
        row = self.connection.execute(
            f"SELECT {display_column} FROM {table} WHERE id=?", (entity_id,)
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    @staticmethod
    def _cycle_intake(cycle: sqlite3.Row | None) -> str:
        if not cycle:
            return "—"
        keys = set(cycle.keys())
        for column in ("intake_display", "intake", "intended_start_date", "course_start_date", "start_date"):
            if column in keys and cycle[column]:
                return str(cycle[column])
        year = cycle["intake_year"] if "intake_year" in keys else None
        term = cycle["intake_term"] if "intake_term" in keys else None
        if year and term:
            return f"{year} · {term}"
        return str(year or term or "—")

    def _billing_status(self, case_id: int) -> str:
        totals = self.connection.execute(
            """SELECT COUNT(*),COALESCE(SUM(f.net_cents),0),
                      COALESCE(SUM((SELECT SUM(a.allocated_base_cents)
                                      FROM revenue_client_payment_allocations a
                                     WHERE a.fee_item_id=f.id)),0)
                 FROM revenue_fee_items f
                WHERE f.commercial_case_id=? AND f.status!='cancelled'""",
            (case_id,),
        ).fetchone()
        fee_count, final_base, received = map(int, totals)
        if not fee_count:
            return "未建立"
        if received <= 0:
            return "待收款"
        if received < final_base:
            return "部分收款"
        return "已收清"

    def _commission_status(self, case_id: int) -> str:
        instalments = self.connection.execute(
            """SELECT COALESCE(SUM(claimed_cents),0),
                      COALESCE(SUM(CASE WHEN i.status IN ('eligible','partial')
                                         AND eligible_cents>claimed_cents
                                        THEN eligible_cents-claimed_cents ELSE 0 END),0),
                      COALESCE(MAX(CASE WHEN i.status='paid' THEN 1 ELSE 0 END),0)
                 FROM revenue_commission_instalments i
                 JOIN revenue_commission_lines l ON l.id=i.commission_line_id
                WHERE l.commercial_case_id=?""",
            (case_id,),
        ).fetchone()
        if instalments and int(instalments[2]):
            return "已收款"
        if instalments and int(instalments[0]) > 0:
            return "已申领"
        if instalments and int(instalments[1]) > 0:
            return "可申领"
        statuses = {
            str(row[0]) for row in self.connection.execute(
                "SELECT DISTINCT status FROM revenue_commission_lines WHERE commercial_case_id=?",
                (case_id,),
            ).fetchall()
        }
        if not statuses:
            return "未建立"
        for status, label in (
            ("claimable", "可申领"),
            ("claimed", "已申领"),
            ("accrued", "已确认"),
            ("forecast", "预测中"),
            ("paid", "已收款"),
            ("closed", "已关闭"),
        ):
            if status in statuses:
                return label
        return "处理中"

    def _latest_forecast_cents(self) -> int:
        run = self.connection.execute(
            "SELECT id FROM revenue_forecast_runs WHERE status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not run:
            return 0
        return self._scalar(
            "SELECT COALESCE(SUM(f.commission_cents),0) FROM revenue_forecast_lines f "
            "JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id "
            "WHERE f.forecast_run_id=? AND " + self.visibility.case_predicate("c"),
            (run[0],),
        )

    @staticmethod
    def _case_status_label(status: str) -> str:
        return {
            "open": "进行中",
            "on_hold": "已暂停",
            "closed": "已关闭",
        }.get(status, status)

    def _client_receivable_cents(self) -> int:
        return self._scalar(
            """SELECT COALESCE(SUM(
                       MAX(0,f.net_cents-COALESCE((
                           SELECT SUM(a.allocated_base_cents)
                             FROM revenue_client_payment_allocations a
                            WHERE a.fee_item_id=f.id
                       ),0))
                   ),0)
                 FROM revenue_fee_items f
                 JOIN revenue_commercial_cases c ON c.id=f.commercial_case_id
                WHERE f.status!='cancelled' AND """ + self.visibility.case_predicate("c")
        )

    def _unmatched_statement_items(self) -> int:
        return self._scalar(
            """SELECT COUNT(*) FROM revenue_master_agent_statement_items i
                WHERE NOT EXISTS(
                    SELECT 1 FROM revenue_statement_matches m
                     WHERE m.statement_item_id=i.id AND m.match_status='confirmed'
                )"""
        )

    def _scalar(self, sql: str, parameters: tuple[Any, ...] = ()) -> int:
        row = self.connection.execute(sql, parameters).fetchone()
        return int((row[0] if row else 0) or 0)

    def _table_exists(self, table: str) -> bool:
        return bool(self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone())

    def _columns(self, table: str) -> set[str]:
        if table not in self._column_cache:
            self._column_cache[table] = {
                str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
        return self._column_cache[table]

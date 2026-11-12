"""Shared editor for confirmed school, provider and education authority contracts."""

import json
from html import escape
from urllib.parse import quote

from revenue import vic_rcti
from revenue.institution_commission_draft import (
    CONFIRMED, FEE_PERIODS, GST_MODES, NSW_CLAIMS_ONLY, NSW_RATE_LABELS, TIER_LABELS, InstitutionCommissionDraft,
)
from revenue.school_contract_pages import _page, confirmed_url
from revenue.school_rule_revision import PERIODS, revise_school_rule, school_rules


class CommissionTermsEditor:
    def __init__(self, repository, intake, school, target, uid, actor, scope=None):
        self.repository, self.school, self.target = repository, school, target
        self.uid, self.actor, self.scope = uid, actor, scope
        self.workflow = None
        if any(workflow_uid == uid for _, _, workflow_uid in school["confirmed"]):
            self.workflow = InstitutionCommissionDraft(repository, intake, uid, actor)
            if self.workflow.target != target:
                raise PermissionError
        elif not scope or not any(document == uid for document, *_ in school["active"]):
            raise LookupError

    def _school_rule(self):
        row = next((row for row in school_rules(self.repository.connection, self.scope)
                    if row["document_uid"] == self.uid), None)
        if row is None:
            raise LookupError
        return row

    def save(self, form, key):
        if self.workflow:
            return self.workflow.revise_terms(form, key)
        return revise_school_rule(self.repository, self.scope, self.uid, form, self.actor, key)

    def render(self, csrf, token, *, error="", form=None):
        fields = {}
        base = confirmed_url(self.target)
        action = base + "&edit_commission=" + quote(self.uid, safe="")

        def value(name, default):
            return str((form or {}).get(name, default))

        def inp(label, name, default, *, kind="text", attrs="required"):
            fields[name] = (f'<label>{escape(label)}<input type="{kind}" name="{name}" '
                            f'value="{escape(value(name, default), quote=True)}" {attrs}></label>')

        def select(label, name, default, choices):
            chosen = value(name, default)
            options = "".join(f'<option value="{key}"{" selected" if key == chosen else ""}>{escape(text)}</option>'
                              for key, text in choices.items())
            fields[name] = f'<label>{escape(label)}<select name="{name}">{options}</select></label>'

        if self.workflow:
            terms = self.workflow.terms()
            if not terms:
                raise LookupError
            mode = terms.get("payment_mode")
            revision, document = terms["revision"], self.workflow.document["uid"]
            start, end, gst = terms["start"], terms["end"], terms["gst"]
            labels = {**TIER_LABELS, **(NSW_RATE_LABELS if mode == NSW_CLAIMS_ONLY else
                                      vic_rcti.LABELS if mode == vic_rcti.PAYMENT else {})}
            for name, points in terms["rates"].items():
                inp(labels[name] + "（%）", name, f"{points / 100:g}")
            if mode not in {NSW_CLAIMS_ONLY, vic_rcti.PAYMENT}:
                periods = {"whole_course": FEE_PERIODS["whole_course"]} if terms["profile"] == "recruitment_matrix" else FEE_PERIODS
                select("计佣周期", "period", terms["period"], periods)
            fields["conditions"] = ('<label>申领条件<textarea name="conditions" rows="5" maxlength="6000" required>'
                                    + escape(value("conditions", terms["conditions"])) + '</textarea></label>')
            if mode != vic_rcti.PAYMENT:
                days = terms["payment_days"]
                inp("付款期限（天）", "payment_days", "" if days is None else days,
                    kind="number", attrs='min="0" max="365"' + (' required' if mode != NSW_CLAIMS_ONLY else ""))
            history = self.repository.connection.execute(
                """SELECT occurred_at, after_json FROM revenue_audit_events
                    WHERE action=? AND aggregate_uid=? ORDER BY id DESC LIMIT 20""", (CONFIRMED, self.uid),
            ).fetchall()
            versions = [(when, tuple(json.loads(raw)["rates"].values())) for when, raw in history]
        else:
            rule = self._school_rule()
            config = json.loads(rule["canonical_json"])
            revision, document = rule["uid"], rule["document_uid"]
            start, end, gst = rule["effective_from"], rule["effective_to"], config.get("gst_mode", "none")
            labels = {"local": "本地档", "international": "国际档", "any": "统一比例"}
            for index, rate in enumerate(config.get("tiers") or config["instalments"]):
                inp(labels.get(rate.get("tuition_kind"), f"第 {index + 1} 档") + "（%）",
                    f"rate_{index}", f'{rate["rate_basis_points"] / 100:g}')
            select("计佣周期", "period", config.get("tuition_period", "first_year"), PERIODS)
            claim = config.get("claim", {})
            inp("开学后满几天可申领", "commencement_days", claim.get("commencement_days", 0), kind="number",
                attrs='min="0" max="365" required')
            if config.get("school_commission_configurable"):
                select("学校注册凭证", "registration_required", str(int(claim.get("registration_required", False))),
                       {"0": "选填", "1": "必须提供"})
            else:
                fields["registration_required"] = '<input type="hidden" name="registration_required" value="1">'
            select("付款期限", "payment_days", str(claim.get("payment_days_from_valid_invoice", 30)),
                   {"0": "立即", "7": "7 天", "15": "15 天", "30": "30 天"})
            history = self.repository.connection.execute(
                """SELECT r.created_at, s.canonical_json FROM revenue_commission_rule_versions r
                    JOIN revenue_rule_snapshots s ON s.id=r.rule_snapshot_id
                    WHERE r.contract_version_id=(SELECT contract_version_id FROM revenue_commission_rule_versions WHERE uid=?)
                    ORDER BY r.version_no DESC LIMIT 20""", (revision,),
            ).fetchall()
            versions = []
            for when, raw in history:
                historical = json.loads(raw)
                rates = historical.get("tiers") or historical["instalments"]
                versions.append((when, tuple(rate["rate_basis_points"] for rate in rates)))
        inp("合同生效日", "start", start, kind="date")
        inp("合同到期日", "end", end, kind="date")
        select("GST", "gst", gst, GST_MODES)
        status = f'<p role="alert">{escape(error)}</p>' if error else ""
        history_html = ""
        if versions:
            rows = "".join(f'<li>{escape(when)} · {escape(" / ".join(f"{points/100:g}%" for points in rates))}</li>'
                           for when, rates in versions)
            history_html = f'<details><summary>修改记录</summary><ul>{rows}</ul></details>'
        body = f'''<h2>{escape(self.school["name"])}</h2>{status}
          <p><a href="/revenue/documents/{quote(document, safe="")}/view">查看原合同 PDF</a></p>
          <form class="revenue-action-form" method="post" action="{escape(action, quote=True)}">
          <input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">
          <input type="hidden" name="revision" value="{escape(value("revision", revision), quote=True)}">
          <input type="hidden" name="idempotency_key" value="{escape(token, quote=True)}">
          {"".join(fields.values())}
          <button class="primary" type="submit">保存修改</button></form>{history_html}'''
        return _page("编辑佣金条款", body, back_href=base, back_label="返回学校佣金")

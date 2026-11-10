"""School commission selection and confirmed internal Claim generation."""
import hashlib
import json

from revenue.money import apply_basis_points, commission_gst_amounts
from revenue.read_models import RevenueReadModels
from revenue.repository import canonical_json, new_uid
from revenue.secure_storage import StorageUnavailable
from revenue.toorak_claims import claim_evidence_fields, formal_evidence_options


class SchoolClaimCandidates:
    def __init__(self, claims, *, toorak_draft_release=False):
        if not (claims.formal and (claims.simulation or toorak_draft_release) and claims.forecast.school_id and claims.entity_id):
            raise PermissionError("当前学校佣金清单未开放。")
        self.claims = claims
        self.connection = claims.connection
        self.school_id = claims.forecast.school_id
        self.model = RevenueReadModels(self.connection)

    def list(self):
        return [self._candidate(row) for row in self.model.school_claim_components(self.school_id)]

    @staticmethod
    def _message(error):
        detail = str(error)
        return detail if any("\u4e00" <= char <= "\u9fff" for char in detail) else "业务资料缺失或不一致，请到档案核对。"

    def _lines(self, component_id):
        return self.connection.execute(
            """SELECT l.id,l.uid,l.status,l.revenue_entity_id,l.gross_cents,s.canonical_json,
                      EXISTS(SELECT 1 FROM revenue_commission_instalments i
                          JOIN revenue_claim_allocations a ON a.commission_instalment_id=i.id
                          WHERE i.commission_line_id=l.id) AS claimed
                 FROM revenue_commission_lines l
                 JOIN revenue_rule_snapshots s ON s.id=l.rule_snapshot_id
                WHERE l.enrolment_component_id=?""",
            (component_id,),
        ).fetchall()

    def _candidate(self, component):
        claims = self.claims
        reasons = []
        result = {**component, "reasons": reasons, "forecast": None, "evidence": []}
        lines = self._lines(component["component_id"])
        result["already_claimed"] = any(
            row["claimed"] or row["status"] in {"claimed", "paid"} for row in lines
        )
        if result["already_claimed"]:
            reasons.append("已申领，不可重复勾选。")
        elif len(lines) > 1 or any(row["status"] != "forecast" or row["revenue_entity_id"] != claims.entity_id
                                   for row in lines):
            reasons.append("已有佣金明细的状态或付款主体需复核。")
        try:
            forecast = claims.forecast.preview(component["case_uid"], component["component_uid"])
        except (ValueError, LookupError) as error:
            reasons.append(self._message(error))
            return result
        result["forecast"] = forecast
        if reasons:
            return result
        if lines and (json.loads(lines[0]["canonical_json"]).get("digest") != forecast["digest"]
                      or lines[0]["gross_cents"] != forecast["gross_cents"]):
            reasons.append("已保存的预计佣金与当前资料不同，请到档案复核。")
        try:
            if lines:
                claims.claimable_instalment(lines[0])
            claims.check_commencement(forecast)
            rule = forecast["rule"]
            claims.check_formal_payment_terms(rule["contract_uid"], rule["rule_id"])
        except ValueError as error:
            reasons.append(self._message(error))
        if forecast["gross_cents"] <= 0:
            reasons.append("佣金金额必须大于零。")
        for field, kind, label in claim_evidence_fields(forecast):
            options = formal_evidence_options(
                self.connection, component["case_id"], component["student_id"], claims.entity_id, kind, claims.simulation,
            )
            if not options:
                reasons.append(f"缺少{label}。")
                continue
            if len(options) != 1:
                reasons.append(f"有多份{label}，请到档案核对。")
                continue
            try:
                proof, _ = claims._formal_evidence(component["case_uid"], component["student_id"], options[0]["id"], kind, label)
            except (OSError, StorageUnavailable):
                reasons.append(f"{label}暂无法验证。")
            except ValueError as error:
                reasons.append(self._message(error))
            else:
                result["evidence"].append({**proof, "field": field})
        return result

    def review(self, component_uids, *, gst_mode=None):
        if not component_uids or len(component_uids) > 100 or len(set(component_uids)) != len(component_uids):
            raise ValueError("请勾选 1 至 100 条不同的入学项目。")
        rows = self.model.school_claim_components(self.school_id, component_uids)
        if {row["component_uid"] for row in rows} != set(component_uids):
            raise ValueError("所选入学项目已变更或不属于该学校，请重新选择。")
        rows = [self._candidate(row) for row in rows]
        blocked = [f'{row["student_name"]}（{row["student_id"]}）：' + " ".join(row["reasons"])
                   for row in rows if row["reasons"]]
        if blocked:
            raise ValueError(" ".join(blocked))
        row_digests = {
            row["component_uid"]: hashlib.sha256(canonical_json(
                [row["forecast"]["digest"], row["evidence"]]
            ).encode()).hexdigest()
            for row in rows
        }
        fingerprint = [self.school_id, self.claims.entity_id, row_digests]
        if gst_mode is not None:
            if gst_mode == "":
                defaults = {row["forecast"]["gst_mode"] for row in rows}
                if len(defaults) != 1:
                    raise ValueError("所选学生的合同 GST 默认值不同，请分别复核。")
                gst_mode = defaults.pop()
            for row in rows:
                forecast = row["forecast"]
                commission = apply_basis_points(forecast["tuition"]["tuition_cents"], forecast["tier"]["rate_basis_points"])
                row["invoice_amounts"] = commission_gst_amounts(commission, gst_mode)
            fingerprint.append(gst_mode)
        return {
            "rows": rows, "row_digests": row_digests, "digest": hashlib.sha256(canonical_json(fingerprint).encode()).hexdigest(),
            **({"gst_mode": gst_mode} if gst_mode is not None else {}),
            **{field: sum(row.get("invoice_amounts", row["forecast"])[field] for row in rows)
               for field in ("net_cents", "gst_cents", "gross_cents")},
        }

    def confirm(self, component_uids, digest, *, reviewed, row_digests=None, gst_mode=None):
        if not reviewed:
            raise ValueError("请确认已核对所选学生和申领凭证。")
        current = self.review(component_uids, gst_mode=gst_mode)
        if current["digest"] != digest:
            changed = [row["student_name"] for row in current["rows"]
                       if (row_digests or {}).get(row["component_uid"]) != current["row_digests"][row["component_uid"]]]
            names = "、".join(changed or [row["student_name"] for row in current["rows"]])
            raise ValueError(f"所选学生（{names}）的金额、GST 或凭证已变更，请重新复核。")
        return current

    def generate(self, component_uids, digest, *, reviewed, row_digests=None, gst_mode=None):
        """The caller holds the school write transaction across this whole batch."""
        if not reviewed:
            raise ValueError("请确认已核对所选学生和申领凭证。")
        if not component_uids or len(component_uids) > 100 or len(set(component_uids)) != len(component_uids):
            raise ValueError("请勾选 1 至 100 条不同的入学项目。")
        ids = sorted(component_uids)
        claims = self.claims
        payload = {"school_id": self.school_id, "entity_id": claims.entity_id, "components": ids, "digest": digest}
        if gst_mode is not None:
            payload["gst_mode"] = gst_mode
        key = hashlib.sha256(canonical_json(payload).encode()).hexdigest()

        def operation():
            selection = self.confirm(ids, digest, reviewed=True, row_digests=row_digests, gst_mode=gst_mode)
            tax = {"gst_mode": selection["gst_mode"]} if gst_mode is not None else {}
            generated = []
            for row in selection["rows"]:
                try:
                    lines = self._lines(row["component_id"])
                    forecast = row["forecast"]
                    line = lines[0] if lines else claims.forecast.save(
                        row["case_uid"], row["component_uid"], False, forecast["digest"]
                    )
                    evidence = {item["field"]: item["document_uid"] for item in row["evidence"]}
                    recruited = "configured_rule" if forecast.get("school_commission_configurable") else "agent_named"
                    review = claims.preview(row["case_uid"], line["uid"], evidence, recruited, True, **tax)
                    claim = claims.issue_claim(row["case_uid"], line["uid"], evidence, recruited, True, review["digest"], **tax)
                except (ValueError, LookupError, OSError) as error:
                    raise ValueError(f'{row["student_name"]}（{row["student_id"]}）：{self._message(error)}') from error
                generated.append({**{name: claim[name] for name in ("uid", "claim_number", "net_cents", "gst_cents", "gross_cents")},
                                  "student_id": row["student_id"], "student_name": row["student_name"]})
            return {"uid": new_uid("school-claim-batch"), "school_id": self.school_id,
                    "entity_id": claims.entity_id, "claims": generated}

        return claims.repository.command(
            idempotency_key=f'{"staging" if claims.simulation else "production"}-school-claim-batch:{key}',
            command_type="school_claim_batch_generated",
            aggregate_type="school_claim_batch", payload=payload, actor_user_id=claims.actor, operation=operation,
        )

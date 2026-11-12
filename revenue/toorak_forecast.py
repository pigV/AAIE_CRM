"""Administrator-initiated forecasts for reviewed school commission contracts."""

from datetime import date
import hashlib
import json

from revenue.money import apply_basis_points, gst_from_inclusive
from revenue.operational_facts import RevenueOperationalFacts
from revenue.operational_visibility import RevenueOperationalVisibility
from revenue.repository import canonical_json, new_uid
from revenue.school_contracts import SchoolCommissionContracts


class ToorakForecast:
    def __init__(self, service, actor_user_id, actor_role, *, formal=False, school_id=None, entity_id=None, simulation=True):
        if actor_role != "Admin":
            raise PermissionError("School commission forecasts require Admin")
        self.service = service
        self.repository = service.repository
        self.connection = service.connection
        self.actor = actor_user_id
        self.facts = RevenueOperationalFacts(self.connection)
        self.formal = formal
        self.school_id = school_id
        self.entity_id = entity_id
        self.simulation = simulation

    def preview(self, case_uid, component_uid, confirm_other=False):
        case = self.repository.require_by_uid("revenue_commercial_cases", case_uid)
        component = self.repository.require_by_uid("revenue_enrolment_components", component_uid)
        cycle = self.facts.cycle(case["application_cycle_id"])
        visibility = RevenueOperationalVisibility(self.connection)
        if not self.connection.execute(
            "SELECT 1 FROM revenue_commercial_cases c WHERE c.id=? AND " + visibility.case_predicate("c"),
            (case["id"],),
        ).fetchone() or cycle.student_id != case["student_id"]:
            raise ValueError("学生业务档案已失效。")
        if (component["commercial_case_id"] != case["id"] or component["component_kind"] != "school"
                or component["status"] != "active" or not component["final_acceptance_id"]
                or cycle.cycle_type == "transfer"):
            raise ValueError("请选择本档案的有效新生学校入学项目。")
        acceptance = self.facts.final_acceptance(component["final_acceptance_id"])
        if (acceptance.application_cycle_id != cycle.id or acceptance.student_id != cycle.student_id
                or acceptance.acceptance_status != "active" or not acceptance.school_id):
            raise ValueError("入学确认与该学生业务不一致。")
        offer = self.facts.offer(acceptance.offer_received_event_id)
        if (offer.application_cycle_id != cycle.id or offer.student_id != cycle.student_id
                or (offer.school_id is not None and offer.school_id != acceptance.school_id)):
            raise ValueError("录取来源与学生入学确认不一致。")
        contracts = SchoolCommissionContracts(self.service,self.actor,"Admin",formal=self.formal,
                                              school_id=self.school_id,entity_id=self.entity_id,simulation=self.simulation)
        if self.formal and self.school_id is not None and self.entity_id is not None:
            if acceptance.school_id != self.school_id:
                raise ValueError("学生入学项目不属于所选学校。")
            school = contracts._school(acceptance.school_id)
        else:
            school = contracts._toorak_school(acceptance.school_id)
        commencement = self.connection.execute(
            """SELECT uid,actual_commencement_date,evidence_document_id FROM revenue_commencement_facts
                WHERE enrolment_component_id=? AND status='confirmed'""", (component["id"],),
        ).fetchone()
        if not commencement:
            raise ValueError("须先确认实际开学日。")
        day = date.fromisoformat(commencement["actual_commencement_date"]).isoformat()
        if day > date.today().isoformat():
            raise ValueError("实际开学日尚未到达。")

        rows = self.connection.execute(
            """SELECT r.uid AS rule_uid,r.id AS rule_id,v.uid AS contract_uid,v.version_no,
                      r.effective_from,r.effective_to,e.id AS entity_id,s.uid AS snapshot_uid,s.canonical_json
                 FROM revenue_entity_provider_links l
                 JOIN revenue_entities e ON e.id=l.revenue_entity_id
                 JOIN revenue_contract_groups g ON g.revenue_entity_id=e.id
                 JOIN revenue_contract_versions v ON v.contract_group_id=g.id
                 JOIN revenue_documents d ON d.id=v.document_id
                 JOIN revenue_commission_rule_versions r ON r.contract_version_id=v.id
                 JOIN revenue_rule_snapshots s ON s.id=r.rule_snapshot_id
                WHERE l.school_id=? AND e.status='active' AND e.entity_type='school'
                  AND l.effective_from<=? AND (l.effective_to IS NULL OR l.effective_to>=?)
                  AND g.contract_kind='direct_commission' AND v.status='active' AND (v.version_no=1 OR ?)
                  AND r.version_no=(SELECT max(latest.version_no) FROM revenue_commission_rule_versions latest
                                     WHERE latest.contract_version_id=v.id AND latest.status='active')
                  AND d.document_kind=? AND r.status='active' AND (? IS NULL OR e.id=?)
                  AND r.effective_from<=? AND r.effective_to>=?
                  AND json_extract(s.canonical_json,'$.toorak_tier_package')=1
                  AND json_extract(s.canonical_json,'$.school_id')=l.school_id
                  AND (?=0 OR json_extract(s.canonical_json,'$.pilot_simulation')=?)""",
            (school["id"],day,day,self.formal,contracts.contract_kind,self.entity_id,self.entity_id,day,day,self.formal,self.simulation),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("实际开学日须匹配唯一已激活的学校佣金合同。")
        rule = dict(rows[0])
        config = json.loads(rule.pop("canonical_json"))
        configurable = self.formal and config.get("school_commission_configurable") is True
        if configurable:
            tier, tuition, visa = self._configurable_tier(config,component["id"],cycle,day)
        else:
            tier, tuition, visa = self._toorak_tier(config,component,cycle,day,confirm_other)
        amount = apply_basis_points(tuition["tuition_cents"], tier["rate_basis_points"])
        mode = config["gst_mode"]
        if mode == "inclusive":
            net, gst = gst_from_inclusive(amount, 1000)
        elif mode in {"exclusive", "none"}:
            net, gst = amount, apply_basis_points(amount, 1000) if mode == "exclusive" else 0
        else:
            raise ValueError("合同 GST 尚未确认。")
        result = {
            "toorak_forecast": True,
            "case_uid": case_uid, "case_id": case["id"], "student_id": cycle.student_id,
            "component_uid": component_uid, "component_id": component["id"],
            "acceptance_id": acceptance.id, "acceptance_version": acceptance.version,
            "commencement": dict(commencement), "school": school["school_name"],
            "rule": rule, "tier": tier, "visa": visa,
            "tuition": dict(tuition), "gst_mode": mode,
            "net_cents": net, "gst_cents": gst, "gross_cents": net + gst,
        }
        if (period := self._academic_period(config)) != "year_1":
            result["tuition"]["academic_period"] = period
        if self.formal:
            result.update(school_commission_formal=True,school_commission_simulation=self.simulation)
        if configurable:
            result.update(school_commission_configurable=True,claim=config["claim"])
        elif config.get("claim", {}).get("commencement_days"):
            result["claim"] = config["claim"]
        return {**result, "digest": hashlib.sha256(canonical_json(result).encode()).hexdigest()}

    def _toorak_tier(self, config, component, cycle, day, confirm_other):
        visa = self.facts.toorak_granted_visa(cycle.id, cycle.student_id, day)
        subclass = visa["subclass"]
        if subclass == "188":
            category = "subclass_188"
        elif subclass == "500":
            category = "full_student"
        elif confirm_other:
            category = "other_resident_confirmed"
        else:
            raise ValueError("签证类别无法匹配，请按签证原件复核。")
        matches = [tier for tier in config["tiers"] if category in tier["visa_categories"]]
        if len(matches) != 1:
            raise ValueError("签证必须唯一命中一条合同规则。")
        tier = matches[0]
        tuition = self.connection.execute(
            """SELECT t.uid,t.tuition_cents,t.fee_component,t.authority_level,t.evidence_document_id,
                      d.uid AS document_uid,d.sha256
                 FROM revenue_tuition_facts t JOIN revenue_documents d ON d.id=t.evidence_document_id
                WHERE t.enrolment_component_id=? AND t.academic_period=?
                  AND t.fee_component=? AND t.status='active' AND t.currency_code='AUD'
                  AND t.authority_level IN ('official','manual') AND t.tuition_cents>0""",
            (component["id"], self._academic_period(config), "tuition_"+tier["tuition_kind"]),
        ).fetchone()
        if not tuition:
            raise ValueError("缺少与签证档对应、有来源的已确认首年 AUD 学费。")
        return tier,tuition,{**visa,"category":category}

    @staticmethod
    def _academic_period(config):
        return {"first_half_year": "first_half_year", "whole_course": "whole_course"}.get(
            config.get("tuition_period"), "year_1"
        )

    def _configurable_tier(self, config, component_id, cycle, day):
        fees = self.connection.execute(
            """SELECT t.uid,t.tuition_cents,t.fee_component,t.authority_level,t.evidence_document_id,
                      d.uid AS document_uid,d.sha256
                 FROM revenue_tuition_facts t JOIN revenue_documents d ON d.id=t.evidence_document_id
                WHERE t.enrolment_component_id=? AND t.academic_period=?
                  AND t.fee_component IN ('tuition_local','tuition_international')
                  AND t.status='active' AND t.currency_code='AUD'
                  AND t.authority_level IN ('official','manual') AND t.tuition_cents>0""",
            (component_id, self._academic_period(config)),
        ).fetchall()
        candidates = [
            (tier,fee) for tier in config["tiers"] for fee in fees
            if tier["tuition_kind"]=="any" or fee["fee_component"]=="tuition_"+tier["tuition_kind"]
        ]
        visa = {}
        if any(tier["visa_subclasses"] for tier,_ in candidates):
            visa = self.facts.toorak_granted_visa(cycle.id,cycle.student_id,day)
        matching = [(tier,fee) for tier,fee in candidates
                    if not tier["visa_subclasses"] or visa["subclass"] in tier["visa_subclasses"]]
        if len(matching)!=1:
            raise ValueError("须有唯一适用的佣金规则及有凭证的首年 AUD 学费。")
        tier,fee = matching[0]
        return {**tier,"tuition_kind":fee["fee_component"].removeprefix("tuition_"),
                "source":config["source"]["clause_reference"]},fee,visa

    def save(self, case_uid, component_uid, confirm_other, digest):
        payload = {"case_uid": case_uid, "component_uid": component_uid,
                   "confirm_other": confirm_other, "preview_digest": digest}

        def operation():
            result = self.preview(case_uid, component_uid, confirm_other)
            if result["digest"] != digest:
                raise ValueError("业务来源已变更，请重新预览。")
            if self.connection.execute(
                "SELECT 1 FROM revenue_commission_lines WHERE enrolment_component_id=?",
                (result["component_id"],),
            ).fetchone():
                raise ValueError("该入学项目已有佣金明细，请人工复核。")
            rule = result["rule"]
            snapshot = self.service._rule_snapshot("commission", rule["rule_uid"], result, self.actor)
            uid = new_uid("commission-line")
            now = self.repository.now()
            amounts = result["net_cents"], result["gst_cents"], result["gross_cents"]
            line_id, = self.connection.execute(
                """INSERT INTO revenue_commission_lines
                   (uid,commercial_case_id,enrolment_component_id,revenue_entity_id,
                    commission_rule_version_id,rule_snapshot_id,commissionable_cents,
                    net_cents,gst_cents,gross_cents,currency_code,status,calculated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'AUD','forecast',?) RETURNING id""",
                (uid,result["case_id"],result["component_id"],rule["entity_id"],rule["rule_id"],
                 snapshot["id"],result["tuition"]["tuition_cents"],*amounts,now),
            ).fetchone()
            self.connection.execute(
                """INSERT INTO revenue_commission_instalments
                   (uid,commission_line_id,instalment_no,academic_period,net_cents,gst_cents,gross_cents,
                    eligible_cents,claimed_cents,status,created_at)
                   VALUES(?,?,1,?,?,?,?,0,0,'forecast',?)""",
                (new_uid("commission-instalment"),line_id,result["tuition"].get("academic_period", "year_1"),*amounts,now),
            )
            return self.repository.require_by_uid("revenue_commission_lines", uid)

        return self.repository.command(
            idempotency_key=f"toorak-forecast:{component_uid}" if not self.formal else f"school-commission-forecast:{component_uid}",
            command_type="toorak_forecast_saved", aggregate_type="commission_line",
            payload=payload, actor_user_id=self.actor, operation=operation,
        )

"""School commission contract V1 configuration and read-only rule preview."""

from datetime import date
import hashlib
import json
import re

from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
from revenue.money import apply_basis_points, gst_from_inclusive
from revenue.repository import canonical_json, new_uid
from revenue.rules import CommissionRuleEvaluator


from revenue.school_commission_pilot import FORMAL_CONTRACT


class SchoolCommissionContracts:
    def __init__(self, service, actor_user_id, actor_role, *, formal=False, school_id=None, entity_id=None, simulation=True):
        self.service = service
        self.repository = service.repository
        self.connection = service.connection
        self.actor_user_id = actor_user_id
        self.actor_role = actor_role
        self.formal = formal
        self.school_id = school_id
        self.entity_id = entity_id
        self.simulation = simulation

    @property
    def contract_kind(self):
        return FORMAL_CONTRACT if self.formal else "school_commission_contract"

    def _require_admin(self):
        if self.actor_role != "Admin":
            raise PermissionError("School commission contracts require Admin")

    def _school(self, school_id):
        school = self.connection.execute(
            "SELECT * FROM schools WHERE id=?", (school_id,)
        ).fetchone()
        if school is None or dict(school).get("deleted_at"):
            raise ValueError("请选择仍有效的学校。")
        return school

    def _bindings(self, school_id, effective_from):
        return self.connection.execute(
            """SELECT e.*,l.effective_from AS binding_effective_from,l.effective_to AS binding_effective_to
                 FROM revenue_entity_provider_links l
                 JOIN revenue_entities e ON e.id=l.revenue_entity_id
                WHERE l.school_id=? AND (l.effective_to IS NULL OR l.effective_to>=?)""",
            (school_id, effective_from),
        ).fetchall()

    def bind(self, school_id, legal_name, effective_from, key):
        self._require_admin()
        if len(legal_name) > 200:
            raise ValueError("佣金主体名称不能超过 200 个字符。")
        effective_from = date.fromisoformat(effective_from).isoformat()
        payload = dict(school_id=school_id, legal_name=legal_name, effective_from=effective_from)

        def operation():
            self._school(school_id)
            bindings = self._bindings(school_id, effective_from)
            if bindings:
                existing = dict(bindings[0])
                if (len(bindings) != 1 or existing["legal_name"].casefold() != legal_name.casefold()
                        or existing["entity_type"] != "school" or existing["status"] != "active"
                        or existing["binding_effective_from"] != effective_from
                        or existing["binding_effective_to"] is not None):
                    raise ValueError("该学校已有重叠或冲突的佣金主体绑定。")
                return self.repository.require_by_uid("revenue_entities", existing["uid"])

            candidates = self.connection.execute(
                "SELECT * FROM revenue_entities WHERE entity_type='school' AND lower(legal_name)=lower(?)",
                (legal_name,),
            ).fetchall()
            if len(candidates) > 1:
                raise ValueError("存在多个同名主体，请先确认正确主体。")
            if candidates:
                entity = dict(candidates[0])
                if entity["status"] != "active" or self.connection.execute(
                    "SELECT 1 FROM revenue_entity_provider_links WHERE revenue_entity_id=?",
                    (entity["id"],),
                ).fetchone():
                    raise ValueError("同名主体已绑定其他学校或已停用。")
            else:
                entity = self.service.create_revenue_entity(
                    "school", legal_name, self.actor_user_id, f"{key}:entity"
                )
            self.connection.execute(
                """INSERT INTO revenue_entity_provider_links
                   (uid,revenue_entity_id,school_id,effective_from,created_at)
                   VALUES(?,?,?,?,?)""",
                (new_uid("school-link"), entity["id"], school_id,
                 effective_from, self.repository.now()),
            )
            return entity

        return self.repository.command(
            idempotency_key=key, command_type="school_commission_entity_bound",
            aggregate_type="revenue_entity", payload=payload,
            actor_user_id=self.actor_user_id, operation=operation,
        )

    def _entity(self, school_id, effective_from):
        self._school(school_id)
        bindings = self._bindings(school_id, effective_from)
        if len(bindings) != 1:
            raise ValueError("学校须有唯一有效的佣金主体绑定。")
        entity = bindings[0]
        if (entity["entity_type"] != "school" or entity["status"] != "active"
                or entity["binding_effective_to"] is not None
                or entity["binding_effective_from"] > effective_from):
            raise ValueError("合同生效日须位于学校佣金主体绑定的有效期内。")
        return entity

    def upload(self, school_id, effective_from, effective_to, reference_code, staged, key):
        self._require_admin()
        effective_from = date.fromisoformat(effective_from).isoformat()
        effective_to = date.fromisoformat(effective_to).isoformat()
        if reference_code and len(reference_code) > 100:
            raise ValueError("合同参考编号不能超过 100 个字符。")
        if effective_to < effective_from:
            raise ValueError("到期日不得早于生效日。")
        payload = dict(school_id=school_id, effective_from=effective_from,
                       effective_to=effective_to, reference_code=reference_code, sha256=staged.sha256)

        def operation():
            entity = self._entity(school_id, effective_from)
            if self.connection.execute(
                """SELECT 1 FROM revenue_contract_groups g
                     JOIN revenue_contract_versions v ON v.contract_group_id=g.id
                     JOIN revenue_documents d ON d.id=v.document_id
                    WHERE g.revenue_entity_id=? AND g.contract_kind='direct_commission'
                      AND d.document_kind='school_commission_contract'""",
                (entity["id"],),
            ).fetchone():
                raise ValueError("该学校已登记直佣合同 V1。")
            document = self.service.register_document(
                "school_commission_contract", str(staged.path), staged.sha256, staged.mime_type,
                staged.size_bytes, "upload", self.actor_user_id, f"{key}:document",
            )
            contract = self.service.create_contract_version(
                entity["uid"], document["uid"], "direct_commission", effective_from,
                self.actor_user_id, self.actor_role, f"{key}:version",
                reference_code=reference_code, effective_to=effective_to,
            )
            return {"uid": contract["uid"], "contract": contract, "document": document}

        return self.repository.command(
            idempotency_key=key, command_type="school_commission_contract_uploaded",
            aggregate_type="contract_version", payload=payload,
            actor_user_id=self.actor_user_id, operation=operation,
        )

    @staticmethod
    def _hundredths(value):
        if not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", value):
            raise ValueError("请输入最多两位小数的有效数字。")
        whole, _, fraction = value.partition(".")
        return int(whole) * 100 + int(fraction.ljust(2, "0"))

    def preview(self, school_id, contract_uid, *, sample_tuition, rate_percent,
                gst_mode, commencement_days, required_coe, clause_reference, evidence_text):
        self._require_admin()
        contract = self.repository.require_by_uid("revenue_contract_versions", contract_uid)
        entity = self._entity(school_id, contract["effective_from"])
        source_document = self.connection.execute(
            """SELECT g.reference_code,d.uid AS document_uid FROM revenue_contract_groups g
                 JOIN revenue_documents d ON d.id=?
                WHERE g.id=? AND g.revenue_entity_id=? AND g.contract_kind='direct_commission'
                  AND d.document_kind='school_commission_contract'""",
            (contract["document_id"], contract["contract_group_id"], entity["id"]),
        ).fetchone()
        if source_document is None or contract["version_no"] != 1 or not contract["effective_to"]:
            raise ValueError("请选择该学校的佣金合同 V1。")
        if not contract["effective_from"] <= date.today().isoformat() <= contract["effective_to"]:
            raise ValueError("合同当前不在有效期内。")
        if len(clause_reference) > 100 or len(evidence_text) > 2000:
            raise ValueError("条款定位或证据说明过长。")
        rate = self._hundredths(rate_percent)
        tuition = self._hundredths(sample_tuition)
        if not 0 < rate <= 10000 or tuition <= 0:
            raise ValueError("佣金比例须大于 0% 且不超过 100%，样本学费须大于零。")
        if gst_mode != "exclusive" or not 0 <= commencement_days <= 365:
            raise ValueError("首批支持 GST 另加 10% 及开学后 0 至 365 天。")
        terms = [{"fact": "days_since_commencement", "operator": "gte", "value": commencement_days}]
        if required_coe:
            terms.append({"fact": "coe_issued", "operator": "eq", "value": True})
        configuration = {
            "school_contract_v1": True,
            "fee_components": ["tuition"],
            "scopes": [{"scope_kind": "school", "scope_value": str(school_id), "priority": 100}],
            "instalments": [{"instalment_no": 1, "academic_period": "single",
                             "rate_basis_points": rate, "gst_basis_points": 1000}],
            "claim_trigger": {"operator": "and", "terms": terms},
            "document_requirements": ["coe"] if required_coe else [],
            "source": {"contract_version_uid": contract_uid, "clause_reference": clause_reference,
                       "evidence_text": evidence_text},
        }
        digest = hashlib.sha256(canonical_json({
            "configuration": configuration, "sample_tuition_cents": tuition,
            "effective_from": contract["effective_from"], "effective_to": contract["effective_to"],
        }).encode()).hexdigest()
        amount = CommissionRuleEvaluator().calculate(tuition, configuration["instalments"])[0]
        school = dict(self._school(school_id))
        return {
            **amount, "tuition_cents": tuition, "configuration": configuration, "digest": digest,
            "contract": {
                **dict(source_document), "school_name": school.get("school_name", str(school_id)),
                "legal_name": entity["legal_name"], "effective_from": contract["effective_from"],
                "effective_to": contract["effective_to"],
            },
        }

    def activate(self, school_id, contract_uid, rule_inputs, expected_digest, key):
        self._require_admin()
        payload = dict(school_id=school_id, contract_version_uid=contract_uid,
                       rule_inputs=rule_inputs, preview_digest=expected_digest)

        def operation():
            preview = self.preview(school_id, contract_uid, **rule_inputs)
            if preview["digest"] != expected_digest:
                raise ValueError("规则或样本已变更，请重新预览后确认。")
            configuration = preview["configuration"]
            contract = self.repository.require_by_uid("revenue_contract_versions", contract_uid)
            if contract["status"] != "draft":
                raise ValueError("只可激活尚未审核的学校佣金合同 V1。")
            if self.connection.execute(
                "SELECT 1 FROM revenue_commission_rule_versions WHERE contract_version_id=?",
                (contract["id"],),
            ).fetchone():
                raise ValueError("该合同已有关联规则。")
            term = configuration["instalments"][0]
            source = configuration["source"]
            rate = str(term["rate_basis_points"])
            clause = source["clause_reference"]
            page = int(clause) if re.fullmatch(r"[1-9][0-9]{0,5}", clause) else None
            provider = ManualContractSuggestionProvider([
                ContractSuggestion("commission_rate_basis_points", rate, page, clause,
                                   source["evidence_text"], None),
            ])
            args = self.actor_user_id, self.actor_role
            extraction = self.service.create_parse_run(
                contract_uid, provider, *args, f"{key}:manual"
            )["extractions"][0]
            self.service.review_extraction(
                extraction["uid"], rate, True, *args, f"{key}:review"
            )
            self.service.activate_contract_version(contract_uid, *args, f"{key}:contract")
            return self.service.activate_commission_rule(
                contract_uid, configuration, *args, f"{key}:rule"
            )

        return self.repository.command(
            idempotency_key=key, command_type="school_commission_contract_and_rule_activated",
            aggregate_type="commission_rule", payload=payload,
            actor_user_id=self.actor_user_id, operation=operation,
        )

    def _toorak_school(self, school_id):
        if self.formal and self.school_id is not None and school_id != self.school_id:
            raise ValueError("该学校不在 Toorak 试点范围。")
        school = self._school(school_id)
        if " ".join(dict(school).get("school_name", "").casefold().split()) != "toorak college":
            raise ValueError("请选择 Toorak College。")
        return school

    def upload_toorak_draft(self, school_id, staged, key, *, signed_confirmed=False):
        self._require_admin()
        if self.formal and not signed_confirmed:
            raise ValueError("请核对并确认上传的是已签署合同。")
        payload = {"school_id": school_id, "sha256": staged.sha256}
        if self.formal:
            payload.update(signed_confirmed=True,pilot_entity_id=self.entity_id,pilot_simulation=self.simulation)

        def operation():
            self._toorak_school(school_id)
            document = self.service.register_document(
                self.contract_kind, str(staged.path), staged.sha256, staged.mime_type,
                staged.size_bytes, "upload", self.actor_user_id, f"{key}:document",
            )
            configuration = {
                "toorak_draft": True,
                "school_id": school_id, "document_uid": document["uid"],
                "effective_from": None, "effective_to": None, "gst_mode": None,
                "term_years": 2, "review_after_months": 12,
                "tiers": [
                    {"tuition_kind": "local", "visa_categories": ["subclass_188", "other_resident_confirmed"],
                     "rate_basis_points": 500, "source": "第 7 页 §7(a)(i)"},
                    {"tuition_kind": "international", "visa_categories": ["full_student"],
                     "rate_basis_points": 1700, "source": "第 7 页 §7(a)(ii)"},
                ],
                "claim": {
                    "conditions": ["agent_recruited", "agent_named_application", "enrolled",
                                   "course_fee_paid_to_school", "commenced", "approved_invoice"],
                    "direct_applications_excluded": True, "payment_days_from_valid_invoice": 30,
                    "source": "第 7 页 §7(b)–(f)",
                },
            }
            if self.formal:
                configuration.update(signed_confirmed=True,pilot_entity_id=self.entity_id,pilot_simulation=self.simulation)
            return self.service._rule_snapshot("commission", document["uid"], configuration, self.actor_user_id)

        return self.repository.command(
            idempotency_key=key, command_type="toorak_commission_draft_created",
            aggregate_type="rule_snapshot", payload=payload,
            actor_user_id=self.actor_user_id, operation=operation,
        )

    def preview_toorak(self, draft_uid, inputs):
        self._require_admin()
        draft = self.repository.require_by_uid("revenue_rule_snapshots", draft_uid)
        configuration = json.loads(draft["canonical_json"])
        if (draft["rule_kind"] != "commission" or configuration.get("toorak_draft") is not True
                or configuration.get("school_commission_configurable") is True):
            raise ValueError("请选择 Toorak College 的佣金草稿。")
        school = self._toorak_school(configuration["school_id"])
        document = self.repository.require_by_uid("revenue_documents", draft["source_rule_uid"])
        if (document["document_kind"] != self.contract_kind
                or (self.formal and (configuration.get("signed_confirmed") is not True
                                    or configuration.get("pilot_entity_id")!=self.entity_id
                                    or configuration.get("pilot_simulation") is not self.simulation))):
            raise ValueError("佣金草稿缺少学校合同原件。")
        start, end = (date.fromisoformat(inputs[key]).isoformat() if inputs[key] else None
                      for key in ("effective_from", "effective_to"))
        if start and end and start > end:
            raise ValueError("到期日不得早于生效日。")
        gst_mode = inputs["gst_mode"] or None
        if gst_mode not in (None, "none", "exclusive", "inclusive"):
            raise ValueError("请选择有效的 GST 处理方式。")
        rows = []
        for tier in configuration["tiers"]:
            kind = tier["tuition_kind"]
            tuition = self._hundredths(inputs[f"{kind}_tuition"])
            if tuition <= 0:
                raise ValueError("首年学费样本须大于零。")
            visa = inputs[f"{kind}_visa"]
            matched = visa in tier["visa_categories"]
            amount = apply_basis_points(tuition, tier["rate_basis_points"]) if matched else None
            tax = total = None
            if matched and gst_mode:
                if gst_mode == "inclusive":
                    _, tax = gst_from_inclusive(amount, 1000)
                    total = amount
                else:
                    tax = apply_basis_points(amount, 1000) if gst_mode == "exclusive" else 0
                    total = amount + tax
            rows.append({**tier, "visa": visa, "tuition_cents": tuition,
                         "commission_cents": amount, "gst_cents": tax, "total_cents": total})
        digest = hashlib.sha256(canonical_json({
            "draft_uid": draft_uid, "inputs": inputs, "document_sha256": document["sha256"],
        }).encode()).hexdigest()
        return {
            "configuration": configuration, "rows": rows, "digest": digest, "formal":self.formal,
            "school_name": school["school_name"], "document_uid": document["uid"],
            "effective_from": start, "effective_to": end, "gst_mode": gst_mode,
            "can_activate": bool(start and end and start <= date.today().isoformat() <= end
                                 and gst_mode and all(row["commission_cents"] is not None for row in rows)),
        }

    def activate_toorak(self, draft_uid, inputs, digest, key):
        self._require_admin()
        payload = dict(draft_uid=draft_uid, inputs=inputs, preview_digest=digest)

        def operation():
            preview = self.preview_toorak(draft_uid, inputs)
            if preview["digest"] != digest:
                raise ValueError("规则或样本已变更，请重新预览。")
            if not preview["can_activate"]:
                raise ValueError("须确认 GST、有效期和两档预览后才能整套激活。")
            config = dict(preview["configuration"])
            config.pop("toorak_draft")
            start, end = preview["effective_from"], preview["effective_to"]
            school_id = config["school_id"]
            args = self.actor_user_id, self.actor_role
            if not self.formal and not self._bindings(school_id, start):
                self.bind(school_id, "Toorak College", start, f"{key}:bind")
            entity = self._entity(school_id, start)
            if (entity["legal_name"].strip().casefold() != "toorak college"
                    or (self.formal and entity["id"]!=self.entity_id)):
                raise ValueError("请核对 Toorak College 的签约及付款主体。")
            if self.connection.execute(
                """SELECT 1 FROM revenue_contract_versions v
                     JOIN revenue_contract_groups g ON g.id=v.contract_group_id
                     JOIN revenue_documents d ON d.id=v.document_id
                    WHERE g.revenue_entity_id=? AND d.document_kind=?""",
                (entity["id"],self.contract_kind),
            ).fetchone():
                raise ValueError("该学校已有同类已登记的佣金合同。")
            contract = self.service.create_contract_version(
                entity["uid"], preview["document_uid"], "direct_commission", start,
                *args, f"{key}:version", effective_to=end, school_commission_formal=self.formal,
            )
            uid = contract["uid"]
            config.update(school_contract_v1=True, toorak_tier_package=True,
                          effective_from=start, effective_to=end, gst_mode=inputs["gst_mode"],
                          scopes=[{"scope_kind": "school", "scope_value": str(school_id)}])
            suggestions = [
                ContractSuggestion(f'{tier["tuition_kind"]}_commission_rate_basis_points',
                                   str(tier["rate_basis_points"]), 7, tier["source"],
                                   "Toorak College 首年学费佣金条款", None)
                for tier in config["tiers"]
            ]
            extractions = self.service.create_parse_run(
                uid, ManualContractSuggestionProvider(suggestions), *args, f"{key}:manual"
            )["extractions"]
            for extraction in extractions:
                self.service.review_extraction(
                    extraction["uid"], extraction["suggested_value"], True,
                    *args, f'{key}:review:{extraction["uid"]}',
                )
            self.service.activate_contract_version(uid, *args, f"{key}:contract", school_commission_formal=self.formal)
            return self.service.activate_commission_rule(uid, config, *args, f"{key}:rule", school_commission_formal=self.formal)

        return self.repository.command(
            idempotency_key=key, command_type="toorak_commission_package_activated",
            aggregate_type="commission_rule", payload=payload,
            actor_user_id=self.actor_user_id, operation=operation,
        )

    def _configurable_school(self, school_id):
        self._require_admin()
        if not self.formal or not self.entity_id or school_id != self.school_id:
            raise ValueError("请选择授权范围内的学校及付款主体。")
        return self._school(school_id)

    def upload_configurable_draft(self, school_id, staged, key, *, signed_confirmed=False):
        self._configurable_school(school_id)
        if signed_confirmed is not True or staged.mime_type != "application/pdf":
            raise ValueError("请核对并上传已签署的 PDF 合同。")
        configuration = {
            "toorak_draft": True,
            "school_commission_configurable": True,
            "school_id": school_id,
            "pilot_entity_id": self.entity_id,
            "pilot_simulation": self.simulation,
            "signed_confirmed": True,
        }
        payload = {**configuration, "sha256": staged.sha256}

        def operation():
            if not self.connection.execute(
                """SELECT 1 FROM revenue_entity_provider_links l
                     JOIN revenue_entities e ON e.id=l.revenue_entity_id
                    WHERE l.school_id=? AND e.id=? AND e.status='active'
                      AND e.entity_type='school' AND l.effective_to IS NULL""",
                (school_id,self.entity_id),
            ).fetchone():
                raise ValueError("学校尚未绑定有效付款主体。")
            if self.connection.execute(
                "SELECT 1 FROM revenue_documents WHERE document_kind=? AND storage_path=?",
                (FORMAL_CONTRACT,str(staged.path)),
            ).fetchone():
                raise ValueError("这份签署合同已上传，请打开原有草稿或合同。")
            document = self.service.register_document(
                FORMAL_CONTRACT,str(staged.path),staged.sha256,staged.mime_type,
                staged.size_bytes,"upload",self.actor_user_id,f"{key}:document",
            )
            return self.service._rule_snapshot(
                "commission",document["uid"],{**configuration,"document_uid":document["uid"]},self.actor_user_id,
            )

        return self.repository.command(
            idempotency_key=key,command_type="school_commission_configurable_draft_created",
            aggregate_type="rule_snapshot",payload=payload,actor_user_id=self.actor_user_id,operation=operation,
        )

    def _configurable_tiers(self, inputs):
        raw_tiers = inputs.get("tiers")
        if raw_tiers is None:
            raw_tiers = [{"rate_percent":inputs.get("rate_percent",""),"tuition_kind":"any","visa_subclasses":[]}]
        if not isinstance(raw_tiers,list) or len(raw_tiers) not in (1,2):
            raise ValueError("请选择一档或两档佣金规则。")
        tiers = []
        for raw in raw_tiers:
            if not isinstance(raw,dict):
                raise ValueError("佣金档位格式无效。")
            kind = raw.get("tuition_kind")
            visas = raw.get("visa_subclasses",[])
            rate = self._hundredths(str(raw.get("rate_percent","")))
            if (kind not in ("any","local","international") or not 0 < rate <= 10000
                    or not isinstance(visas,list) or len(visas) > 20
                    or any(not re.fullmatch(r"[0-9]{3}",str(visa)) for visa in visas)):
                raise ValueError("请核对学费档位、佣金比例和三位数签证类别。")
            tiers.append({"tuition_kind":kind,"rate_basis_points":rate,
                          "visa_subclasses":sorted(set(map(str,visas)))})
        if len(tiers) == 2:
            first, second = tiers
            kinds = {first["tuition_kind"],second["tuition_kind"]}
            visas = first["visa_subclasses"],second["visa_subclasses"]
            if ((len(kinds)==1 or "any" in kinds)
                    and (not all(visas) or set(visas[0]).intersection(visas[1]))):
                raise ValueError("两档佣金适用条件重叠，请调整学费类型或签证类别。")
        return tiers

    def preview_configurable(self, draft_uid, inputs):
        self._require_admin()
        draft = self.repository.require_by_uid("revenue_rule_snapshots",draft_uid)
        original = json.loads(draft["canonical_json"])
        if (draft["rule_kind"]!="commission" or original.get("toorak_draft") is not True
                or original.get("school_commission_configurable") is not True
                or original.get("signed_confirmed") is not True
                or original.get("pilot_entity_id")!=self.entity_id
                or original.get("pilot_simulation") is not self.simulation):
            raise ValueError("请选择该付款主体的已签署合同草稿。")
        school = self._configurable_school(original["school_id"])
        document = self.repository.require_by_uid("revenue_documents",draft["source_rule_uid"])
        if document["document_kind"]!=FORMAL_CONTRACT:
            raise ValueError("合同草稿缺少已签署 PDF。")
        start,end = (date.fromisoformat(str(inputs.get(field,""))).isoformat()
                     for field in ("effective_from","effective_to"))
        if start > end or end < date.today().isoformat():
            raise ValueError("请核对合同生效日及到期日。")
        entity = self._entity(school["id"],start)
        if entity["id"]!=self.entity_id:
            raise ValueError("该合同有效期内的付款主体已变更。")
        gst_mode = inputs.get("gst_mode")
        clause = str(inputs.get("clause_reference") or "").strip()
        tuition = self._hundredths(str(inputs.get("sample_tuition","")))
        commencement = str(inputs.get("commencement_days",""))
        payment = str(inputs.get("payment_days",""))
        registration = inputs.get("registration_required")
        if (gst_mode not in ("none","exclusive","inclusive") or not 1<=len(clause)<=100
                or tuition<=0 or not re.fullmatch(r"[0-9]{1,3}",commencement)
                or int(commencement)>365 or payment not in ("0","7","15","30")
                or (type(registration) is not bool and registration not in ("0","1"))):
            raise ValueError("请确认 GST、条款出处、样本学费、申领条件和付款期限。")
        tiers = self._configurable_tiers(inputs)
        configuration = {
            **original,
            "school_contract_v1":True,
            "toorak_tier_package":True,
            "effective_from":start,"effective_to":end,"gst_mode":gst_mode,
            "fee_components":["tuition"],
            "scopes":[{"scope_kind":"school","scope_value":str(school["id"])}],
            "tiers":tiers,
            "claim":{
                "payment_days_from_valid_invoice":int(payment),
                "commencement_days":int(commencement),
                "registration_required":registration is True or registration=="1",
            },
            "source":{"clause_reference":clause},
        }
        rows = []
        for tier in tiers:
            amount = apply_basis_points(tuition,tier["rate_basis_points"])
            if gst_mode=="inclusive":
                net,tax = gst_from_inclusive(amount,1000)
                total = amount
            else:
                net = amount
                tax = apply_basis_points(amount,1000) if gst_mode=="exclusive" else 0
                total = net+tax
            rows.append({**tier,"tuition_cents":tuition,"commission_cents":amount,
                         "net_cents":net,"gst_cents":tax,"total_cents":total})
        digest = hashlib.sha256(canonical_json({
            "draft_uid":draft_uid,"configuration":configuration,"sample_tuition_cents":tuition,
            "document_sha256":document["sha256"],"payer_name":entity["legal_name"],
        }).encode()).hexdigest()
        return {
            "configuration":configuration,"rows":rows,"digest":digest,"formal":True,
            "school_name":school["school_name"],"legal_name":entity["legal_name"],"document_uid":document["uid"],
            "effective_from":start,"effective_to":end,"gst_mode":gst_mode,"can_activate":True,
        }

    def activate_configurable(self, draft_uid, inputs, digest, key):
        self._require_admin()
        payload = dict(draft_uid=draft_uid,inputs=inputs,preview_digest=digest)

        def operation():
            preview = self.preview_configurable(draft_uid,inputs)
            if preview["digest"]!=digest:
                raise ValueError("规则、样本或付款主体已变更，请重新预览后确认。")
            configuration = dict(preview["configuration"])
            configuration.pop("toorak_draft")
            start,end = preview["effective_from"],preview["effective_to"]
            entity = self._entity(configuration["school_id"],start)
            if self.connection.execute(
                """SELECT 1 FROM revenue_contract_versions v
                     JOIN revenue_contract_groups g ON g.id=v.contract_group_id
                     JOIN revenue_documents d ON d.id=v.document_id
                    WHERE d.uid=? OR (g.revenue_entity_id=? AND g.contract_kind='direct_commission'
                         AND d.document_kind=? AND v.status='active' AND v.effective_from<=?
                         AND (v.effective_to IS NULL OR v.effective_to>=?))""",
                (preview["document_uid"],entity["id"],FORMAL_CONTRACT,end,start),
            ).fetchone():
                raise ValueError("合同已登记或与该学校的有效合同日期重叠。")
            args = self.actor_user_id,self.actor_role
            contract = self.service.create_contract_version(
                entity["uid"],preview["document_uid"],"direct_commission",start,
                *args,f"{key}:version",effective_to=end,school_commission_formal=True,
            )
            uid = contract["uid"]
            clause = configuration["source"]["clause_reference"]
            suggestions = [
                ContractSuggestion(f"tier_{index}_commission_rate_basis_points",str(tier["rate_basis_points"]),
                                   None,clause,"已核对签署合同中的首年学费佣金条款",None)
                for index,tier in enumerate(configuration["tiers"],1)
            ]
            extractions = self.service.create_parse_run(
                uid,ManualContractSuggestionProvider(suggestions),*args,f"{key}:manual",
            )["extractions"]
            for extraction in extractions:
                self.service.review_extraction(
                    extraction["uid"],extraction["suggested_value"],True,*args,f'{key}:review:{extraction["uid"]}',
                )
            self.service.activate_contract_version(uid,*args,f"{key}:contract",school_commission_formal=True)
            return self.service.activate_commission_rule(
                uid,configuration,*args,f"{key}:rule",school_commission_formal=True,
            )

        return self.repository.command(
            idempotency_key=key,command_type="school_commission_configurable_package_activated",
            aggregate_type="commission_rule",payload=payload,actor_user_id=self.actor_user_id,operation=operation,
        )

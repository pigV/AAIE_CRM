"""School commission claims, invoice previews, and signed invoices."""

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from revenue.pdf import render_configured_commission_invoice, render_toorak_invoice_pdf
from revenue.invoice_templates import (
    FORMAL_INVOICE_KIND, TEST_INVOICE_KIND, current_invoice_template_revisions, default_layout,
    financial_profile, invoice_origin_predicate, render_with_active_template,
    school_test_billing_address,
)
from revenue.money import apply_basis_points, commission_gst_amounts
from revenue.repository import canonical_json, new_uid
from revenue.secure_storage import StorageUnavailable, read_secure, write_immutable
from revenue.data_invoice_storage import configured_invoice_store
from revenue.school_invoice_mail import enqueue_mail
from revenue.toorak_forecast import ToorakForecast


EVIDENCE_FIELDS = (
    ("agent_document_id", "ToorakAgentNamedApplication", "代理招募及署名申请"),
    ("enrolment_document_id", "ToorakEnrolmentConfirmation", "学校注册确认"),
    ("payment_document_id", "ToorakSchoolTuitionPaid", "学校课程费实收证据"),
)


EVIDENCE_REGISTERED = "school_commission_evidence_registered"
FORMAL_CLAIM_EVIDENCE_KIND = "school_commission_claim_evidence"


def claim_evidence_fields(forecast):
    if forecast.get("school_commission_configurable") is True:
        return tuple(entry for entry in EVIDENCE_FIELDS
                     if entry[0]=="enrolment_document_id" and forecast["claim"]["registration_required"])
    return EVIDENCE_FIELDS


def formal_evidence_options(connection, case_id, student_id, entity_id, kind, simulation):
    return [dict(row) for row in connection.execute(
        """SELECT d.uid AS id, max(json_extract(a.after_json,'$.filename')) AS original_name
             FROM revenue_audit_events a JOIN revenue_documents d ON d.uid=a.aggregate_uid
             JOIN revenue_commercial_cases c ON c.uid=json_extract(a.after_json,'$.case_uid')
            WHERE a.action=? AND d.document_kind=? AND c.id=?
              AND json_extract(a.after_json,'$.student_id')=?
              AND json_extract(a.after_json,'$.entity_id')=?
              AND json_extract(a.after_json,'$.evidence_kind')=?
              AND json_extract(a.after_json,'$.simulation')=?
            GROUP BY d.id ORDER BY max(a.id) DESC""",
        (EVIDENCE_REGISTERED,FORMAL_CLAIM_EVIDENCE_KIND,case_id,student_id,entity_id,kind,simulation),
    )]


def _today():
    return datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()


class ToorakClaims:
    def __init__(self, service, actor, role, storage_root, *,
                 formal=False, simulation=True, school_id=None, entity_id=None, mail_release=None):
        if role != "Admin":
            raise PermissionError("School commission claims require Admin")
        self.service = service
        self.repository = service.repository
        self.connection = service.connection
        self.actor = actor
        self._storage_root = storage_root
        self.formal = formal
        self.mail_release = mail_release
        self.simulation = simulation
        self.entity_id = entity_id
        self.invoice_kind = FORMAL_INVOICE_KIND if formal else TEST_INVOICE_KIND
        self.workflow = "school-commission" if formal else "toorak"
        self.claim_action = "school_commission_claim_issued" if formal else "toorak_internal_claim_issued"
        self.invoice_action = "school_commission_invoice_issued" if formal else "toorak_test_invoice_issued"
        self.evidence_kind = FORMAL_CLAIM_EVIDENCE_KIND if formal else "toorak_claim_evidence"
        self.forecast = ToorakForecast(service,actor,role,formal=formal,school_id=school_id,entity_id=entity_id,
                                       simulation=simulation)

    @property
    def storage_root(self):
        root = self._storage_root
        return Path(root() if callable(root) else root)

    def register_evidence(self, case_uid, field, staged, key):
        allowed = {name:kind for name,kind,_ in EVIDENCE_FIELDS}
        if not self.formal or self.entity_id is None or field not in allowed:
            raise ValueError("请选择正式学校佣金申请及有效凭证类别。")
        if not key or staged.mime_type!="application/pdf":
            raise ValueError("请上传有效 PDF 凭证并刷新操作表单。")
        kind = allowed[field]
        payload = {"case_uid":case_uid,"entity_id":self.entity_id,"evidence_kind":kind,
                   "filename":staged.original_filename,"sha256":staged.sha256,"simulation":self.simulation}

        def operation():
            case = self.connection.execute(
                """SELECT c.student_id,s.canonical_json FROM revenue_commercial_cases c
                     JOIN revenue_commission_lines l ON l.commercial_case_id=c.id
                     JOIN revenue_rule_snapshots s ON s.id=l.rule_snapshot_id
                    WHERE c.uid=? AND l.revenue_entity_id=? AND l.status='forecast'
                      AND json_extract(s.canonical_json,'$.toorak_forecast')=1
                      AND json_extract(s.canonical_json,'$.school_commission_formal')=1
                      AND json_extract(s.canonical_json,'$.school_commission_simulation')=?
                    """,(case_uid,self.entity_id,self.simulation),
            ).fetchall()
            if not case:
                raise ValueError("凭证须关联本付款主体下已保存的正式预计佣金。")
            if not any(kind in {entry[1] for entry in claim_evidence_fields(json.loads(row["canonical_json"]))}
                       for row in case):
                raise ValueError("该学校的合同规则未要求此类别的申领凭证。")
            content = read_secure(self.storage_root,staged.path,staged.sha256,staged.size_bytes)
            if not content.startswith(b"%PDF-"):
                raise ValueError("请上传有效 PDF 凭证。")
            scope = hashlib.sha256(canonical_json([case_uid,self.entity_id,kind,self.simulation]).encode()).hexdigest()
            path,_ = write_immutable(self.storage_root/"school-commission-claim-evidence"/scope,
                                     staged.sha256+".pdf",content)
            existing = self.connection.execute(
                """SELECT uid FROM revenue_documents WHERE document_kind=? AND storage_path=?
                     AND sha256=? AND size_bytes=?""",
                (self.evidence_kind,str(path),staged.sha256,staged.size_bytes),
            ).fetchone()
            if existing:
                uid = existing["uid"]
            else:
                document = self.service.register_document(
                    self.evidence_kind,str(path),staged.sha256,"application/pdf",staged.size_bytes,
                    "upload",self.actor,f"school-commission-proof-document:{scope}:{staged.sha256}",
                )
                uid = document["uid"]
            return {**payload,"uid":uid,"document_uid":uid,"student_id":case[0]["student_id"]}

        return self.repository.command(
            idempotency_key=f"school-commission-proof:{key}",command_type=EVIDENCE_REGISTERED,
            aggregate_type="document",payload=payload,actor_user_id=self.actor,operation=operation,
        )

    def _formal_evidence(self, case_uid, student, document_uid, kind, label):
        row = self.connection.execute(
            """SELECT d.id,d.uid,d.storage_path,d.sha256,d.size_bytes,
                      json_extract(a.after_json,'$.filename') AS filename
                 FROM revenue_documents d JOIN revenue_audit_events a ON a.aggregate_uid=d.uid
                WHERE d.uid=? AND d.document_kind=? AND d.mime_type='application/pdf'
                  AND a.action=? AND json_extract(a.after_json,'$.case_uid')=?
                  AND json_extract(a.after_json,'$.student_id')=?
                  AND json_extract(a.after_json,'$.entity_id')=?
                  AND json_extract(a.after_json,'$.evidence_kind')=?
                  AND json_extract(a.after_json,'$.simulation')=?
                  AND json_extract(a.after_json,'$.sha256')=d.sha256
                ORDER BY a.id DESC LIMIT 1""",
            (document_uid,self.evidence_kind,EVIDENCE_REGISTERED,case_uid,student,self.entity_id,kind,self.simulation),
        ).fetchone()
        if row is None:
            raise ValueError(f"{label}缺失、类别错误或不属于该学生档案。")
        content = read_secure(self.storage_root,row["storage_path"],row["sha256"],row["size_bytes"])
        if not content.startswith(b"%PDF-"):
            raise ValueError(f"{label}不是有效的 PDF 文件。")
        return {"revenue_document_id":row["id"],"document_uid":row["uid"],"kind":kind,"label":label,
                "sha256":row["sha256"],"size_bytes":row["size_bytes"],"filename":row["filename"]},content

    def _evidence(self, student, document_id, kind, label):
        row = self.connection.execute(
            "SELECT * FROM student_documents WHERE id=? AND student_id=? AND document_type=? AND is_active=1",
            (document_id, student, kind),
        ).fetchone()
        if not row or dict(row).get("voided_at"):
            raise ValueError(f"{label}缺失、类型错误或不属于该学生。")
        path = Path(row["file_path"])
        if not re.fullmatch(r"[a-f0-9]{64}\.pdf", path.name):
            raise ValueError(f"{label}须先登记为隔离测试库中的可验证 PDF。")
        digest = path.stem
        content = read_secure(self.storage_root, path, digest, int(row["file_size"]))
        if not content.startswith(b"%PDF-"):
            raise ValueError(f"{label}不是有效的 PDF 文件。")
        return {"student_document_id": document_id, "kind": kind, "label": label,
                "sha256": digest, "size_bytes": len(content), "filename": row["original_name"]}, content

    def check_commencement(self, forecast):
        if forecast.get("school_commission_configurable") is True:
            elapsed = (date.fromisoformat(_today()) -
                       date.fromisoformat(forecast["commencement"]["actual_commencement_date"])).days
            minimum = forecast["claim"]["commencement_days"]
            if elapsed < minimum:
                raise ValueError(f"须开学满 {minimum} 天方可申领。")

    def check_formal_payment_terms(self, contract_uid, rule_id):
        contract = self.connection.execute(
            """SELECT d.sha256,s.canonical_json FROM revenue_contract_versions v
                 JOIN revenue_documents d ON d.id=v.document_id
                 JOIN revenue_commission_rule_versions r ON r.contract_version_id=v.id
                 JOIN revenue_rule_snapshots s ON s.id=r.rule_snapshot_id
                WHERE v.uid=? AND r.id=? AND d.document_kind='school_commission_signed_contract'""",
            (contract_uid, rule_id),
        ).fetchone()
        if contract is None:
            raise ValueError("正式 Claim 须关联已审核的学校签署合同。")
        terms = json.loads(contract["canonical_json"]).get("claim", {}).get("payment_days_from_valid_invoice")
        if type(terms) is not int or terms not in (0, 7, 15, 30):
            raise ValueError("正式 Claim 须关联已确认的付款期限。")
        return contract["sha256"], terms

    def _review(self, case_uid, line_uid, evidence_ids, recruited, reviewed, *, gst_mode=None):
        line = self.repository.require_by_uid("revenue_commission_lines", line_uid)
        snapshot = self.connection.execute(
            "SELECT canonical_json FROM revenue_rule_snapshots WHERE id=?", (line["rule_snapshot_id"],)
        ).fetchone()
        configuration = json.loads(snapshot["canonical_json"])
        if (configuration.get("toorak_forecast") is not True or configuration["case_uid"] != case_uid
                or bool(configuration.get("school_commission_formal")) != self.formal
                or (self.formal and configuration.get("school_commission_simulation") is not self.simulation)
                or (self.entity_id is not None and line["revenue_entity_id"] != self.entity_id)):
            raise ValueError("请选择本档案的学校预计佣金。")
        configurable = self.formal and configuration.get("school_commission_configurable") is True
        if configurable:
            if not reviewed or recruited not in {"agent_named","configured_rule"}:
                raise ValueError("须核对该学校合同及学生申领资料。")
            self.check_commencement(configuration)
        elif recruited != "agent_named" or not reviewed:
            raise ValueError("须确认由代理招募、代理提交且申请载有代理名称，并核实资料属于本入学项目。")
        current = self.forecast.preview(case_uid, configuration["component_uid"],
                                        configuration["visa"].get("category") == "other_resident_confirmed")
        if current["digest"] != configuration["digest"]:
            raise ValueError("签证、入学、学费或规则来源已变更，须复核预计佣金。")
        evidence = []
        for field,kind,label in claim_evidence_fields(current):
            document = evidence_ids.get(field)
            if not document:
                raise ValueError(f"请提交{label}。")
            if self.formal:
                info,content = self._formal_evidence(case_uid,current["student_id"],document,kind,label)
            else:
                info,content = self._evidence(current["student_id"],document,kind,label)
            evidence.append(({**info,"field":field},content))
        facts = {
            "toorak_claim": True, "case_uid": case_uid, "student_id": current["student_id"],
            "line_uid": line_uid, "forecast_digest": current["digest"],
            "contract_uid": current["rule"]["contract_uid"],
            "tier": current["tier"], "commencement": current["commencement"],
            "approved_invoice_pending": True,
            "evidence": [info for info,_ in evidence],
            "net_cents": line["net_cents"], "gst_cents": line["gst_cents"], "gross_cents": line["gross_cents"],
        }
        if gst_mode is not None:
            if not self.formal or current["school"].strip().casefold() != "toorak college":
                raise ValueError("本张发票的 GST 选择仅开放给 Toorak 佣金草稿。")
            commission = apply_basis_points(current["tuition"]["tuition_cents"], current["tier"]["rate_basis_points"])
            facts.update(commission_gst_amounts(commission, gst_mode), gst_mode=gst_mode)
        if configurable:
            facts.update(school_commission_configurable=True,
                         commencement_days=configuration["claim"]["commencement_days"],
                         registration_required=configuration["claim"]["registration_required"])
        else:
            facts.update(agent_recruited=True,agent_named_application=True,enrolled=True,
                         course_fee_paid_to_school=True)
        if self.formal:
            contract_hash, terms = self.check_formal_payment_terms(
                facts["contract_uid"], line["commission_rule_version_id"]
            )
            facts.update(school_commission_formal=True,school_commission_simulation=self.simulation,
                         contract_sha256=contract_hash,payment_days=terms)
        return line, facts, evidence

    def preview(self, case_uid, line_uid, evidence_ids, recruited, reviewed, *, gst_mode=None):
        _, facts, _ = self._review(case_uid, line_uid, evidence_ids, recruited, reviewed, gst_mode=gst_mode)
        return {**facts, "digest": hashlib.sha256(canonical_json(facts).encode()).hexdigest()}

    def _link_evidence(self, line, claim, evidence, now):
        for info, content in evidence:
            if self.formal:
                document = {"id":info["revenue_document_id"]}
                student_document_id = None
                source_kind,source_reference = "revenue_document",f'revenue_documents:{info["document_uid"]}'
            else:
                digest = info["sha256"]
                path, _ = write_immutable(self.storage_root / f"{self.workflow}-claim-evidence", digest+".pdf", content)
                document = self.connection.execute(
                    """SELECT id FROM revenue_documents WHERE document_kind=?
                         AND storage_path=? AND sha256=? AND size_bytes=?""",
                    (self.evidence_kind,str(path),digest,info["size_bytes"]),
                ).fetchone()
                if document is None:
                    document = self.service.register_document(
                        self.evidence_kind,str(path),digest,"application/pdf",
                        info["size_bytes"],"imported",self.actor,f"{self.workflow}-evidence:{digest}",
                    )
                student_document_id = info["student_document_id"]
                source_kind,source_reference = "student_document",f'student_documents:{student_document_id}'
            requirement = self.connection.execute(
                """SELECT id FROM revenue_claim_document_requirements
                    WHERE commission_rule_version_id=? AND document_kind=? AND required=1""",
                (line["commission_rule_version_id"], info["kind"]),
            ).fetchone()
            if requirement is None:
                requirement = self.connection.execute(
                    """INSERT INTO revenue_claim_document_requirements
                        (uid,commission_rule_version_id,document_kind,required,created_at)
                        VALUES(?,?,?,1,?) RETURNING id""",
                    (new_uid("claim-requirement"), line["commission_rule_version_id"], info["kind"], now),
                ).fetchone()
            self.connection.execute(
                """INSERT INTO revenue_claim_evidence_links
                    (uid,claim_id,claim_document_requirement_id,revenue_document_id,student_document_id,
                     source_kind,source_reference,verification_status,verified_by,verified_at,created_at)
                    VALUES(?,?,?,?,?,?,?,'verified',?,?,?)""",
                (new_uid("claim-evidence"),claim["id"],requirement["id"],document["id"],
                 student_document_id,source_kind,source_reference,self.actor,now,now),
            )

    def claimable_instalment(self, line):
        instalments = self.connection.execute(
            "SELECT * FROM revenue_commission_instalments WHERE commission_line_id=?", (line["id"],)
        ).fetchall()
        if (line["status"] != "forecast" or len(instalments) != 1
                or instalments[0]["status"] != "forecast" or instalments[0]["claimed_cents"]
                or instalments[0]["eligible_cents"] or instalments[0]["gross_cents"] != line["gross_cents"]
                or line["gross_cents"] <= 0):
            raise ValueError("预计佣金已有申领或期次状态不适用。")
        instalment = instalments[0]
        if self.connection.execute(
            "SELECT 1 FROM revenue_claim_allocations WHERE commission_instalment_id=?", (instalment["id"],)
        ).fetchone():
            raise ValueError("该期次已生成 Claim。")
        return instalment

    def issue_claim(self, case_uid, line_uid, evidence_ids, recruited, reviewed, digest, *, gst_mode=None):
        payload = dict(case_uid=case_uid,line_uid=line_uid,evidence_ids=evidence_ids,
                       recruited=recruited,reviewed=reviewed,digest=digest)
        if gst_mode is not None:
            payload["gst_mode"] = gst_mode

        def operation():
            line, facts, evidence = self._review(case_uid,line_uid,evidence_ids,recruited,reviewed,gst_mode=gst_mode)
            facts_json = canonical_json(facts)
            fingerprint = hashlib.sha256(facts_json.encode()).hexdigest()
            if fingerprint != digest:
                raise ValueError("证据或业务来源已变更，请重新核对。")
            instalment = self.claimable_instalment(line)
            uid = new_uid("claim")
            now, today = self.repository.now(), _today()
            kind,prefix = ("school_commission_claim","SCLM") if self.formal else ("toorak_internal_claim","TCLM")
            number = self.service._next_number(kind,prefix,int(today[:4]))
            amounts = facts["net_cents"], facts["gst_cents"], facts["gross_cents"]
            self.connection.execute(
                """INSERT INTO revenue_claims(uid,claim_number,claim_kind,revenue_entity_id,claim_date,
                    net_cents,gst_cents,gross_cents,status,issued_by,issued_at,created_at)
                    VALUES(?,?,'direct',?,?,?,?,?,'issued',?,?,?)""",
                (uid,number,line["revenue_entity_id"],today,*amounts,self.actor,now,now),
            )
            claim = self.repository.require_by_uid("revenue_claims",uid)
            self.connection.execute(
                """INSERT INTO revenue_claim_allocations
                    (uid,claim_id,commission_instalment_id,claimed_cents,created_at) VALUES(?,?,?,?,?)""",
                (new_uid("claim-allocation"),claim["id"],instalment["id"],facts["gross_cents"],now),
            )
            self.connection.execute(
                """UPDATE revenue_commission_instalments
                   SET net_cents=?,gst_cents=?,gross_cents=?,eligible_cents=?,claimed_cents=?,status='claimed' WHERE id=?""",
                (*amounts,facts["gross_cents"],facts["gross_cents"],instalment["id"]),
            )
            self.connection.execute(
                """UPDATE revenue_commission_lines SET net_cents=?,gst_cents=?,gross_cents=?,status='claimed' WHERE id=?""",
                (*amounts,line["id"]),
            )
            self.connection.execute(
                """INSERT INTO revenue_claim_eligibility_evaluations(uid,claim_id,commission_line_id,
                      rule_snapshot_id,evaluation_version,result_code,eligible,evaluator_source,
                      authoritative_facts_json,authoritative_facts_sha256,evaluated_by,evaluated_at,created_at)
                    VALUES(?,?,?,?,1,'eligible',1,'admin_confirmed',?,?,?,?,?)""",
                (new_uid("claim-evaluation"),claim["id"],line["id"],line["rule_snapshot_id"],
                 facts_json,fingerprint,self.actor,now,now),
            )
            self._link_evidence(line,claim,evidence,now)
            return claim

        return self.repository.command(
            idempotency_key=f"{self.workflow}-claim:{line_uid}", command_type=self.claim_action,
            aggregate_type="claim",payload=payload,actor_user_id=self.actor,operation=operation,
        )

    def _invoice_claims(self, entity_id=None, claim_uids=None):
        conditions = []
        params = [self.formal]
        if self.formal:
            conditions.append("json_extract(e.authoritative_facts_json,'$.school_commission_simulation')=?")
            params.append(self.simulation)
        if self.entity_id is not None:
            conditions.append("claim.revenue_entity_id=?")
            params.append(self.entity_id)
        if entity_id is not None:
            conditions.append("claim.revenue_entity_id=?")
            params.append(entity_id)
        if claim_uids is not None:
            conditions.append("claim.uid IN (" + ",".join("?" for _ in claim_uids) + ")")
            params.extend(claim_uids)
        clause = "".join(" AND " + condition for condition in conditions)
        return [dict(row) for row in self.connection.execute(
            """SELECT claim.uid,claim.claim_number,claim.revenue_entity_id,
                      claim.net_cents,claim.gst_cents,claim.gross_cents,
                      e.authoritative_facts_json,c.id AS case_id,c.student_id,c.uid AS case_uid,
                      entity.legal_name,
                      EXISTS(SELECT 1 FROM revenue_invoice_lines il
                          WHERE il.source_kind='claim' AND il.source_uid=claim.uid) AS invoiced
               FROM revenue_claims claim
               JOIN revenue_claim_eligibility_evaluations e ON e.claim_id=claim.id
               JOIN revenue_commission_lines l ON l.id=e.commission_line_id
               JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
               JOIN revenue_entities entity ON entity.id=claim.revenue_entity_id
               WHERE claim.status='issued' AND e.eligible=1 AND claim.gross_cents>0
                 AND l.revenue_entity_id=claim.revenue_entity_id
                 AND json_extract(e.authoritative_facts_json,'$.toorak_claim')=1
                 AND COALESCE(json_extract(e.authoritative_facts_json,'$.school_commission_formal'),0)=?""" + clause +
            " ORDER BY c.student_id,claim.id", params,
        )]

    def invoice_candidates(self, entity_id=None):
        rows = self._invoice_claims(entity_id)
        return {
            "entities": sorted({(row["revenue_entity_id"],row["legal_name"]) for row in rows}),
            "claims": [self._invoice_line(row) for row in rows if not row["invoiced"]],
        }

    def school_invoices(self, entity_id, *, issued_by=None):
        if self.entity_id is not None and self.entity_id!=entity_id:
            return []
        return [dict(row) for row in self.connection.execute(
            """SELECT i.uid,i.invoice_number,i.gross_cents,i.issue_date
               FROM revenue_invoices i JOIN revenue_documents d ON d.id=i.document_id
               WHERE i.revenue_entity_id=? AND i.document_kind='direct_commission_invoice'
                 AND i.status='issued' AND d.document_kind=?
                 AND (? IS NULL OR i.issued_by=?) AND """
            + invoice_origin_predicate() + " ORDER BY i.id DESC",
            (entity_id,self.invoice_kind,issued_by,issued_by,self.formal,self.simulation),
        )]

    @staticmethod
    def _invoice_line(row):
        facts = json.loads(row["authoritative_facts_json"])
        basis = "local" if facts["tier"]["tuition_kind"] == "local" else "international"
        basis_points = facts["tier"]["rate_basis_points"]
        rate = basis_points // 100 if basis_points % 100 == 0 else basis_points / 100
        return {
            "claim_uid":row["uid"],"claim_number":row["claim_number"],"student_id":row["student_id"],
            "rate":rate,"quantity":1,
            "description":(f'{row["student_id"]} | {row["claim_number"]} | first-year {basis} tuition '
                           f'commission ({rate}%)'),
            **{key:row[key] for key in ("net_cents","gst_cents","gross_cents")},
        }

    @staticmethod
    def _selected_claims(claim_uids):
        if not claim_uids or len(claim_uids)>100 or len(set(claim_uids))!=len(claim_uids):
            raise ValueError("请选择 1 至 100 条不同的申领记录。")
        return sorted(claim_uids)

    def _invoice_snapshot(self, rows, number):
        lines = [self._invoice_line(row) for row in rows]
        amounts = {key:sum(line[key] for line in lines) for key in ("net_cents","gst_cents","gross_cents")}
        payment_terms = "Payment due date is confirmed after school receipt."
        if self.formal:
            terms = {json.loads(row["authoritative_facts_json"]).get("payment_days") for row in rows}
            if len(terms)!=1 or not terms <= {0,7,15,30}:
                raise ValueError("正式发票的 Claim 必须使用同一已确认的付款期限。")
            days = terms.pop()
            payment_terms = (f"Payment within {days} days of school receipt of a valid invoice."
                             if days else "Payment due upon school receipt of a valid invoice.")
        return dict(
            document_number=number, issue_date=_today(), issuer_name="AAIE Education Services",
            issuer_abn="Test fixture", bill_to=rows[0]["legal_name"], currency_code="AUD",
            student_id="\n".join(line["student_id"] for line in lines),
            claim_number="\n".join(line["claim_number"] for line in lines),
            payment_terms=payment_terms,
            lines=lines, **amounts,
        )

    def _checked_invoice_claims(self, entity_id, claim_uids):
        rows = self._invoice_claims(entity_id, claim_uids)
        if len(rows)!=len(claim_uids):
            raise ValueError("请选择本付款主体下已审核的学校佣金 Claim。")
        if any(row["invoiced"] for row in rows):
            raise ValueError("所选 Claim 已生成发票，请刷新后重新选择。")
        return rows

    def _school_invoice_preview(self, entity_id, claim_uids):
        rows = self._checked_invoice_claims(entity_id, claim_uids)
        snapshot = self._invoice_snapshot(rows,"DRAFT")
        revisions = current_invoice_template_revisions(self.connection,formal=self.formal,entity_id=self.entity_id,
                                                       simulation=self.simulation,include_billing=True)
        digest = hashlib.sha256(canonical_json([entity_id,claim_uids,snapshot,revisions]).encode()).hexdigest()
        return rows,snapshot,digest

    def invoice_preview(self, entity_id, claim_uids):
        ids = self._selected_claims(claim_uids)
        _,snapshot,digest = self._school_invoice_preview(entity_id,ids)
        return {**snapshot,"entity_id":entity_id,"claim_uids":ids,"digest":digest}

    def invoice_preview_pdf(self, entity_id, claim_uids, digest):
        _,snapshot,current_digest = self._school_invoice_preview(entity_id,self._selected_claims(claim_uids))
        if digest!=current_digest:
            raise ValueError("发票明细、模板或财务资料已更新，请重新预览。")
        return self._render_invoice(snapshot, use_default=True)[0]

    def _render_invoice(self, snapshot, use_default):
        configured = render_with_active_template(self.connection,snapshot,formal=self.formal,simulation=self.simulation,
                                                 entity_id=self.entity_id)
        if configured:
            return configured
        if not use_default:
            return render_toorak_invoice_pdf(snapshot),None,None
        profile,profile_uid = financial_profile(self.connection)
        data = {**snapshot,**profile,"bill_to_address":school_test_billing_address(snapshot["bill_to"])}
        return render_configured_commission_invoice(data,default_layout()),None,profile_uid

    def _persist_invoice(self, rows, snapshot, command_key, *, use_default=False):
        invoice_store = configured_invoice_store() if self.formal else None
        if self.formal and not self.simulation and invoice_store is None:
            raise StorageUnavailable()
        now,today = self.repository.now(),snapshot["issue_date"]
        uid = new_uid("invoice")
        sequence = "school_commission_invoice" if self.formal else "toorak_commission_invoice"
        number = self.service._next_number(sequence,"COM",int(today[:4]))
        while self.connection.execute("SELECT 1 FROM revenue_invoices WHERE invoice_number=?",(number,)).fetchone():
            number = self.service._next_number(sequence,"COM",int(today[:4]))
        data = {**snapshot,"document_number":number}
        if self.formal:
            from revenue.invoice_billing_details import bill_to_fields

            data["invoice_bill_to"] = bill_to_fields(self.connection, self.entity_id, self.simulation)
        pdf,template_uid,profile_uid = self._render_invoice(data,use_default)
        digest = hashlib.sha256(pdf).hexdigest()
        if invoice_store:
            path = invoice_store.write(pdf)
        else:
            path,_ = write_immutable(self.storage_root/f"{self.workflow}-invoices",digest+".pdf",pdf)
        document = self.service.register_document(
            self.invoice_kind,str(path),digest,"application/pdf",len(pdf),"generated",
            self.actor,command_key,
        )
        invoice_id, = self.connection.execute(
            """INSERT INTO revenue_invoices(uid,document_kind,invoice_number,commercial_case_id,
                  revenue_entity_id,issue_date,currency_code,net_cents,gst_cents,gross_cents,
                  status,document_id,issued_by,issued_at,created_at)
                VALUES(?,'direct_commission_invoice',?,?,?,?,'AUD',?,?,?,'issued',?,?,?,?) RETURNING id""",
            (uid,number,rows[0]["case_id"] if len(rows)==1 else None,rows[0]["revenue_entity_id"],today,
             data["net_cents"],data["gst_cents"],data["gross_cents"],document["id"],self.actor,now,now),
        ).fetchone()
        self.connection.executemany(
            """INSERT INTO revenue_invoice_lines(uid,invoice_id,source_kind,source_uid,student_id,
                   description,quantity,net_cents,gst_cents,gross_cents,sort_order)
                VALUES(?,?,'claim',?,?,?,1,?,?,?,?)""",
            [(new_uid("invoice-line"),invoice_id,line["claim_uid"],line["student_id"],line["description"],
              line["net_cents"],line["gst_cents"],line["gross_cents"],index)
             for index,line in enumerate(data["lines"])],
        )
        if self.formal:
            case_id = rows[0]["case_id"] if len(rows)==1 else None
            for field,account in (("net_cents","CommissionRevenue"),("gst_cents","GSTPayable")):
                if data[field]:
                    self.service.append_balanced_ledger_event(
                        "invoiced","school_commission_invoice",uid,data[field],
                        "CommissionReceivable",account,self.actor,commercial_case_id=case_id,
                    )
        invoice = self.repository.require_by_uid("revenue_invoices",uid)
        if self.formal and (self.simulation or self.mail_release):
            if self.mail_release == "payer" or self.simulation:
                enqueue_mail(self.repository, invoice, document, invoice["revenue_entity_id"],
                             self.actor, payer_routing=True, simulation=self.simulation)
            if self.mail_release != "payer":
                enqueue_mail(self.repository, invoice, document, invoice["revenue_entity_id"], self.actor,
                             release=self.mail_release, claim_uids=[line["claim_uid"] for line in data["lines"]])
        return {**invoice,"document_uid":document["uid"],
                "template_uid":template_uid,"finance_profile_uid":profile_uid,
                **({"simulation":self.simulation, "invoice_bill_to":data["invoice_bill_to"]} if self.formal else {})}

    def issue_school_invoice(self, entity_id, claim_uids, digest):
        ids = self._selected_claims(claim_uids)
        key = hashlib.sha256(canonical_json([entity_id,ids]).encode()).hexdigest()

        def operation():
            rows,snapshot,current_digest = self._school_invoice_preview(entity_id,ids)
            if digest!=current_digest:
                raise ValueError("发票明细、模板或财务资料已更新，请重新预览。")
            return self._persist_invoice(rows,snapshot,f"{self.workflow}-school-invoice-pdf:{key}",use_default=True)

        return self.repository.command(
            idempotency_key=f"{self.workflow}-school-invoice:{key}",command_type=self.invoice_action,
            aggregate_type="invoice",payload={"entity_id":entity_id,"claim_uids":ids,"preview_digest":digest},
            actor_user_id=self.actor,operation=operation,
        )

    def issue_invoice(self, case_uid, claim_uid, template_confirmed, expected_revisions=None):
        if not template_confirmed:
            raise ValueError("请确认使用本批合成测试发票模板。")

        def operation():
            if expected_revisions is not None and expected_revisions!=current_invoice_template_revisions(self.connection,formal=self.formal,entity_id=self.entity_id,
                                                       simulation=self.simulation):
                raise ValueError("发票模板或测试财务资料已更新，请刷新后重新确认。")
            rows = self._checked_invoice_claims(None,[claim_uid])
            if rows[0]["case_uid"]!=case_uid:
                raise ValueError("请选择本档案已签发的学校佣金 Claim。")
            return self._persist_invoice(rows,self._invoice_snapshot(rows,"DRAFT"),f"{self.workflow}-invoice-pdf:{claim_uid}")

        payload=dict(case_uid=case_uid,claim_uid=claim_uid)
        if expected_revisions is not None:
            payload["template_revision"],payload["finance_revision"]=expected_revisions
        return self.repository.command(
            idempotency_key=f"{self.workflow}-invoice:{claim_uid}", command_type=self.invoice_action,
            aggregate_type="invoice",payload=payload,actor_user_id=self.actor,operation=operation,
        )

"""Fail-closed authorization for the managed school commission pilot."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3


STAGES = ("contracts", "preview", "invoicing", "settlement")
FORMAL_CONTRACT = "school_commission_signed_contract"
FORMAL_INVOICE = "school_commission_invoice"
FORMAL_DOCUMENTS = frozenset({
    FORMAL_CONTRACT, FORMAL_INVOICE, "school_commission_claim_evidence",
    "school_commission_delivery_evidence", "school_commission_remittance_evidence",
})


@dataclass(frozen=True)
class PilotPolicy:
    school_id: int = 0
    entity_id: int = 0
    admin_ids: tuple[int, ...] = ()
    enabled: bool = False
    stage: str = ""
    simulation: bool = True
    self_service: bool = False
    revision: int = 0
    contract_intake: bool = False
    education_authorities: tuple[str, ...] = ()

    def allows(self, user, stage):
        if not user or user["role_name"] != "Admin" or user["id"] not in self.admin_ids:
            return False
        if stage == "read":
            return True
        if stage == "setup":
            return self.enabled and self.self_service
        if stage == "contract-intake":
            return self.enabled and self.contract_intake and not self.self_service
        return self.enabled and stage in STAGES and STAGES.index(stage) <= STAGES.index(self.stage)

    def matches_payer(self, connection):
        return bool(connection.execute(
            """SELECT 1 FROM schools s JOIN revenue_entity_provider_links l ON l.school_id=s.id
               JOIN revenue_entities e ON e.id=l.revenue_entity_id
               WHERE s.id=? AND e.id=? AND s.deleted_at IS NULL
                 AND (? OR (lower(trim(s.school_name))='toorak college'
                             AND lower(trim(e.legal_name))='toorak college'))
                 AND e.entity_type='school' AND e.status='active'
                 AND l.effective_to IS NULL""", (self.school_id,self.entity_id,self.self_service),
        ).fetchone())

    def owns_document(self, connection, document):
        kind, uid, pk = document["document_kind"], document["uid"], document["id"]
        if kind == FORMAL_CONTRACT:
            return bool(connection.execute(
                """SELECT 1 FROM revenue_rule_snapshots
                   WHERE source_rule_uid=? AND json_extract(canonical_json,'$.toorak_draft')=1
                     AND json_extract(canonical_json,'$.school_id')=?
                     AND json_extract(canonical_json,'$.pilot_entity_id')=?
                     AND json_extract(canonical_json,'$.pilot_simulation')=?""",
                (uid,self.school_id,self.entity_id,self.simulation),
            ).fetchone())
        if kind == FORMAL_INVOICE:
            return bool(connection.execute(
                """SELECT 1 FROM revenue_invoices i JOIN revenue_audit_events a ON a.aggregate_uid=i.uid
                   WHERE i.document_id=? AND i.revenue_entity_id=?
                     AND a.action='school_commission_invoice_issued'
                     AND json_extract(a.after_json,'$.simulation')=?""",
                (pk,self.entity_id,self.simulation),
            ).fetchone())
        if kind == "school_commission_claim_evidence":
            return bool(connection.execute(
                """SELECT 1 FROM revenue_audit_events WHERE action='school_commission_evidence_registered'
                     AND json_extract(after_json,'$.document_uid')=?
                     AND json_extract(after_json,'$.entity_id')=?
                     AND json_extract(after_json,'$.simulation')=?""", (uid,self.entity_id,self.simulation),
            ).fetchone())
        if kind == "school_commission_remittance_evidence":
            return bool(connection.execute(
                """SELECT 1 FROM revenue_payments p JOIN revenue_audit_events a ON a.aggregate_uid=p.uid
                   WHERE p.evidence_document_id=? AND p.revenue_entity_id=? AND p.counterparty_kind='institution'
                     AND a.action='school_commission_payment_allocated'
                     AND json_extract(a.after_json,'$.simulation')=?""", (pk,self.entity_id,self.simulation),
            ).fetchone())
        if kind == "school_commission_delivery_evidence":
            return bool(connection.execute(
                """SELECT 1 FROM revenue_audit_events a JOIN revenue_invoices i
                   ON i.uid=json_extract(a.after_json,'$.invoice_uid')
                   WHERE a.action='school_commission_delivery_confirmed'
                     AND json_extract(a.after_json,'$.document_uid')=? AND i.revenue_entity_id=?
                     AND EXISTS (SELECT 1 FROM revenue_audit_events issued WHERE issued.aggregate_uid=i.uid
                       AND issued.action='school_commission_invoice_issued'
                       AND json_extract(issued.after_json,'$.simulation')=?)""",
                (uid,self.entity_id,self.simulation),
            ).fetchone())
        return False


def read_pilot_policy(root, mode, override=None):
    """A file replace can close the pilot without reloading the application."""
    closed = PilotPolicy(simulation=mode != "production")
    if mode not in {"rehearsal","production"} or (override and mode != "rehearsal"):
        return closed
    try:
        config = Path(root).resolve(strict=True) / "config"
        path = Path(override) if override else config / "school-commission-pilot.json"
        if not path.is_absolute() or path.parent != config or path.resolve(strict=True) != path:
            return closed
        if not path.is_file() or path.stat().st_size > 4096:
            return closed
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value,dict)
                or set(value)-{"self_service","contract_intake","education_authorities"}!={"mode","enabled","stage","school_id","entity_id","admin_ids"}
                or type(value.get("self_service",False)) is not bool
                or type(value.get("contract_intake",False)) is not bool
                or value["mode"]!=mode or type(value["enabled"]) is not bool
                or value["stage"] not in STAGES):
            return closed
        admins = value["admin_ids"]
        if not isinstance(admins,list) or not 1<=len(admins)<=25:
            return closed
        ids = (value["school_id"],value["entity_id"],*admins)
        if any(type(item) is not int or item<=0 for item in ids) or len(set(admins))!=len(admins):
            return closed
        from revenue.state_school_commission import STATE_AUTHORITIES

        states = value.get("education_authorities", [])
        if (not isinstance(states, list) or any(type(state) is not str or state not in STATE_AUTHORITIES for state in states)
                or len(set(states)) != len(states)):
            states = []
        return PilotPolicy(value["school_id"],value["entity_id"],tuple(admins),value["enabled"],
                           value["stage"],mode=="rehearsal",value.get("self_service",False),
                           contract_intake=value.get("contract_intake",False),
                           education_authorities=tuple(sorted(states)))
    except (OSError,ValueError,RuntimeError):
        return closed


def payer_matches(policy, connection):
    try:
        return policy.matches_payer(connection)
    except sqlite3.Error:
        return False


STAGE_ACTION = "school_commission_scope_stage_changed"


def school_pilot_scope(connection, base, *, school_id=None, entity_id=None):
    if school_id is None and entity_id is None:
        school_id = base.school_id
    if not base.self_service:
        if ((school_id is not None and school_id!=base.school_id)
                or (entity_id is not None and entity_id!=base.entity_id)):
            return None
        return base
    rows = connection.execute(
        """SELECT s.id,e.id FROM schools s
           JOIN revenue_entity_provider_links l ON l.school_id=s.id
           JOIN revenue_entities e ON e.id=l.revenue_entity_id
           WHERE (? IS NULL OR s.id=?) AND (? IS NULL OR e.id=?)
             AND s.deleted_at IS NULL AND e.status='active' AND e.entity_type='school'
             AND l.effective_to IS NULL""",
        (school_id,school_id,entity_id,entity_id),
    ).fetchall()
    if len(rows)!=1:
        return None
    school_id,entity_id = rows[0]
    saved = connection.execute(
        """SELECT id,after_json FROM revenue_audit_events
           WHERE action=? AND aggregate_uid=? ORDER BY id DESC LIMIT 1""",
        (STAGE_ACTION,f"{int(base.simulation)}:{school_id}:{entity_id}"),
    ).fetchone()
    stage = base.stage if (school_id,entity_id)==(base.school_id,base.entity_id) else "contracts"
    if saved:
        stage = json.loads(saved[1])["stage"]
    enabled = base.enabled and stage in STAGES
    if not enabled:
        stage = base.stage
    elif STAGES.index(stage)>STAGES.index(base.stage):
        stage = base.stage
    return replace(base,school_id=school_id,entity_id=entity_id,enabled=enabled,stage=stage,
                   revision=saved[0] if saved else 0)


def list_school_pilot_scopes(connection, base):
    if not base.self_service:
        return [base]
    return [scope for (school_id,) in connection.execute(
        """SELECT DISTINCT school_id FROM revenue_entity_provider_links
           WHERE school_id IS NOT NULL AND effective_to IS NULL ORDER BY school_id"""
    ) if (scope := school_pilot_scope(connection,base,school_id=school_id)) is not None]




def set_school_pilot_stage(repository, base, school_id, stage, actor, key):
    connection = repository.connection
    scope = school_pilot_scope(connection,base,school_id=school_id)
    if not base.self_service or not base.enabled or scope is None:
        raise ValueError("请先完成学校及付款主体关联。")
    if stage not in (*STAGES,"paused") or (
        stage in STAGES and STAGES.index(stage)>STAGES.index(base.stage)
    ):
        raise ValueError("该阶段超出本环境的授权范围。")
    if stage in ("invoicing","settlement") and not connection.execute(
        """SELECT 1 FROM revenue_commission_rule_versions r
           JOIN revenue_contract_versions v ON v.id=r.contract_version_id
           JOIN revenue_contract_groups g ON g.id=v.contract_group_id
           JOIN revenue_documents d ON d.id=v.document_id
           JOIN revenue_rule_snapshots s ON s.id=r.rule_snapshot_id
           WHERE g.revenue_entity_id=? AND v.status='active' AND r.status='active'
             AND d.document_kind=? AND json_extract(s.canonical_json,'$.pilot_simulation')=?""",
        (scope.entity_id,FORMAL_CONTRACT,scope.simulation),
    ).fetchone():
        raise ValueError("请先启用已核对的学校佣金合同。")
    uid = f"{int(scope.simulation)}:{school_id}:{scope.entity_id}"
    payload = {"uid":uid,"stage":stage,"school_id":school_id,"entity_id":scope.entity_id}
    return repository.command(
        idempotency_key=key,command_type=STAGE_ACTION,aggregate_type="school_commission_scope",
        payload=payload,actor_user_id=actor,operation=lambda:payload,
    )

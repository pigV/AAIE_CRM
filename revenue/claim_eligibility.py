"""Authoritative, read-only Direct Commission claim eligibility resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import mimetypes
from pathlib import Path

from revenue.rules import CommissionRuleEvaluator


DOCUMENT_TYPE_ALIASES = {
    "coe": {"coe", "confirmationofenrolment", "confirmationofenrollment"},
    "student_id": {
        "studentid", "studentidentity", "identity", "passport", "id"
    },
    "tuition_receipt": {
        "tuitionreceipt", "paymentreceipt", "receipt", "tuitionpayment"
    },
    "commencement_evidence": {
        "commencement", "commencementevidence", "enrolmentconfirmation"
    },
}


def _normalise_document_type(value: str) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


@dataclass(frozen=True)
class ClaimEvidence:
    requirement_id: int
    document_kind: str
    student_document_id: int
    source_reference: str
    storage_path: str
    sha256: str
    mime_type: str
    size_bytes: int

    def public_dict(self) -> dict:
        return {
            "document_kind": self.document_kind,
            "source_kind": "student_document",
            "source_reference": self.source_reference,
            "storage_path": self.storage_path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ClaimEligibilityDecision:
    eligible: bool
    facts: dict
    reasons: tuple[dict, ...]
    evidence: tuple[ClaimEvidence, ...]

    @property
    def status(self) -> str:
        return "READY" if self.eligible else "NOT_READY"

    def public_dict(self) -> dict:
        return {
            "eligibility_status": self.status,
            "eligible": self.eligible,
            "facts": self.facts,
            "reasons": list(self.reasons),
            "evidence": [item.public_dict() for item in self.evidence],
        }


class ClaimEligibilityResolver:
    """Resolves claim facts only from Revenue and canonical CRM authorities."""

    def __init__(self, connection):
        self.connection = connection

    def evaluate(self, commission_line_uid: str, claim_date: str) -> ClaimEligibilityDecision:
        line = self.connection.execute(
            """SELECT l.*,c.student_id,ec.coe_oshc_id
                 FROM revenue_commission_lines l
                 JOIN revenue_commercial_cases c ON c.id=l.commercial_case_id
                 JOIN revenue_enrolment_components ec ON ec.id=l.enrolment_component_id
                WHERE l.uid=?""",
            (commission_line_uid,),
        ).fetchone()
        if not line:
            raise ValueError("revenue_commission_lines record not found")

        commencement = self.connection.execute(
            """SELECT * FROM revenue_commencement_facts
                WHERE enrolment_component_id=? AND status='confirmed'""",
            (line["enrolment_component_id"],),
        ).fetchone()
        commencement_date = commencement["actual_commencement_date"] if commencement else None
        facts = {
            "commenced": commencement is not None,
            "actual_commencement_date": commencement_date,
            "days_since_commencement": self._elapsed_days(commencement_date, claim_date),
            "coe_issued": self._coe_exists(line["coe_oshc_id"]),
            "tuition_recorded": self.connection.execute(
                """SELECT 1 FROM revenue_tuition_facts
                    WHERE enrolment_component_id=? AND status='active' LIMIT 1""",
                (line["enrolment_component_id"],),
            ).fetchone() is not None,
        }

        snapshot = self.connection.execute(
            "SELECT canonical_json FROM revenue_rule_snapshots WHERE id=?",
            (line["rule_snapshot_id"],),
        ).fetchone()
        if not snapshot:
            raise ValueError("claim rule snapshot not found")
        import json
        configuration = json.loads(snapshot["canonical_json"])
        reasons: list[dict] = []
        if commencement is None:
            reasons.append({
                "code": "missing_actual_commencement",
                "message": "Confirmed Actual Commencement is required.",
            })
        trigger = configuration.get("claim_trigger")
        if (
            trigger
            and commencement is not None
            and not CommissionRuleEvaluator().evaluate_trigger(trigger, facts)
        ):
            reasons.append({
                "code": "claim_trigger_not_satisfied",
                "message": "The authoritative claim trigger is not yet satisfied.",
            })

        evidence: list[ClaimEvidence] = []
        requirements = self.connection.execute(
            """SELECT * FROM revenue_claim_document_requirements
                WHERE commission_rule_version_id=? AND required=1 ORDER BY id""",
            (line["commission_rule_version_id"],),
        ).fetchall()
        for requirement in requirements:
            resolved = self._resolve_student_document(
                line["student_id"], requirement["document_kind"], requirement["id"]
            )
            if resolved:
                evidence.append(resolved)
            else:
                reasons.append({
                    "code": f"missing_required_document:{requirement['document_kind']}",
                    "message": f"Required document is unavailable: {requirement['document_kind']}.",
                })
        return ClaimEligibilityDecision(
            eligible=not reasons,
            facts=facts,
            reasons=tuple(reasons),
            evidence=tuple(evidence),
        )

    @staticmethod
    def _elapsed_days(actual_date: str | None, claim_date: str) -> int | None:
        if not actual_date:
            return None
        return (date.fromisoformat(claim_date[:10]) - date.fromisoformat(actual_date[:10])).days

    def _coe_exists(self, coe_oshc_id: int | None) -> bool:
        if coe_oshc_id is None:
            return False
        return self.connection.execute(
            "SELECT 1 FROM coe_oshc WHERE id=?", (coe_oshc_id,)
        ).fetchone() is not None

    def _resolve_student_document(
        self, student_id: str, document_kind: str, requirement_id: int
    ) -> ClaimEvidence | None:
        aliases = DOCUMENT_TYPE_ALIASES.get(
            document_kind, {_normalise_document_type(document_kind)}
        )
        rows = self.connection.execute(
            """SELECT id,document_type,file_path,file_size,original_name,uploaded_at
                 FROM student_documents
                WHERE student_id=? AND is_active=1
                ORDER BY version DESC,uploaded_at DESC,id DESC""",
            (student_id,),
        ).fetchall()
        for row in rows:
            if _normalise_document_type(row["document_type"]) not in aliases:
                continue
            path = Path(row["file_path"])
            if not path.is_file():
                continue
            content = path.read_bytes()
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return ClaimEvidence(
                requirement_id=requirement_id,
                document_kind=document_kind,
                student_document_id=row["id"],
                source_reference=f"student_documents:{row['id']}",
                storage_path=str(path),
                sha256=hashlib.sha256(content).hexdigest(),
                mime_type=mime_type,
                size_bytes=len(content),
            )
        return None

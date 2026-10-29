"""Read-only adapters over canonical CRM lifecycle authorities."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3


POST_SUBMISSION_TRANSFER_STATUSES = frozenset(
    {
        "submitted",
        "waiting_offer",
        "offer_received",
        "waiting_payment",
        "payment_satisfied",
        "waiting_completion_document",
        "ready",
        "accepted",
    }
)


@dataclass(frozen=True)
class ExistingAAIEDecision:
    is_existing: bool
    reason: str
    current_cycle_id: int
    prior_cycle_id: int | None = None
    contract_id: int | None = None
    contract_date: str | None = None
    contract_date_source: str | None = None
    submission_kind: str | None = None
    submission_id: int | None = None
    submitted_at: str | None = None

    def evidence(self) -> dict:
        return {
            "definition": "existing_aaie_k12_v1",
            "is_existing": self.is_existing,
            "reason": self.reason,
            "current_cycle_id": self.current_cycle_id,
            "prior_cycle_id": self.prior_cycle_id,
            "contract_id": self.contract_id,
            "contract_date": self.contract_date,
            "contract_date_source": self.contract_date_source,
            "submission_kind": self.submission_kind,
            "submission_id": self.submission_id,
            "submitted_at": self.submitted_at,
        }


class RevenueFactAdapter:
    """Queries existing authorities without issuing INSERT, UPDATE or DELETE."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def resolve_existing_aaie_k12(self, current_cycle_id: int) -> ExistingAAIEDecision:
        current = self.connection.execute(
            """SELECT id,student_id,education_stream,created_at
                 FROM application_cycles WHERE id=?""",
            (int(current_cycle_id),),
        ).fetchone()
        if current is None:
            raise ValueError("current application cycle not found")
        if str(current[2]) != "school_education":
            return ExistingAAIEDecision(
                False, "current_cycle_not_school_education", int(current[0])
            )
        prior_cycles = self.connection.execute(
            """SELECT id,created_at
                 FROM application_cycles
                WHERE student_id=?
                  AND id<>?
                  AND education_stream='school_education'
                  AND (created_at<? OR (created_at=? AND id<?))
                ORDER BY created_at DESC,id DESC""",
            (current[1], current[0], current[3], current[3], current[0]),
        ).fetchall()
        for prior in prior_cycles:
            contract = self._fully_executed_contract(int(prior[0]))
            if contract is None:
                continue
            submission = self._formal_submission(int(prior[0]))
            if submission is None:
                continue
            contract_date = contract[2] or (
                str(contract[3])[:10] if contract[3] else None
            )
            date_source = "contract_date" if contract[2] else "fully_executed_at"
            return ExistingAAIEDecision(
                True,
                "qualified_prior_school_cycle",
                int(current[0]),
                prior_cycle_id=int(prior[0]),
                contract_id=int(contract[0]),
                contract_date=contract_date,
                contract_date_source=date_source,
                submission_kind=submission[0],
                submission_id=int(submission[1]),
                submitted_at=submission[2],
            )
        return ExistingAAIEDecision(
            False, "no_qualifying_prior_cycle", int(current[0])
        )

    def _fully_executed_contract(self, cycle_id: int):
        return self.connection.execute(
            """SELECT id,application_cycle_id,contract_date,fully_executed_at
                 FROM contracts
                WHERE application_cycle_id=?
                  AND contract_status='FullyExecuted'
                ORDER BY COALESCE(contract_date,fully_executed_at) DESC,id DESC
                LIMIT 1""",
            (cycle_id,),
        ).fetchone()

    def _formal_submission(self, cycle_id: int):
        candidates: list[tuple[str, int, str | None]] = []
        for row in self.connection.execute(
            """SELECT id,submitted_at FROM public_application_groups
                WHERE application_cycle_id=? AND submitted_at IS NOT NULL""",
            (cycle_id,),
        ):
            candidates.append(("public_application_group", int(row[0]), str(row[1])))
        for row in self.connection.execute(
            """SELECT id,submitted_at FROM private_school_applications
                WHERE application_cycle_id=? AND submitted_at IS NOT NULL""",
            (cycle_id,),
        ):
            candidates.append(
                ("private_school_application", int(row[0]), str(row[1]))
            )
        placeholders = ",".join("?" for _ in POST_SUBMISSION_TRANSFER_STATUSES)
        params = (cycle_id, *sorted(POST_SUBMISSION_TRANSFER_STATUSES))
        for row in self.connection.execute(
            f"""SELECT id,application_status FROM transfer_school_applications
                  WHERE application_cycle_id=?
                    AND application_status IN ({placeholders})""",
            params,
        ):
            candidates.append(("transfer_school_application", int(row[0]), None))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[2] is None, item[2] or "", item[0], item[1]))
        return candidates[0]

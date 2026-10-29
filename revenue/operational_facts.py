"""Typed read-only adapters over existing CRM authoritative records."""

from __future__ import annotations

from dataclasses import dataclass


def _value(row, name, default=None):
    return row[name] if row is not None and name in row.keys() else default


@dataclass(frozen=True)
class CycleFact:
    id: int
    student_id: str
    cycle_uid: str
    cycle_type: str
    status: str
    education_stream: str
    version: str


@dataclass(frozen=True)
class ContractFact:
    id: int
    student_id: str
    application_cycle_id: int | None
    contract_status: str
    contract_type: str | None
    contract_date: str | None
    fully_executed_at: str | None
    version: str

    @property
    def fully_executed(self) -> bool:
        return self.contract_status.replace("_", "").replace(" ", "").lower() == "fullyexecuted"


@dataclass(frozen=True)
class VisaCaseFact:
    id: int
    student_id: str | None
    application_cycle_id: int | None
    case_type: str | None
    visa_subclass: str | None
    application_type: str | None
    status: str | None
    version: str


@dataclass(frozen=True)
class OfferFact:
    id: int
    application_cycle_id: int
    student_id: str
    source_kind: str | None
    school_id: int | None
    school_name: str | None
    higher_education_offer_id: int | None
    version: str


@dataclass(frozen=True)
class FinalAcceptanceFact:
    id: int
    application_cycle_id: int
    student_id: str
    offer_received_event_id: int
    school_id: int | None
    school_name: str | None
    acceptance_status: str
    version: str


@dataclass(frozen=True)
class CoeFact:
    id: int
    application_cycle_id: int | None
    student_id: str | None
    provider: str | None
    course: str | None
    start_date: str | None
    file_path: str | None
    language_course_name: str | None
    main_course_name: str | None
    version: str


@dataclass(frozen=True)
class HigherEducationApplicationFact:
    id: int
    application_cycle_id: int
    provider_id: int
    course_type: str | None
    course_name: str | None
    intake: str | None
    submission_channel: str | None
    master_agent_id: int | None
    status: str | None
    version: str


@dataclass(frozen=True)
class HigherEducationPackageSegmentFact:
    id: int
    application_id: int
    teaching_provider_id: int | None
    course_type: str | None
    course_name: str | None
    intake: str | None
    version: str


@dataclass(frozen=True)
class DocumentFact:
    id: int
    student_id: str
    document_type: str
    storage_path: str
    active: bool
    source_reference: str
    version: str


class RevenueOperationalFacts:
    """Normalizes core facts without writing or guessing missing authorities."""

    def __init__(self, connection):
        self.connection = connection

    def _require(self, table: str, record_id: int):
        row = self.connection.execute(
            f'SELECT * FROM "{table}" WHERE id=?', (int(record_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"{table} authoritative record not found")
        return row

    def _cycle_identity(self, cycle_id: int) -> tuple[str, CycleFact]:
        cycle = self.cycle(cycle_id)
        return cycle.student_id, cycle

    @staticmethod
    def _version(row, *preferred: str) -> str:
        for name in preferred:
            value = _value(row, name)
            if value not in (None, ""):
                return str(value)
        return str(row["id"])

    def cycle(self, cycle_id: int) -> CycleFact:
        row = self._require("application_cycles", cycle_id)
        student_id = _value(row, "student_id")
        if not student_id:
            raise ValueError("application cycle has no authoritative student")
        return CycleFact(
            id=row["id"], student_id=student_id,
            cycle_uid=_value(row, "cycle_uid", f"cycle-{row['id']}"),
            cycle_type=_value(row, "cycle_type", "initial"),
            status=_value(row, "status", "active"),
            education_stream=_value(row, "education_stream", "school_education"),
            version=self._version(row, "updated_at", "created_at"),
        )

    def contract(self, contract_id: int) -> ContractFact:
        row = self._require("contracts", contract_id)
        return ContractFact(
            id=row["id"], student_id=_value(row, "student_id"),
            application_cycle_id=_value(row, "application_cycle_id"),
            contract_status=_value(row, "contract_status", ""),
            contract_type=_value(row, "contract_type"),
            contract_date=_value(row, "contract_date"),
            fully_executed_at=_value(row, "fully_executed_at"),
            version=self._version(row, "updated_at", "fully_executed_at", "contract_date"),
        )

    def visa_case(self, visa_case_id: int) -> VisaCaseFact:
        row = self._require("visa_cases", visa_case_id)
        cycle_id = _value(row, "application_cycle_id")
        student_id = _value(row, "student_id")
        if not student_id and cycle_id is not None:
            student_id, _cycle = self._cycle_identity(cycle_id)
        return VisaCaseFact(
            id=row["id"], student_id=student_id, application_cycle_id=cycle_id,
            case_type=_value(row, "case_type"), visa_subclass=_value(row, "visa_subclass"),
            application_type=_value(row, "application_type"), status=_value(row, "status"),
            version=self._version(row, "updated_at", "created_at"),
        )

    def offer(self, offer_event_id: int) -> OfferFact:
        row = self._require("offer_received_events", offer_event_id)
        student_id, cycle = self._cycle_identity(row["application_cycle_id"])
        return OfferFact(
            id=row["id"], application_cycle_id=cycle.id,
            student_id=_value(row, "student_id", student_id),
            source_kind=_value(row, "source_kind"), school_id=_value(row, "school_id"),
            school_name=_value(row, "school_name_snapshot"),
            higher_education_offer_id=_value(row, "higher_education_offer_id"),
            version=self._version(row, "received_at", "created_at"),
        )

    def final_acceptance(self, acceptance_id: int) -> FinalAcceptanceFact:
        row = self._require("cycle_final_acceptances", acceptance_id)
        student_id, cycle = self._cycle_identity(row["application_cycle_id"])
        return FinalAcceptanceFact(
            id=row["id"], application_cycle_id=cycle.id,
            student_id=_value(row, "student_id", student_id),
            offer_received_event_id=row["offer_received_event_id"],
            school_id=_value(row, "school_id"),
            school_name=_value(row, "school_name_snapshot"),
            acceptance_status=_value(row, "acceptance_status", "active"),
            version=self._version(row, "updated_at", "selected_at", "created_at"),
        )

    def coe(self, coe_id: int) -> CoeFact:
        row = self._require("coe_oshc", coe_id)
        cycle_id = _value(row, "application_cycle_id")
        student_id = _value(row, "student_id")
        if not student_id and cycle_id is not None:
            student_id, _cycle = self._cycle_identity(cycle_id)
        return CoeFact(
            id=row["id"], application_cycle_id=cycle_id, student_id=student_id,
            provider=_value(row, "coe_provider", _value(row, "school_name")),
            course=_value(row, "coe_course"), start_date=_value(row, "coe_start_date"),
            file_path=_value(row, "file_path"),
            language_course_name=_value(row, "letter_language_course_name"),
            main_course_name=_value(row, "letter_main_course_name"),
            version=self._version(row, "created_at"),
        )

    def higher_education_application(self, application_id: int) -> HigherEducationApplicationFact:
        row = self._require("higher_education_applications", application_id)
        return HigherEducationApplicationFact(
            id=row["id"], application_cycle_id=row["application_cycle_id"],
            provider_id=row["provider_id"], course_type=_value(row, "course_type"),
            course_name=_value(row, "course_name"), intake=_value(row, "intake"),
            submission_channel=_value(row, "submission_channel"),
            master_agent_id=_value(row, "master_agent_id"), status=_value(row, "status"),
            version=self._version(row, "updated_at", "created_at"),
        )

    def higher_education_package_segment(
        self, segment_id: int
    ) -> HigherEducationPackageSegmentFact:
        row = self._require("higher_education_package_segments", segment_id)
        return HigherEducationPackageSegmentFact(
            id=row["id"], application_id=row["application_id"],
            teaching_provider_id=_value(row, "teaching_provider_id"),
            course_type=_value(row, "course_type"), course_name=_value(row, "course_name"),
            intake=_value(row, "intake"),
            version=self._version(row, "updated_at", "created_at"),
        )

    def document(self, document_id: int) -> DocumentFact:
        row = self._require("student_documents", document_id)
        return DocumentFact(
            id=row["id"], student_id=row["student_id"],
            document_type=row["document_type"], storage_path=row["file_path"],
            active=bool(_value(row, "is_active", 1)),
            source_reference=f"student_documents:{row['id']}",
            version=str(_value(row, "version", row["id"])),
        )

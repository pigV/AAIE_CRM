from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleResult:
    case_id: int | None
    student_id: str
    lifecycle_type: str
    case_status: str | None
    active_slot: int
    cycle_id: int | None = None
    cycle_status: str | None = None
    cycle_outcome: str | None = None
    cycle_sequence_no: int | None = None
    cycle_uid: str | None = None
    opened_at: str | None = None
    closed_at: str | None = None
    education_stream: str = "school_education"


@dataclass(frozen=True)
class LifecycleActivationDTO(LifecycleResult):
    """Stable public result returned only by explicit activation."""

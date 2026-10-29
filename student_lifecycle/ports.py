from typing import Protocol
import sqlite3

from application_cycle import ApplicationCycle


class LifecycleGateway(Protocol):
    def attach_case(
        self, connection: sqlite3.Connection, *, case_id: int, student_id: str,
        lifecycle_type: str, occurred_at: str, actor_user_id: int,
        education_stream: str = "school_education",
    ) -> ApplicationCycle: ...

    def close(
        self, connection: sqlite3.Connection, *, cycle_id: int, outcome: str,
        occurred_at: str, actor_user_id: int
    ) -> ApplicationCycle: ...


class ApplicationAdapter(Protocol):
    def attach_context(self, connection: sqlite3.Connection, **context): ...


class OfferAdapter(Protocol):
    def attach_context(self, connection: sqlite3.Connection, **context): ...


class CoeAdapter(Protocol):
    def attach_context(self, connection: sqlite3.Connection, **context): ...


class ContractContextAdapter(Protocol):
    def attach_context(self, connection: sqlite3.Connection, **context): ...


class VisaContextAdapter(Protocol):
    def attach_context(self, connection: sqlite3.Connection, **context): ...


class DocumentAdapter(Protocol):
    def list_for_context(self, connection: sqlite3.Connection, **context): ...


class SchoolEnrolmentAdapter(Protocol):
    def apply_transition(self, connection: sqlite3.Connection, **transition): ...

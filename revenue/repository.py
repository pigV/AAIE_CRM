"""Transactional persistence boundary for Revenue & Commission V1."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Callable, TypeVar
from uuid import uuid4


T = TypeVar("T")


class IdempotencyConflict(ValueError):
    pass


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def new_uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class RevenueRepository:
    """Owns Revenue transactions; never writes lifecycle authority tables."""

    def __init__(self, connection: sqlite3.Connection, now: Callable[[], str] | None = None):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._now = now or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
        )

    def now(self) -> str:
        return self._now()

    @contextmanager
    def transaction(self):
        name = f"revenue_{uuid4().hex}"
        self.connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.connection.execute(f"ROLLBACK TO {name}")
            self.connection.execute(f"RELEASE {name}")
            raise
        else:
            self.connection.execute(f"RELEASE {name}")

    def command(
        self,
        *,
        idempotency_key: str,
        command_type: str,
        aggregate_type: str,
        payload: dict,
        actor_user_id: int,
        operation: Callable[[], dict],
        replay_action: str | None = None,
        replay_result: Callable[[dict], dict] | None = None,
    ) -> dict:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        request_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        with self.transaction():
            prior = self.connection.execute(
                "SELECT command_type,request_hash,result_json FROM revenue_idempotency_receipts WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if prior:
                if prior["command_type"] != command_type or prior["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency key reused with different request")
                result = json.loads(prior["result_json"])
                if replay_result is not None:
                    result = replay_result(result)
                if replay_action:
                    occurred_at = self.now()
                    self.connection.execute(
                        """INSERT INTO revenue_audit_events(
                             uid,action,aggregate_type,aggregate_uid,after_json,actor_user_id,occurred_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            new_uid("audit"), replay_action, aggregate_type,
                            result.get("uid") or result.get("aggregate_uid") or "none",
                            canonical_json(result), actor_user_id, occurred_at,
                        ),
                    )
                return result
            result = operation()
            result_json = canonical_json(result)
            aggregate_uid = result.get("uid") or result.get("aggregate_uid")
            occurred_at = self.now()
            self.connection.execute(
                """INSERT INTO revenue_idempotency_receipts(
                     uid,idempotency_key,command_type,aggregate_type,aggregate_uid,
                     request_hash,result_json,actor_user_id,occurred_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_uid("idem"), key, command_type, aggregate_type, aggregate_uid,
                    request_hash, result_json, actor_user_id, occurred_at,
                ),
            )
            self.connection.execute(
                """INSERT INTO revenue_audit_events(
                     uid,action,aggregate_type,aggregate_uid,after_json,actor_user_id,occurred_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    new_uid("audit"), command_type, aggregate_type,
                    aggregate_uid or "none", result_json, actor_user_id, occurred_at,
                ),
            )
            self.connection.execute(
                """INSERT INTO revenue_outbox_events(
                     uid,aggregate_type,aggregate_uid,event_type,payload_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    new_uid("event"), aggregate_type, aggregate_uid or "none",
                    command_type, result_json, occurred_at,
                ),
            )
            return result

    def get_by_uid(self, table: str, uid: str) -> dict | None:
        if not table.startswith("revenue_") or not table.replace("_", "").isalnum():
            raise ValueError("invalid Revenue table")
        row = self.connection.execute(
            f'SELECT * FROM "{table}" WHERE uid=?', (uid,)
        ).fetchone()
        return dict(row) if row else None

    def require_by_uid(self, table: str, uid: str) -> dict:
        row = self.get_by_uid(table, uid)
        if row is None:
            raise ValueError(f"{table} record not found")
        return row

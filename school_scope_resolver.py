"""Deterministic read-only school city/sector scope resolution.

Legacy school rows can predate consistent ``schools.city_id`` population. A
missing city may fall back to the one active CRM city registered for the
school's state. Zero or multiple matches stay unresolved; no data is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ResolvedSchoolScope:
    school_id: int
    school_name: str
    city_id: int | None
    city_name: str | None
    sector_key: str | None
    city_source: str | None


def _table_columns(connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    }


def _state_key(value) -> str:
    return str(value or "").strip().upper()


def _unique_ids(school_ids: Sequence[int] | None) -> tuple[int, ...] | None:
    if school_ids is None:
        return None
    resolved: list[int] = []
    for raw_id in school_ids:
        try:
            school_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if school_id not in resolved:
            resolved.append(school_id)
    return tuple(resolved)


def _unique_active_cities_by_state(connection) -> dict[str, tuple[int, str]]:
    city_columns = _table_columns(connection, "school_cities")
    if "state" not in city_columns:
        return {}
    active_clause = "WHERE COALESCE(is_active,1)=1" if "is_active" in city_columns else ""
    rows = connection.execute(
        f"SELECT id,city_name,state FROM school_cities {active_clause} ORDER BY id"
    ).fetchall()
    grouped: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        state = _state_key(row["state"])
        city_name = str(row["city_name"] or "").strip()
        if state and city_name:
            grouped.setdefault(state, []).append((int(row["id"]), city_name))
    return {
        state: cities[0]
        for state, cities in grouped.items()
        if len(cities) == 1
    }


def resolve_school_scopes(
    connection,
    school_ids: Sequence[int] | None = None,
) -> tuple[ResolvedSchoolScope, ...]:
    """Resolve explicit scope first, then one unique active city per state."""

    selected_ids = _unique_ids(school_ids)
    if selected_ids == ():
        return ()
    school_columns = _table_columns(connection, "schools")
    state_expression = "s.state" if "state" in school_columns else "NULL"
    parameters: tuple[int, ...] = ()
    where_clause = ""
    if selected_ids is not None:
        placeholders = ",".join("?" for _ in selected_ids)
        where_clause = f"WHERE s.id IN ({placeholders})"
        parameters = selected_ids
    rows = connection.execute(
        f"""SELECT s.id,s.school_name,s.city_id,s.sector_key,
                   {state_expression} AS school_state,c.city_name
              FROM schools s
              LEFT JOIN school_cities c ON c.id=s.city_id
              {where_clause}
             ORDER BY s.id""",
        parameters,
    ).fetchall()
    fallback_by_state = _unique_active_cities_by_state(connection)
    resolved: list[ResolvedSchoolScope] = []
    for row in rows:
        city_id = int(row["city_id"]) if row["city_id"] is not None else None
        city_name = str(row["city_name"] or "").strip() or None
        city_source = "explicit" if city_id is not None and city_name else None
        if city_source is None:
            fallback = fallback_by_state.get(_state_key(row["school_state"]))
            if fallback is not None:
                city_id, city_name = fallback
                city_source = "state_unique"
            else:
                city_id, city_name = None, None
        sector_key = str(row["sector_key"] or "").strip().casefold() or None
        resolved.append(
            ResolvedSchoolScope(
                school_id=int(row["id"]),
                school_name=str(row["school_name"] or ""),
                city_id=city_id,
                city_name=city_name,
                sector_key=sector_key,
                city_source=city_source,
            )
        )
    return tuple(resolved)

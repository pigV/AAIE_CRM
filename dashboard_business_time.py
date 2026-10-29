"""Shared Australia/Melbourne calendar semantics for Dashboard KPIs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
import sqlite3
from zoneinfo import ZoneInfo


MELBOURNE_TIMEZONE = ZoneInfo("Australia/Melbourne")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class BusinessWindows:
    week_start: date
    week_end: date
    month_start: date
    next_month: date

    @property
    def week_parameters(self) -> tuple[str, str]:
        return self.week_start.isoformat(), self.week_end.isoformat()

    @property
    def month_parameters(self) -> tuple[str, str]:
        return self.month_start.isoformat(), self.next_month.isoformat()


def _next_month(month_start: date) -> date:
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def business_windows(today: date) -> BusinessWindows:
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    return BusinessWindows(
        week_start=week_start,
        week_end=week_start + timedelta(days=7),
        month_start=month_start,
        next_month=_next_month(month_start),
    )


def business_date_iso(value: object) -> str | None:
    """Return the value's Melbourne business date as ISO text.

    Date-only and timezone-naive database values are established local business
    values. Offset-aware timestamps (including ``Z``) are converted through
    ``zoneinfo`` so both DST and non-DST boundaries use IANA timezone rules.
    Invalid or blank legacy values are excluded from KPI windows.
    """

    text = str(value or "").strip()
    if not text:
        return None
    if _DATE_ONLY.fullmatch(text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MELBOURNE_TIMEZONE)
    else:
        parsed = parsed.astimezone(MELBOURNE_TIMEZONE)
    return parsed.date().isoformat()


def register_dashboard_business_date(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "dashboard_business_date",
        1,
        business_date_iso,
        deterministic=True,
    )

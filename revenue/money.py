"""Deterministic integer-only money and rate operations."""

from __future__ import annotations

from dataclasses import dataclass


def _integer(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


@dataclass(frozen=True)
class Money:
    cents: int
    currency: str = "AUD"

    def __post_init__(self):
        _integer(self.cents, "cents")
        currency = str(self.currency or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        object.__setattr__(self, "currency", currency)

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise ValueError("currency mismatch")
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise ValueError("currency mismatch")
        return Money(self.cents - other.cents, self.currency)


def apply_basis_points(cents: int, rate_basis_points: int) -> int:
    cents = _integer(cents, "cents")
    rate_basis_points = _integer(rate_basis_points, "rate_basis_points")
    if cents < 0 or not 0 <= rate_basis_points <= 10000:
        raise ValueError("money and basis points must be non-negative and bounded")
    return (cents * rate_basis_points + 5000) // 10000


def gst_from_inclusive(gross_cents: int, gst_rate_basis_points: int) -> tuple[int, int]:
    gross_cents = _integer(gross_cents, "gross_cents")
    gst_rate_basis_points = _integer(gst_rate_basis_points, "gst_rate_basis_points")
    if gross_cents < 0 or not 0 <= gst_rate_basis_points <= 10000:
        raise ValueError("gross and GST rate must be non-negative and bounded")
    denominator = 10000 + gst_rate_basis_points
    net_cents = (gross_cents * 10000 + denominator // 2) // denominator
    return net_cents, gross_cents - net_cents


def allocate_cents(total_cents: int, weights: list[int] | tuple[int, ...]) -> list[int]:
    total_cents = _integer(total_cents, "total_cents")
    normalized = [_integer(value, "weight") for value in weights]
    if total_cents < 0 or not normalized or any(value < 0 for value in normalized):
        raise ValueError("allocation inputs must be non-negative and non-empty")
    weight_total = sum(normalized)
    if weight_total <= 0:
        if total_cents == 0:
            return [0 for _ in normalized]
        raise ValueError("positive total requires a positive weight")
    allocations = [(total_cents * value) // weight_total for value in normalized]
    remainders = [(total_cents * value) % weight_total for value in normalized]
    residual = total_cents - sum(allocations)
    order = sorted(range(len(normalized)), key=lambda index: (-remainders[index], index))
    for index in order[:residual]:
        allocations[index] += 1
    return allocations

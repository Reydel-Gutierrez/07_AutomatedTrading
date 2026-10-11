"""Exact decimal arithmetic for cash and share quantities.

Cash and quantities are stored as decimal strings and summed in Python, so
the ledger never accumulates binary floating-point error.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
CENT = Decimal("0.01")
SHARE_STEP = Decimal("0.000001")  # Robinhood fractional precision (6 dp)
CASH_TOLERANCE = Decimal("0.01")
QTY_TOLERANCE = Decimal("0.000001")


def D(value: Any) -> Decimal:
    """Decimal from str/int/float/Decimal. Floats go through repr to avoid binary noise."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc


def opt_D(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return D(value)
    except ValueError:
        return None


def cents(value: Any) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_EVEN)


def shares_floor(value: Any) -> Decimal:
    """Round a share quantity down to broker precision (never sell more than held)."""
    return D(value).quantize(SHARE_STEP, rounding=ROUND_DOWN)


def s(value: Any) -> str:
    """Canonical storage string."""
    return format(D(value).normalize(), "f") if D(value) != ZERO else "0"

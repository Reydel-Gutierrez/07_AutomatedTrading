"""Friendly labels for dashboard display. Does not change stored enums."""

from __future__ import annotations

SLEEVE_LABELS = {
    "CORE_GROWTH": "Core Growth",
    "OPPORTUNISTIC": "Opportunistic",
    "TACTICAL": "Tactical",
    "SPECULATIVE": "Speculative",
    "CASH": "Cash",
}

ALLOCATION_ORDER = (
    "CORE_GROWTH",
    "OPPORTUNISTIC",
    "TACTICAL",
    "SPECULATIVE",
    "CASH",
)

UNAVAILABLE = "Not enough data"
HISTORY_COLLECTING = "Performance history is being collected."


def friendly_enum(value: str | None) -> str:
    if value is None or str(value).strip() == "":
        return "—"
    key = str(value).strip()
    if key in SLEEVE_LABELS:
        return SLEEVE_LABELS[key]
    return key.replace("_", " ").title()


def friendly_reason(value: str | None) -> str:
    if value is None or str(value).strip() == "":
        return "—"
    return str(value).replace("_", " ").strip()

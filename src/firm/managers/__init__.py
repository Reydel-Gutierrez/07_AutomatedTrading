"""Managers: Tim (core ETFs), Alex (systematic growth with research), Max (Phase 4)."""

from __future__ import annotations

from typing import Any, Mapping


def validate_mandate(manager_id: str, mandate: Mapping[str, Any]) -> list[str]:
    """Problems with a proposed mandate for this manager (empty when valid)."""
    if manager_id == "tim":
        from firm.managers.tim import validate_mandate as tim

        return tim(mandate)
    if manager_id == "alex":
        from firm.managers.alex import validate_mandate as alex

        return alex(mandate)
    return []

"""Firm-wide stops: kill switch, reconciliation hold, drawdown brake.

Kill switch: anyone (or the system) may engage; only the owner releases.
``AGENTIC_KILL_SWITCH=1`` in the Pi env forces it on regardless of the database.
Reconcile hold: set and cleared automatically by the reconciler; blocks BUYs.
Drawdown brake: set automatically; blocks BUYs until the owner resumes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from firm.clock import Clock, iso, utc_now
from firm.events import EventLog
from firm.settings import Settings


@dataclass(frozen=True)
class SwitchState:
    engaged: bool
    reason: str | None
    by: str | None
    at: str | None
    source: str  # 'env' | 'db' | 'off'


class Controls:
    def __init__(self, settings: Settings, events: EventLog, *, clock: Clock = utc_now, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.events = events
        self.clock = clock
        self._env = environ

    def _environ(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    # -- kill switch ----------------------------------------------------------
    def kill_switch(self) -> SwitchState:
        if str(self._environ().get("AGENTIC_KILL_SWITCH", "")).strip().lower() in {"1", "true", "yes", "on"}:
            return SwitchState(True, "AGENTIC_KILL_SWITCH set in the Pi environment", "env", None, "env")
        state = self.settings.get("kill_switch") or {}
        if state.get("engaged"):
            return SwitchState(True, state.get("reason"), state.get("by"), state.get("at"), "db")
        return SwitchState(False, None, None, None, "off")

    def engage(self, reason: str, *, by: str) -> None:
        if self.kill_switch().engaged:
            return
        self.settings.put("kill_switch", {"engaged": True, "reason": reason, "by": by, "at": iso(self.clock())}, by=by)
        self.events.emit("KILL_SWITCH_ENGAGED", f"Kill switch engaged: {reason}", actor=by, severity="critical", payload={"reason": reason})

    def release(self, *, by: str, note: str) -> None:
        if not note.strip():
            raise ValueError("a release note is required")
        prior = self.settings.get("kill_switch") or {}
        self.settings.put("kill_switch", {"engaged": False, "reason": None, "by": by, "at": iso(self.clock()), "released_note": note}, by=by)
        self.events.emit(
            "KILL_SWITCH_RELEASED",
            f"Kill switch released by {by}: {note}",
            actor=by,
            severity="warning",
            payload={"note": note, "previous_reason": prior.get("reason")},
        )

    # -- reconcile hold -------------------------------------------------------
    def reconcile_hold(self) -> dict[str, Any]:
        return self.settings.get("reconcile_hold") or {"active": False, "reasons": [], "since": None}

    def set_reconcile_hold(self, reasons: list[str], diff: dict[str, Any]) -> None:
        current = self.reconcile_hold()
        if current.get("active") and current.get("reasons") == reasons:
            return
        self.settings.put("reconcile_hold", {"active": True, "reasons": reasons, "since": current.get("since") or iso(self.clock()), "diff": diff}, by="reconciler")
        self.events.emit("RECONCILE_HOLD", "Buying paused: the books do not match the broker. " + "; ".join(reasons), actor="reconciler", severity="critical", payload={"reasons": reasons, "diff": diff})

    def clear_reconcile_hold(self) -> None:
        if not self.reconcile_hold().get("active"):
            return
        self.settings.put("reconcile_hold", {"active": False, "reasons": [], "since": None}, by="reconciler")
        self.events.emit("RECONCILE_CLEARED", "Books match the broker again; buying resumed.", actor="reconciler", severity="info")

    # -- drawdown brake -------------------------------------------------------
    def brake(self) -> dict[str, Any]:
        return self.settings.get("drawdown_brake") or {"engaged": False}

    def engage_brake(self, *, nav: str, hwm: str, drawdown: float) -> None:
        if self.brake().get("engaged"):
            return
        self.settings.put("drawdown_brake", {"engaged": True, "nav": nav, "hwm": hwm, "drawdown": drawdown, "at": iso(self.clock())}, by="risk")
        self.events.emit(
            "DRAWDOWN_BRAKE",
            f"Firm is {drawdown:.1%} below its high-water mark. New buys stopped until you resume.",
            actor="risk",
            severity="critical",
            payload={"nav": nav, "hwm": hwm},
        )

    def resume_brake(self, *, by: str, note: str) -> None:
        self.settings.put("drawdown_brake", {"engaged": False, "resumed_by": by, "note": note, "at": iso(self.clock())}, by=by)
        self.events.emit("DRAWDOWN_BRAKE_RESUMED", f"Buying resumed by {by}: {note}", actor=by)

    def buys_blocked(self) -> list[str]:
        reasons = []
        if self.kill_switch().engaged:
            reasons.append("KILL_SWITCH")
        if self.reconcile_hold().get("active"):
            reasons.append("RECONCILE_HOLD")
        if self.brake().get("engaged"):
            reasons.append("DRAWDOWN_BRAKE")
        return reasons

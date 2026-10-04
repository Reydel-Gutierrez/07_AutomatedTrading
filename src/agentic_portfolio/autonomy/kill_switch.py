"""Emergency kill switch. When engaged, no order of any kind is submitted.

Sources (either one engages it):

* the environment variable ``AGENTIC_KILL_SWITCH`` (truthy) — set on the Pi in
  ``/etc/agentic-portfolio/env``; can only be cleared by editing that file;
* the persisted file ``state/runtime/kill_switch.json`` — engaged by an operator
  (dashboard / ``scripts/kill_switch.py``) or automatically by the executor on an
  ambiguous submission, repeated failures, or an account-identity problem.

Release is always a human action. The runtime never releases itself.
An unreadable or corrupt state file is treated as ENGAGED (fail closed).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_portfolio.agent.persist import atomic_write_json
from agentic_portfolio.autonomy.policy import KILL_SWITCH_ENV, ExecutionPolicy, load_execution_policy
from agentic_portfolio.journal import append_jsonl

_TRUE = {"1", "true", "yes", "on", "engaged", "stop", "halt"}


@dataclass(frozen=True)
class KillSwitchStatus:
    engaged: bool
    source: str | None = None
    reason: str | None = None
    engaged_at: str | None = None
    engaged_by: str | None = None
    released_at: str | None = None
    released_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class KillSwitch:
    def __init__(
        self,
        root: Path,
        *,
        policy: ExecutionPolicy | None = None,
        environ: Mapping[str, str] | None = None,
        notify: Any = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.policy = policy or load_execution_policy(environ=environ)
        self._environ = environ
        self.notify = notify
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.path = self.root / self.policy.kill_switch_path
        self.journal = self.root / self.policy.kill_switch_journal

    def _env(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def now(self) -> datetime:
        stamp = self._now()
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    def _read_file(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"engaged": True, "reason": "kill_switch_state_unreadable", "source": "file"}
        if not isinstance(data, dict):
            return {"engaged": True, "reason": "kill_switch_state_invalid", "source": "file"}
        return data

    def status(self) -> KillSwitchStatus:
        raw_env = str(self._env().get(KILL_SWITCH_ENV) or "").strip().lower()
        if raw_env in _TRUE:
            return KillSwitchStatus(engaged=True, source=f"env:{KILL_SWITCH_ENV}", reason="environment kill switch")
        data = self._read_file()
        if not data:
            return KillSwitchStatus(engaged=False)
        return KillSwitchStatus(
            engaged=bool(data.get("engaged", True)),
            source=str(data.get("source") or "file"),
            reason=data.get("reason"),
            engaged_at=data.get("engaged_at"),
            engaged_by=data.get("engaged_by"),
            released_at=data.get("released_at"),
            released_by=data.get("released_by"),
        )

    def engaged(self) -> bool:
        return self.status().engaged

    def engage(self, reason: str, *, by: str = "operator", automatic: bool = False) -> KillSwitchStatus:
        current = self.status()
        if current.engaged and current.source and current.source.startswith("file"):
            return current
        stamp = self.now().isoformat()
        payload = {
            "engaged": True,
            "source": "file:automatic" if automatic else "file:operator",
            "reason": str(reason),
            "engaged_at": stamp,
            "engaged_by": by,
            "released_at": None,
            "released_by": None,
        }
        atomic_write_json(self.path, payload)
        append_jsonl({"type": "KILL_SWITCH_ENGAGED", "at": stamp, "reason": reason, "by": by, "automatic": automatic}, self.journal)
        self._emit("Kill switch ENGAGED", f"All order submission stopped: {reason}", payload)
        return self.status()

    def release(self, *, by: str, note: str | None = None) -> KillSwitchStatus:
        if not by or not str(by).strip():
            raise ValueError("release requires the name of the human releasing the kill switch")
        current = self.status()
        if current.source and current.source.startswith("env"):
            raise PermissionError(f"kill switch is engaged by {KILL_SWITCH_ENV}; clear it in the service environment")
        stamp = self.now().isoformat()
        payload = {
            "engaged": False,
            "source": "file:operator",
            "reason": note or current.reason,
            "engaged_at": current.engaged_at,
            "engaged_by": current.engaged_by,
            "released_at": stamp,
            "released_by": str(by),
        }
        atomic_write_json(self.path, payload)
        append_jsonl({"type": "KILL_SWITCH_RELEASED", "at": stamp, "by": by, "note": note, "previous_reason": current.reason}, self.journal)
        self._emit("Kill switch released", f"Released by {by}. {note or ''}".strip(), payload)
        return self.status()

    def _emit(self, title: str, body: str, payload: dict[str, Any]) -> None:
        if self.notify is None:
            return
        try:
            from agentic_portfolio.notify import NotificationKind

            self.notify.emit(NotificationKind.KILL_SWITCH, title=title, body=body, payload=payload)
        except Exception:  # noqa: BLE001 — notification failure must not block the switch
            pass

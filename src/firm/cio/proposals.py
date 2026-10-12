"""Structured proposals: validated when filed, re-validated and applied only on owner approval.

Kinds
  reallocate      moves [{from, to, amount}] between 'firm' (unallocated) and managers
  mandate_change  {manager_id, changes: {...}} → new mandate version (validated; stale if the mandate moved)
  mode_change     {manager_id, mode: PAUSED|SIM|INACTIVE}  (LIVE is never proposable)
  deposit_split   {split: {manager: fraction}}
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from firm.clock import iso, parse
from firm.ledger import FIRM, LedgerError
from firm.managers import validate_mandate
from firm.cio import CIO_NAME
from firm.money import D, cents

if TYPE_CHECKING:
    from firm.core import Firm

KINDS = ("reallocate", "mandate_change", "mode_change", "deposit_split")
PROPOSABLE_MODES = ("PAUSED", "SIM", "INACTIVE")
TTL = timedelta(days=7)


class ProposalError(ValueError):
    pass


class Proposals:
    def __init__(self, firm: "Firm") -> None:
        self.firm = firm

    # -- validation ------------------------------------------------------------
    def validate(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a normalized payload or raise ProposalError with a reason the CIO can act on."""
        f = self.firm
        managers = {m.id: m for m in f.registry.all()}
        if kind not in KINDS:
            raise ProposalError(f"kind must be one of {KINDS}")
        if kind == "reallocate":
            moves = payload.get("moves") or []
            if not moves:
                raise ProposalError("reallocate needs at least one move")
            out, pending = [], {}
            for mv in moves:
                src, dst = str(mv.get("from", "")).lower(), str(mv.get("to", "")).lower()
                try:
                    amount = cents(D(mv.get("amount")))
                except (ValueError, InvalidOperation):
                    raise ProposalError(f"amount {mv.get('amount')!r} is not a number") from None
                if amount <= 0 or src == dst or {src, dst} - ({FIRM} | set(managers)):
                    raise ProposalError(f"invalid move {mv}: use 'firm' (unallocated) or a manager id, positive amounts")
                pending[src] = pending.get(src, Decimal(0)) + amount
                out.append({"from": src, "to": dst, "amount": str(amount)})
            for src, amount in pending.items():
                free = f.ledger.free_cash("LIVE", src)
                if amount > free:
                    who = "unallocated cash" if src == FIRM else f"{managers[src].display_name}'s free cash"
                    raise ProposalError(f"{who} is ${cents(free)}; cannot move ${amount}. Invested money must be sold by the manager first.")
            return {"moves": out}
        manager_id = str(payload.get("manager_id", "")).lower()
        if kind in ("mandate_change", "mode_change") and manager_id not in managers:
            raise ProposalError(f"unknown manager {manager_id!r}")
        if kind == "mandate_change":
            changes = payload.get("changes") or {}
            if not isinstance(changes, dict) or not changes:
                raise ProposalError("mandate_change needs a non-empty 'changes' object")
            version, current = f.registry.mandate(manager_id)
            new = {**current, **changes}
            problems = validate_mandate(manager_id, new)
            if problems:
                raise ProposalError("; ".join(problems))
            diff = {k: {"from": current.get(k), "to": new.get(k)} for k in changes if current.get(k) != new.get(k)}
            if not diff:
                raise ProposalError("the proposed values equal the current mandate")
            return {"manager_id": manager_id, "base_version": version, "changes": changes, "diff": diff}
        if kind == "mode_change":
            mode = str(payload.get("mode", "")).upper()
            if mode == "LIVE":
                raise ProposalError("putting a manager LIVE is the owner's decision on the manager page; recommend it in conversation instead")
            if mode not in PROPOSABLE_MODES:
                raise ProposalError(f"mode must be one of {PROPOSABLE_MODES}")
            if mode in ("SIM",) and manager_id not in f.managers:
                raise ProposalError("that manager's strategy is not built yet")
            return {"manager_id": manager_id, "mode": mode, "from": managers[manager_id].mode}
        split = payload.get("split") or {}
        if set(split) - set(managers) or any(float(v) < 0 for v in split.values()) or sum(float(v) for v in split.values()) > 1.0001:
            raise ProposalError("split must name managers with non-negative fractions totaling at most 1")
        return {"split": {k: float(v) for k, v in split.items()}, "from": f.settings.get("deposit_split")}

    # -- lifecycle -------------------------------------------------------------
    def create(self, kind: str, title: str, payload: dict[str, Any], rationale: str, *, evidence: list[Any] | None = None, created_by: str = "cio", conversation_id: int | None = None) -> dict[str, Any]:
        if not title.strip() or not rationale.strip():
            raise ProposalError("a title and a rationale are required")
        normalized = self.validate(kind, payload)
        now = self.firm.clock()
        with self.firm.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO proposals (kind, title, payload_json, rationale, evidence_json, status, created_by, conversation_id, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (kind, title.strip(), json.dumps(normalized), rationale.strip(), json.dumps(evidence or []), "PENDING", created_by, conversation_id, iso(now), iso(now + TTL)),
            )
            pid = int(cur.lastrowid)
        self.firm.events.emit(
            "PROPOSAL_CREATED",
            f"{CIO_NAME + ' (CIO)' if created_by == 'cio' else created_by} proposes #{pid}: {title.strip()} (awaiting your decision)",
            actor=created_by,
            manager_id=normalized.get("manager_id"),
            payload={"proposal_id": pid, "kind": kind},
        )
        return self.get(pid)

    def get(self, pid: int) -> dict[str, Any]:
        row = self.firm.db.one("SELECT * FROM proposals WHERE id = ?", (pid,))
        if row is None:
            raise ProposalError(f"no proposal #{pid}")
        d = dict(row)
        for k in ("payload_json", "evidence_json", "result_json"):
            d[k.removesuffix("_json")] = json.loads(d.pop(k) or "null")
        return d

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.expire_stale()
        sql, params = "SELECT id FROM proposals", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        return [self.get(r["id"]) for r in self.firm.db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]

    def expire_stale(self) -> None:
        now = iso(self.firm.clock())
        for r in self.firm.db.query("SELECT id FROM proposals WHERE status = 'PENDING' AND expires_at < ?", (now,)):
            self._decide(r["id"], "EXPIRED", by="system", note="not decided within 7 days")

    def _decide(self, pid: int, status: str, *, by: str, note: str, result: Any = None) -> None:
        with self.firm.db.tx() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, decided_by = ?, decided_at = ?, decision_note = ?, result_json = ? WHERE id = ?",
                (status, by, iso(self.firm.clock()), note, json.dumps(result, default=str) if result is not None else None, pid),
            )

    def reject(self, pid: int, *, by: str, note: str = "") -> dict[str, Any]:
        p = self.get(pid)
        if p["status"] != "PENDING":
            raise ProposalError(f"proposal #{pid} is {p['status']}")
        self._decide(pid, "REJECTED", by=by, note=note)
        self.firm.events.emit("PROPOSAL_REJECTED", f"Proposal #{pid} declined: {p['title']}" + (f" ({note})" if note else ""), actor=by, payload={"proposal_id": pid})
        return self.get(pid)

    def approve(self, pid: int, *, by: str, note: str = "") -> dict[str, Any]:
        """Owner approval: re-validate against today's books, then apply deterministically."""
        p = self.get(pid)
        if p["status"] != "PENDING":
            raise ProposalError(f"proposal #{pid} is {p['status']}")
        if parse(p["expires_at"]) < self.firm.clock():
            self._decide(pid, "EXPIRED", by="system", note="expired before approval")
            raise ProposalError(f"proposal #{pid} expired")
        try:
            result = apply(self.firm, p, by=by, note=note)
        except (ProposalError, LedgerError, ValueError) as exc:
            self._decide(pid, "FAILED", by=by, note=note, result={"error": str(exc)})
            self.firm.events.emit("PROPOSAL_FAILED", f"Proposal #{pid} could not be applied: {exc}", actor=by, severity="warning", payload={"proposal_id": pid})
            raise ProposalError(str(exc)) from exc
        self._decide(pid, "APPLIED", by=by, note=note, result=result)
        self.firm.events.emit("PROPOSAL_APPLIED", f"Proposal #{pid} approved and applied: {p['title']}", actor=by, payload={"proposal_id": pid, "result": result})
        return self.get(pid)


def apply(firm: "Firm", proposal: dict[str, Any], *, by: str, note: str) -> dict[str, Any]:
    """The only code that turns a proposal into a change. Reached solely via owner approval."""
    kind, payload = proposal["kind"], proposal["payload"]
    label = f"proposal #{proposal['id']}: {proposal['title']}"
    current = Proposals(firm).validate(kind, payload if kind != "mandate_change" else {"manager_id": payload["manager_id"], "changes": payload["changes"]})
    if kind == "reallocate":
        for mv in current["moves"]:
            amount = D(mv["amount"])
            if mv["from"] != FIRM:
                firm.deallocate(mv["from"], amount, by=by, note=label)
            if mv["to"] != FIRM:
                firm.allocate(mv["to"], amount, by=by, note=label)
        return {"moves": current["moves"]}
    if kind == "mandate_change":
        if current["base_version"] != payload["base_version"]:
            raise ProposalError(f"the mandate changed since this proposal (v{payload['base_version']} → v{current['base_version']}); ask for a fresh proposal")
        _, mandate = firm.registry.mandate(payload["manager_id"])
        version = firm.registry.update_mandate(payload["manager_id"], {**mandate, **payload["changes"]}, by=by, note=f"{label}" + (f" ({note})" if note else ""))
        firm.request_run(payload["manager_id"], "mandate_changed")
        return {"version": version, "diff": current["diff"]}
    if kind == "mode_change":
        firm.registry.set_mode(payload["manager_id"], current["mode"], by=by, note=label)
        return {"mode": current["mode"]}
    firm.settings.put("deposit_split", current["split"], by=by)
    firm.events.emit("SETTINGS_CHANGED", f"Deposit split updated ({label}).", actor=by)
    return {"split": current["split"]}

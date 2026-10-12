"""Graham's capital pool: money the owner gives the CIO to distribute at his discretion.

The owner funds the pool like any manager (HQ → Capital, or the deposit split).
The CIO may then move pool money into managers and pull back what he gave, on his
own judgment of who is doing well. The owner's risk allocation is protected by
hard rules enforced here, not by the model:

- Only pool money moves. The CIO never touches money the owner allocated directly:
  a recall is limited to what the CIO granted that manager (net) and to the
  manager's free cash.
- A grant may not push a manager's share of the firm above the owner's cap for
  that manager (defaults: Tim 90%, Alex 30%, Max 10%).
- Only managers trading real money (LIVE) receive pool money; a simulated manager
  has nothing to invest it in.
- Minimum move $5, at most four moves per day, every move logged and alerted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from firm.clock import et, iso
from firm.ledger import LedgerError
from firm.money import ZERO, D, cents

POOL = "cio"
POOL_NAME = "Graham's pool"
DEFAULT_POLICY: dict[str, Any] = {"caps": {"tim": 0.9, "alex": 0.3, "max": 0.1}, "eligible_modes": ["LIVE"], "min_move_usd": 5, "max_moves_per_day": 4}


class PoolError(ValueError):
    pass


class CapitalPool:
    def __init__(self, firm: Any) -> None:
        self.firm = firm

    # -- reads -----------------------------------------------------------------
    def policy(self) -> dict[str, Any]:
        stored = self.firm.settings.get("cio_pool") or {}
        return {**DEFAULT_POLICY, **stored, "caps": {**DEFAULT_POLICY["caps"], **(stored.get("caps") or {})}}

    def grants(self) -> dict[str, Decimal]:
        return {k: D(v) for k, v in (self.firm.settings.get("cio_grants") or {}).items()}

    def _navs(self) -> tuple[dict[str, Decimal], Decimal]:
        try:
            navs = self.firm.firm_nav("LIVE")
        except Exception:  # noqa: BLE001 — no prices: judge shares on cash plus cost
            navs = dict(self.firm.ledger.balances("LIVE"))
            for bucket, syms in self.firm.ledger.holdings("LIVE").items():
                navs[bucket] = navs.get(bucket, ZERO) + sum((sum((l.open_cost for l in self.firm.ledger.open_lots("LIVE", bucket, s)), ZERO) for s in syms), ZERO)
        return navs, sum(navs.values(), ZERO)

    def _moves_today(self) -> int:
        start = et(self.firm.clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(self.firm.db.one("SELECT count(*) AS n FROM events WHERE type = 'CIO_ALLOCATION' AND ts >= ?", (iso(start),))["n"])

    def status(self) -> dict[str, Any]:
        pol = self.policy()
        grants = self.grants()
        navs, total = self._navs()
        managers = []
        for m in self.firm.registry.all():
            nav = navs.get(m.id, ZERO)
            cap = D(str(pol["caps"].get(m.id, 0)))
            share = nav / total if total > 0 else ZERO
            headroom = max(cap * total - nav, ZERO)
            managers.append({
                "id": m.id, "name": m.display_name, "mode": m.mode,
                "eligible": m.mode in pol["eligible_modes"] and m.id in self.firm.managers,
                "share": float(share), "cap": float(cap), "headroom": str(cents(headroom)),
                "granted": str(cents(grants.get(m.id, ZERO))),
                "recallable": str(cents(min(grants.get(m.id, ZERO), self.firm.ledger.free_cash("LIVE", m.id)))),
            })
        return {
            "cash": str(cents(self.firm.ledger.cash("LIVE", POOL))),
            "free": str(cents(self.firm.ledger.free_cash("LIVE", POOL))),
            "firm_nav": str(cents(total)),
            "moves_today": self._moves_today(),
            "policy": pol,
            "managers": managers,
        }

    # -- the only two ways pool money moves -------------------------------------
    def grant(self, manager_id: str, amount: Decimal, *, reason: str, by: str = "cio") -> dict[str, Any]:
        amt = cents(D(amount))
        pol = self.policy()
        row = self.firm.registry.get(manager_id)
        self._common(amt, reason, pol)
        if row.mode not in pol["eligible_modes"] or manager_id not in self.firm.managers:
            raise PoolError(f"{row.display_name} is {row.mode}; pool money goes only to managers trading real money ({', '.join(pol['eligible_modes'])})")
        free = self.firm.ledger.free_cash("LIVE", POOL)
        if amt > free:
            raise PoolError(f"the pool has ${cents(free)} free")
        navs, total = self._navs()
        cap = D(str(pol["caps"].get(manager_id, 0)))
        after = (navs.get(manager_id, ZERO) + amt) / total if total > 0 else Decimal(1)
        if after > cap + Decimal("0.0001"):
            room = max(cap * total - navs.get(manager_id, ZERO), ZERO)
            raise PoolError(f"{row.display_name} would hold {float(after):.1%} of the firm, above the owner's {float(cap):.0%} cap (room: ${cents(room)})")
        self._move(POOL, manager_id, amt, kind="CIO_GRANT", by=by, reason=reason)
        grants = self.grants()
        grants[manager_id] = grants.get(manager_id, ZERO) + amt
        self._save(grants, by)
        e = self.firm.events.emit(
            "CIO_ALLOCATION",
            f"Graham moved ${amt} from his pool to {row.display_name}: {reason}",
            actor=by, manager_id=manager_id, payload={"direction": "grant", "amount": str(amt), "reason": reason, "share_after": float(after)},
        )
        self.firm.request_run(manager_id, "allocation_changed")
        return {"ok": True, "event_id": e.id, "amount": str(amt), "share_after": float(after)}

    def recall(self, manager_id: str, amount: Decimal, *, reason: str, by: str = "cio") -> dict[str, Any]:
        amt = cents(D(amount))
        pol = self.policy()
        row = self.firm.registry.get(manager_id)
        self._common(amt, reason, pol)
        granted = self.grants().get(manager_id, ZERO)
        if amt > granted:
            raise PoolError(f"Graham can only recall what he granted {row.display_name} (${cents(granted)}); the owner's allocation is not his to move")
        free = self.firm.ledger.free_cash("LIVE", manager_id)
        if amt > free:
            raise PoolError(f"{row.display_name} has ${cents(free)} free cash; the rest is invested")
        self._move(manager_id, POOL, amt, kind="CIO_RECALL", by=by, reason=reason)
        grants = self.grants()
        grants[manager_id] = granted - amt
        self._save(grants, by)
        e = self.firm.events.emit(
            "CIO_ALLOCATION",
            f"Graham returned ${amt} from {row.display_name} to his pool: {reason}",
            actor=by, manager_id=manager_id, payload={"direction": "recall", "amount": str(amt), "reason": reason},
        )
        self.firm.request_run(manager_id, "allocation_changed")
        return {"ok": True, "event_id": e.id, "amount": str(amt)}

    # -- internals -------------------------------------------------------------
    def _common(self, amt: Decimal, reason: str, pol: dict[str, Any]) -> None:
        if amt < D(str(pol["min_move_usd"])):
            raise PoolError(f"moves must be at least ${pol['min_move_usd']}")
        if not reason or len(reason.strip()) < 10:
            raise PoolError("give a reason the owner can read")
        if self._moves_today() >= int(pol["max_moves_per_day"]):
            raise PoolError(f"the daily limit of {pol['max_moves_per_day']} pool moves is reached")

    def _move(self, src: str, dst: str, amt: Decimal, *, kind: str, by: str, reason: str) -> None:
        try:
            self.firm.ledger.transfer(src, dst, amt, kind=kind, by=by, memo=reason[:300])
        except LedgerError as exc:
            raise PoolError(str(exc)) from None

    def _save(self, grants: dict[str, Decimal], by: str) -> None:
        self.firm.settings.put("cio_grants", {k: str(cents(v)) for k, v in grants.items() if v > 0}, by=by)

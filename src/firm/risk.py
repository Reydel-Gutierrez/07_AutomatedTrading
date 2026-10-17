"""The owner's risk policy: what each department may lose, and the risk each agent carries.

Departments hold the owner's limits. Each agent's own risk settings (a manager's mandate, a
forex agent's risk envelope) must sit inside its department's limits; a change that would not
is refused. Expected returns and benchmarks are a scorecard only: nothing trades toward them.

Who changes what
  owner   department limits (HQ Risk page) and any agent's risk (the agent's office)
  Graham  reads all of it and may propose an agent's risk change; the owner decides
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from firm.core import Firm

KEY = "risk_policy"

DEPARTMENTS: dict[str, dict[str, Any]] = {
    "core": {
        "name": "Core Investments", "floor": "L5", "planned_capital": 30000, "expected_low": 0.06, "expected_high": 0.10,
        "benchmark": "S&P 500", "drawdown_limit": 0.25, "max_risk_per_trade": None,
        "note": "Long-term money. Tim stays invested through declines by your policy; the limit applies to Alex's own stop.",
    },
    "trading": {
        "name": "Equity Trading", "floor": "L3", "planned_capital": 5000, "expected_low": 0.08, "expected_high": 0.20,
        "benchmark": "S&P 500", "drawdown_limit": 0.20, "max_risk_per_trade": 0.02,
        "note": "Live swing offices. Risk per trade is position size × stop distance.",
    },
    "forex": {
        "name": "Forex Division", "floor": "L2", "planned_capital": 10000, "expected_low": 0.05, "expected_high": 0.15,
        "benchmark": "Cash (T-bills, about 4%)", "drawdown_limit": 0.15, "max_risk_per_trade": 0.015,
        "note": "If forex cannot beat cash over a year, it is not worth its risk.",
    },
    "options": {
        "name": "Options Trading", "floor": "L4", "planned_capital": 5000, "expected_low": 0.0, "expected_high": 0.30,
        "benchmark": "Cash (T-bills, about 4%)", "drawdown_limit": 0.30, "max_risk_per_trade": 0.03,
        "note": "Not open. Prove a strategy on paper first; at this size, defined-risk spreads only.",
    },
}

MANAGER_DEPT = {"tim": "core", "alex": "core", "max": "trading"}

# Owner-editable forex envelope fields and their hard bounds (the department ceiling applies on top).
FOREX_FIELDS: dict[str, tuple[float, float]] = {
    "risk_pct": (0.001, 0.03),
    "max_positions": (1, 5),
    "daily_loss": (0.005, 0.10),
    "drawdown_pause": (0.03, 0.50),
}


class RiskError(ValueError):
    pass


def policy(firm: "Firm") -> dict[str, dict[str, Any]]:
    stored = firm.settings.get(KEY) or {}
    return {k: {**v, **(stored.get(k) or {})} for k, v in DEPARTMENTS.items()}


def _num(x: Any, name: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        raise RiskError(f"{name} must be a number") from None


def manager_risk(manager_id: str, m: Mapping[str, Any]) -> dict[str, Any]:
    """The risk numbers in a manager's mandate, in one shape."""
    if manager_id == "max":
        per_trade = float(m.get("position_pct", 0)) * float(m.get("stop_pct", 0))
        return {"risk_per_trade": per_trade, "drawdown_stop": m.get("sleeve_drawdown_pause"), "daily_loss": m.get("daily_loss_pause"),
                "max_positions": m.get("max_positions"), "position_pct": m.get("position_pct"), "stop_pct": m.get("stop_pct")}
    if manager_id == "alex":
        return {"risk_per_trade": None, "drawdown_stop": m.get("sleeve_drawdown_pause"), "daily_loss": None,
                "max_positions": m.get("positions"), "position_pct": m.get("max_position_pct"), "stop_pct": None}
    return {"risk_per_trade": None, "drawdown_stop": None, "daily_loss": None, "max_positions": None, "position_pct": None, "stop_pct": None,
            "stays_invested": m.get("drawdown_policy") == "stay_invested"}


def mandate_problems(firm: "Firm", manager_id: str, m: Mapping[str, Any], pol: Mapping[str, Any] | None = None) -> list[str]:
    """Problems with a manager's mandate against its department's limits (empty when inside them)."""
    dept_id = MANAGER_DEPT.get(manager_id)
    if not dept_id:
        return []
    d = (pol or policy(firm))[dept_id]
    r = manager_risk(manager_id, m)
    out = []
    if r["drawdown_stop"] is not None and float(r["drawdown_stop"]) > float(d["drawdown_limit"]) + 1e-9:
        out.append(f"its drawdown stop ({float(r['drawdown_stop']):.0%}) is beyond {d['name']}'s limit of {float(d['drawdown_limit']):.0%}")
    if r["risk_per_trade"] is not None and d.get("max_risk_per_trade") is not None and r["risk_per_trade"] > float(d["max_risk_per_trade"]) + 1e-9:
        out.append(f"its risk per trade ({r['risk_per_trade']:.2%} = position size × stop) is beyond {d['name']}'s limit of {float(d['max_risk_per_trade']):.2%}")
    return out


def forex_changes(changes: Mapping[str, Any], current: Mapping[str, Any], pol: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an owner change to a forex agent's risk envelope; returns the normalized changes."""
    unknown = set(changes) - set(FOREX_FIELDS)
    if unknown:
        raise RiskError(f"only {', '.join(FOREX_FIELDS)} can be changed (not {', '.join(sorted(unknown))})")
    out: dict[str, Any] = {}
    for k, v in changes.items():
        lo, hi = FOREX_FIELDS[k]
        x = _num(v, k)
        if not lo <= x <= hi:
            raise RiskError(f"{k} must be between {lo:g} and {hi:g}")
        out[k] = int(x) if k == "max_positions" else round(x, 5)
    new = {**current, **out}
    d = pol["forex"]
    if d.get("max_risk_per_trade") is not None and float(new.get("risk_pct", 0)) > float(d["max_risk_per_trade"]) + 1e-9:
        raise RiskError(f"risk per trade {float(new['risk_pct']):.2%} is beyond the Forex Division's limit of {float(d['max_risk_per_trade']):.2%}; raise the department limit first")
    if float(new.get("drawdown_pause", 0)) > float(d["drawdown_limit"]) + 1e-9:
        raise RiskError(f"a {float(new['drawdown_pause']):.0%} drawdown stop is beyond the Forex Division's limit of {float(d['drawdown_limit']):.0%}")
    if not any(current.get(k) != v for k, v in out.items()):
        raise RiskError("those are the agent's current settings")
    return out


def _forex_agents() -> list[dict[str, Any]]:
    from firm.api import academy

    try:
        return [a for a in academy.overview().get("agents", []) if a.get("market") == "forex"]
    except Exception:  # noqa: BLE001 - the Academy may not be running; the page still shows the firm
        return []


def _stock_agents() -> list[dict[str, Any]]:
    from firm.api import equity_academy

    try:
        out = []
        for c in equity_academy.cards():
            d = equity_academy.detail(c["id"]) or {}
            params = ((d.get("versions") or [{}])[0]).get("params") or {}
            out.append({**c, "params": params})
        return out
    except Exception:  # noqa: BLE001
        return []


def check_policy(firm: "Firm", new: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate the owner's department limits; refuse ones that current agents already break."""
    cur = policy(firm)
    out: dict[str, dict[str, Any]] = {}
    for dept_id, body in new.items():
        if dept_id not in DEPARTMENTS:
            raise RiskError(f"unknown department {dept_id!r}")
        d = {**cur[dept_id]}
        for k in ("planned_capital", "expected_low", "expected_high", "drawdown_limit", "max_risk_per_trade"):
            if k in body:
                d[k] = None if body[k] is None and k == "max_risk_per_trade" else _num(body[k], k)
        if "benchmark" in body:
            d["benchmark"] = str(body["benchmark"])[:60]
        if not 0 <= float(d["planned_capital"]) <= 10_000_000:
            raise RiskError("planned capital must be between $0 and $10,000,000")
        if not -0.5 <= float(d["expected_low"]) <= float(d["expected_high"]) <= 2:
            raise RiskError(f"{d['name']}: the expected range must run low to high, between -50% and 200%")
        if not 0.02 <= float(d["drawdown_limit"]) <= 0.6:
            raise RiskError(f"{d['name']}: the drawdown limit must be between 2% and 60%")
        if d["max_risk_per_trade"] is not None and not 0.001 <= float(d["max_risk_per_trade"]) <= 0.05:
            raise RiskError(f"{d['name']}: risk per trade must be between 0.1% and 5%")
        out[dept_id] = {k: d[k] for k in ("planned_capital", "expected_low", "expected_high", "benchmark", "drawdown_limit", "max_risk_per_trade")}
    merged = {k: {**cur[k], **out.get(k, {})} for k in cur}
    over = []
    for mid in MANAGER_DEPT:
        try:
            _, m = firm.registry.mandate(mid)
        except KeyError:
            continue
        over += [f"{firm.registry.get(mid).display_name}: {p}" for p in mandate_problems(firm, mid, m, merged)]
    fx = merged["forex"]
    for a in _forex_agents():
        env = a.get("envelope") or {}
        if a.get("stage") in ("PAPER", "DEMO", "RESEARCH"):
            if fx.get("max_risk_per_trade") is not None and float(env.get("risk_pct") or 0) > float(fx["max_risk_per_trade"]) + 1e-9:
                over.append(f"{a['name']}: risk per trade {float(env['risk_pct']):.2%} is beyond the new limit")
            if float(env.get("drawdown_pause") or 0) > float(fx["drawdown_limit"]) + 1e-9:
                over.append(f"{a['name']}: drawdown stop {float(env['drawdown_pause']):.0%} is beyond the new limit")
    if over:
        raise RiskError("lower these agents first: " + "; ".join(over))
    return out


def overview(firm: "Firm") -> dict[str, Any]:
    """Everything on the HQ Risk page (and what Graham reads): limits, scorecard, every agent's risk."""
    from firm.api import views

    pol = policy(firm)
    meta = firm.settings.meta(KEY)
    managers = []
    for m in firm.registry.all():
        if m.id not in MANAGER_DEPT:
            continue
        version, mandate = firm.registry.mandate(m.id)
        book = "SIM" if m.mode == "SIM" else "LIVE"
        mood = views._mood(firm, m.id, book)
        sleeve = views._sleeve(firm, book, m.id, views.prices(firm, sorted(firm.ledger.holdings(book, m.id).get(m.id, {})))[0])
        managers.append({
            "id": m.id, "name": m.display_name, "kind": "manager", "department": MANAGER_DEPT[m.id], "mode": m.mode, "book": book,
            "capital": views.f(sleeve["nav"]), "return": mood["twr"], "drawdown": mood["drawdown"], "mandate_version": version,
            "risk": manager_risk(m.id, mandate), "problems": mandate_problems(firm, m.id, mandate, pol), "office": f"/managers/{m.id}",
        })
    academy = []
    for a in _forex_agents():
        env = a.get("envelope") or {}
        academy.append({
            "id": a["id"], "name": a["name"], "kind": "forex", "department": "forex", "stage": a["stage"], "style": a.get("style"),
            "color": (a.get("persona") or {}).get("color"), "capital": a.get("equity"), "return": a.get("return_pct"), "drawdown": a.get("max_drawdown"),
            "version": a.get("version"), "risk": {k: env.get(k) for k in FOREX_FIELDS}, "editable": a["stage"] != "RETIRED", "office": f"/academy/{a['id']}",
        })
    for a in _stock_agents():
        p = a.get("params") or {}
        academy.append({
            "id": a["id"], "name": a["name"], "kind": "stocks", "department": "trading", "stage": a["stage"], "style": "swing",
            "color": (a.get("persona") or {}).get("color"), "capital": a.get("equity"), "return": a.get("return_pct"), "drawdown": a.get("max_drawdown"),
            "version": a.get("version"),
            "risk": {"position_pct": p.get("position_pct"), "max_positions": p.get("max_positions") or p.get("top_n"), "stop_atr": p.get("stop_atr")},
            "editable": False, "office": f"/academy/{a['id']}",
        })
    total_planned = sum(float(d["planned_capital"]) for d in pol.values())
    return {
        "departments": [{"id": k, **v, "share": float(v["planned_capital"]) / total_planned if total_planned else 0.0} for k, v in pol.items()],
        "planned_total": total_planned,
        "managers": managers,
        "academy": academy,
        "updated": meta,
        "forex_fields": {k: list(v) for k, v in FOREX_FIELDS.items()},
    }

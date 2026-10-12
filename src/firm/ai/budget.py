"""AI spend ledger and per-agent monthly caps (owner-set in HQ → System).

Every model call is authorized against a worst-case cost before it is made
and recorded at its actual cost afterwards, so a restart can never reset the
month and one agent can never spend another agent's share.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from firm.clock import Clock, et, iso, utc_now
from firm.db import Database
from firm.money import ZERO, D
from firm.settings import Settings

# USD per million tokens: (input, output, cache read, cache write). Unknown models are
# priced at the most expensive rate so an unpriced model can never under-spend the cap.
PRICES: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (D("4.00"), D("20.00"), D("0.20"), D("5.00")),
    "claude-sonnet-5-5": (D("2.00"), D("10.00"), D("0.20"), D("2.50")),
    "claude-haiku-5-5": (D("0.10"), D("0.50"), D("0.01"), D("0.125")),
    "gpt-5.6-luna": (D("0.20"), D("1.20"), D("0.02"), D("0.20")),
    "gpt-5.6-terra": (D("2.00"), D("12.00"), D("0.20"), D("2.00")),
    "gpt-5.6-sol": (D("4.00"), D("20.00"), D("0.40"), D("4.00")),
    "scripted": (ZERO, ZERO, ZERO, ZERO),
}
DEFAULT_PRICE = (D("15.00"), D("75.00"), D("15.00"), D("18.75"))
MILLION = D(1_000_000)


def price(model: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    for key, value in PRICES.items():
        if model == key or model.startswith(key + "-"):
            return value
    return DEFAULT_PRICE


def cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> Decimal:
    pin, pout, pread, pwrite = price(model)
    return (pin * input_tokens + pout * output_tokens + pread * cache_read_tokens + pwrite * cache_write_tokens) / MILLION


@dataclass(frozen=True)
class AgentBudget:
    agent: str
    cap: Decimal
    spent: Decimal
    month: str

    @property
    def remaining(self) -> Decimal:
        return max(self.cap - self.spent, ZERO)


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, db: Database, settings: Settings, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock

    def month(self) -> str:
        return et(self.clock()).strftime("%Y-%m")

    def status(self, agent: str) -> AgentBudget:
        cfg = self.settings.get("ai_budget") or {}
        share = D(str((cfg.get("per_agent") or {}).get(agent, 0)))
        cap = D(str(cfg.get("monthly_usd", 0))) * share
        month = self.month()
        rows = self.db.query("SELECT cost FROM ai_usage WHERE month = ? AND agent = ?", (month, agent))
        return AgentBudget(agent, cap, sum((D(r["cost"]) for r in rows), ZERO), month)

    def authorize(self, agent: str, model: str, *, input_tokens_estimate: int, max_output_tokens: int) -> None:
        st = self.status(agent)
        # Worst case: every input token is a fresh cache write.
        worst = cost(model, 0, max_output_tokens, cache_write_tokens=input_tokens_estimate)
        if worst > st.remaining:
            raise BudgetExceeded(
                f"{agent.upper()} AI budget for {st.month}: ${st.spent:.2f} of ${st.cap:.2f} used; "
                f"this request could cost up to ${worst:.3f}."
            )

    def record(self, agent: str, *, provider: str, model: str, purpose: str, input_tokens: int, output_tokens: int, ok: bool, error: str | None = None, cache_read_tokens: int = 0, cache_write_tokens: int = 0, extra_cost: Decimal = ZERO) -> Decimal:
        amount = cost(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) + extra_cost  # extra: server-tool fees (web search)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO ai_usage (ts, month, agent, provider, model, purpose, input_tokens, output_tokens, cost, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (iso(self.clock()), self.month(), agent, provider, model, purpose, input_tokens + cache_read_tokens + cache_write_tokens, output_tokens, str(amount), 1 if ok else 0, error),
            )
        return amount

    def summary(self) -> dict:
        cfg = self.settings.get("ai_budget") or {}
        agents = {a: self.status(a) for a in (cfg.get("per_agent") or {})}
        return {
            "month": self.month(),
            "monthly_usd": cfg.get("monthly_usd"),
            "agents": {a: {"cap": float(b.cap), "spent": float(b.spent), "remaining": float(b.remaining)} for a, b in agents.items()},
            "total_spent": float(sum((b.spent for b in agents.values()), ZERO)),
        }

"""Alex's research desk: AI due diligence on the screen's top candidates.

Once a day (before Alex's run) the desk takes the highest-ranked names from the
systematic screen that lack a fresh note, and asks Claude to research each one
with web search. Each note is a verdict (approve or reject), a conviction, a
short thesis, the main risks, and the sources actually retrieved. Research can
only veto: Alex never buys a name the screen did not rank.

Safety and honesty:
- Every call is authorized against Alex's monthly AI budget first and recorded
  at its real cost (tokens plus web-search fees) afterwards.
- A cited source is kept only if it appeared in this call's search results, so
  a note cannot cite a page nobody retrieved.
- Notes and events are written by this module only; the model has no tools that
  touch the firm.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from firm.ai.budget import Budget, BudgetExceeded
from firm.ai.providers import ProviderError
from firm.clock import Clock, iso, utc_now
from firm.db import Database

log = logging.getLogger(__name__)

AGENT = "alex"
WEB_SEARCH_USD = Decimal("0.01")  # per search
MAX_SEARCHES = 6
MAX_OUTPUT_TOKENS = 8000
INPUT_ESTIMATE = 60_000  # worst-case context per note, including search results

VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Record your research verdict on the stock. Call exactly once, after researching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "conviction", "thesis", "risks", "sources"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "conviction": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "thesis": {"type": "string", "description": "Two to four sentences: why the business supports (or does not support) the price trend."},
            "risks": {"type": "array", "items": {"type": "string"}, "description": "The main risks, one short phrase each (at most five)."},
            "sources": {
                "type": "array",
                "description": "Pages you actually read in your web searches that support the verdict.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "url"],
                    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                },
            },
        },
    },
}

INSTRUCTIONS = """You are Alex's research analyst at The 537 Firm, a small private investment firm.
Alex runs a systematic strategy: he buys large US stocks with strong 12-month price momentum
that are trading above their 200-day average, equal-weighted, rebalanced monthly. Your job is
due diligence on one candidate the screen already ranked. You can only approve or reject it;
you cannot suggest other stocks.

Research the company's last ~90 days with web search: earnings and guidance, major news,
legal or regulatory actions, accounting or governance concerns, pending acquisitions, and
anything that would make the price trend unreliable.

Approve when the business news is consistent with the trend and nothing disqualifying is
pending. Reject when you find a concrete red flag, for example: an announced cash acquisition
of the company at a fixed price, an accounting investigation or restatement, a going-concern
warning, a guidance collapse the price has not absorbed, or an imminent binary event (court
ruling, regulatory decision) that could reverse the business. Uncertainty alone is not a
red flag; most large companies should be approved.

Rules: base everything on what your searches returned. Cite only pages you read. Do not
invent figures. Keep the thesis factual and short. When done, call submit_verdict once."""


@dataclass
class Verdict:
    verdict: str
    conviction: int
    thesis: str
    risks: list[str]
    sources: list[dict[str, str]]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    searches: int = 0
    dropped_sources: int = 0


class Researcher(Protocol):
    model: str

    def available(self) -> bool: ...
    def research(self, symbol: str, context: str) -> Verdict: ...


class AnthropicResearcher:
    """Claude with server-side web search and a strict verdict tool."""

    def __init__(self, *, api_key: str, model: str = "claude-opus-5-5", effort: str = "medium", client: Any = None, timeout: float = 300.0) -> None:
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self._client = client or (anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2) if api_key else None)

    def available(self) -> bool:
        return self._client is not None

    def research(self, symbol: str, context: str) -> Verdict:
        if self._client is None:
            raise ProviderError("ANTHROPIC_API_KEY is not configured", account_problem="auth")
        a = self._anthropic
        messages: list[dict[str, Any]] = [{"role": "user", "content": context}]
        seen: dict[str, str] = {}
        usage = {"in": 0, "out": 0, "read": 0, "write": 0, "searches": 0}
        nudged = False
        for _ in range(8):
            try:
                response = self._client.beta.messages.create(
                    model=self.model,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=INSTRUCTIONS,
                    messages=messages,
                    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES}, VERDICT_TOOL],
                    output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except a.AuthenticationError:
                raise ProviderError("Anthropic rejected the API key", account_problem="auth") from None
            except a.PermissionDeniedError:
                raise ProviderError("the Anthropic API key lacks permission for this model or web search", account_problem="auth") from None
            except a.BadRequestError as exc:
                text = str(getattr(exc, "message", exc))
                if "credit balance" in text.lower() or "billing" in text.lower():
                    raise ProviderError("Anthropic account is out of credits", account_problem="billing") from None
                raise ProviderError(f"Anthropic rejected the request: {text[:300]}") from None
            except (a.RateLimitError, a.APIStatusError, a.APIConnectionError, a.APITimeoutError) as exc:
                raise ProviderError(f"Anthropic API unavailable: {type(exc).__name__}") from None
            u = response.usage
            usage["in"] += int(getattr(u, "input_tokens", 0) or 0)
            usage["out"] += int(getattr(u, "output_tokens", 0) or 0)
            usage["read"] += int(getattr(u, "cache_read_input_tokens", 0) or 0)
            usage["write"] += int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            stu = getattr(u, "server_tool_use", None)
            usage["searches"] += int(getattr(stu, "web_search_requests", 0) or 0) if stu else 0
            for block in response.content:
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    for r in block.content:
                        url = getattr(r, "url", None)
                        if url:
                            seen[url] = getattr(r, "title", "") or url
            if response.stop_reason == "refusal":
                raise ProviderError("the model declined to research this name")
            call = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_verdict"), None)
            if call is not None:
                return _verdict(call.input, seen, usage, str(getattr(response, "model", self.model) or self.model))
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if nudged:
                break
            messages.append({"role": "user", "content": "Please call submit_verdict now with your verdict."})
            nudged = True
        raise ProviderError("the research call ended without a verdict")


def _verdict(raw: Mapping[str, Any], seen: Mapping[str, str], usage: Mapping[str, int], model: str) -> Verdict:
    data = raw if isinstance(raw, Mapping) else json.loads(str(raw))
    verdict = str(data.get("verdict"))
    if verdict not in ("approve", "reject"):
        raise ProviderError(f"invalid verdict {verdict!r}")
    cited = [s for s in data.get("sources") or [] if isinstance(s, Mapping) and s.get("url")]
    kept = [{"title": str(s.get("title") or seen[s["url"]])[:200], "url": str(s["url"])} for s in cited if s["url"] in seen]
    return Verdict(
        verdict=verdict,
        conviction=max(1, min(5, int(data.get("conviction") or 3))),
        thesis=str(data.get("thesis") or "")[:1200],
        risks=[str(r)[:200] for r in (data.get("risks") or [])][:5],
        sources=kept[:8],
        model=model,
        input_tokens=usage["in"],
        output_tokens=usage["out"],
        cache_read_tokens=usage["read"],
        cache_write_tokens=usage["write"],
        searches=usage["searches"],
        dropped_sources=len(cited) - len(kept),
    )


class Research:
    """Stored research notes. Newest note per symbol is the one that counts."""

    def __init__(self, db: Database, *, clock: Clock = utc_now) -> None:
        self.db = db
        self.clock = clock

    def add(self, manager_id: str, symbol: str, v: Verdict, *, rank: int | None, cost: Decimal) -> int:
        with self.db.tx() as conn:
            return int(
                conn.execute(
                    "INSERT INTO research_notes (manager_id, symbol, verdict, conviction, thesis, risks_json, sources_json, rank, model, searches, cost, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (manager_id, symbol, v.verdict, v.conviction, v.thesis, json.dumps(v.risks), json.dumps(v.sources), rank, v.model, v.searches, str(cost), iso(self.clock())),
                ).lastrowid
            )

    def latest(self, manager_id: str) -> dict[str, dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM research_notes WHERE id IN (SELECT max(id) FROM research_notes WHERE manager_id = ? GROUP BY symbol)",
            (manager_id,),
        )
        return {r["symbol"]: _note(r) for r in rows}

    def recent(self, manager_id: str, limit: int = 40) -> list[dict[str, Any]]:
        return [_note(r) for r in self.db.query("SELECT * FROM research_notes WHERE manager_id = ? ORDER BY id DESC LIMIT ?", (manager_id, limit))]

    def today_count(self, manager_id: str, since: str) -> int:
        return int(self.db.one("SELECT count(*) AS n FROM research_notes WHERE manager_id = ? AND created_at >= ?", (manager_id, since))["n"])


def _note(r: Any) -> dict[str, Any]:
    return {
        "id": r["id"], "symbol": r["symbol"], "verdict": r["verdict"], "conviction": r["conviction"], "thesis": r["thesis"],
        "risks": json.loads(r["risks_json"] or "[]"), "sources": json.loads(r["sources_json"] or "[]"), "rank": r["rank"],
        "model": r["model"], "searches": r["searches"], "cost": r["cost"], "created_at": r["created_at"],
    }


@dataclass
class DeskResult:
    researched: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    skipped: str | None = None
    bars_refreshed: int = 0


class ResearchDesk:
    def __init__(self, firm: Any, researcher: Researcher, budget: Budget, *, on_progress: Callable[[str | None], None] | None = None) -> None:
        self.firm = firm
        self.researcher = researcher
        self.budget = budget
        self.notes = Research(firm.db, clock=firm.clock)
        self.on_progress = on_progress or (lambda _s: None)

    def candidates(self, mandate: Mapping[str, Any]) -> list[tuple[int, str, dict[str, float]]]:
        """(rank, symbol, score) for the top screened names whose note is missing or stale."""
        from firm.managers.alex import Alex, fresh, screen

        closes = Alex().inputs(self.firm, mandate)["closes"]
        latest = self.notes.latest(AGENT)
        top = screen(closes, mandate)[: int(mandate.get("research_candidates", 16))]
        held = set(self.firm.ledger.holdings(self.firm.registry.get(AGENT).book or "SIM", AGENT).get(AGENT, {}))
        max_age = int(mandate.get("research_max_age_days", 30))
        out = [(i + 1, s, sc) for i, (s, sc) in enumerate(top) if not fresh(latest.get(s), self.firm.clock(), max_age)]
        # Holdings that dropped out of the top still get their notes refreshed, so exits stay informed.
        ranked = {s for s, _ in top}
        for s in sorted(held - ranked):
            if not fresh(latest.get(s), self.firm.clock(), max_age):
                out.append((0, s, {}))
        return out

    def run(self) -> DeskResult:
        result = DeskResult()
        _, mandate = self.firm.registry.mandate(AGENT)
        if not self.researcher.available():
            result.skipped = "AI provider not configured"
            return result
        from firm.managers.alex import score

        universe = list(mandate.get("universe") or [])
        sample = self.firm.market.recent_closes(universe, sessions=300)
        need = max(int(mandate.get("momentum_lookback_days", 252)) + 1, int(mandate.get("trend_sma_days", 200)))
        if sum(1 for s in universe if len(sample.get(s, [])) >= need) < len(universe) * 0.8:
            result.bars_refreshed = self.firm.market.refresh_bars(universe)
        todo = self.candidates(mandate)
        since = iso(self.firm.clock() - timedelta(hours=20))
        room = max(0, int(mandate.get("research_daily_limit", 12)) - self.notes.today_count(AGENT, since))
        if not todo:
            result.skipped = "every top candidate has a fresh note"
            return result
        if room == 0:
            result.skipped = "daily research limit reached"
            return result
        for rank, symbol, sc in todo[:room]:
            try:
                self.budget.authorize(AGENT, self.researcher.model, input_tokens_estimate=INPUT_ESTIMATE, max_output_tokens=MAX_OUTPUT_TOKENS * 2)
            except BudgetExceeded as exc:
                self.firm.events.emit("RESEARCH_PAUSED", f"Alex's research paused: {exc}", actor=AGENT, manager_id=AGENT, severity="warning")
                result.skipped = "budget"
                break
            self.on_progress(symbol)
            context = _context(symbol, rank, sc or score(sample.get(symbol) or [], mandate) or {})
            try:
                v = self.researcher.research(symbol, context)
            except ProviderError as exc:
                self.budget.record(AGENT, provider="anthropic", model=self.researcher.model, purpose="research", input_tokens=0, output_tokens=0, ok=False, error=str(exc)[:300])
                if exc.account_problem:
                    self.firm.events.emit("AI_PROVIDER_UNAVAILABLE", f"Alex's research could not reach the AI provider ({exc.account_problem}). Check the API key or credits.", actor=AGENT, manager_id=AGENT, severity="critical", payload={"problem": exc.account_problem})
                    break
                self.firm.events.emit("RESEARCH_FAILED", f"Alex could not finish research on {symbol}: {exc}", actor=AGENT, manager_id=AGENT, symbol=symbol, severity="warning")
                continue
            cost = self.budget.record(
                AGENT, provider="anthropic", model=v.model, purpose="research", input_tokens=v.input_tokens, output_tokens=v.output_tokens,
                cache_read_tokens=v.cache_read_tokens, cache_write_tokens=v.cache_write_tokens, ok=True, extra_cost=WEB_SEARCH_USD * v.searches,
            )
            note_id = self.notes.add(AGENT, symbol, v, rank=rank or None, cost=cost)
            label = "approved" if v.verdict == "approve" else "rejected"
            self.firm.events.emit(
                "RESEARCH_NOTE",
                f"Alex researched {symbol}: {label} (conviction {v.conviction}/5). {v.thesis[:180]}",
                actor=AGENT,
                manager_id=AGENT,
                symbol=symbol,
                payload={"note_id": note_id, "verdict": v.verdict, "conviction": v.conviction, "rank": rank or None, "sources": len(v.sources), "dropped_sources": v.dropped_sources, "cost": str(cost)},
            )
            result.researched.append(symbol)
            (result.approved if v.verdict == "approve" else result.rejected).append(symbol)
        self.on_progress(None)
        return result


def _context(symbol: str, rank: int, sc: Mapping[str, Any]) -> str:
    facts = []
    if rank:
        facts.append(f"The screen ranked {symbol} #{rank} today.")
    else:
        facts.append(f"{symbol} is currently held but has dropped out of the screen's top candidates.")
    if sc:
        facts.append(
            f"12-1 month momentum {sc['momentum']:+.1%}, annualized volatility {sc['vol']:.1%}, last close {sc['last']:.2f}"
            f" vs 200-day average {sc['sma']:.2f}."
        )
    return " ".join(facts) + f" Research {symbol} and call submit_verdict."

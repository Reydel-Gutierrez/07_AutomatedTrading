from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from firm.ai.providers import ProviderError
from firm.broker.types import Bar, Quote
from firm.core import Firm
from firm.managers import validate_mandate
from firm.managers.alex import DEFAULT_MANDATE, Alex, score, screen
from firm.managers.base import BookView, ManagerContext
from firm.money import D
from firm.research import AnthropicResearcher, Research, ResearchDesk, Verdict
from firm.runtime import build_research

from tests.firm_suite.fakes import FakeRobinhood

SMALL = {**DEFAULT_MANDATE, "universe": ["AAA", "BBB", "CCC", "DDD"], "positions": 2, "max_position_pct": 0.5, "research_candidates": 4}


def series(n=300, drift=0.001, start=100.0, wiggle=0.004):
    return [start * math.exp(drift * i + wiggle * math.sin(i)) for i in range(n)]


CLOSES = {
    "AAA": series(drift=0.0020),  # strongest
    "BBB": series(drift=0.0012),
    "CCC": series(drift=0.0006),
    "DDD": series(drift=-0.0010),  # downtrend: never eligible
}


def note(verdict="approve", days_old=1, now=None):
    now = now or __import__("datetime").datetime(2026, 10, 13, 15, 0, tzinfo=__import__("datetime").timezone.utc)
    return {"verdict": verdict, "conviction": 4, "created_at": (now - timedelta(days=days_old)).isoformat()}


def ctx(clock, *, cash="1000", holdings=None, research=None, closes=CLOSES, trigger="scheduled", last_run=None, mandate=SMALL, **extras):
    holdings = holdings or {}
    quotes = {s: Quote(s, bid=D(str(round(c[-1], 2))) - D("0.01"), ask=D(str(round(c[-1], 2))) + D("0.01"), last=D(str(round(c[-1], 2))), observed_at=clock(), close=None) for s, c in closes.items()}
    view = BookView(book="SIM", cash=D(cash), holdings={k: D(v) for k, v in holdings.items()}, quotes=quotes)
    return ManagerContext(manager_id="alex", book="SIM", now=clock(), trigger=trigger, mandate=mandate, view=view, last_run=last_run, extras={"closes": closes, "research": research or {}, **extras})


def test_screen_ranks_risk_adjusted_momentum_and_requires_an_uptrend():
    ranked = [s for s, _ in screen(CLOSES, SMALL)]
    assert ranked == ["AAA", "BBB", "CCC"]
    assert score(CLOSES["DDD"], SMALL)["trend"] is False
    assert score(CLOSES["AAA"][:100], SMALL) is None  # not enough history: no score, no trade


def test_no_research_means_no_buys(clock):
    r = Alex().run(ctx(clock))
    assert not r.intents and r.activity == "waiting for research"
    assert "no screened names have a fresh research approval" in r.summary


def test_buys_top_approved_names_equal_weight_and_research_can_veto(clock):
    research = {"AAA": note("reject"), "BBB": note(), "CCC": note()}
    r = Alex().run(ctx(clock, research=research))
    buys = {i.symbol: i.notional for i in r.intents}
    assert set(buys) == {"BBB", "CCC"}  # AAA ranked first but research rejected it
    assert all(v == D("490.00") for v in buys.values())  # 98% invested, 2 equal positions
    assert r.detail["last_rebalance_month"] == "2026-10"


def test_stale_research_does_not_count(clock):
    r = Alex().run(ctx(clock, research={"AAA": note(days_old=45), "BBB": note()}))
    assert [i.symbol for i in r.intents] == ["BBB"]


def test_trend_break_and_rejection_exit_immediately_between_rebalances(clock):
    broken = {**CLOSES, "BBB": CLOSES["BBB"][:-5] + [CLOSES["BBB"][-6] * 0.6] * 5}
    last = {"last_rebalance_month": "2026-10"}
    r = Alex().run(ctx(clock, cash="0", holdings={"AAA": "2", "BBB": "3"}, research={"AAA": note(), "BBB": note()}, closes=broken, last_run=last))
    assert [(i.symbol, i.side, i.quantity) for i in r.intents] == [("BBB", "sell", D("3"))]
    assert "trend broke" in r.intents[0].reason and r.rerun_after_minutes == 10
    r2 = Alex().run(ctx(clock, cash="0", holdings={"AAA": "2"}, research={"AAA": note("reject")}, last_run=last))
    assert r2.intents[0].symbol == "AAA" and "research now rejects" in r2.intents[0].reason


def test_holds_between_rebalances_without_idle_cash(clock):
    last = {"last_rebalance_month": "2026-10"}
    held = {"AAA": str(round(490 / CLOSES["AAA"][-1], 6)), "BBB": str(round(490 / CLOSES["BBB"][-1], 6))}
    r = Alex().run(ctx(clock, cash="20", holdings=held, research={"AAA": note(), "BBB": note()}, last_run=last))
    assert not r.intents and r.activity == "holding"


def test_mandate_validation():
    assert validate_mandate("alex", DEFAULT_MANDATE) == []
    assert validate_mandate("alex", {**DEFAULT_MANDATE, "universe": ["aapl"]})
    assert validate_mandate("alex", {**DEFAULT_MANDATE, "positions": 3, "max_position_pct": 0.1})


@pytest.fixture
def firm(db, clock):
    rh = FakeRobinhood(clock=clock)
    for sym, c in CLOSES.items():
        rh.prices[sym] = D(str(round(c[-1], 2)))
    f = Firm(db, reader=rh, writer=lambda: None, clock=clock, environ={})
    f.reconciler.bootstrap(rh)
    return f


def test_phase3_strategy_replaces_placeholder_but_keeps_risk_policy(firm):
    v, m = firm.registry.mandate("alex")
    assert m["universe"] and m["sleeve_drawdown_pause"] == 0.2 and m["drawdown_policy"] == "sleeve_pause"
    assert "status" not in m
    Firm(firm.db, reader=firm.market.reader, writer=lambda: None, clock=firm.clock, environ={})  # idempotent
    assert firm.registry.mandate("alex")[0] == v


def store(firm, clock):
    day0 = date(2026, 10, 12) - timedelta(days=len(CLOSES["AAA"]))
    for sym, c in CLOSES.items():
        firm.market.store_bars(sym, [Bar((day0 + timedelta(days=i)).isoformat(), D(str(round(x, 4))), D(str(round(x, 4))), D(str(round(x, 4))), D(str(round(x, 4))), 1) for i, x in enumerate(c)])


class FakeResearcher:
    model = "claude-opus-5-5"

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.asked = []

    def available(self):
        return True

    def research(self, symbol, context):
        self.asked.append((symbol, context))
        v = self.verdicts.get(symbol, "approve")
        if v == "error":
            raise ProviderError("timeout")
        return Verdict(v, 4, f"{symbol} thesis", ["risk"], [{"title": "t", "url": "https://example.com"}], self.model, input_tokens=10_000, output_tokens=1_000, searches=3)


def test_research_desk_then_simulated_trades_end_to_end(firm, clock):
    firm.registry.update_mandate("alex", {**firm.registry.mandate("alex")[1], **{k: SMALL[k] for k in ("universe", "positions", "max_position_pct", "research_candidates")}}, by="test", note="small universe")
    store(firm, clock)
    firm.allocate("alex", D("100"), by="reydel")
    firm.registry.set_mode("alex", "SIM", by="reydel")
    fake = FakeResearcher({"AAA": "reject", "CCC": "error"})
    desk = build_research(firm, {}, researcher=fake)
    res = desk.run()
    assert [s for s, _ in fake.asked] == ["AAA", "BBB", "CCC"] and "ranked AAA #1" in fake.asked[0][1]
    assert res.approved == ["BBB"] and res.rejected == ["AAA"]
    notes = Research(firm.db).latest("alex")
    assert notes["BBB"]["verdict"] == "approve" and notes["AAA"]["verdict"] == "reject" and "CCC" not in notes
    spent = desk.budget.status("alex").spent
    assert spent == (Decimal("0.04") + Decimal("0.02") + Decimal("0.03")) * 2  # 10k in + 1k out on Opus 5.5, plus $0.01 per search; two notes
    assert firm.events.last_of("RESEARCH_FAILED").symbol == "CCC"
    # A second pass researches only what is missing.
    fake.verdicts["CCC"] = "approve"
    assert desk.run().researched == ["CCC"]

    out = firm.run_manager("alex")
    assert out.ran, out.summary
    fills = [e for e in firm.events.recent(manager_id="alex", types=["ORDER_FILLED"])]
    assert sorted(e.symbol for e in fills) == ["BBB", "CCC"]  # AAA vetoed; DDD never screened
    assert firm.events.last_of("MANAGER_RUN", manager_id="alex").payload["activity"] == "investing"


def small_universe(firm):
    firm.registry.update_mandate("alex", {**firm.registry.mandate("alex")[1], **{k: SMALL[k] for k in ("universe", "positions", "max_position_pct", "research_candidates")}}, by="test", note="small universe")


def test_research_respects_budget(firm, clock):
    small_universe(firm)
    store(firm, clock)
    firm.settings.put("ai_budget", {"monthly_usd": 0.01, "per_agent": {"alex": 1.0}}, by="test")
    firm.registry.set_mode("alex", "SIM", by="reydel")
    fake = FakeResearcher({})
    res = build_research(firm, {}, researcher=fake).run()
    assert res.skipped == "budget" and fake.asked == []
    assert firm.events.last_of("RESEARCH_PAUSED") is not None


class FakeClient:
    """Anthropic client double: first a paused search turn, then the verdict."""

    def __init__(self):
        self.calls = []
        u = lambda searches: SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=searches))
        result = SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url="https://news.example/aaa", title="AAA beats")])
        self.responses = [
            SimpleNamespace(stop_reason="pause_turn", content=[result], usage=u(2), model="claude-opus-5-5"),
            SimpleNamespace(
                stop_reason="tool_use",
                usage=u(0),
                model="claude-opus-5-5",
                content=[SimpleNamespace(type="tool_use", name="submit_verdict", input={
                    "verdict": "approve", "conviction": 4, "thesis": "Strong quarter.", "risks": ["valuation"],
                    "sources": [{"title": "AAA beats", "url": "https://news.example/aaa"}, {"title": "made up", "url": "https://nowhere.example"}],
                })],
            ),
        ]
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def test_anthropic_researcher_continues_paused_turns_and_drops_unretrieved_sources():
    client = FakeClient()
    v = AnthropicResearcher(api_key="", client=client).research("AAA", "Research AAA")
    assert v.verdict == "approve" and v.searches == 2
    assert v.sources == [{"title": "AAA beats", "url": "https://news.example/aaa"}] and v.dropped_sources == 1
    assert client.calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert client.calls[1]["messages"][-1]["role"] == "assistant"  # paused turn continued, not restarted


def test_rebalance_sale_the_desk_did_not_carry_out_is_sent_again(clock):
    last = {"last_rebalance_month": "2026-10"}  # the month is already marked rebalanced
    held = {"AAA": "2", "BBB": "3", "CCC": "4"}
    research = {"AAA": note(), "BBB": note(), "CCC": note()}
    # Before the fix CCC was simply held until next month.
    r = Alex().run(ctx(clock, cash="0", holdings=held, research=research, last_run=last))
    assert not r.intents and r.activity == "holding"
    r = Alex().run(ctx(clock, cash="0", holdings=held, research=research, last_run=last, rebalance_exits=["CCC"]))
    assert [(i.symbol, i.side, i.quantity) for i in r.intents] == [("CCC", "sell", D("4"))]
    assert "left the target portfolio" in r.intents[0].reason and r.detail["last_rebalance_month"] == "2026-10"


def test_rebalance_retry_never_double_sells_or_sells_a_name_back_in_the_target(clock):
    last = {"last_rebalance_month": "2026-10"}
    research = {"AAA": note(), "BBB": note(), "CCC": note()}
    r = Alex().run(ctx(clock, cash="0", holdings={"AAA": "2", "BBB": "3", "CCC": "4"}, research=research, last_run=last, rebalance_exits=["CCC", "BBB"], open_sells=["CCC"]))
    assert not r.intents  # CCC has a sell working; BBB is back in the target
    # Once sold it is not held, so nothing is sent again.
    r = Alex().run(ctx(clock, cash="0", holdings={"AAA": "2", "BBB": "3"}, research=research, last_run=last, rebalance_exits=["CCC", "BBB"]))
    assert not r.intents


def test_deferred_rebalance_sells_are_resent_and_filled_once(firm, clock):
    from datetime import datetime, timezone

    m = {**firm.registry.mandate("alex")[1], **{k: SMALL[k] for k in ("universe", "max_position_pct", "research_candidates")}, "positions": 3}
    firm.registry.update_mandate("alex", m, by="test", note="three names")
    store(firm, clock)
    firm.allocate("alex", D("100"), by="reydel")
    firm.registry.set_mode("alex", "SIM", by="reydel")
    build_research(firm, {}, researcher=FakeResearcher({})).run()
    firm.run_manager("alex")
    assert sorted(firm.ledger.holdings("SIM", "alex")["alex"]) == ["AAA", "BBB", "CCC"]

    # Next morning, before the open: two positions, so CCC leaves the target. The desk defers the sale.
    clock.set(datetime(2026, 10, 14, 12, 56, tzinfo=timezone.utc))
    firm.registry.update_mandate("alex", {**m, "positions": 2}, by="test", note="two names")
    out = firm.run_manager("alex", trigger="mandate_changed")
    assert [(o["symbol"], o["side"], o["status"]) for o in out.orders] == [("CCC", "sell", "DEFERRED")]

    # The scheduled run after the open sends it again (the month is already marked rebalanced) and it fills.
    clock.set(datetime(2026, 10, 14, 14, 20, tzinfo=timezone.utc))
    out = firm.run_manager("alex")
    assert [(o["symbol"], o["side"], o["status"]) for o in out.orders] == [("CCC", "sell", "FILLED")]
    assert "CCC" not in firm.ledger.holdings("SIM", "alex")["alex"]

    # Filled: later runs never sell it again.
    clock.advance(minutes=10)
    out = firm.run_manager("alex")
    assert not [o for o in out.orders if o["side"] == "sell"]

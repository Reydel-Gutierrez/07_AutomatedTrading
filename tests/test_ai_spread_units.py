"""AI liquidity fields must not present $0.019 as 1.9%."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agentic_portfolio.ai.context import assemble_context
from agentic_portfolio.ai.identity import collect_candidate_facts, facts_from_payloads
from agentic_portfolio.ai.pipeline import run_candidate_pipeline
from agentic_portfolio.discovery.eligibility import hard_reject
from agentic_portfolio.discovery.snapshot import compute_spread_metrics
from agentic_portfolio.runtime import RuntimeMode
from agentic_portfolio.schemas import FactOrigin
from tests.conftest import ctx
from tests.test_ai_gateway import NOW, _gw
from tests.test_live_identity import _providers, live_equity_payloads, qual_etf_payloads
from tests.test_live_mode import _accounts, _fetcher, _portfolio, _positions, _quotes

QUAL_WEEKEND_BID = 222.24
QUAL_WEEKEND_ASK = 226.57
QUAL_DIAGNOSTIC_SPREAD = 0.019295470243532828
DOLLAR_LOOKALIKE = 0.019295470243532828


def _collect_keys(obj) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        keys.update(obj)
        for value in obj.values():
            keys |= _collect_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _collect_keys(item)
    return keys


def _qual_quote(*, bid, ask, last="223.610000", bid_time=None, ask_time=None):
    payloads = qual_etf_payloads()
    quote = payloads["quotes"]["data"]["results"][0]["quote"]
    quote["bid_price"] = f"{bid:.12f}".rstrip("0").rstrip(".") if isinstance(bid, float) else str(bid)
    quote["ask_price"] = f"{ask:.12f}".rstrip("0").rstrip(".") if isinstance(ask, float) else str(ask)
    quote["last_trade_price"] = last
    if bid_time:
        quote["venue_bid_time"] = bid_time
    if ask_time:
        quote["venue_ask_time"] = ask_time
    return payloads


def _assert_explicit_liquidity(blob: dict) -> None:
    keys = _collect_keys(blob)
    assert "spread" not in keys
    assert "bid_price" in blob
    assert "ask_price" in blob
    assert "absolute_spread_usd" in blob
    assert "spread_percent" in blob
    assert "spread_bps" in blob


def test_live_qual_spread_is_fraction_not_dollars():
    metrics = compute_spread_metrics(QUAL_WEEKEND_BID, QUAL_WEEKEND_ASK)
    assert metrics is not None
    assert metrics["absolute_spread_usd"] == QUAL_WEEKEND_ASK - QUAL_WEEKEND_BID
    assert abs(metrics["spread_percent"] - QUAL_DIAGNOSTIC_SPREAD) < 1e-15
    assert abs(metrics["spread_bps"] - QUAL_DIAGNOSTIC_SPREAD * 10000.0) < 1e-9
    assert metrics["absolute_spread_usd"] > 4.0
    assert 0.019 < metrics["spread_percent"] < 0.02
    assert 190 < metrics["spread_bps"] < 195


def test_dollar_spread_matching_diagnostic_number_is_not_1_point_9_percent():
    mid = 223.61
    bid = mid - DOLLAR_LOOKALIKE / 2.0
    ask = mid + DOLLAR_LOOKALIKE / 2.0
    metrics = compute_spread_metrics(bid, ask)
    assert metrics is not None
    assert abs(metrics["absolute_spread_usd"] - DOLLAR_LOOKALIKE) < 1e-12
    assert metrics["spread_percent"] < 0.001
    assert metrics["spread_bps"] < 2.0
    assert not (0.018 < metrics["spread_percent"] < 0.021)


def test_ai_context_does_not_expose_ambiguous_spread_for_dollar_lookalike():
    mid = 223.61
    bid = mid - DOLLAR_LOOKALIKE / 2.0
    ask = mid + DOLLAR_LOOKALIKE / 2.0
    snap, validation = facts_from_payloads("QUAL", _qual_quote(bid=bid, ask=ask), now=NOW)
    assert validation.eligible_for_ai is True
    facts = collect_candidate_facts(snap, now=NOW)
    ai_ctx = assemble_context("QUAL", ctx(500), now_iso=NOW.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=facts)
    _assert_explicit_liquidity(ai_ctx.liquidity)
    prompt = ai_ctx.to_prompt_dict()
    assert "spread" not in _collect_keys(prompt)
    absolute = ai_ctx.liquidity["absolute_spread_usd"]["value"]
    percent = ai_ctx.liquidity["spread_percent"]["value"]
    bps = ai_ctx.liquidity["spread_bps"]["value"]
    assert abs(absolute - DOLLAR_LOOKALIKE) < 1e-9
    assert percent < 0.001
    assert bps < 2.0
    assert "unit=usd" in ai_ctx.liquidity["absolute_spread_usd"]["notes"]
    assert "unit=fraction" in ai_ctx.liquidity["spread_percent"]["notes"]
    assert "unit=bps" in ai_ctx.liquidity["spread_bps"]["notes"]
    assert facts.get("absolute_spread_usd").origin is FactOrigin.DERIVED
    assert facts.get("spread_percent").origin is FactOrigin.DERIVED
    assert facts.get("spread_bps").origin is FactOrigin.DERIVED
    assert facts.get("spread_percent").as_of
    assert facts.get("spread_percent").freshness
    assert any("Do not treat $0.019 as 1.9%" in note for note in ai_ctx.notes)


def test_ai_context_weekend_qual_spread_is_labeled_percent_and_dollars():
    now = datetime(2026, 8, 31, 1, 6, tzinfo=timezone.utc)
    payloads = _qual_quote(
        bid=QUAL_WEEKEND_BID,
        ask=QUAL_WEEKEND_ASK,
        bid_time="2026-08-31T00:03:23.00981Z",
        ask_time="2026-08-31T00:03:23.00981Z",
    )
    snap, validation = facts_from_payloads("QUAL", payloads, now=now)
    assert validation.eligible_for_ai is True
    facts = collect_candidate_facts(snap, now=now)
    ai_ctx = assemble_context("QUAL", ctx(500), now_iso=now.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=facts)
    _assert_explicit_liquidity(ai_ctx.liquidity)
    absolute = ai_ctx.liquidity["absolute_spread_usd"]["value"]
    percent = ai_ctx.liquidity["spread_percent"]["value"]
    bps = ai_ctx.liquidity["spread_bps"]["value"]
    assert abs(absolute - 4.33) < 1e-9
    assert abs(percent - QUAL_DIAGNOSTIC_SPREAD) < 1e-12
    assert 190 < bps < 195
    assert absolute != percent
    assert ai_ctx.liquidity["bid_price"]["value"] == QUAL_WEEKEND_BID
    assert ai_ctx.liquidity["ask_price"]["value"] == QUAL_WEEKEND_ASK
    assert ai_ctx.liquidity["spread_percent"]["as_of"] == "2026-08-31T00:03:23.00981Z"
    assert "spread" not in _collect_keys(ai_ctx.to_prompt_dict())
    assert facts.get("last_price").freshness.value == "LAST_SESSION"
    assert facts.get("bid").freshness.value == "OFF_HOURS"
    assert facts.get("ask").freshness.value == "OFF_HOURS"
    assert facts.get("spread_percent").freshness.value == "INDICATIVE"
    assert facts.get("spread_percent").freshness.value != "LAST_SESSION"
    assert ai_ctx.liquidity["bid_price"]["freshness"] == "OFF_HOURS"
    assert ai_ctx.liquidity["spread_percent"]["freshness"] == "INDICATIVE"
    assert ai_ctx.liquidity["bid_price"]["session"] == "OFF_HOURS"
    assert ai_ctx.liquidity["spread_percent"]["session"] == "INDICATIVE"
    assert ai_ctx.market["last"]["freshness"] == "LAST_SESSION"
    assert ai_ctx.market["bid"]["freshness"] == "OFF_HOURS"


def test_sunday_qual_bid_ask_not_inherited_from_friday_last_trade():
    now = datetime(2026, 8, 31, 1, 6, tzinfo=timezone.utc)
    payloads = _qual_quote(bid=QUAL_WEEKEND_BID, ask=QUAL_WEEKEND_ASK)
    snap, validation = facts_from_payloads("QUAL", payloads, now=now)
    facts = collect_candidate_facts(snap, now=now)
    assert validation.eligible_for_ai is True
    assert facts.get("last_price").freshness.value == "LAST_SESSION"
    assert facts.get("last_price").as_of.startswith("2026-08-28")
    assert facts.get("bid").value == QUAL_WEEKEND_BID
    assert facts.get("ask").value == QUAL_WEEKEND_ASK
    assert facts.get("bid").freshness.value == "OFF_HOURS"
    assert facts.get("ask").freshness.value == "OFF_HOURS"
    assert facts.get("spread_percent").freshness.value == "INDICATIVE"
    assert facts.get("absolute_spread_usd").freshness.value == "INDICATIVE"
    assert facts.get("spread_bps").freshness.value == "INDICATIVE"
    assert facts.get("spread_percent").freshness.value != "LAST_SESSION"
    assert facts.get("bid").session == "OFF_HOURS"
    assert facts.get("spread_percent").session == "INDICATIVE"
    assert "indicative_context_only" in facts.get("bid").notes
    assert "indicative_off_hours_not_regular_session" in facts.get("spread_percent").notes
    ai_ctx = assemble_context("QUAL", ctx(500), now_iso=now.isoformat(), runtime_mode=RuntimeMode.LIVE, instrument_facts=facts)
    assert ai_ctx.liquidity["spread_percent"]["value"] is not None
    assert ai_ctx.liquidity["bid_price"]["freshness"] == "OFF_HOURS"


def test_scripted_ai_prompt_cannot_confuse_dollar_spread_with_percent(tmp_path):
    from agentic_portfolio.live.engine import refresh_live_portfolio

    mid = 91.50
    bid = mid - DOLLAR_LOOKALIKE / 2.0
    ask = mid + DOLLAR_LOOKALIKE / 2.0
    payloads = live_equity_payloads()
    quote = payloads["quotes"]["data"]["results"][0]["quote"]
    quote["bid_price"] = str(bid)
    quote["ask_price"] = str(ask)
    snap, validation = facts_from_payloads("QCOR", payloads, now=NOW)
    assert validation.eligible_for_ai is True
    refresh = refresh_live_portfolio(
        _fetcher(accounts=_accounts(), portfolio=_portfolio(), positions=_positions(), quotes=_quotes(("SPY", 500.0))),
        now=NOW,
        root=tmp_path,
        persist=True,
    )
    gw = _gw(tmp_path, _providers())
    gw.runtime_mode = RuntimeMode.LIVE.value
    result = run_candidate_pipeline(
        [snap],
        refresh.context,
        gw,
        runtime_mode=RuntimeMode.LIVE,
        root=tmp_path,
        now=NOW,
        snapshot=refresh.snapshot,
        snapshot_id=refresh.snapshot_id,
        skip_universe_discovery=True,
    )
    assert result.placement_attempted is False
    provider = gw.providers["openai"]
    assert provider.calls
    user_payload = json.loads(provider.calls[0].messages[1]["content"])
    keys = _collect_keys(user_payload)
    assert "spread" not in keys
    liq = user_payload["liquidity"]
    assert abs(liq["absolute_spread_usd"]["value"] - DOLLAR_LOOKALIKE) < 1e-9
    assert liq["spread_percent"]["value"] < 0.001
    assert liq["spread_bps"]["value"] < 3.0
    assert result.ai_calls > 0


def test_diagnostic_prints_explicit_spread_units(capsys):
    from agentic_portfolio.adapters.robinhood_read import MappingReadOnlyFetcher
    from scripts.check_live_candidate_facts import main

    payloads = _qual_quote(bid=QUAL_WEEKEND_BID, ask=QUAL_WEEKEND_ASK)
    code = main(["QUAL"], fetcher=MappingReadOnlyFetcher.from_payloads("QUAL", payloads), now=NOW)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert "spread" not in report
    assert "spread" not in report["provenance"]
    assert abs(report["absolute_spread_usd"]["value"] - 4.33) < 1e-9
    assert abs(report["spread_percent"]["value"] - QUAL_DIAGNOSTIC_SPREAD) < 1e-12
    assert 190 < report["spread_bps"]["value"] < 195
    assert report["spread_percent"]["notes"]
    assert report["ai_provider_called"] is False
    assert report["ai_cost"] == 0
    assert report["quote"]["freshness"] == "LAST_SESSION"
    assert report["bid_price"]["freshness"] == "OFF_HOURS"
    assert report["spread_percent"]["freshness"] == "INDICATIVE"
    assert report["spread_percent"]["freshness"] != "LAST_SESSION"


def test_weekend_qual_fraction_still_eligible_under_existing_spread_rule():
    snap, validation = facts_from_payloads(
        "QUAL",
        _qual_quote(bid=QUAL_WEEKEND_BID, ask=QUAL_WEEKEND_ASK),
        now=NOW,
    )
    assert snap.spread_pct is not None
    assert 0.019 < snap.spread_pct < 0.02
    assert validation.eligible_for_ai is True
    reason, _evidence, _signals = hard_reject(snap)
    assert reason != "extreme_spread"

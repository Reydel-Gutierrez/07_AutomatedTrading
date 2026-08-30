"""One-shot read-only live discovery run. Never calls order or account-mutation tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.classification import classify
from agentic_portfolio.adapters.robinhood_read import (
    RobinhoodSecurityBundle,
    adapt_classification_evidence,
    adapt_liquidity_evidence,
)
from agentic_portfolio.context import build_context
from agentic_portfolio.discovery.engine import run_discovery
from agentic_portfolio.discovery.snapshot import SecuritySnapshot
from agentic_portfolio.discovery.store import CandidateStore, DiscoveryRunStore, ResearchQueue
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.schemas import LiquidityEvidence, MarketRegime, MarketRegimeStatus, to_dict

NOW = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]


def _wrap(symbol, **fields):
    return {"data": {"results": [{"symbol": symbol, **fields}]}}


def _cls_bundle(symbol, name, description, sector, industry, **quote):
    bundle = RobinhoodSecurityBundle(
        symbol=symbol,
        tradability=_wrap(symbol, name=name, state="active", tradeable=True),
        fundamentals=_wrap(symbol, description=description, sector=sector, industry=industry),
        quotes=_wrap(symbol, **quote) if quote else None,
        observed_at=TS,
        source_version="robinhood_live_readonly",
    )
    ev = adapt_classification_evidence(bundle)
    liq = adapt_liquidity_evidence(bundle)
    cl = classify(symbol, ev)
    cl.liquidity = liq
    return cl, liq, ev


def snap(
    symbol,
    *,
    name,
    description,
    sector,
    industry,
    price,
    prev,
    mcap,
    pe,
    high52,
    avg_vol,
    volume,
    bid,
    ask,
    sources,
    revenue=None,
    ni=None,
    nm=None,
    r21=None,
    r5=None,
    sma50=None,
    sma200=None,
    rsi=None,
    vol_vs=None,
    earn_days=None,
    headlines=None,
    instrument_kind=None,
):
    quote_kw = {"last_trade_price": price, "previous_close": prev}
    # Weekend/after-hours quotes can be unusably wide. Discovery must not
    # hard-reject on stale spread when last trade is usable RTH data.
    spread = ((ask - bid) / ((ask + bid) / 2.0)) if ask and bid and (ask + bid) else None
    if spread is not None and spread > 0.03:
        bid, ask, spread = None, None, None
    if bid is not None and ask is not None:
        quote_kw["bid_price"] = bid
        quote_kw["ask_price"] = ask
    cl, liq, ev = _cls_bundle(symbol, name, description, sector, industry, **quote_kw)
    liq = LiquidityEvidence(
        recent_dollar_volume=(avg_vol or 0) * price if avg_vol else None,
        bid_ask_spread_pct=spread,
        average_volume_proxy=(avg_vol or 0) * price if avg_vol else None,
        status="PARTIAL",
    )
    dd = ((high52 - price) / high52) if high52 and price else None
    return SecuritySnapshot(
        symbol=symbol,
        observed_at=TS,
        sources=sources,
        name=name,
        instrument_kind=ev.instrument_kind or instrument_kind,
        tradable=True,
        trade_state="active",
        current_price=price,
        previous_close=prev,
        bid=bid,
        ask=ask,
        volume=volume,
        market_cap=mcap,
        pe_ratio=pe,
        sector=sector,
        industry=industry,
        description=description,
        average_volume=avg_vol,
        high_52_week=high52,
        revenue_periods=list(revenue or []),
        net_income_periods=list(ni or []),
        net_margin_periods=[x / 100.0 if x is not None and abs(x) > 1 else x for x in (nm or [])],
        rsi=rsi,
        sma_50=sma50,
        sma_200=sma200,
        return_5d=r5,
        return_21d=r21,
        drawdown_from_52w_high=dd,
        volume_vs_avg=(volume / avg_vol) if volume and avg_vol else vol_vs,
        earnings_upcoming_days=earn_days,
        news_headlines=list(headlines or []),
        is_leveraged=ev.is_leveraged,
        is_inverse=ev.is_inverse,
        classification=cl,
        liquidity=liq,
        evidence_refs=["get_equity_fundamentals", "get_equity_quotes", "get_equity_tradability", "get_financials"],
    )


def main() -> None:
    snapshots = [
        snap("SPY", name="SPDR S&P 500 ETF Trust", description="SPY tracks a market cap-weighted index of US large- and mid-cap stocks selected by the S&P Committee.", sector="Miscellaneous", industry="Investment Trusts Or Mutual Funds", price=769.39, prev=771.10, mcap=8.14e11, pe=26.24, high52=779.37, avg_vol=3.63e7, volume=3.67e7, bid=768.66, ask=770.06, sources=["search", "get_popular_watchlists", "get_equity_fundamentals"], instrument_kind="etf"),
        snap("VOO", name="Vanguard S&P 500 ETF", description="The fund is passively managed to hold large-cap US stocks selected by an S&P Committee.", sector="Miscellaneous", industry="Investment Trusts Or Mutual Funds", price=707.22, prev=708.75, mcap=1.05e12, pe=27.37, high52=716.39, avg_vol=7.13e6, volume=8.08e6, bid=707.28, ask=707.79, sources=["search", "get_popular_watchlists"], instrument_kind="etf"),
        snap("VTI", name="Vanguard Total Stock Market ETF", description="The fund seeks to track a market-cap-weighted portfolio that provides total market exposure to the US equity space.", sector="Miscellaneous", industry="Investment Trusts Or Mutual Funds", price=379.32, prev=380.63, mcap=6.93e11, pe=28.67, high52=385.12, avg_vol=2.99e6, volume=2.82e6, bid=379.36, ask=380.00, sources=["search", "get_popular_watchlists"], instrument_kind="etf"),
        snap("MSFT", name="Microsoft", description="Microsoft develops software, cloud, and devices.", sector="Technology Services", industry="Packaged Software", price=513.67, prev=505.06, mcap=3.81e12, pe=28.62, high52=553.72, avg_vol=2.32e7, volume=2.92e7, bid=507.33, ask=517.00, sources=["get_watchlist_items", "get_popular_watchlists", "get_financials"], revenue=[90.007e9, 82.886e9, 81.273e9, 77.673e9, 76.441e9], ni=[35.766e9, 31.778e9, 38.458e9, 27.747e9, 27.233e9], nm=[39.74, 38.34, 47.32, 35.72, 35.63]),
        snap("AAPL", name="Apple", description="Apple designs and sells smartphones, computers, and wearables.", sector="Electronic Technology", industry="Telecommunications Equipment", price=319.65, prev=314.58, mcap=4.67e12, pe=36.65, high52=344.57, avg_vol=3.96e7, volume=3.86e7, bid=316.94, ask=320.26, sources=["get_watchlist_items", "get_popular_watchlists", "get_financials"], revenue=[109.417e9, 111.184e9, 143.756e9, 102.466e9, 94.036e9], ni=[29.789e9, 29.578e9, 42.097e9, 27.466e9, 23.434e9], nm=[27.23, 26.60, 29.28, 26.80, 24.92]),
        snap("JNJ", name="Johnson & Johnson", description="J&J researches, develops, and sells healthcare products.", sector="Health Technology", industry="Pharmaceuticals: Major", price=268.04, prev=265.77, mcap=6.46e11, pe=31.07, high52=276.47, avg_vol=6.42e6, volume=5.77e6, bid=268.00, ask=274.50, sources=["get_watchlists", "get_financials"], revenue=[25.31e9, 24.062e9, 24.564e9, 23.993e9, 23.743e9], ni=[5.534e9, 5.235e9, 5.116e9, 5.152e9, 5.537e9], nm=[21.86, 21.76, 20.83, 21.47, 23.32]),
        snap("KO", name="Coca-Cola", description="Coca-Cola manufactures and markets non-alcoholic beverages.", sector="Consumer Non-Durables", industry="Beverages: Non-Alcoholic", price=89.65, prev=89.06, mcap=3.86e11, pe=27.01, high52=92.49, avg_vol=1.38e7, volume=9.89e6, bid=89.16, ask=90.25, sources=["get_watchlists", "get_financials"], revenue=[13.38e9, 12.472e9, 11.822e9, 12.455e9, 12.535e9], ni=[4.425e9, 3.924e9, 2.271e9, 3.696e9, 3.810e9], nm=[33.07, 31.46, 19.21, 29.67, 30.39]),
        snap("COST", name="Costco", description="Costco operates membership warehouses.", sector="Retail Trade", industry="Specialty Stores", price=945.90, prev=934.66, mcap=4.19e11, pe=47.55, high52=1096.50, avg_vol=1.80e6, volume=1.40e6, bid=940.00, ask=953.00, sources=["get_popular_watchlists", "get_financials"], revenue=[70.527e9, 69.597e9, 67.307e9, 86.156e9, 63.205e9], ni=[2.192e9, 2.035e9, 2.001e9, 2.610e9, 1.903e9], nm=[3.11, 2.92, 2.97, 3.03, 3.01]),
        snap("WMT", name="Walmart", description="Walmart engages in retail and wholesale.", sector="Retail Trade", industry="Specialty Stores", price=103.11, prev=102.63, mcap=8.21e11, pe=37.35, high52=135.16, avg_vol=3.41e7, volume=1.94e7, bid=102.39, ask=103.21, sources=["get_earnings_calendar", "get_popular_watchlists", "get_financials"], revenue=[187.937e9, 177.751e9, 190.656e9, 179.496e9, 177.402e9], ni=[6.366e9, 5.330e9, 4.237e9, 6.143e9, 7.026e9], nm=[3.39, 3.00, 2.22, 3.42, 3.96]),
        snap("BRK.B", name="Berkshire Hathaway Class B", description="Berkshire Hathaway is a holding company.", sector="Finance", industry="Property/Casualty Insurance", price=505.02, prev=503.70, mcap=1.08e12, pe=12.70, high52=537.74, avg_vol=4.01e6, volume=4.85e6, bid=502.98, ask=505.25, sources=["get_popular_watchlists", "get_financials"], revenue=[101.808e9, 93.675e9, 94.232e9, 94.972e9, 92.515e9], ni=[25.667e9, 10.106e9, 19.199e9, 30.796e9, 12.370e9], nm=[25.21, 10.79, 20.37, 32.43, 13.37]),
        snap("NKE", name="Nike", description="Nike designs and sells athletic footwear and apparel.", sector="Consumer Non-Durables", industry="Apparel/Footwear", price=39.60, prev=38.44, mcap=5.87e10, pe=18.87, high52=79.13, avg_vol=3.15e7, volume=2.84e7, bid=39.11, ask=39.99, sources=["run_scan", "get_popular_watchlists", "get_financials"], revenue=[10.972e9, 11.279e9, 12.427e9, 11.720e9, 11.097e9], ni=[1.069e9, 0.520e9, 0.792e9, 0.727e9, 0.211e9], nm=[9.74, 4.61, 6.37, 6.20, 1.90], r21=-0.18),
        snap("PYPL", name="PayPal", description="PayPal provides digital payments platforms.", sector="Commercial Services", industry="Miscellaneous Commercial Services", price=53.66, prev=61.47, mcap=4.59e10, pe=10.14, high52=79.22, avg_vol=1.28e7, volume=3.63e7, bid=52.86, ask=54.61, sources=["run_scan", "get_popular_watchlists", "get_financials"], revenue=[8.682e9, 8.353e9, 8.676e9, 8.417e9, 8.288e9], ni=[1.104e9, 1.113e9, 1.437e9, 1.248e9, 1.261e9], nm=[12.72, 13.32, 16.56, 14.83, 15.21], r21=-0.16, r5=-0.13, vol_vs=2.8),
        snap("RCL", name="Royal Caribbean", description="Royal Caribbean operates global cruise brands.", sector="Consumer Services", industry="Hotels/Resorts/Cruise lines", price=279.60, prev=284.80, mcap=7.50e10, pe=17.26, high52=366.50, avg_vol=1.44e6, volume=1.11e6, bid=275.03, ask=285.79, sources=["run_scan", "get_financials"], revenue=[4.832e9, 4.452e9, 4.259e9, 5.139e9, 4.538e9], ni=[1.128e9, 0.941e9, 0.753e9, 1.575e9, 1.210e9], nm=[23.34, 21.14, 17.68, 30.65, 26.66], r21=-0.12),
        snap("TGT", name="Target", description="Target operates general merchandise stores.", sector="Retail Trade", industry="Specialty Stores", price=163.19, prev=165.93, mcap=7.41e10, pe=16.93, high52=170.75, avg_vol=6.07e6, volume=3.64e6, bid=153.33, ask=163.39, sources=["get_watchlists", "get_earnings_calendar", "get_financials"], revenue=[26.539e9, 25.443e9, 30.453e9, 25.270e9, 25.211e9], ni=[1.877e9, 0.781e9, 1.045e9, 0.689e9, 0.935e9], nm=[7.07, 3.07, 3.43, 2.73, 3.71]),
        snap("NVDA", name="NVIDIA", description="NVIDIA designs GPUs and accelerated computing platforms.", sector="Electronic Technology", industry="Semiconductors", price=217.54, prev=227.98, mcap=5.35e12, pe=27.50, high52=236.54, avg_vol=1.42e8, volume=1.95e8, bid=217.88, ask=221.37, sources=["get_earnings_calendar", "get_popular_watchlists", "get_financials"], revenue=[96.221e9, 81.615e9, 68.127e9, 57.006e9, 46.743e9], ni=[59.688e9, 58.321e9, 42.960e9, 31.910e9, 26.422e9], nm=[62.03, 71.46, 63.06, 55.98, 56.53], earn_days=None, r5=-0.046, vol_vs=1.38),
        snap("AVGO", name="Broadcom", description="Broadcom supplies semiconductors and infrastructure software.", sector="Electronic Technology", industry="Semiconductors", price=368.68, prev=371.54, mcap=1.75e12, pe=61.39, high52=495.00, avg_vol=2.11e7, volume=1.66e7, bid=363.45, ask=379.00, sources=["get_earnings_calendar", "get_popular_watchlists", "get_financials"], revenue=[22.187e9, 19.311e9, 18.015e9, 15.952e9, 15.004e9], ni=[9.310e9, 7.349e9, 8.518e9, 4.140e9, 4.965e9], nm=[41.96, 38.06, 47.28, 25.95, 33.09], earn_days=4),
        snap("PLTR", name="Palantir", description="Palantir builds software platforms for commercial and government customers.", sector="Technology Services", industry="Packaged Software", price=186.25, prev=185.93, mcap=4.48e11, pe=159.21, high52=207.52, avg_vol=3.10e7, volume=2.51e7, bid=185.40, ask=190.63, sources=["get_watchlists", "get_financials"], revenue=[1.935e9, 1.633e9, 1.407e9, 1.181e9, 1.004e9], ni=[1.062e9, 0.871e9, 0.609e9, 0.476e9, 0.327e9], nm=[54.86, 53.32, 43.27, 40.27, 32.55]),
        snap("ESTC", name="Elastic", description="Elastic provides open-source search and analytics.", sector="Technology Services", industry="Packaged Software", price=99.95, prev=83.74, mcap=1.04e10, pe=27.98, high52=108.00, avg_vol=3.43e6, volume=1.01e7, bid=90.00, ask=108.00, sources=["get_popular_watchlists", "get_earnings_calendar"], r5=0.19, vol_vs=2.95, sma50=70.0, sma200=55.0, rsi=68.0),
        snap("GAP", name="Gap", description="Gap operates global apparel retail brands.", sector="Retail Trade", industry="Apparel/Footwear Retail", price=23.51, prev=20.79, mcap=8.46e9, pe=7.01, high52=29.36, avg_vol=1.02e7, volume=2.64e7, bid=23.45, ask=23.57, sources=["get_popular_watchlists", "get_financials"], revenue=[3.651e9, 3.497e9, 4.236e9, 3.942e9, 3.725e9], ni=[0.501e9, 0.339e9, 0.171e9, 0.236e9, 0.216e9], nm=[13.72, 9.69, 4.04, 5.99, 5.80], r5=0.13, vol_vs=2.59, sma50=22.0, sma200=21.0, rsi=64.0),
        snap("IREN", name="IREN Limited", description="IREN is a vertically integrated data center business powering Bitcoin, AI and beyond with renewable energy.", sector="Technology Services", industry="Data Processing Services", price=35.43, prev=40.53, mcap=1.27e10, pe=70.03, high52=76.87, avg_vol=4.68e7, volume=8.99e7, bid=35.60, ask=36.20, sources=["get_popular_watchlists", "get_earnings_calendar", "get_financials"], revenue=[0.137e9, 0.145e9, 0.185e9, 0.240e9, 0.181e9], ni=[-0.684e9, -0.248e9, -0.155e9, 0.385e9, 0.136e9], nm=[-498.45, -171.16, -84.14, 160.06, 52.89], r21=-0.20, headlines=["IREN stock wavers on Q4 revenue miss"]),
        snap("RGTI", name="Rigetti Computing", description="Rigetti provides full-stack quantum computing services.", sector="Electronic Technology", industry="Computer Processing Hardware", price=15.61, prev=16.44, mcap=5.21e9, pe=None, high52=58.15, avg_vol=1.67e7, volume=1.46e7, bid=15.65, ask=16.48, sources=["get_watchlists", "get_equity_news", "get_financials"], revenue=[5.138e6, 4.4e6, 1.868e6, 1.947e6, 1.801e6], ni=[-52.606e6, 33.109e6, -18.207e6, -200.968e6, -39.654e6], headlines=["Rigetti Q2 shows commercial mix inflection on Novera shipments"]),
        snap("JOBY", name="Joby Aviation", description="Joby is developing an all-electric VTOL commercial passenger aircraft.", sector="Electronic Technology", industry="Aerospace & Defense", price=6.98, prev=7.15, mcap=6.90e9, pe=None, high52=19.98, avg_vol=2.29e7, volume=2.70e7, bid=6.89, ask=7.18, sources=["get_watchlists", "get_financials"], revenue=[38.639e6, 24.246e6, 22.574e6, 0.015e6, 0.055e6], ni=[-245.443e6, -109.95e6, -121.536e6, -401.226e6, -324.674e6], headlines=["Joby quarterly revenue beat with eVTOL partnership news"]),
        snap("SLS", name="SELLAS Life Sciences", description="SELLAS is a clinical stage biopharmaceutical company developing immunotherapeutics for cancer, including galinpepimut-S and SLS009 pipeline.", sector="Health Technology", industry="Pharmaceuticals: Major", price=13.19, prev=15.21, mcap=2.66e9, pe=None, high52=15.88, avg_vol=8.62e6, volume=9.83e6, bid=13.44, ask=13.57, sources=["get_popular_watchlists", "get_equity_news", "get_financials"], ni=[-9.605e6, -8.4e6, -7.708e6, -6.791e6, -6.601e6], headlines=["SELLAS stock surges on SLS009 preclinical pancreatic cancer data and Phase 2 AML trial"]),
        snap("LCID", name="Lucid Group", description="Lucid manufactures electric vehicles.", sector="Consumer Durables", industry="Motor Vehicles", price=4.99, prev=5.09, mcap=1.97e9, pe=None, high52=25.23, avg_vol=1.21e7, volume=8.12e6, bid=4.95, ask=5.10, sources=["get_popular_watchlists", "get_financials"], revenue=[0.405e9, 0.282e9, 0.523e9, 0.337e9, 0.259e9], ni=[-1.035e9, -1.028e9, -0.814e9, -0.978e9, -0.539e9], nm=[-255.3, -364.06, -155.72, -290.70, -207.93]),
        snap("AFRM", name="Affirm", description="Affirm operates a platform for digital and mobile-first commerce.", sector="Finance", industry="Finance/Rental/Leasing", price=77.75, prev=77.49, mcap=2.60e10, pe=14.06, high52=100.00, avg_vol=6.06e6, volume=2.91e7, bid=77.41, ask=77.83, sources=["get_earnings_calendar", "get_financials"], revenue=[1.166e9, 1.039e9, 1.123e9, 0.933e9, 0.876e9], ni=[1.617e9, 0.103e9, 0.130e9, 0.081e9, 0.069e9], nm=[138.65, 9.91, 11.54, 8.65, 7.90], r21=-0.14, headlines=["Affirm reported a large quarterly EPS miss versus estimates"]),
    ]

    ctx = build_context(
        account_number=ACCOUNT,
        current_nav=500.0,
        cash=500.0,
        buying_power=500.0,
        positions=[],
        start_of_day_nav=500.0,
        timestamp=TS,
    )
    spy_closes_last = 769.35
    regime = MarketRegime(
        status=MarketRegimeStatus.OBSERVED,
        trend="up",
        spy_trend="up",
        observed_at=TS,
        confidence="LOW",
        source="get_equity_historicals:SPY",
        notes=["heuristic_sma_alignment_not_a_full_regime_engine", f"spy_last={spy_closes_last}"],
    )

    root = project_root()
    out = run_discovery(
        snapshots,
        ctx,
        regime=regime,
        persist=True,
        promote_shortlist=True,
        now=NOW,
        candidate_store=CandidateStore(root / "state" / "candidates.json"),
        queue_store=ResearchQueue(root / "state" / "research_queue.json"),
        run_store=DiscoveryRunStore(root / "state" / "discovery_runs.json"),
        sources_queried=[
            "get_accounts",
            "get_portfolio",
            "get_equity_positions",
            "get_scans",
            "run_scan",
            "get_watchlists",
            "get_watchlist_items",
            "get_popular_watchlists",
            "get_earnings_calendar",
            "search",
            "get_equity_fundamentals",
            "get_equity_quotes",
            "get_equity_tradability",
            "get_financials",
            "get_equity_historicals",
            "get_equity_news",
        ],
        session_context={"session": "weekend_after_2026-08-28_close", "nav": 500.0, "cash_pct": 1.0},
    )

    report = {
        "as_of": TS,
        "execution": {
            "auto_execution": False,
            "live_trade_actions_allowed": False,
            "require_human_approval": True,
        },
        "conclusion": out.conclusion,
        "run": to_dict(out.run),
        "candidates": [to_dict(c) for c in out.candidates],
        "rejected": [
            {
                "symbol": c.symbol,
                "reason": c.rejection_reason,
                "score": c.discovery_score,
                "sleeve": c.provisional_sleeve.value if c.provisional_sleeve else None,
            }
            for c in out.rejected
        ],
        "queue": [to_dict(q) for q in out.queue],
        "mcp_not_called": [
            "review_equity_order",
            "place_equity_order",
            "cancel_equity_order",
            "create_scan",
            "add_to_watchlist",
            "follow_watchlist",
        ],
        "note": "Discovery finds research candidates. It does not buy.",
    }
    rp = root / "reports" / "2026-08-29_discovery.json"
    rp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md_lines = [
        "# Candidate Discovery — 2026-08-29 (read-only)",
        "",
        "Discovery finds securities worth researching. It does **not** buy.",
        "",
        f"**Conclusion:** `{out.conclusion}`",
        "",
        "**NAV (observed):** $500 cash, no positions, risk state NORMAL. Weekend after 2026-08-28 close.",
        "",
        "**Execution:** `auto_execution=false` · `live_trade_actions_allowed=false` · `require_human_approval=true`.",
        "",
        "**MCP NOT called:** review/place/cancel, create_scan, watchlist writes, deposits/withdrawals/transfers.",
        "",
        "## Research queue",
        "",
        "| Symbol | Sleeve | Score | Priority | Deferred | Why |",
        "|---|---|---:|---|---|---|",
    ]
    for q in sorted(out.queue, key=lambda x: (-x.discovery_score, x.symbol)):
        deferred = "yes" if q.deferred_due_to_research_queue_overlap else ""
        md_lines.append(
            f"| {q.symbol} | {q.provisional_sleeve.value} | {q.discovery_score:.1f} | {q.priority.value} | {deferred} | {q.why_research_warranted} |"
        )
    if not out.queue:
        md_lines.append("| — | — | — | — | — | empty queue is valid |")
    md_lines += [
        "",
        "## Candidates (not rejected)",
        "",
        "| Symbol | Sleeve | Score | Status | Priority | Deferred | Sources |",
        "|---|---|---:|---|---|---|---|",
    ]
    for c in sorted(out.candidates, key=lambda x: (-x.discovery_score, x.symbol)):
        deferred = "yes" if c.deferred_due_to_overlap else ""
        md_lines.append(
            f"| {c.symbol} | {c.provisional_sleeve.value} | {c.discovery_score:.1f} | {c.status.value} | {c.priority.value} | {deferred} | {', '.join(c.discovery_sources[:4])} |"
        )
    md_lines += ["", "## Rejected", "", "| Symbol | Reason | Score |", "|---|---|---:|"]
    for c in out.rejected:
        md_lines.append(f"| {c.symbol} | {c.rejection_reason} | {c.discovery_score:.1f} |")
    if not out.rejected:
        md_lines.append("| — | none | — |")
    md_lines += [
        "",
        "No ACTIVE theses. No BUY ProposedActions. Speculative 3%/5% ceilings remain downstream.",
        "",
        "## How to read this",
        "",
        "- `URGENT_RESEARCH` means **research first**, not buy first.",
        "- Same-sector/sleeve crowding is `DEFERRED_DUE_TO_RESEARCH_QUEUE_OVERLAP` plus a priority notch. It is **not** a reject. Research compares the group; Portfolio Decision later chooses capital.",
        "- A 70% promote rate on a ~25-name live slice is not a production target. Large-universe triage is a future AI Research step, not another max-N cap.",
        "- Discovery does not create ACTIVE sleeve assignments or theses.",
        "",
    ]
    md_path = root / "reports" / "2026-08-29_candidates.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(out.conclusion)
    print("created", [(c.symbol, c.provisional_sleeve.value, round(c.discovery_score, 1), c.status.value, c.priority.value) for c in out.candidates])
    print("rejected", [(c.symbol, c.rejection_reason, round(c.discovery_score, 1)) for c in out.rejected])
    print("queue", len(out.queue), "report", rp, md_path)


if __name__ == "__main__":
    main()

"""Read-only Deep Research pilot on a small research-queue subset.

Never calls review/place/cancel or capital-transfer tools.
Does not create ProposedActions, ACTIVE theses, or orders.
A favorable ResearchReport is not permission to trade.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_portfolio.context import build_context
from agentic_portfolio.discovery.store import CandidateStore, ResearchQueue
from agentic_portfolio.paths import project_root
from agentic_portfolio.policy import load_account_rules
from agentic_portfolio.research.engine import run_research
from agentic_portfolio.research.packet import ResearchPayload
from agentic_portfolio.research.reasoner import ScriptedResearchReasoner
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import ResearchSubjectKind
from agentic_portfolio.schemas import Candidate, Sleeve

NOW = datetime(2026, 8, 30, 16, 30, tzinfo=timezone.utc)
TS = NOW.isoformat()
ACCOUNT = load_account_rules()["account"]["account_number"]

# Queue subset only — do not research all queued names.
PILOT = ("NVDA", "NKE", "ESTC", "SPY")


def _wrap(symbol, **fields):
    return {"data": {"results": [{"symbol": symbol, **fields}]}}


def _quote(symbol, last, prev, bid, ask, volume=None):
    q = {
        "symbol": symbol,
        "last_trade_price": last,
        "previous_close": prev,
        "bid_price": bid,
        "ask_price": ask,
        "state": "active",
        "has_traded": True,
    }
    if volume is not None:
        q["volume"] = volume
    return {"data": {"results": [{"quote": q}]}}


def _ind(symbol, typ, value, period):
    return {
        "data": {
            "symbol": symbol,
            "interval": "day",
            "indicators": [{"type": typ, "params": {"period": period}, "series": [{"begins_at": "2026-08-28T00:00:00Z", "value": value}]}],
        }
    }


def _news(symbol, titles):
    return {"data": {"symbol": symbol, "articles": [{"title": t, "published_at": "2026-08-28T16:00:00-04:00", "publisher": "wire"} for t in titles]}}


def _filings(symbol, rows):
    return {"data": {"symbol": symbol, "filings": rows}}


def payloads() -> dict[str, ResearchPayload]:
    nvda_fin = {
        "data": {
            "results": [
                {
                    "symbol": "NVDA",
                    "period": "quarterly",
                    "financials": [
                        {"period_end_date": "2026-07-26", "revenue": 96221000000.0, "gross_profit": 72142000000.0, "net_income": 59688000000.0, "net_margin": 62.03},
                        {"period_end_date": "2026-04-26", "revenue": 81615000000.0, "gross_profit": 61157000000.0, "net_income": 58321000000.0, "net_margin": 71.46},
                        {"period_end_date": "2026-01-25", "revenue": 68127000000.0, "gross_profit": 51093000000.0, "net_income": 42960000000.0, "net_margin": 63.06},
                        {"period_end_date": "2025-10-26", "revenue": 57006000000.0, "gross_profit": 41849000000.0, "net_income": 31910000000.0, "net_margin": 55.98},
                    ],
                }
            ]
        }
    }
    nke_fin = {
        "data": {
            "results": [
                {
                    "symbol": "NKE",
                    "period": "quarterly",
                    "financials": [
                        {"period_end_date": "2026-05-31", "revenue": 10972000000.0, "gross_profit": 5393000000.0, "net_income": 1069000000.0, "net_margin": 9.74},
                        {"period_end_date": "2026-02-28", "revenue": 11279000000.0, "gross_profit": 4530000000.0, "net_income": 520000000.0, "net_margin": 4.61},
                        {"period_end_date": "2025-11-30", "revenue": 12427000000.0, "gross_profit": 5045000000.0, "net_income": 792000000.0, "net_margin": 6.37},
                        {"period_end_date": "2025-08-31", "revenue": 11720000000.0, "gross_profit": 4943000000.0, "net_income": 727000000.0, "net_margin": 6.20},
                        {"period_end_date": "2025-05-31", "revenue": 11097000000.0, "gross_profit": 4469000000.0, "net_income": 211000000.0, "net_margin": 1.90},
                    ],
                }
            ]
        }
    }
    nvda_earn = {
        "data": {
            "results": [
                {"symbol": "NVDA", "eps": {"estimate": "2.090000", "actual": "2.220000"}, "report": {"date": "2026-08-26", "timing": "pm"}},
                {"symbol": "NVDA", "eps": {"estimate": "2.340000", "actual": None}, "report": {"date": "2026-11-17", "timing": "pm"}},
            ]
        }
    }
    nke_earn = {
        "data": {
            "results": [
                {"symbol": "NKE", "eps": {"estimate": "0.120000", "actual": "0.200000"}, "report": {"date": "2026-06-30", "timing": "pm"}},
                {"symbol": "NKE", "eps": {"estimate": "0.450000", "actual": None}, "report": {"date": "2026-10-01", "timing": "pm"}},
            ]
        }
    }
    estc_earn = {
        "data": {
            "results": [
                {"symbol": "ESTC", "eps": {"estimate": "0.440000", "actual": "0.700000"}, "report": {"date": "2026-08-27", "timing": "pm"}},
                {"symbol": "ESTC", "eps": {"estimate": "0.660000", "actual": None}, "report": {"date": "2026-11-19", "timing": "pm"}},
            ]
        }
    }
    nvda_facts = {
        "data": {
            "facts": [
                {"concept": "Revenues", "value": "96221000000.00", "end_date": "2026-07-26", "axises": []},
                {"concept": "NetIncomeLoss", "value": "59688000000.00", "end_date": "2026-07-26", "axises": []},
                {"concept": "Assets", "value": "320272000000.00", "end_date": "2026-07-26", "axises": []},
                {"concept": "LongTermDebt", "value": "33366000000.00", "end_date": "2026-07-26", "axises": []},
                {"concept": "CashAndCashEquivalentsAtCarryingValue", "value": "22443000000.00", "end_date": "2026-07-26", "axises": []},
            ]
        }
    }
    nke_facts = {
        "data": {
            "facts": [
                {"concept": "NetIncomeLoss", "value": "3108000000.00", "end_date": "2026-05-31", "axises": []},
                {"concept": "Assets", "value": "38410000000.00", "end_date": "2026-05-31", "axises": []},
                {"concept": "LongTermDebt", "value": "7942000000.00", "end_date": "2026-05-31", "axises": []},
                {"concept": "CashAndCashEquivalentsAtCarryingValue", "value": "7563000000.00", "end_date": "2026-05-31", "axises": []},
            ]
        }
    }
    return {
        "NVDA": ResearchPayload(
            symbol="NVDA",
            observed_at=TS,
            sources_attempted=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_earnings_results", "get_equity_news", "get_sec_filing_index", "get_sec_filing_facts", "get_equity_technical_indicators", "get_equity_tradability"],
            sources_observed=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_earnings_results", "get_equity_news", "get_sec_filing_index", "get_sec_filing_facts", "get_equity_technical_indicators", "get_equity_tradability"],
            tradability=_wrap("NVDA", name="NVIDIA Corporation Common Stock", state="active", tradeable=True),
            fundamentals=_wrap(
                "NVDA",
                description="NVIDIA Corp. engages in the design and manufacture of computer graphics processors, chipsets, and related multimedia software.",
                sector="Electronic Technology",
                industry="Semiconductors",
                market_cap=5351114182000.0,
                pe_ratio=27.501075,
                pb_ratio=22.9412,
                shares_outstanding=24598300000.0,
                high_52_weeks=236.54,
                low_52_weeks=164.07,
                average_volume_2_weeks=141635312.9,
                volume=195116232.0,
            ),
            quotes=_quote("NVDA", "217.54", "227.98", "217.88", "221.37", "195116232"),
            financials=nvda_fin,
            rsi=_ind("NVDA", "rsi", 52.34, 14),
            sma_50=_ind("NVDA", "sma", 208.42, 50),
            sma_200=_ind("NVDA", "sma", 195.83, 200),
            earnings_results=nvda_earn,
            news=_news("NVDA", [
                "Nvidia Q2 revenue $96.2B, up 106% y/y; Q3 guide $108B",
                "Broadcom/OpenAI chip collaboration cited as competitive benchmark vs Nvidia",
                "Post-earnings bounce faded into Friday tech weakness after hawkish Fed remarks",
            ]),
            sec_index=_filings("NVDA", [
                {"filing_id": "c2acc89a-db67-49c6-a6d7-803a2cba3e62", "form_type": "10-Q", "date_filed": "2026-08-26"},
                {"filing_id": "44569391-63e7-4bcb-95f0-b983c909576f", "form_type": "10-K", "date_filed": "2026-02-25"},
                {"filing_id": "3eaf949c-b22f-474a-963e-367c0a4a4408", "form_type": "8-K", "date_filed": "2026-08-26"},
            ]),
            sec_facts=nvda_facts,
        ),
        "NKE": ResearchPayload(
            symbol="NKE",
            observed_at=TS,
            sources_attempted=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_earnings_results", "get_equity_news", "get_sec_filing_index", "get_sec_filing_facts", "get_equity_technical_indicators", "get_equity_tradability"],
            sources_observed=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_earnings_results", "get_equity_news", "get_sec_filing_index", "get_sec_filing_facts", "get_equity_technical_indicators", "get_equity_tradability"],
            tradability=_wrap("NKE", name="Nike, Inc.", state="active", tradeable=True),
            fundamentals=_wrap(
                "NKE",
                description="NIKE, Inc. engages in the design, development, marketing, and sale of athletic footwear, apparel, accessories, equipment, and services.",
                sector="Consumer Non-Durables",
                industry="Apparel/Footwear",
                market_cap=58746546972.67,
                pe_ratio=18.869723,
                pb_ratio=3.95068,
                shares_outstanding=1483498660.9,
                high_52_weeks=79.13,
                low_52_weeks=38.17,
                average_volume_2_weeks=31523833.67,
                volume=28437324.0,
                dividend_yield=4.116162,
            ),
            quotes=_quote("NKE", "39.60", "38.44", "39.11", "39.99", "28437324"),
            financials=nke_fin,
            rsi=_ind("NKE", "rsi", 43.51, 14),
            sma_50=_ind("NKE", "sma", 41.92, 50),
            sma_200=_ind("NKE", "sma", 52.06, 200),
            earnings_results=nke_earn,
            news=_news("NKE", [
                "Nike shares at multi-year lows; founder wealth tied to the decline",
                "Truist downgraded Nike to hold from buy, target $42 from $47",
                "Converse sales declined for 13 consecutive quarters; new Converse COO named",
                "Nike among companies reporting large tariff refunds; use of proceeds not disclosed",
                "Jane Ewing named chief commercial officer effective Sept. 7",
            ]),
            sec_index=_filings("NKE", [
                {"filing_id": "8cf98f71-e2ba-4811-a75d-3b08829b4cb8", "form_type": "10-K", "date_filed": "2026-07-15"},
                {"filing_id": "ef4aad25-1a61-4788-b4e7-0e7a537370b5", "form_type": "10-Q", "date_filed": "2026-04-01"},
                {"filing_id": "85e2af6e-71b2-475a-832c-212af233e9b6", "form_type": "8-K", "date_filed": "2026-08-10"},
            ]),
            sec_facts=nke_facts,
        ),
        "ESTC": ResearchPayload(
            symbol="ESTC",
            observed_at=TS,
            sources_attempted=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_earnings_results", "get_equity_news", "get_sec_filing_index", "get_equity_technical_indicators", "get_equity_tradability"],
            sources_observed=["get_equity_quotes", "get_equity_fundamentals", "get_earnings_results", "get_equity_news", "get_sec_filing_index", "get_equity_technical_indicators", "get_equity_tradability"],
            sources_unavailable=["get_financials", "get_sec_filing_facts"],
            tradability=_wrap("ESTC", name="Elastic N.V.", state="active", tradeable=True),
            fundamentals=_wrap(
                "ESTC",
                description="Elastic NV is a data analytics company, which engages in the provision of open-source search and analytics engine services.",
                sector="Technology Services",
                industry="Packaged Software",
                market_cap=10390002400.0,
                pe_ratio=27.977374,
                pb_ratio=13.1549,
                shares_outstanding=103952000.0,
                high_52_weeks=108.0,
                low_52_weeks=42.05,
                average_volume_2_weeks=3428363.3,
                volume=10113365.0,
            ),
            quotes=_quote("ESTC", "99.95", "83.74", "90.00", "108.00", "10113365"),
            rsi=_ind("ESTC", "rsi", 77.48, 14),
            sma_50=_ind("ESTC", "sma", 68.27, 50),
            sma_200=_ind("ESTC", "sma", 63.79, 200),
            earnings_results=estc_earn,
            news=_news("ESTC", [
                "Elastic Q1 revenue $478.1M, non-GAAP EPS $0.70; FY27 guidance raised",
                "Shares surged ~17% after the print; Deductive AI acquisition completed",
                "Weekend bid/ask unusually wide after the gap",
            ]),
            sec_index=_filings("ESTC", [
                {"filing_id": "a8fa8856-8f32-4d75-bfb9-70667e947ed9", "form_type": "10-Q", "date_filed": "2026-08-28"},
                {"filing_id": "42ed90fc-f7f9-4378-b736-fae5685a3696", "form_type": "8-K", "date_filed": "2026-08-27"},
                {"filing_id": "79b483a7-3a14-43f8-976d-690ae565ac0e", "form_type": "10-K", "date_filed": "2026-06-08"},
            ]),
        ),
        "SPY": ResearchPayload(
            symbol="SPY",
            observed_at=TS,
            sources_attempted=["get_equity_quotes", "get_equity_fundamentals", "get_financials", "get_equity_news", "get_equity_tradability"],
            sources_observed=["get_equity_quotes", "get_equity_fundamentals", "get_equity_news", "get_equity_tradability"],
            sources_unavailable=["get_financials", "get_sec_filing_index"],
            tradability=_wrap("SPY", name="State Street SPDR S&P 500 ETF Trust", state="active", tradeable=True),
            fundamentals=_wrap(
                "SPY",
                description="SPY tracks a market cap-weighted index of US large- and mid-cap stocks selected by the S&P Committee.",
                sector="Miscellaneous",
                industry="Investment Trusts Or Mutual Funds",
                market_cap=814424024729.24,
                pe_ratio=26.2413,
                pb_ratio=5.52053,
                shares_outstanding=1058532116.0,
                high_52_weeks=779.37,
                low_52_weeks=629.28,
                average_volume_2_weeks=36319863.65,
                volume=36744276.0,
                dividend_yield=1.0235,
            ),
            quotes=_quote("SPY", "769.39", "771.10", "768.66", "770.06", "36744276"),
            news=_news("SPY", [
                "S&P 500 near highs; September seasonal caution vs strong YTD tape",
                "Fed Chair Warsh hawkish Jackson Hole remarks lifted September hike odds",
            ]),
        ),
    }


def interpretations() -> dict[str, dict]:
    """Pilot AI interpretations. Not trade advice. Not ProposedActions."""
    cases = lambda bull, base, bear: {
        "bull_case": {"case": "BULL_CASE", "summary": bull, "major_assumptions": ["observed growth/quality persists"], "price_target": None, "evidence_refs": ["fact:market_price"]},
        "base_case": {"case": "BASE_CASE", "summary": base, "major_assumptions": ["normalization"], "price_target": None, "evidence_refs": ["fact:pe_ratio"]},
        "bear_case": {"case": "BEAR_CASE", "summary": bear, "major_assumptions": ["demand or multiple compresses"], "price_target": None, "evidence_refs": ["fact:high_52_week"]},
    }
    return {
        "NVDA": {
            "executive_summary": "Observed facts show a still-exceptional AI compute franchise: quarterly revenue $96.2B and net income $59.7B, with a beat versus $2.09 EPS estimate ($2.22 actual). That is Core-quality evidence. It is not a buy signal. Valuation (trailing P/E ~27.5, P/B ~23) is not extreme versus this growth, but competition (Broadcom/custom silicon) and a post-print fade from $228 toward $217, plus long-term debt rising to $33.4B in the 10-Q facts, keep conviction from being HIGH.",
            "business_summary": "NVIDIA designs GPUs and networking for data-center AI and gaming. The 10-Q tagged facts show Compute & Networking as the dominant observed revenue engine.",
            "investment_question": "Is NVDA attractive enough as a long-term compounding holding versus cash and SPY?",
            "fundamental_analysis": "Revenue and income series are expanding on a very large base. That looks durable in the observed windows, not a one-quarter spike only. Durability still depends on hyperscaler capex continuing; that is an interpretation, not a guaranteed fact.",
            "financial_analysis": "Cash $22.4B vs long-term debt $33.4B (10-Q). Debt increased versus $8.5B at Jan 25, 2026. Balance sheet remains large relative to earnings, but leverage is no longer negligible.",
            "valuation_analysis": "P/E ~27.5 with triple-digit y/y revenue growth is not a mechanical bargain or bubble call. Relative to SPY P/E ~26, NVDA is not cheap; the premium has to be earned by continued growth. No P/E cutoff is applied.",
            "earnings_analysis": "Q2 FY27 beat and $108B Q3 guide are material. The Friday fade after a Thursday jump is a market-structure fact, not proof the print was low quality. Effect kind: STRUCTURAL_CHANGE in demand appears more likely than a one-time item, with residual uncertainty.",
            "competitive_analysis": "News cites custom silicon (Broadcom/OpenAI) as a benchmark competitor. That is a real risk to discuss in a thesis, not a conclusion that NVDA has already lost share — share data is not in the packet.",
            "technical_context": "Price ~217 above SMA50 ~208 and SMA200 ~196; RSI ~52. Supporting context only for Core. The post-earnings drawdown from 228 is not a thesis by itself.",
            "market_context": "Book is 100% cash at observed NAV $500. Research attractiveness ≠ allocation. Hawkish Fed remarks were a tape fact the same week.",
            "sector_context": "Discovery already has multiple Tech Core names. NVDA should be compared with MSFT/AVGO/AAPL in a later ResearchComparison, not first-come-first-served.",
            "news_analysis": "Independent items: (1) earnings/guide, (2) competitive chip commentary, (3) macro/Fed fade. Several NVDA headlines reprint the same earnings event.",
            "filing_analysis": "10-Q/10-K/8-K are present. Tagged facts confirm revenue, income, cash, assets, and higher long-term debt. No going-concern language was observed in the retrieved facts. Item 1A text was not fetched in this pilot.",
            "catalyst_analysis": "Next report dated 2026-11-17 (estimate $2.34, actual null). Product/platform adoption remains the fundamental catalyst.",
            "risk_analysis": "Customer concentration (not quantified here), custom-silicon competition, valuation compression if growth decelerates, higher debt, China/export policy (not in packet — missing).",
            **cases(
                "AI infrastructure demand stays strong and NVDA keeps most of the stack economics.",
                "Growth remains high but decelerates; multiple stays in the mid-20s P/E area.",
                "Capex pause or share loss to custom silicon compresses both earnings and the multiple.",
            ),
            "key_catalysts": ["next earnings 2026-11-17", "hyperscaler capex commentary"],
            "key_risks": ["custom silicon competition", "multiple compression", "debt up vs prior 10-Q"],
            "invalidation_candidates": ["sequential revenue decline without a clear one-time cause", "sustained loss of data-center leadership evidence"],
            "expected_horizon": "multi-year",
            "missing_information": ["full Item 1A risk excerpt", "customer concentration weights", "export-control detail"],
            "conflicting_evidence": ["blowout print vs immediate post-print price fade"],
            "evidence_refs": ["fact:market_price", "fact:pe_ratio", "derived:revenue_growth_qoq", "fact:sec_fact.LongTermDebt"],
            "ai_interpretations": [
                {"name": "growth_durability", "value": "observed series still compounding at scale", "evidence_refs": ["derived:revenue_growth_qoq"]},
                {"name": "balance_sheet_note", "value": "debt increase is material relative to the prior period but earnings coverage remains large", "evidence_refs": ["fact:sec_fact.LongTermDebt"]},
            ],
            "confidence": "MEDIUM",
            "research_conclusion": "ADVANCE_TO_THESIS",
            "recommended_next_step": "ADVANCE_TO_THESIS",
            "earnings_effect_kind": "STRUCTURAL_CHANGE",
        },
        "NKE": {
            "executive_summary": "Price ~$39.60 sits near the 52-week low $38.17 versus a $79.13 high — a large drawdown. That is a Discovery-style dislocation flag, not proof of a temporary mispricing. Annual 10-K net income $3.11B vs $3.22B vs $5.70B two years prior is evidence of weaker earning power. Last quarter NI $1.07B recovered from $0.52B, so the picture is mixed. Research should not treat 'down a lot' as opportunistic by default.",
            "business_summary": "Nike designs and sells athletic footwear and apparel globally, plus Converse. Converse has 13 consecutive down revenue quarters per news (not an SEC tagged fact in this packet).",
            "investment_question": "Is the repricing a temporary dislocation or deserved deterioration, and is NKE attractive enough versus buying a stronger company?",
            "fundamental_analysis": "Brand remains a going concern on observed facts (profitable, cash $7.6B, assets $38.4B). Quality is not obviously destroyed, but FY net income has stepped down from the FY2024 peak. That is deterioration of earning power, not just a multiple reset.",
            "financial_analysis": "Cash ~$7.6B vs LT debt ~$7.9B. Not a distressed balance sheet on these facts. Dividend yield ~4.1% is observed, not a reason to buy.",
            "valuation_analysis": "P/E ~18.9 vs prior higher-growth years. Cheaper than NVDA/SPY on P/E is not a buy rule. If earning power has structurally reset lower, the multiple may be fair rather than a dislocation.",
            "earnings_analysis": "Last reported quarter beat ($0.20 vs $0.12). Next print 2026-10-01 (estimate $0.45, actual null). Recent beats do not erase the multi-year NI decline in the 10-K.",
            "competitive_analysis": "Packet does not include share data versus Adidas/On/Hoka. Management changes (CCO, Converse COO) are observed news, not proof of a turnaround.",
            "technical_context": "Price below SMA50 (~42) and SMA200 (~52); RSI ~44. For Opportunistic this is supporting context for the selloff, not a mean-reversion order.",
            "market_context": "Consumer and rates tape (hawkish Fed) can pressure discretionary retail. Not a complete macro model.",
            "sector_context": "Consumer/apparel, not Tech Core overlap.",
            "news_analysis": "Independent: (1) price/wealth decline, (2) Truist downgrade, (3) Converse slump + COO, (4) tariff-refund use undisclosed, (5) new CCO. Several market wrap items repeat the downgrade.",
            "filing_analysis": "10-K facts: NI down vs two years ago; cash and assets stable-ish; LT debt ~$7.9B. Revenues concept was not returned for this filing in the facts call — missing. No going-concern fact observed.",
            "catalyst_analysis": "Oct 1 earnings; execution under new commercial leadership; Converse stabilization would be evidence of recovery — not yet observed.",
            "risk_analysis": "If the market is correct that brand heat and wholesale/DTC mix have lastingly weakened, further downside exists even from a 50% drawdown. Tariff-refund cash is a potential ONE_TIME_EFFECT.",
            **cases(
                "Brand restabilizes and FY earnings recover toward the prior peak without more share loss.",
                "Earnings stay in the lower-$3B area; stock range-bound until a clearer turn.",
                "Further brand/wholesale deterioration; the 52-week low fails and earning power steps down again.",
            ),
            "temporary_dislocation_assessment": {
                "verdict": "MIXED",
                "reasoning": "Drawdown is large, but 10-K net income is also lower than two years ago. Price and fundamentals both moved. Cannot call this a clean temporary dislocation.",
                "evidence_refs": ["derived:drawdown_from_52w_high", "fact:sec_fact.NetIncomeLoss", "fact:net_income_periods"],
            },
            "fundamental_deterioration_assessment": {
                "verdict": "LIKELY_DETERIORATION",
                "reasoning": "Annual NI $5.7B → $3.2B → $3.1B is a material step-down in earning power. Last quarter improved, so not a collapse, but the impairment is not merely a multiple change.",
                "evidence_refs": ["fact:sec_fact.NetIncomeLoss", "fact:net_income_periods"],
            },
            "key_catalysts": ["2026-10-01 earnings", "evidence of Converse/brand stabilization"],
            "key_risks": ["structural demand/brand heat loss", "one-time tariff refunds masking ops", "further multiple compression"],
            "invalidation_candidates": ["another year of lower NI without an identifiable one-time cause", "break of the 52-week low on deteriorating quarters"],
            "expected_horizon": "12-24 months if a thesis is later written",
            "missing_information": ["10-K revenue concept not returned", "segment gross margin detail", "inventory/channel checks"],
            "conflicting_evidence": ["last-quarter NI recovery vs two-year annual NI decline", "cheap vs history vs possibly cheaper for a reason"],
            "evidence_refs": ["derived:drawdown_from_52w_high", "fact:sec_fact.NetIncomeLoss", "fact:pe_ratio"],
            "ai_interpretations": [
                {"name": "dislocation_vs_deterioration", "value": "mixed; earning-power decline is real", "evidence_refs": ["fact:sec_fact.NetIncomeLoss"]}
            ],
            "confidence": "MEDIUM",
            "research_conclusion": "KEEP_WATCHING",
            "recommended_next_step": "KEEP_WATCHING",
            "earnings_effect_kind": "UNCERTAIN",
        },
        "ESTC": {
            "executive_summary": "Tactical Discovery flagged SMA/momentum/volume. The live tape then gaped ~19% on a Q1 beat ($0.70 vs $0.44) and a guidance raise. RSI ~77 and price far above SMA50/200 means the setup is extended, not a clean un-run event. get_financials returned no series. This is not a Core compounding file and should not be treated as one.",
            "business_summary": "Elastic sells search/analytics/security/observability software. Deductive AI acquisition completed (news + 8-K index).",
            "investment_question": "What is the remaining tactical setup now that the earnings gap has already occurred?",
            "fundamental_analysis": "Quarterly financial statement series were unavailable from get_financials. EPS history shows repeated beats. That is not a substitute for revenue/FCF quality.",
            "financial_analysis": "NEED_MORE_DATA on cash generation, dilution, and GAAP profitability. P/B ~13 is observed, not interpreted as cheap or expensive without a model.",
            "valuation_analysis": "Trailing P/E ~28 after a gap. Framework for a software grower differs from Core hardware. No threshold rule applied. Incomplete financials cap confidence.",
            "earnings_analysis": "Print looks like a beat-and-raise. Whether that is STRUCTURAL_CHANGE in AI search adoption or a one-quarter budget flush is UNCERTAIN without cohorts. The move already happened.",
            "competitive_analysis": "Not evidenced versus Splunk/Datadog/OpenSearch in this packet.",
            "technical_context": "Primary for Tactical: uptrend vs SMA50/200 is intact, but RSI 77 and a ~17-19% one-day pop plus weekend 90–108 quotes mean confirmation already occurred and invalidation (gap-fill toward ~84) is wide. Reward/risk after the move is worse than before the print.",
            "market_context": "Same-week Fed hawkishness hit other high-duration names; ESTC still ripped on company-specific news.",
            "sector_context": "Software/tech services; not a Core quality file.",
            "news_analysis": "The earnings/guide/acquisition cluster is one event, reprinted many times. Treat as a single catalyst, not independent bullish articles.",
            "filing_analysis": "10-Q filed 2026-08-28 and 8-Ks around the print exist. Facts were not fetched (unavailable in this pilot). Do not invent filing conclusions.",
            "catalyst_analysis": "The event that created the Discovery setup has largely expressed. Next dated print ~2026-11-19.",
            "risk_analysis": "Gap fill, multiple compression, acquisition integration, incomplete financial packet.",
            **cases(
                "AI search adoption continues and the gap holds as a higher base.",
                "Stock digest the move; no immediate follow-through.",
                "Gap fills toward the prior close as the print is faded.",
            ),
            "key_catalysts": ["hold of post-earnings range", "next earnings ~Nov 2026"],
            "key_risks": ["extended RSI/gap-fill", "missing financials", "weekend quote width"],
            "invalidation_candidates": ["close back toward $83.74 prior close without new fundamental information"],
            "expected_horizon": "days to a few weeks",
            "missing_information": ["get_financials series", "SEC tagged cash/debt/FCF", "10-Q MD&A excerpt"],
            "conflicting_evidence": ["strong print vs already-extended price"],
            "evidence_refs": ["fact:market_price", "fact:rsi", "derived:sma_alignment", "fact:earnings_history"],
            "ai_interpretations": [
                {"name": "setup_quality_after_gap", "value": "event has expressed; remaining tactical reward is less attractive", "evidence_refs": ["fact:rsi", "fact:market_price"]}
            ],
            "confidence": "LOW",
            "research_conclusion": "KEEP_WATCHING",
            "recommended_next_step": "KEEP_WATCHING",
            "earnings_effect_kind": "UNCERTAIN",
        },
        "SPY": {
            "executive_summary": "Validated broad-market S&P 500 ETF. Description and classification support Core use as the default comparison asset versus single names. Price ~769 vs 52-week high 779. This research does not size a position and does not create a BUY.",
            "business_summary": "SPY holds a committee-selected, cap-weighted US large/mid portfolio. It is not an operating company.",
            "investment_question": "Is broad-market exposure via SPY a reasonable Core building block versus cash and versus individual names?",
            "fundamental_analysis": "Fund mandate is definitionally diversified US large/mid. Operating-company financials are N/A; get_financials was empty as expected.",
            "financial_analysis": "Not applicable at the issuer P&L level. Trailing fund P/E ~26.2 is an observed characteristic of the index, not a cheap/expensive rule.",
            "valuation_analysis": "Index P/E near the mid-20s. Hawkish Fed commentary is a near-term market fact. Valuation vs cash still depends on expected index earnings — not computed here.",
            "earnings_analysis": "Not an operating-company earnings file.",
            "competitive_analysis": "VOO/VTI are close substitutes already on the Discovery queue. Research/Portfolio Decision should compare cost, tracking, and overlap later rather than picking whichever ETF appeared first.",
            "technical_context": "Secondary for Core. Price is close to the 52-week high; drawdown from high is small on observed 52w range.",
            "market_context": "YTD strength vs hawkish Jackson Hole repricing. Seasonal September commentary is not a trading rule.",
            "sector_context": "Broad market, not a sector bet.",
            "news_analysis": "Macro/Fed and seasonality pieces. Repeated index wrap-ups are one tape, not independent catalysts.",
            "filing_analysis": "SEC issuer filings were not retrieved for the ETF in this pilot.",
            "catalyst_analysis": "Macro prints and Fed path. Not a company catalyst.",
            "risk_analysis": "Market drawdowns, rate shock, concentration of the cap-weighted index in mega-cap tech (weights not in MCP — missing, not invented).",
            **cases(
                "Index earnings and multiples hold up; SPY remains a simple Core vehicle.",
                "Range-bound as rates and earnings offset.",
                "Risk-off drawdown from highs if hike odds and earnings disappoint together.",
            ),
            "key_catalysts": ["labor/CPI path into the September FOMC"],
            "key_risks": ["index drawdown", "unobserved mega-cap concentration"],
            "invalidation_candidates": ["mandate change (not observed)", "inability to classify as broad-market — already classified"],
            "expected_horizon": "multi-year if later held as Core",
            "missing_information": ["ETF holdings/sector weights (not in MCP)", "expense ratio not fetched"],
            "conflicting_evidence": [],
            "evidence_refs": ["fact:description", "fact:pe_ratio", "fact:market_price"],
            "ai_interpretations": [
                {"name": "role_vs_single_names", "value": "SPY is the relevant Core alternative, not a tactical setup", "evidence_refs": ["fact:description"]}
            ],
            "confidence": "MEDIUM",
            "research_conclusion": "ADVANCE_TO_THESIS",
            "recommended_next_step": "ADVANCE_TO_THESIS",
            "earnings_effect_kind": None,
        },
    }


def _candidate(symbol: str) -> Candidate:
    store = CandidateStore()
    found = store.active_for_symbol(symbol)
    if found:
        return found
    sleeve = {
        "NVDA": Sleeve.CORE_GROWTH,
        "NKE": Sleeve.OPPORTUNISTIC,
        "ESTC": Sleeve.TACTICAL,
        "SPY": Sleeve.CORE_GROWTH,
    }[symbol]
    return Candidate(
        candidate_id=f"pilot-{symbol}",
        symbol=symbol,
        discovered_at=TS,
        discovery_source="research_queue_subset",
        provisional_sleeve=sleeve,
        discovery_score={"NVDA": 70.625, "NKE": 59.7262, "ESTC": 70.4313, "SPY": 65.1}[symbol],
    )


def main() -> None:
    context = build_context(
        account_number=ACCOUNT,
        current_nav=500.0,
        cash=500.0,
        buying_power=500.0,
        positions=[],
        timestamp=TS,
    )
    pls = payloads()
    reasoner = ScriptedResearchReasoner(interpretations())
    store = ResearchStore()
    journal = project_root() / "logs" / "research.jsonl"
    qstore = ResearchQueue()
    queue_by_symbol = {}
    for q in qstore.all():
        if q.symbol in PILOT and q.symbol not in queue_by_symbol:
            queue_by_symbol[q.symbol] = q

    reports = []
    for symbol in PILOT:
        out = run_research(
            _candidate(symbol),
            pls[symbol],
            context,
            reasoner,
            subject_kind=ResearchSubjectKind.NEW_CANDIDATE,
            queue_entry=queue_by_symbol.get(symbol),
            store=store,
            queue_store=qstore if symbol in queue_by_symbol else None,
            persist=True,
            now=NOW,
            journal=journal,
        )
        reports.append(out.report)
        assert out.buy_actions_created == 0
        assert out.proposed_actions_created == 0
        assert out.execution_attempted is False

    summary = {
        "run": "live_readonly_research_pilot",
        "observed_at": TS,
        "symbols": list(PILOT),
        "nav_observed": 500.0,
        "nav_is_not_a_policy_constraint": True,
        "buy_actions_created": 0,
        "proposed_actions_created": 0,
        "execution_attempted": False,
        "mcp_read_tools": [
            "get_accounts",
            "get_portfolio",
            "get_equity_quotes",
            "get_equity_fundamentals",
            "get_financials",
            "get_equity_tradability",
            "get_equity_news",
            "get_earnings_results",
            "get_sec_filing_index",
            "get_sec_filing_facts",
            "get_equity_historicals",
            "get_equity_technical_indicators",
        ],
        "mcp_not_called": [
            "review_equity_order",
            "place_equity_order",
            "cancel_equity_order",
            "create_scan",
            "watchlist_writes",
            "any_deposit_withdrawal_transfer",
        ],
        "reports": [
            {
                "symbol": r.symbol,
                "research_id": r.research_id,
                "sleeve": r.provisional_sleeve.value,
                "status": r.research_status.value,
                "conclusion": r.research_conclusion.value if r.research_conclusion else None,
                "confidence": r.confidence.value,
                "freshness": r.freshness.value,
            }
            for r in reports
        ],
        "note": "Favorable ResearchReports are not permission to trade. Thesis, Portfolio Decision, and Risk Gate remain downstream.",
    }
    out_path = project_root() / "reports" / "2026-08-30_research.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md = ["# Read-only Deep Research pilot — 2026-08-30", "", "Subset of the research queue only (NVDA, NKE, ESTC, SPY). Not a buy list.", ""]
    for r in reports:
        md.append(f"## {r.symbol} ({r.provisional_sleeve.value})")
        md.append(f"- status: `{r.research_status.value}`")
        md.append(f"- conclusion: `{r.research_conclusion.value if r.research_conclusion else None}`")
        md.append(f"- confidence: `{r.confidence.value}`")
        md.append(f"- research_id: `{r.research_id}`")
        md.append(f"- {r.executive_summary}")
        md.append("")
    md.append("No ProposedAction. No orders. No transfers.")
    (project_root() / "reports" / "2026-08-30_research.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

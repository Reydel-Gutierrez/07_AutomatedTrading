from datetime import datetime, timedelta, timezone

from agentic_portfolio.adapters.robinhood_read import (
    RobinhoodSecurityBundle,
    adapt_classification_evidence,
)
from agentic_portfolio.classification import classify
from agentic_portfolio.evidence_cache import needs_refresh, put_classification
from agentic_portfolio.schemas import (
    ClassificationEvidence,
    ClassificationStatus,
    EmbeddedSectorStatus,
    RefreshReason,
    SecurityClass,
)


def _wrap(symbol, **fields):
    return {"data": {"results": [{"symbol": symbol, **fields}]}}


def test_adapter_verified_broad_etf_without_invented_weights():
    bundle = RobinhoodSecurityBundle(
        symbol="SPY",
        tradability=_wrap("SPY", name="State Street SPDR S&P 500 ETF Trust", simple_name="SPDR S&P 500 ETF Trust"),
        fundamentals=_wrap(
            "SPY",
            description="SPY tracks a market cap-weighted index of US large- and mid-cap stocks selected by the S&P Committee.",
            sector="Miscellaneous",
            industry="Investment Trusts Or Mutual Funds",
        ),
        search={"data": {"results": [{"symbol": "SPY", "name": "State Street SPDR S&P 500 ETF Trust"}]}},
    )
    ev = adapt_classification_evidence(bundle)
    assert ev.instrument_kind == "etf"
    assert ev.constituent_count is None
    assert ev.embedded_sector_weights is None
    assert ev.embedded_sector_exposure_status == EmbeddedSectorStatus.UNKNOWN
    assert ev.underlying_index_definitionally_broad is True
    r = classify("SPY", ev)
    assert r.security_class == SecurityClass.BROAD_MARKET_INDEX_ETF
    assert r.status == ClassificationStatus.VALIDATED
    assert ev.provenance["legal_name"].provenance.value == "MCP_OBSERVED_FACT"
    assert ev.provenance["constituent_count"].provenance.value == "MISSING"


def test_adapter_sector_etf_is_other_diversified():
    bundle = RobinhoodSecurityBundle(
        symbol="XLK",
        tradability=_wrap("XLK", name="State Street Technology Select Sector SPDR ETF"),
        fundamentals=_wrap(
            "XLK",
            description="XLK tracks an index of S&P 500 technology stocks.",
            sector="Miscellaneous",
            industry="Investment Trusts Or Mutual Funds",
        ),
    )
    ev = adapt_classification_evidence(bundle)
    r = classify("XLK", ev)
    assert ev.is_sector_or_industry_fund is True
    assert r.security_class == SecurityClass.OTHER_DIVERSIFIED_ETF
    assert r.effective_class_for_ceiling != SecurityClass.BROAD_MARKET_INDEX_ETF


def test_adapter_leveraged_etf_not_broad():
    bundle = RobinhoodSecurityBundle(
        symbol="TQQQ",
        tradability=_wrap("TQQQ", name="ProShares UltraPro QQQ"),
        fundamentals=_wrap(
            "TQQQ",
            description="TQQQ provides 3x leveraged exposure to a modified market-cap-weighted index tracking 100 of the largest non-financial firms listed on NASDAQ.",
            sector="Miscellaneous",
            industry="Investment Trusts Or Mutual Funds",
        ),
    )
    ev = adapt_classification_evidence(bundle)
    r = classify("TQQQ", ev)
    assert ev.is_leveraged is True
    assert r.security_class == SecurityClass.INDIVIDUAL_EQUITY


def test_adapter_insufficient_etf_evidence_fails_closed():
    bundle = RobinhoodSecurityBundle(symbol="XYZ")
    ev = adapt_classification_evidence(bundle)
    r = classify("XYZ", ev)
    assert r.status == ClassificationStatus.INSUFFICIENT_EVIDENCE
    assert r.effective_class_for_ceiling != SecurityClass.BROAD_MARKET_INDEX_ETF


def test_adapter_individual_equity_and_sector_map():
    bundle = RobinhoodSecurityBundle(
        symbol="AAPL",
        tradability=_wrap("AAPL", name="Apple Inc. Common Stock", simple_name="Apple"),
        fundamentals=_wrap(
            "AAPL",
            description="Apple, Inc. engages in the design, manufacture, and sale of smartphones.",
            sector="Electronic Technology",
            industry="Telecommunications Equipment",
        ),
    )
    ev = adapt_classification_evidence(bundle)
    r = classify("AAPL", ev)
    assert ev.instrument_kind == "equity"
    assert r.security_class == SecurityClass.INDIVIDUAL_EQUITY
    assert r.sector.value == "INFORMATION_TECHNOLOGY"


def test_stale_evidence_requires_refresh(tmp_path):
    now = datetime.now(timezone.utc)
    r = classify(
        "SPY",
        ClassificationEvidence(
            instrument_kind="etf",
            is_leveraged=False,
            is_inverse=False,
            is_thematic=False,
            is_sector_or_industry_fund=False,
            is_narrow_factor=False,
            is_single_stock_fund=False,
            underlying_index="S&P 500",
            underlying_index_definitionally_broad=True,
        ),
    )
    cache_path = tmp_path / "classification_cache.json"
    put_classification("SPY", r, now=now - timedelta(days=5), path=cache_path)
    from agentic_portfolio.evidence_cache import get_cached

    entry = get_cached("SPY", cache_path)
    need, reason = needs_refresh(entry, now=now)
    assert need is True
    assert reason == RefreshReason.STALE
    need2, reason2 = needs_refresh(entry, now=now, using_high_concentration_capacity=True)
    assert reason2 == RefreshReason.HIGH_CONCENTRATION_CAPACITY
    need3, reason3 = needs_refresh(entry, now=now, human_request=True)
    assert reason3 == RefreshReason.HUMAN_REQUEST

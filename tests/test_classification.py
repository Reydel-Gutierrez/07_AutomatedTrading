from agentic_portfolio.classification import classify
from agentic_portfolio.schemas import (
    ClassificationEvidence,
    ClassificationStatus,
    EmbeddedSectorStatus,
    SecurityClass,
)


def test_seed_list_alone_is_not_broad_market():
    r = classify(
        "SPY",
        ClassificationEvidence(seed_list_match=True, instrument_kind=None),
    )
    assert r.status == ClassificationStatus.INSUFFICIENT_EVIDENCE
    assert r.effective_class_for_ceiling != SecurityClass.BROAD_MARKET_INDEX_ETF


def test_validated_broad_market_spy_like():
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
            constituent_count=503,
            max_sector_weight=0.28,
            seed_list_match=True,
        ),
    )
    assert r.status == ClassificationStatus.VALIDATED
    assert r.security_class == SecurityClass.BROAD_MARKET_INDEX_ETF


def test_sector_etf_is_other_not_broad():
    r = classify(
        "XLK",
        ClassificationEvidence(
            instrument_kind="etf",
            is_leveraged=False,
            is_inverse=False,
            is_thematic=False,
            is_sector_or_industry_fund=True,
            is_narrow_factor=False,
            is_single_stock_fund=False,
            underlying_index="Technology Select Sector",
            constituent_count=60,
        ),
    )
    assert r.security_class == SecurityClass.OTHER_DIVERSIFIED_ETF
    assert r.effective_class_for_ceiling != SecurityClass.BROAD_MARKET_INDEX_ETF


def test_missing_diversification_metrics_fail_closed_no_40():
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
            seed_list_match=True,
        ),
    )
    assert r.status == ClassificationStatus.INSUFFICIENT_EVIDENCE
    assert r.effective_class_for_ceiling == SecurityClass.OTHER_DIVERSIFIED_ETF


def test_definitional_broad_index_without_invented_weights():
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
            embedded_sector_exposure_status=EmbeddedSectorStatus.UNKNOWN,
        ),
    )
    assert r.security_class == SecurityClass.BROAD_MARKET_INDEX_ETF
    assert r.status == ClassificationStatus.VALIDATED
    assert r.evidence is not None
    assert r.evidence.embedded_sector_weights is None


def test_leveraged_etf_not_broad_market():
    r = classify(
        "TQQQ",
        ClassificationEvidence(
            instrument_kind="etf",
            is_leveraged=True,
            is_inverse=False,
            is_thematic=False,
            is_sector_or_industry_fund=False,
            is_narrow_factor=False,
            is_single_stock_fund=False,
            underlying_index="Nasdaq-100",
        ),
    )
    assert r.security_class == SecurityClass.INDIVIDUAL_EQUITY
    assert r.effective_class_for_ceiling != SecurityClass.BROAD_MARKET_INDEX_ETF


def test_individual_company_classification():
    r = classify(
        "AAPL",
        ClassificationEvidence(
            instrument_kind="equity",
            sector_label_raw="Electronic Technology",
        ),
    )
    assert r.security_class == SecurityClass.INDIVIDUAL_EQUITY
    assert r.status == ClassificationStatus.VALIDATED
    assert r.sector.value == "INFORMATION_TECHNOLOGY"


def test_unknown_sector_not_fabricated():
    r = classify(
        "ZZZZ",
        ClassificationEvidence(instrument_kind="equity", sector_label_raw="Not A Real Sector"),
    )
    assert r.sector.value == "UNKNOWN"
    assert r.sector_status.value == "UNKNOWN"


def test_conflicting_classification_evidence_fails_closed():
    r = classify(
        "SPY",
        ClassificationEvidence(
            instrument_kind="etf",
            is_leveraged=False,
            conflict_notes=["instrument_kind_etf_and_equity"],
        ),
    )
    assert r.status == ClassificationStatus.CONFLICTING_EVIDENCE
    assert r.effective_class_for_ceiling != SecurityClass.BROAD_MARKET_INDEX_ETF

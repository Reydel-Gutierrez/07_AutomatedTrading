from agentic_portfolio.sectors import CanonicalSector, SectorStatus, map_sector


def test_gics_label_maps():
    s, st = map_sector("Information Technology")
    assert s == CanonicalSector.INFORMATION_TECHNOLOGY
    assert st == SectorStatus.MAPPED


def test_robinhood_factset_label_maps_to_canonical():
    s, st = map_sector("Electronic Technology")
    assert s == CanonicalSector.INFORMATION_TECHNOLOGY
    assert st == SectorStatus.MAPPED


def test_reit_industry_overrides_finance_sector():
    s, st = map_sector("Finance", industry="Real Estate Investment Trusts")
    assert s == CanonicalSector.REAL_ESTATE


def test_etf_miscellaneous_is_unknown_not_fabricated():
    s, st = map_sector("Miscellaneous", industry="Investment Trusts Or Mutual Funds")
    assert s == CanonicalSector.UNKNOWN
    assert st == SectorStatus.UNKNOWN


def test_unmapped_label_is_unknown():
    s, st = map_sector("Completely Invented Sector Name")
    assert s == CanonicalSector.UNKNOWN
    assert st == SectorStatus.UNKNOWN


def test_canonical_enum_passthrough():
    s, st = map_sector("HEALTH_CARE")
    assert s == CanonicalSector.HEALTH_CARE

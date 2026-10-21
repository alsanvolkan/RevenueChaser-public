"""R3 cluster routing — classify_ticker's override/direct-map/keyword/broad-match cascade,
plus the cluster metadata lookup helpers. Previously had zero coverage despite being central
to which sector-scorer rule3_financial routes a ticker into.
"""
from RevenueChaser.module_earnings_radar.classifier import (
    classify_ticker,
    get_cluster_label,
    get_cluster_red_flags,
)


class TestTickerOverrides:
    def test_override_wins_over_sector(self):
        # MARA sits in Financial Services but must always route to crypto mining.
        assert classify_ticker("MARA", sector="Financial Services") == "C4_crypto_mining"
        assert classify_ticker("LYFT", sector="Technology") == "C5_marketplace_consumer"

    def test_override_is_case_insensitive_on_ticker(self):
        assert classify_ticker("mara") == "C4_crypto_mining"
        assert classify_ticker("cart") == "C5_marketplace_consumer"


class TestDirectSectorMap:
    """Keys are the Morningstar sector names the spine stores (UW's own list, #266)."""

    def test_exact_sector_key_hits(self):
        assert classify_ticker("A", sector="Technology") == "C1_saas"
        assert classify_ticker("B", sector="Communication Services") == "C2_hardware"
        assert classify_ticker("C", sector="Financial Services") == "C3_fintech_lending"
        assert classify_ticker("D", sector="Healthcare") == "C7_healthcare"
        assert classify_ticker("E", sector="Energy") == "C6_energy_utilities"
        assert classify_ticker("F", sector="Consumer Cyclical") == "C5_marketplace_consumer"

    def test_every_morningstar_sector_has_a_cluster(self):
        from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS
        from RevenueChaser.module_earnings_radar.classifier import GICS_SECTOR_MAP
        assert set(GICS_SECTOR_MAP) == set(MORNINGSTAR_SECTORS)
        assert {s: classify_ticker("X", sector=s) for s in MORNINGSTAR_SECTORS} == {
            "Basic Materials": "C6_energy_utilities", "Communication Services": "C2_hardware",
            "Consumer Cyclical": "C5_marketplace_consumer", "Consumer Defensive": "C5_marketplace_consumer",
            "Energy": "C6_energy_utilities", "Financial Services": "C3_fintech_lending",
            "Healthcare": "C7_healthcare", "Industrials": "C2_hardware", "Real Estate": "C3_fintech_lending",
            "Technology": "C1_saas", "Utilities": "C6_energy_utilities"}

    def test_hardware_industry_overrides_technology_saas(self):
        # Semis/hardware under Technology are NOT SaaS: NVDA/AMD/MU/AVGO.
        assert classify_ticker("G", sector="Technology", industry="Semiconductor Equipment & Materials") == "C2_hardware"

    def test_hardware_hints_route_technology_semis_to_hardware(self):
        assert classify_ticker("NVDA", sector="Technology", industry="Semiconductors") == "C2_hardware"
        assert classify_ticker("AAPL", sector="Technology", industry="Consumer Electronics") == "C2_hardware"
        assert classify_ticker("DELL", sector="Technology", industry="Computer Hardware") == "C2_hardware"

    def test_software_industry_stays_saas_under_technology(self):
        # Pure software is not caught by the semi/hardware hints.
        assert classify_ticker("MSFT", sector="Technology", industry="Software - Application") == "C1_saas"
        assert classify_ticker("Q", sector="Technology", industry="Software - Infrastructure") == "C1_saas"
        assert classify_ticker("IBM", sector="Technology", industry="Information Technology Services") == "C1_saas"
        # "Electronic Gaming" must not trip the "electronics" hint
        assert classify_ticker("EA", sector="Technology", industry="Electronic Gaming & Multimedia") == "C1_saas"

    def test_the_hardware_hint_only_reroutes_technology(self):
        # The hints only re-route Technology; a hardware-sounding industry elsewhere keeps its sector's cluster.
        assert classify_ticker("H", sector="Industrials", industry="Aerospace & Defense") == "C2_hardware"
        assert classify_ticker("I", sector="Healthcare", industry="Medical Devices") == "C7_healthcare"
        assert classify_ticker("J", sector="Consumer Cyclical", industry="Consumer Electronics") == "C5_marketplace_consumer"


class TestKeywordFallback:
    def test_keyword_match_on_unmapped_sector(self):
        # "Unknown Sector" isn't a GICS_SECTOR_MAP key, so this only resolves via industry keyword.
        result = classify_ticker("H", sector="Unknown Sector", industry="Semiconductor Equipment & Materials")
        assert result == "C2_hardware"

    def test_keyword_priority_order_c7_before_others(self):
        # "biotech" (C7 keyword) and "software" (C1 keyword) both present; C7 is checked first
        # in the loop order, so it must win even though "software" also matches.
        result = classify_ticker("I", sector="Unknown Sector", industry="biotech software platform")
        assert result == "C7_healthcare"

    def test_keyword_priority_order_c1_before_c3(self):
        # "software" (C1) and "bank" (C3) both present; C1 is checked before C3.
        result = classify_ticker("J", sector="Unknown Sector", industry="banking software provider")
        assert result == "C1_saas"


class TestLowercaseSectorFallback:
    """Lowercased Morningstar sector names still route (keyword or broad-match step)."""

    def test_lowercase_technology(self):
        assert classify_ticker("K", sector="technology") == "C1_saas"

    def test_real_estate(self):
        assert classify_ticker("L", sector="real estate") == "C3_fintech_lending"

    def test_basic_materials(self):
        assert classify_ticker("M", sector="basic materials") == "C2_hardware"

    def test_utilities(self):
        assert classify_ticker("N", sector="utilities") == "C6_energy_utilities"

    def test_financial_services_consumer_and_healthcare(self):
        assert classify_ticker("O1", sector="financial services") == "C3_fintech_lending"
        assert classify_ticker("O2", sector="consumer cyclical") == "C5_marketplace_consumer"
        assert classify_ticker("O3", sector="consumer defensive") == "C5_marketplace_consumer"
        assert classify_ticker("O4", sector="healthcare") == "C7_healthcare"
        assert classify_ticker("O5", sector="industrials") == "C2_hardware"
        assert classify_ticker("O6", sector="energy") == "C6_energy_utilities"


class TestUnmapped:
    def test_no_sector_or_industry_returns_unmapped(self):
        assert classify_ticker("O") == "unmapped"

    def test_unclassified_returns_unmapped(self):
        assert classify_ticker("O", sector="Unclassified", industry="Unclassified") == "unmapped"

    def test_unrecognized_sector_and_industry_returns_unmapped(self):
        assert classify_ticker("P", sector="Made Up Sector", industry="made up industry") == "unmapped"


class TestClusterMetadataLookups:
    def test_get_cluster_label_known_and_fallback(self):
        assert get_cluster_label("C1_saas") == "SaaS/Software"
        assert get_cluster_label("unmapped") == "Unmapped"
        assert get_cluster_label("not_a_real_cluster") == "not_a_real_cluster"

    def test_get_cluster_red_flags_known_and_fallback(self):
        assert "nim_compression" in get_cluster_red_flags("C3_fintech_lending")
        assert get_cluster_red_flags("not_a_real_cluster") == get_cluster_red_flags("unmapped")

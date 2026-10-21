from typing import Optional

TICKER_OVERRIDES = {
    "MARA": "C4_crypto_mining",
    "CLSK": "C4_crypto_mining",
    "LYFT": "C5_marketplace_consumer",
    "CART": "C5_marketplace_consumer",
}

# Morningstar sector -> cluster. Keys are the 11 sector names the spine stores (UW's own list,
# ``core.gics_map.MORNINGSTAR_SECTORS``, #266); a vendor or older spelling must go through
# ``core.gics_map.canonical_sector`` before reaching this map.
GICS_SECTOR_MAP = {
    "Technology": "C1_saas",
    "Communication Services": "C2_hardware",
    "Industrials": "C2_hardware",
    "Consumer Cyclical": "C5_marketplace_consumer",
    "Consumer Defensive": "C5_marketplace_consumer",
    "Financial Services": "C3_fintech_lending",
    "Real Estate": "C3_fintech_lending",
    "Healthcare": "C7_healthcare",
    "Energy": "C6_energy_utilities",
    "Utilities": "C6_energy_utilities",
    "Basic Materials": "C6_energy_utilities",
}

# Industry-text keyword matching (fallback when sector alone doesn't match)
GICS_C1_KEYWORDS = {"software", "internet", "data processing", "cloud", "saas", "it services", "information technology"}
GICS_C2_KEYWORDS = {"semiconductor", "hardware", "electronic", "aerospace", "defense", "industrial", "computer", "machinery"}
GICS_C3_KEYWORDS = {"bank", "financial", "insurance", "financing", "capital market", "mortgage", "asset management", "investment"}
GICS_C5_KEYWORDS = {"retail", "consumer", "restaurant", "hotel", "entertainment", "travel", "leisure", "apparel", "e-commerce"}
GICS_C6_KEYWORDS = {"energy", "utility", "oil", "gas", "mining", "metals", "chemical", "electric", "pipeline", "power", "water", "renewable"}
GICS_C7_KEYWORDS = {"health", "pharmaceutical", "biotech", "medical", "drug", "hospital", "life science", "diagnostic"}

# Hardware/semi industry hints that OVERRIDE the "all Technology = SaaS" sector
# map. They read the industry text of the spine (UW's, e.g. ``Semiconductors``,
# ``Consumer Electronics``, ``Computer Hardware``): NVDA/AMD/MU/TSM/AVGO and AAPL
# must route to C2_hardware, not C1_saas. Kept tighter than GICS_C2_KEYWORDS so
# "Software - Application" / "Electronic Gaming & Multimedia" do not get misrouted.
SEMI_HARDWARE_HINTS = (
    "semiconductor", "semis", "hardware", "electronics", "data storage",
    "foundry", "peripherals",
)


def classify_ticker(
    ticker: str,
    sector: str = "",
    industry: str = "",
) -> str:
    upper = ticker.upper()

    if upper in TICKER_OVERRIDES:
        return TICKER_OVERRIDES[upper]

    sector = (sector or "").strip()
    industry = (industry or "").strip()
    sec_lower = sector.lower()
    ind_lower = industry.lower()
    combined = sec_lower + " " + ind_lower

    # 1. Direct sector → cluster mapping
    if sector in GICS_SECTOR_MAP:
        mapped = GICS_SECTOR_MAP[sector]
        # Hardware/semis detected by industry override 'all Technology = SaaS':
        # NVDA/AAPL/AVGO/MU are not software just because their sector is Technology.
        if mapped == "C1_saas" and any(kw in combined for kw in SEMI_HARDWARE_HINTS):
            return "C2_hardware"
        return mapped

    # 2. Keyword matching on sector + industry

    for keyword, cluster in [
        (GICS_C7_KEYWORDS, "C7_healthcare"),
        (GICS_C1_KEYWORDS, "C1_saas"),
        (GICS_C3_KEYWORDS, "C3_fintech_lending"),
        (GICS_C5_KEYWORDS, "C5_marketplace_consumer"),
        (GICS_C6_KEYWORDS, "C6_energy_utilities"),
        (GICS_C2_KEYWORDS, "C2_hardware"),
    ]:
        if any(kw in combined for kw in keyword):
            return cluster

    # 3. Sector-level broad match (Morningstar names only)
    if sec_lower == "technology":
        return "C1_saas"
    if sec_lower in {"financial services", "real estate"}:
        return "C3_fintech_lending"
    if sec_lower in {"consumer cyclical", "consumer defensive"}:
        return "C5_marketplace_consumer"
    if sec_lower in {"industrials", "basic materials"}:
        return "C2_hardware"
    if sec_lower in {"energy", "utilities"}:
        return "C6_energy_utilities"
    if sec_lower == "healthcare":
        return "C7_healthcare"

    return "unmapped"


def get_cluster_label(cluster: str) -> str:
    labels = {
        "C1_saas": "SaaS/Software",
        "C2_hardware": "Hardware/Semis",
        "C3_fintech_lending": "Fintech/Lending",
        "C4_crypto_mining": "Crypto Mining",
        "C5_marketplace_consumer": "Marketplace/Consumer",
        "C6_energy_utilities": "Energy/Utilities",
        "C7_healthcare": "Healthcare",
        "unmapped": "Unmapped",
    }
    return labels.get(cluster, cluster)


def get_cluster_red_flags(cluster: str) -> list[str]:
    flags = {
        "C1_saas": [
            "deferred_revenue_deceleration",
            "margin_contraction_despite_growth",
        ],
        "C2_hardware": [
            "inventory_growth_exceeds_revenue_growth",
            "negative_gross_margin_elasticity",
        ],
        "C3_fintech_lending": [
            "nim_compression",
            "deteriorating_efficiency_ratio",
            "high_receivables",
        ],
        "C4_crypto_mining": [
            "cost_to_mine_rises_faster_than_btc",
            "declining_cash_btc_reserve_coverage",
        ],
        "C5_marketplace_consumer": [
            "take_rate_compression",
            "margin_compression",
        ],
        "C6_energy_utilities": [
            "negative_gm_elasticity",
            "thin_operating_margin",
        ],
        "C7_healthcare": [
            "low_gross_margin",
            "revenue_contraction",
        ],
        "unmapped": ["sloan_accruals_high", "cash_flow_conversion_declining"],
    }
    return flags.get(cluster, flags["unmapped"])

"""Value Chain board configuration.

The corpus is the latest 10-K (else 20-F) per issuer; edges are supplier /
customer relations stated in Item 1 (10-K) or Item 4 (20-F). See
docs/plan_rest.md.
"""
from __future__ import annotations

from RevenueChaser.core.config import OUTPUT_DIR

VALUE_CHAIN_LATEST = OUTPUT_DIR / "value_chain_latest.html"
VALUE_CHAIN_JSON = OUTPUT_DIR / "value_chain" / "value_chain_latest.json"

# First ship is a seed list; the full universe crawl is a later, resumable run.
SEED: tuple[str, ...] = (
    "NVDA", "AMD", "TSM", "ASML", "AAPL", "MSFT", "AVGO", "AMAT", "SNPS",
    "CDNS", "MU", "INTC", "QCOM", "TXN", "LRCX", "KLAC", "MRVL", "ON", "NXPI",
    "ARM", "GOOGL", "META", "AMZN", "ORCL", "CRM", "ADBE", "NOW", "PANW",
    "CRWD", "SNOW", "NET", "DDOG", "ANET", "DELL", "HPQ", "WDC", "STX",
    "TER", "MCHP", "ADI", "SWKS", "QRVO", "MPWR", "ENTG", "AZTA", "COHR",
    "SMCI", "VRT", "ETN", "APH",
)

# The heading patterns and the two length limits moved to ``rcdata/sec_sections.py``, which owns every version of
# the cut (#752). They are re-exported here unchanged, because they are read as ``config.<name>`` from
# ``rcdata/build/fundamentals/filings.py``, ``module_relatedness/config.py`` and ``module_value_chain/history.py``,
# and because this board's own tests assert on the regexes themselves. Their reasoning (a filer writes
# "Item 1 — Business" with any dash, breaks words as "Bus iness", and a 10-K stop needs the section title so a
# cross-reference does not cut the body short, #116 C17) is in that module beside the patterns.
from RevenueChaser.rcdata.sec_sections import (  # noqa: F401  (re-exported for this board's callers and tests)
    DASH,
    MAX_ITEM_CHARS,
    MIN_ITEM_CHARS,
    START_PATTERNS,
    STOP_PATTERNS,
    spaced,
)

_DASH = DASH
_spaced = spaced

MENTION_WINDOW = 120          # chars around a name scanned for a relation verb
MAX_HOP2 = 40                 # cap edges followed on the second hop

# A ticker whose CIK has no usable annual filing (none, or a 40-F) is recorded as
# a ``miss:<TICKER>`` key in ``chain_meta`` and not requested again for this many
# days, or sooner when its CIK changes (#116 C26). 7 days: the daily sync's
# repeat submissions calls drop about 7x, and a newly listed issuer's first
# 10-K still reaches the board within a week. A missing CIK is only counted.
MISS_RETRY_DAYS = 7

# Default corpus = the shared classification universe (>= the universe), so Value Chain,
# Co-movement and Relatedness read the same ticker set (core/universe.py). SEED
# remains an explicit override for tests/demos.
USE_SHARED_UNIVERSE = True

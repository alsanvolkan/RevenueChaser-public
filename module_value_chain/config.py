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

# 10-K: Item 1 Business -> stop at Item 1A (else 1B / 2). 20-F: Item 4 -> stop at
# Item 4A / 5. Headers come with a period, colon or any dash ("Item 1 — Business",
# "Item 1A—Risk Factors"), as "Items 1 and 2", or as "Description of Business".
# A 10-K stop needs the section title, so a cross-reference such as "see Item 1A of
# this report" does not cut the body short (#116 C17).
_DASH = r"\-\u2010-\u2015\u2212"     # hyphen-minus, U+2010..U+2015 dashes, minus sign
START_PATTERNS = {
    "10-K": (rf"\bitems?\s*1\s*\.?\s*(?:(?:and|&|,)\s*2\s*\.?\s*)?[\:{_DASH}]*\s*"
             r"(?:(?:description\s+of|our)\s+(?:the\s+)?)?b\s?usiness"),
    "20-F": (rf"\bitem\s*4\s*[\.\:{_DASH}\s]\s*[{_DASH}]?\s*"
             r"information\s+on\s+the\s+(?:company|group)"),
}
STOP_PATTERNS = {
    "10-K": (rf"\bitem\s*1a\s*[\.\:{_DASH}\s]*\s*risk\s+factors"
             rf"|\bitem\s*1b\s*[\.\:{_DASH}\s]*\s*unresolved"
             rf"|\bitem\s*2\s*[\.\:{_DASH}\s]*\s*(?:description\s+of\s+)?propert"),
    "20-F": rf"\bitem\s*4a\s*[\.\:{_DASH}\s]|\bitem\s*5\s*[\.\:{_DASH}\s]",
}

MIN_ITEM_CHARS = 2000
MAX_ITEM_CHARS = 200_000      # cap stored text; a missed stop-regex can be huge
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

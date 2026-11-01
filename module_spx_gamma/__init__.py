"""SPX Critical Thresholds — Unusual Whales ground-up build.

Self-contained module that renders the SPX critical thresholds report: an
8-week SPX daily candlestick window with option-positioning overlays, a
critical levels table, and concentration warnings.
"""

#: The needs this board reads (``rcdata.needs.boards``; #330), all through ``store_reads``.
READS = ("market.cot", "market.cot_release", "market.spx_close", "market.spx_expiry_strike", "market.vix")

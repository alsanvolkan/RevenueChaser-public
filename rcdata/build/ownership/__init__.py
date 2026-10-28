"""The ownership store (build plan PR 16): who is short a name, who fails to deliver it, who holds it at quarter end
and who trades it from inside, served from ``ownership.duckdb``.

Each source dataset fetches raw files, types them to Parquet and builds its own served table from that Parquet:

- ``finra/short_interest``: FINRA's consolidated short interest, twice a month, one settlement date at a time;
- ``regsho/threshold``: the Reg SHO threshold lists of Nasdaq and the NYSE markets, one file per market and day.

A symbol a source writes is named by the spine (``common.symbol_tickers``): the ticker itself when the live
``security`` table holds it, else the one ticker that squashes to it (``BFA`` -> ``BF-A``), else none. The spine is
read, never written.
"""
from __future__ import annotations

KIND = "ownership"

"""The ownership store (build plan PR 16): who is short a name, who fails to deliver it, who holds it at quarter end
and who trades it from inside, served from ``ownership.duckdb``.

Each source dataset fetches raw files, types them to Parquet and builds its own served table from that Parquet:

- ``finra/short_interest``: FINRA's consolidated short interest, twice a month, one settlement date at a time;
- ``regsho/threshold``: the Reg SHO threshold lists of Nasdaq and the NYSE markets, one file per market and day;
- ``ibkr/shortable``: IBKR's public short-stock file (borrow fee, rebate, shares available), once a session;
- ``sec/ftd``: the SEC's fails-to-deliver data, one file per half month;
- ``sec/13f``, ``sec/form4``, ``sec/13dg``: Form 13F, Form 4 and Schedules 13D and 13G as they arrive, walked
  through the EDGAR daily index (``edgar.Feed``), each request paced for the SEC (``sec.Pacer``).

``ownership/serve`` is built last: the 13F totals per ticker and quarter, each CUSIP named through the spine, and
the views under the old table names (``sec13f_totals``, ``short_interest``).

A symbol a source writes is named by the spine (``common.symbol_tickers``): the ticker itself when the live
``security`` table holds it, else the one ticker that squashes to it (``BFA`` -> ``BF-A``), else none; an issuer by
its CIK or CUSIP (``common.issuer_tickers``, ``common.cusip_issuer_map``). The spine is read, never written.
"""
from __future__ import annotations

KIND = "ownership"

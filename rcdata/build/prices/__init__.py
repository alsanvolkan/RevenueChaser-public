"""The prices store (build plan PR 7): daily bars, corporate actions, shares, market cap and float, served from
``prices.duckdb``.

Source datasets fetch raw files and type them to Parquet; none of them writes a serving table of its own. Each one's
``build`` loads its Parquet into TEMP staging tables of the build connection (never stored in the file), and the
last dataset of the kind, ``prices/serving``, builds the served tables and the bridge views from them:

- ``alpaca/bars`` (daily, after the close): Alpaca's daily bars of the scope (below), ``adjustment`` raw and all,
  100 symbols a request; the last 5 sessions are read again every run and a changed value is kept as a revision;
- ``alpaca/corporate_actions`` (daily): Alpaca's splits, dividends, spin-offs and name changes processed from two
  weeks back to 90 days ahead (a dividend is processed on its payable date);
- ``sec/cover_shares`` (daily): the shares outstanding per class from the cover page of each new 10-K and 10-Q
  (the filing's XBRL), found through the SEC's daily index;
- ``yfinance/float`` (daily, a slice): Yahoo's float, paced 1.5 s apart, stopped on a 401 or 429;
- ``uw/total_float`` (daily): Unusual Whales' ``total_float``, the daily check of the float;
- ``wrds/prices`` (attended, monthly; **code only until Phase 8**): Compustat ``secd`` from 1984 and CRSP
  ``dsf_v2`` to 2025, with Compustat's dividends and splits, in ``year=`` partitions;
- ``prices/serving``: no fetch; the served tables, the views under the old ``prices.duckdb`` names, and the build
  issues (``serving.ISSUE_KINDS``), synced to ``ops.sqlite`` once the new file is live.

**The scope** of the daily fetches is the spine's Tier A, the three benchmarks, the rotation ETFs the old nightly
kept bars for, and the stock watch list (``scope``). The spine is read from its live file, read-only.

**The security id** keys every served table: ``<gvkey>:<iid>`` when the spine links the ticker to Compustat, else
``figi:<composite FIGI>``, else ``cik:<CIK>:<ticker>``, else ``sym:<ticker>`` (``security_id``). Parquet keeps what
the source said (the symbol, and the ticker and CIK the spine gave it at the fetch) and the id of that day; the build
resolves each row against the spine it builds with, so an id the spine improved later (a Compustat link found)
reaches the old rows too, and a ticker reused by another company (another CIK) never does.
"""
from __future__ import annotations

from ....core.rotation_reference import ROTATION_ETFS

KIND = "prices"
BENCHMARKS = ("SPY", "DIA", "QQQ")
# The rotation ETFs the old nightly kept bars for (``core/prices.bar_scope``): every group but yield and commodity.
ROTATION = tuple(t for t, info in ROTATION_ETFS.items() if (info or {}).get("group") not in ("yield", "commodity"))
SPINE_COLUMNS = ("ticker", "cik", "tier", "gvkey", "iid", "composite_figi", "exchange", "security_type", "name")


class Spacer:
    """At least ``interval`` seconds between two calls on the run's clock: ``wait()`` before each call. The SEC's
    fair access and Yahoo's pacing are a spacing, not a count per minute (``base.Pacer`` would allow a burst)."""

    def __init__(self, interval: float, clock, sleep):
        self.interval, self._clock, self._sleep, self._last = interval, clock, sleep, None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            self._sleep(self.interval - (now - self._last))
            now = self._clock()
        self._last = now


def security_id(gvkey=None, iid=None, composite_figi=None, cik=None, ticker=None) -> str:
    """The prices store's key of one security, from the strongest identifier the spine has for it."""
    if gvkey and iid:
        return f"{gvkey}:{iid}"
    if composite_figi:
        return f"figi:{composite_figi}"
    t = str(ticker or "").strip().upper()
    return f"cik:{cik}:{t}" if cik else f"sym:{t}"


def alpaca_symbol(ticker: str) -> str:
    """The SEC file's ticker as Alpaca writes it: a share class with a dot (``BRK-B`` -> ``BRK.B``)."""
    return str(ticker).strip().upper().replace("-", ".")


def sec_ticker(symbol: str) -> str:
    """Alpaca's symbol as the SEC file writes it (``BRK.B`` -> ``BRK-B``)."""
    return str(symbol).strip().upper().replace(".", "-")


def spine_rows() -> list[dict]:
    """Every row of the live spine's ``security`` table (the columns of ``SPINE_COLUMNS``), read-only; ``[]`` when
    no spine has been built yet."""
    from ... import serving
    try:
        con = serving.open_read("spine")
    except serving.ServingFileMissing:
        return []
    try:
        cur = con.execute(f"SELECT {', '.join(SPINE_COLUMNS)} FROM security ORDER BY ticker")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def with_id(row: dict) -> dict:
    return {**row, "security_id": security_id(row.get("gvkey"), row.get("iid"), row.get("composite_figi"),
                                              row.get("cik"), row.get("ticker"))}


def scope(rows: list[dict] | None = None, watched: set[str] | None = None) -> list[dict]:
    """The names the daily fetches ask for: Tier A, then the benchmarks, the rotation ETFs and the stock watch list
    (any tier), each once, as ``{ticker, cik, tier, security_id, ...}``. A benchmark, ETF or watched name the spine
    does not hold is asked under its ticker alone (``sym:<ticker>``)."""
    if rows is None:
        rows = spine_rows()
    if watched is None:
        from ... import read
        watched = read.watched("stock")
    by_ticker = {r["ticker"]: r for r in rows}
    out, seen = [], set()
    for r in rows:
        if r.get("tier") == "A":
            out.append(with_id(r))
            seen.add(r["ticker"])
    for t in (*BENCHMARKS, *ROTATION, *sorted(watched)):
        t = str(t).strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(with_id(by_ticker.get(t) or {"ticker": t, "cik": None, "tier": None}))
    return out

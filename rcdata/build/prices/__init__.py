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

**The scope** of the daily fetches is the spine's Tier A and B (``DEFAULT_TIERS``), the three benchmarks, the
rotation ETFs the old nightly kept bars for, the stock watch list, and the other listed classes the SEC covers
name for a Tier A issuer (``scope``). A request may narrow it to its own tiers or names (#265).
The spine is read from its live file, read-only.

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
# The tiers a daily fetch asks when the request names none: the listed universe, Tier A and B (avo, 30 Oct 2026:
# "the limit is A+B", no market-cap floor; 5,655 + 2,015 = 7,670 names on the live spine). A request may narrow it
# (``scope(want=...)``, #265).
DEFAULT_TIERS = ("A", "B")


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


def cover_classes(rows: list[dict]) -> list[dict]:
    """The listed share classes of the Tier A issuers that the SEC covers name (a ``dei:TradingSymbol`` with a share
    count, ``sec/cover_shares``), as names to ask: the spine's row of the symbol, else ``class:<CIK>:<member>``, the
    id ``prices/serving`` keys that class's count under. So the issuer cap counts every listed class, Berkshire's A
    shares too, which the spine does not hold (the server's sample, 29 Oct 2026: BRK-B's cap 0.658 of Yahoo's)."""
    import duckdb

    from .. import base
    from .stage import partitions
    files = [f for _, f in partitions(base.parquet_root() / "sec" / "cover_shares", "year")]
    if not files:
        return []
    con = duckdb.connect()
    try:
        found = con.execute("SELECT cik, coalesce(nullif(class_member, ''), 'common'), arg_max(symbol, as_of) "
                            f"FROM read_parquet([{', '.join(base.sql_path(f) for f in files)}], union_by_name = true) "
                            "WHERE symbol IS NOT NULL GROUP BY ALL ORDER BY 1, 2").fetchall()
    finally:
        con.close()
    issuers = {r.get("cik") for r in rows if r.get("tier") == "A"}
    by_key = {(r.get("cik"), r["ticker"]): r for r in rows}
    out = []
    for cik, member, symbol in found:
        if cik in issuers:
            ticker = sec_ticker(symbol)
            r = by_key.get((cik, ticker))
            out.append(with_id(r) if r else {"ticker": ticker, "cik": cik, "tier": None,
                                             "security_id": f"class:{cik}:{member}"})
    return out


def scope(rows: list[dict] | None = None, watched: set[str] | None = None,
          want: "base.Scope | None" = None) -> list[dict]:
    """The names the daily fetches ask for, as ``{ticker, cik, tier, security_id, ...}``.

    A request that names nothing (``want`` empty) is the daily run: the spine's ``DEFAULT_TIERS``, then the
    benchmarks and the rotation ETFs (``anchors``), the stock watch list, and the other listed classes the SEC
    covers name for a Tier A issuer (``cover_classes``), each name once. A benchmark, ETF or watched name the
    spine does not hold is asked under its ticker alone (``sym:<ticker>``).

    A request that names something gets **only** what it names (avo's Karar 82, 30 Oct 2026): its ``tiers``, its
    ``names``, and whichever of the three groups it lists in ``include``. So ``tiers=["B"]`` is 2,015 names and not
    2,015 plus every Tier A issuer's other class plus 70 ETFs, which was the opposite of #265's point; and
    ``include=["anchors"]`` alone is a benchmark-only fill.
    """
    from .. import base

    if rows is None:
        rows = spine_rows()
    want = want if want is not None else base.Scope()
    tiers = want.tiers or (() if want else DEFAULT_TIERS)
    groups = want.include if want else base.SCOPE_GROUPS
    by_ticker = {r["ticker"]: r for r in rows}
    out, seen = [], set()

    def add(ticker: str, row: dict | None = None) -> None:
        ticker = str(ticker).strip().upper()
        if not ticker or ticker in seen:
            return
        seen.add(ticker)
        out.append(with_id(row if row is not None else
                           (by_ticker.get(ticker) or {"ticker": ticker, "cik": None, "tier": None})))

    for r in rows:
        if tiers and r.get("tier") in tiers:
            add(r["ticker"], r)
    for t in want.names:
        add(t)
    if "anchors" in groups:
        for t in (*BENCHMARKS, *ROTATION):
            add(t)
    if "watchlist" in groups:
        if watched is None:
            from ... import read
            watched = read.watched("stock")                 # only when it was asked for: this opens the store
        for t in sorted(watched):
            add(t)
    if "cover_classes" in groups:
        for r in cover_classes(rows):
            if r["ticker"] not in seen:
                seen.add(r["ticker"])
                out.append(r)                               # already carries its own security_id
    return out

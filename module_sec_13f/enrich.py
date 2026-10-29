"""The 13F board's snapshot inputs (#375 F2, 13F-B): read from the shared stores, never from a provider.

Per 13F ticker (``load_inputs``), in the shape ``report.board_entries`` reads:

- ``shares_outstanding`` from ``ownership_snapshot`` (D20, ``core/ownership.py``): the newest session of the first
  source in ``SHARES_ORDER`` that holds a value. That is the readers' rule of D20: UW ``info`` (the 13F Fetch's
  shares step), then SEC dei via D11, then yfinance (an AG / universe Fetch). No job stores SEC dei rows yet (#161),
  so its slot is empty until one does.
- ``insider_pct`` in % (the stored fraction × 100, as the board showed it before) from ``ownership_snapshot``, the
  newest session of the first source in the same order that has one (only yfinance rows carry it today).
- ``price``: ``{quarter_end: close}`` from D01 (``price_bars_daily``), nominal (splits applied by
  ``prices.apply_events``, as ``prices.daily_bars`` applies them): the last close on or before each quarter end, at
  most ``PRICE_LOOKBACK_DAYS`` calendar days earlier, read in short bounded windows (``quarter_end_closes`` says
  why not the open-ended K8a window). The board's Price Δ is the change between the first and the last of these
  closes.

Short interest is not read here since G9: the board takes FINRA's from the ownership store
(``store_reads.short_interest``); the old ``short_interest`` table is not read. A ticker with no value for a field has no key for it, so the board shows "—", never 0.

shortcut: shares outstanding, insider % and the quarter-end closes still come from ``raw.duckdb``
(``ownership_snapshot``, ``price_bars_daily``), empty since the 27 Oct purge; upgrade when the fundamentals and price
stores serve them (G5/G2 data, then a board PR).

The Fetch (after its shares step), the store-only Refresh and the ``/api/sec_13f/<t>`` chart all read these inputs
here, so every number comes out of the same path.

``load_enrichment`` reads the retired ``sec13f_enrichment`` rows. Since F2 no board, Refresh or route reads it; the
table stays (#402) and research scripts compare against it.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import ownership, rawstore

SEC_DEI = "sec_dei"          # the D11 slot of the readers' order; no writer stores it yet (#161)
SHARES_ORDER = (ownership.UW_INFO, SEC_DEI, ownership.YFINANCE)
PRICE_LOOKBACK_DAYS = 10     # a quarter end on a weekend or a holiday still finds the session before it


def load_enrichment() -> dict:
    """The retired ``sec13f_enrichment`` rows as ``{ticker: entry}`` (research reads only)."""
    out = {}
    for r in rawstore.get_all("sec13f_enrichment"):
        entry = r.get("entry")
        if isinstance(entry, dict):
            out[r["ticker"]] = entry
    return out


def _rank(source: str) -> int:
    return SHARES_ORDER.index(source) if source in SHARES_ORDER else len(SHARES_ORDER)


def load_ownership(tickers) -> dict[str, dict]:
    """``{ticker: {"shares_outstanding": .., "insider_pct": ..}}`` from ``ownership_snapshot``; a field with no
    stored value in any source is left out. One store read: the newest non-NULL value per (ticker, source)."""
    wanted = {str(t).upper() for t in tickers if t}
    if not wanted:
        return {}
    rows = rawstore.query(
        f"SELECT ticker, source, "
        f"arg_max(shares_outstanding, session_date) FILTER (WHERE shares_outstanding IS NOT NULL) AS shares, "
        f"arg_max(insider_pct, session_date) FILTER (WHERE insider_pct IS NOT NULL) AS insider "
        f"FROM {ownership.TABLE} GROUP BY ticker, source")
    out: dict[str, dict] = {}
    for r in sorted((r for r in rows if r["ticker"] in wanted), key=lambda r: _rank(r["source"])):
        entry = out.setdefault(r["ticker"], {})
        if r["shares"] is not None:
            entry.setdefault("shares_outstanding", float(r["shares"]))
        if r["insider"] is not None:
            entry.setdefault("insider_pct", float(r["insider"]) * 100.0)
    return {t: e for t, e in out.items() if e}


def quarter_end_closes(tickers, quarters) -> dict[str, dict[str, float]]:
    """``{ticker: {quarter_end: close}}``: the last D01 nominal close on or before each quarter end, within
    ``PRICE_LOOKBACK_DAYS``.

    The K8a window (``prices.daily_bars(start=)``) has no upper bound (an adjusted bar needs the closes after it),
    so one window per quarter end would read every bar from 2025 to today for the whole 13F universe: about 53 s on
    a 2,222-ticker store, against a 3 s Refresh. This read is bounded on both sides instead: only the bars inside
    the quarter-end windows, plus the splits after the first window. The nominal series is ``prices.apply_events``
    (the rule ``daily_bars`` applies: a split divides the bars fetched before it), so a close is the one
    ``daily_bars(basis="nominal")`` returns for that session. Dividends do not change a nominal close.
    """
    from RevenueChaser.core import prices
    names = sorted({str(t).strip().upper() for t in tickers if t})
    qs = sorted(set(quarters))
    out: dict[str, dict[str, float]] = {}
    if not names or not qs:
        return out
    windows = [(date.fromisoformat(q) - timedelta(days=PRICE_LOOKBACK_DAYS), date.fromisoformat(q)) for q in qs]
    db = rawstore.MANIFEST["price_bars_daily"].get("db", "raw")
    marks = ", ".join("?" * len(names))
    ranges = " OR ".join("session_date BETWEEN ? AND ?" for _ in windows)
    bars: dict[str, list[dict]] = {}
    for r in rawstore.query(f"SELECT * FROM price_bars_daily WHERE ticker IN ({marks}) AND ({ranges})",
                            [*names, *(d for w in windows for d in w)], db=db):
        bars.setdefault(r["ticker"], []).append(r)
    splits: dict[str, list[dict]] = {}
    for r in rawstore.query(f"SELECT * FROM price_actions WHERE action = 'split' AND ticker IN ({marks}) "
                            f"AND ex_date > ?", [*names, windows[0][0]], db=db):
        splits.setdefault(r["ticker"], []).append(r)
    for ticker, rows in bars.items():
        series = [b for b in prices.apply_events(rows, splits.get(ticker, []), "nominal") if b.get("close") is not None]
        for (start, end), q in zip(windows, qs):
            inside = [b for b in series if start <= b["session_date"] <= end]
            if inside:
                out.setdefault(ticker, {})[q] = float(inside[-1]["close"])
    return out


def load_inputs(totals: dict) -> dict[str, dict]:
    """The board's snapshot inputs of every ticker in ``totals`` (``{ticker: {period: cell}}``): see the module
    docstring. Store reads only, zero network."""
    from RevenueChaser.module_sec_13f import engine
    tickers = [t for t in totals if t]
    quarters = sorted({q for periods in totals.values() for q in periods if q >= engine.START_QUARTER})
    out = load_ownership(tickers)
    for ticker, px in quarter_end_closes(tickers, quarters).items():
        out.setdefault(ticker, {})["price"] = px
    return out

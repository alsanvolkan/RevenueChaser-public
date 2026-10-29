"""The read API of the market store (D4): the only place a board gets market data, and the only SQL it runs.

Serving kinds: ``market`` (``market_series``, ``spx_expiry_strike``), since PR 5 the identity ``spine``
(``universe``, ``identity``, ``membership``), since PR 16 ``ownership`` (``holders_13f``, ``periods_13f``,
``finra_short_interest``, ``shortable``, ``threshold_list``, ``fails_to_deliver``, ``insider_transactions``,
``beneficial_ownership``), and since PR 12 the ``fundamentals`` layer (``statements``, ``earnings_events``,
``estimates``, ``filing_text``, ``segments``, ``customers``, ``peers``, ``revisions``).

Every row comes back with its provenance (``session_date``, ``source``, ``fetched_at``, ``source_as_of``, ``run_id``)
and ``status``, the newest ``dataset_check`` result of its dataset (``never_checked`` before the first). Each call
opens the live serving file read-only and closes it again (``serving.open_read``), and counts one read of its table
in the read log. No serving file yet means no rows, not an error: the status says why.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from ..core import calendar
from . import ops, serving

NEVER_CHECKED = "never_checked"
DATASET_OF_TABLE = {"market_series": "market_series", "spx_gamma_expiry_strike": "uw/spot_expiry_strike",
                    "security": "spine/security", "index_membership": "funds/index_holdings"}
KIND_OF_TABLE = {"market_series": "market", "spx_gamma_expiry_strike": "market", "security": "spine",
                 "index_membership": "spine"}
# The ownership store (PR 16).
DATASET_OF_TABLE.update({"sec13f_ticker_period": "ownership/serve", "sec13f_periods": "ownership/serve",
                         "finra_short_interest": "finra/short_interest", "ibkr_shortable": "ibkr/shortable",
                         "regsho_threshold": "regsho/threshold", "sec_ftd": "sec/ftd",
                         "insider_transactions": "sec/form4", "beneficial_ownership": "sec/13dg"})
KIND_OF_TABLE.update({t: "ownership" for t in ("sec13f_ticker_period", "sec13f_periods", "finra_short_interest",
                                               "ibkr_shortable", "regsho_threshold", "sec_ftd",
                                               "insider_transactions", "beneficial_ownership")})


def dataset_status(dataset: str) -> str:
    check = ops.latest_check(dataset)
    return check["result"] if check else NEVER_CHECKED


def _rows(table: str, sql: str, params: list) -> list[dict]:
    ops.read_log.bump(table)
    try:
        con = serving.open_read(KIND_OF_TABLE[table])
    except serving.ServingFileMissing:
        return []
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()
    dataset = DATASET_OF_TABLE[table]
    if dataset is None:
        # A table several datasets write: each row's status is its own source's newest check.
        seen: dict[str, str] = {}
        for r in rows:
            src = str(r.get("source") or "")
            r["status"] = seen[src] if src in seen else seen.setdefault(src, dataset_status(src))
        return rows
    status = dataset_status(dataset)
    for r in rows:
        r["status"] = status
    return rows


def _day(value) -> date | None:
    return None if value is None else (value if isinstance(value, date) else date.fromisoformat(str(value)))


def market_series(series_ids: Iterable[str], start=None, end=None) -> list[dict]:
    """Observations of ``series_ids`` with ``start <= obs_date <= end`` (either bound optional), by series and date."""
    ids = [str(s) for s in series_ids]
    if not ids:
        return []
    sql = f"SELECT * FROM market_series WHERE series_id IN ({', '.join('?' * len(ids))})"
    params: list = list(ids)
    if start is not None:
        sql += " AND obs_date >= ?"
        params.append(_day(start))
    if end is not None:
        sql += " AND obs_date <= ?"
        params.append(_day(end))
    return _rows("market_series", sql + " ORDER BY series_id, obs_date, source", params)


def spx_expiry_strike(session_date, min_dte: int = 0, max_dte: int = 1) -> list[dict]:
    """SPX spot gamma by expiry and strike for one session, ``min_dte <= dte <= max_dte``."""
    sql = ("SELECT * FROM spx_gamma_expiry_strike WHERE session_date = ? AND dte BETWEEN ? AND ? "
           "ORDER BY expiry, strike")
    return _rows("spx_gamma_expiry_strike", sql, [_day(session_date), int(min_dte), int(max_dte)])


def watchlists() -> dict[str, list[dict]]:
    """The two watch lists (D2) from ``ops.sqlite``: ``{"option": rows, "stock": rows}``, each by ticker, a row
    ``{ticker, added_at, added_by, note}``. Operational state, not market data: no provenance and no ``status``. No
    ops file yet reads as two empty lists. The read itself never creates it; the read log it counts into is
    written to ops.sqlite once a minute (``ops.ReadLog``), and that flush does, as for every read here."""
    out = {}
    for kind in ops.WATCH_KINDS:
        ops.read_log.bump(f"watchlist_{kind}")
        out[kind] = ops.watch_rows(kind)
    return out


def watched(kind: str) -> set[str]:
    """The tickers of one watch list (``option`` or ``stock``), for a board that only asks "is it on the list"."""
    ops.read_log.bump(f"watchlist_{kind}")
    return {r["ticker"] for r in ops.watch_rows(kind)}


# ── The identity spine (PR 5) ─────────────────────────────────────────────────────────────────────────────────

def universe(tiers: Iterable[str] = ("A",)) -> list[dict]:
    """The ``security`` rows of ``tiers`` (Tier A by default: the names the boards screen), by ticker.

    Provenance: ``source`` / ``source_as_of`` / ``fetched_at`` / ``run_id`` of the SEC file the row came from, and each
    identifier's own ``*_source``; ``status`` is ``spine/security``'s newest check.
    """
    tiers = [str(t) for t in tiers]
    if not tiers:
        return []
    sql = f"SELECT * FROM security WHERE tier IN ({', '.join('?' * len(tiers))}) ORDER BY ticker"
    return _rows("security", sql, tiers)


def identity(tickers: Iterable[str] | None = None) -> list[dict]:
    """The ``security`` rows of ``tickers`` (upper-cased; any tier), or of every ticker when None, by ticker."""
    if tickers is None:
        return _rows("security", "SELECT * FROM security ORDER BY ticker", [])
    wanted = sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()})
    if not wanted:
        return []
    return _rows("security", f"SELECT * FROM security WHERE ticker IN ({', '.join('?' * len(wanted))}) "
                             "ORDER BY ticker", wanted)


def membership(index: str | None = None, on=None, source: str = "funds/index_holdings") -> list[dict]:
    """``index_membership`` rows of ``index`` (every index when None) from ``source`` that hold on day ``on``
    (``valid_from <= on < valid_to``), or the current ones (``valid_to`` NULL) when ``on`` is None.

    ``source`` is the fund files (the membership) by default; ``"wrds/idxcst_his"`` is Compustat's history, the
    check, which also covers days before the first fund file. A row whose ticker the spine could not name is left
    out. ``status`` is the fund files' newest check.
    """
    sql = "SELECT * FROM index_membership WHERE source = ? AND ticker IS NOT NULL"
    params: list = [source]
    if index is not None:
        sql += " AND index_slug = ?"
        params.append(str(index))
    if on is None:
        sql += " AND valid_to IS NULL"
    else:
        sql += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        params += [_day(on), _day(on)]
    return _rows("index_membership", sql + " ORDER BY index_slug, ticker", params)


# ── The ownership store (PR 16) ───────────────────────────────────────────────────────────────────────────────

def _tickers(tickers: Iterable[str] | None) -> list[str] | None:
    return None if tickers is None else sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()})


def _where(tickers: list[str] | None, day_col: str, start, end, extra: str = "") -> tuple[str, list]:
    """`` WHERE`` over ``tickers`` (all when None) and ``start <= day_col <= end`` (either bound optional)."""
    parts, params = ["ticker IS NOT NULL"] + ([extra] if extra else []), []
    if tickers is not None:
        parts.append(f"ticker IN ({', '.join('?' * len(tickers))})")
        params += tickers
    if start is not None:
        parts.append(f"{day_col} >= ?")
        params.append(_day(start))
    if end is not None:
        parts.append(f"{day_col} <= ?")
        params.append(_day(end))
    return " WHERE " + " AND ".join(parts), params


def _completed(rows: list[dict]) -> list[dict]:
    today = calendar.today_ny()
    for r in rows:
        r["complete"] = r["filing_deadline"] is not None and r["filing_deadline"] < today
    return rows


def holders_13f(tickers: Iterable[str] | None = None, periods: Iterable | None = None) -> list[dict]:
    """``sec13f_ticker_period`` rows (13F institutional holders per ticker and report period: shares, value, holders,
    concentration, flows against the calendar quarter before, breadth) of ``tickers`` (every ticker when None) and
    ``periods`` (every period when None), by ticker and period. ``complete`` says whether the period's filing
    deadline has passed (New York today): before it, the quarter is still arriving."""
    wanted = _tickers(tickers)
    if wanted == []:
        return []
    sql, params = _where(wanted, "period", None, None)
    if periods is not None:
        days = sorted({_day(p) for p in periods})
        if not days:
            return []
        sql += f" AND period IN ({', '.join('?' * len(days))})"
        params += days
    return _completed(_rows("sec13f_ticker_period",
                            f"SELECT * FROM sec13f_ticker_period{sql} ORDER BY ticker, period", params))


def periods_13f() -> list[dict]:
    """``sec13f_periods``: per report period its filers, value, the share of value named a ticker (``coverage``) and
    ``complete``, by period."""
    return _completed(_rows("sec13f_periods", "SELECT * FROM sec13f_periods ORDER BY period", []))


def finra_short_interest(tickers: Iterable[str], start=None, end=None) -> list[dict]:
    """FINRA's consolidated short interest of ``tickers`` by settlement date (``start <= settlement_date <= end``),
    by ticker and date: the ``finra_short_interest`` table, not the ``short_interest`` bridge view (review N3)."""
    wanted = _tickers(tickers)
    if not wanted:
        return []
    sql, params = _where(wanted, "settlement_date", start, end)
    return _rows("finra_short_interest", f"SELECT * FROM finra_short_interest{sql} ORDER BY ticker, settlement_date, "
                                         "symbol", params)


def shortable(tickers: Iterable[str] | None = None, session=None) -> list[dict]:
    """IBKR's borrow fee, rebate and shares available for ``tickers`` (every named ticker when None) in the snapshot of
    ``session`` (the newest session stored when None), by ticker."""
    wanted = _tickers(tickers)
    if wanted == []:
        return []
    sql, params = _where(wanted, "session_date", None, None)
    if session is None:
        sql += " AND session_date = (SELECT max(session_date) FROM ibkr_shortable)"
    else:
        sql += " AND session_date = ?"
        params.append(_day(session))
    return _rows("ibkr_shortable", f"SELECT * FROM ibkr_shortable{sql} ORDER BY ticker, snapshot_at", params)


def threshold_list(on=None) -> list[dict]:
    """The Reg SHO threshold securities of day ``on`` (the newest list stored when None), every market, by market and
    symbol; a symbol the spine does not name is kept (``ticker`` None)."""
    if on is None:
        sql, params = "list_date = (SELECT max(list_date) FROM regsho_threshold)", []
    else:
        sql, params = "list_date = ?", [_day(on)]
    return _rows("regsho_threshold", f"SELECT * FROM regsho_threshold WHERE {sql} ORDER BY market, symbol", params)


def fails_to_deliver(tickers: Iterable[str], start=None, end=None) -> list[dict]:
    """The SEC's fails-to-deliver of ``tickers`` (``start <= settlement_date <= end``), by ticker and date."""
    wanted = _tickers(tickers)
    if not wanted:
        return []
    sql, params = _where(wanted, "settlement_date", start, end)
    return _rows("sec_ftd", f"SELECT * FROM sec_ftd{sql} ORDER BY ticker, settlement_date, cusip", params)


def insider_transactions(tickers: Iterable[str], start=None, end=None) -> list[dict]:
    """The Form 4 transactions of ``tickers`` filed ``start <= filed <= end``, by ticker, newest filing first. Only
    rows that are a transaction (the table's ``status`` ok, here ``parse_status``, since ``status`` is the dataset's
    check): a filing that did not parse or reported none is left out."""
    wanted = _tickers(tickers)
    if not wanted:
        return []
    sql, params = _where(wanted, "filed", start, end, "status = 'ok'")
    return _rows("insider_transactions", f"SELECT * EXCLUDE (status), status AS parse_status "
                                         f"FROM insider_transactions{sql} "
                                         "ORDER BY ticker, filed DESC, accession, tx_table DESC, line", params)


def beneficial_ownership(tickers: Iterable[str], start=None, end=None) -> list[dict]:
    """The Schedule 13D and 13G rows of ``tickers`` filed ``start <= filed <= end``, by ticker, newest filing first:
    one per reporting person (``parse_status`` ok), or one ``header_only`` row for a text filing (who filed on whom,
    no amounts). A filing whose document did not parse is left out."""
    wanted = _tickers(tickers)
    if not wanted:
        return []
    sql, params = _where(wanted, "filed", start, end, "status <> 'unparsed'")
    return _rows("beneficial_ownership", f"SELECT * EXCLUDE (status), status AS parse_status "
                                         f"FROM beneficial_ownership{sql} "
                                         "ORDER BY ticker, filed DESC, accession, person_line", params)


# ── The fundamentals layer (PR 12) ────────────────────────────────────────────────────────────────────────────

# A table several datasets write maps to None: each row's ``status`` is the newest check of its ``source``.
DATASET_OF_TABLE.update({"fundamental_fact": None, "earnings_event": None, "estimate_latest": "yfinance/estimates",
                         "filing_item": "sec/filings", "segment_revenue": None, "customer_share": None,
                         "peer_group": "fundamentals/tables", "fundamental_revision": None})
KIND_OF_TABLE.update({t: "fundamentals" for t in ("fundamental_fact", "earnings_event", "estimate_latest",
                                                  "filing_item", "segment_revenue", "customer_share", "peer_group",
                                                  "fundamental_revision")})


def _cik(cik) -> str:
    """A CIK as the layer keys it: digits without leading zeros (``0000789019`` and 789019 are ``789019``)."""
    text = str(cik or "").strip()
    return str(int(text)) if text.isdigit() else text


def statements(cik, period_type: str = "quarterly", n: int = 8, fields: Iterable[str] | None = None) -> list[dict]:
    """``fundamental_fact`` rows of company ``cik``: its ``n`` newest period ends of ``period_type`` (``annual`` or
    ``quarterly``), one row per field (``fields`` only, when given), newest first. Every SEC row carries its
    accession, filed date and form; a ``preliminary`` row (UW's, ahead of the 10-Q) has none.

    The ``n`` period ends are the company's, counted over every field, before ``fields`` narrows the rows: a field
    missing on some of those ends gets fewer than ``n`` rows, and its older values are not reached for (so the
    rows of several fields line up on the same ends)."""
    sql = "SELECT * FROM fundamental_fact WHERE cik = ? AND period_type = ?"
    params: list = [_cik(cik), str(period_type)]
    wanted = [str(f) for f in fields or []]
    if wanted:
        sql += f" AND field IN ({', '.join('?' * len(wanted))})"
        params += wanted
    sql += (" AND period_end IN (SELECT DISTINCT period_end FROM fundamental_fact WHERE cik = ? AND period_type = ? "
            "ORDER BY period_end DESC LIMIT ?) ORDER BY period_end DESC, field")
    return _rows("fundamental_fact", sql, params + [_cik(cik), str(period_type), int(n)])


def earnings_events(cik=None, start=None, end=None) -> list[dict]:
    """``earnings_event`` rows (of ``cik``, or every company) with ``start <= report_date <= end``, by date: a
    coming report from a calendar (``confirmed`` false) or one that happened (an 8-K item 2.02, its accession)."""
    sql, params = "SELECT * FROM earnings_event WHERE true", []
    if cik is not None:
        sql += " AND cik = ?"
        params.append(_cik(cik))
    if start is not None:
        sql += " AND report_date >= ?"
        params.append(_day(start))
    if end is not None:
        sql += " AND report_date <= ?"
        params.append(_day(end))
    return _rows("earnings_event", sql + " ORDER BY report_date, cik, source", params)


def estimates(tickers: Iterable[str]) -> list[dict]:
    """``estimate_latest`` rows of ``tickers`` (upper-cased): each ticker's newest estimates per period."""
    wanted = sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()})
    if not wanted:
        return []
    return _rows("estimate_latest", f"SELECT * FROM estimate_latest WHERE ticker IN ({', '.join('?' * len(wanted))}) "
                                    "ORDER BY ticker, period", wanted)


def filing_text(cik, item: str = "business") -> dict | None:
    """The served text of company ``cik``'s newest annual report (``item`` ``business``) or proxy (``proxy``), with
    its ``quality`` (never ``ok`` at the cap); None when there is none."""
    rows = _rows("filing_item", "SELECT * FROM filing_item WHERE cik = ? AND kind = 'document' AND item = ? "
                                "ORDER BY filing_date DESC LIMIT 1", [_cik(cik), str(item)])
    return rows[0] if rows else None


def segments(cik, axis: str | None = None, source: str | None = None) -> list[dict]:
    """``segment_revenue`` rows of company ``cik`` (``axis`` ``business``, ``product`` or ``geographic``; ``source``
    ``sec/filings`` or ``wrds/segments``; each optional), newest period first, the biggest value first."""
    sql, params = "SELECT * FROM segment_revenue WHERE cik = ?", [_cik(cik)]
    for col, value in (("axis", axis), ("source", source)):
        if value is not None:
            sql += f" AND {col} = ?"
            params.append(str(value))
    return _rows("segment_revenue", sql + " ORDER BY period_end DESC, source, axis, value DESC", params)


def customers(cik) -> list[dict]:
    """``customer_share`` rows of company ``cik``, newest period first, the biggest share first."""
    return _rows("customer_share", "SELECT * FROM customer_share WHERE cik = ? "
                                   "ORDER BY period_end DESC, share DESC NULLS LAST, customer", [_cik(cik)])


def peers(cik) -> list[dict]:
    """``peer_group`` rows of company ``cik``, nearest first (``rank``)."""
    return _rows("peer_group", "SELECT * FROM peer_group WHERE cik = ? ORDER BY rank", [_cik(cik)])


def revisions(cik=None, since=None) -> list[dict]:
    """``fundamental_revision`` rows (of ``cik``, or every company), Compustat snapshots from ``since`` on."""
    sql, params = "SELECT * FROM fundamental_revision WHERE true", []
    if cik is not None:
        sql += " AND cik = ?"
        params.append(_cik(cik))
    if since is not None:
        sql += " AND new_as_of >= ?"
        params.append(_day(since))
    return _rows("fundamental_revision", sql + " ORDER BY new_as_of DESC, cik, datadate, item", params)


def as_frame(rows: list[dict]):
    """The rows as a pandas DataFrame (the one helper; pandas is imported only here)."""
    import pandas as pd
    return pd.DataFrame(rows)

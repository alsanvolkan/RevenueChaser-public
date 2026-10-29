"""The read API of the market store (D4): the only place a board gets market data, and the only SQL it runs.

Three serving kinds: ``market`` (``market_series``, ``spx_expiry_strike``), since PR 5 the identity ``spine``
(``universe``, ``identity``, ``membership``) and, since PR 16, ``ownership`` (``holders_13f``, ``periods_13f``,
``finra_short_interest``, ``shortable``, ``threshold_list``, ``fails_to_deliver``, ``insider_transactions``,
``beneficial_ownership``).

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
    status = dataset_status(DATASET_OF_TABLE[table])
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
    """``sec13f_periods``: per report period its filers, value, the share of value named a ticker (``coverage``), the
    same share outside ETFs (``coverage_ex_etf``, with ``value_etf``) and ``complete``, by period."""
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


def as_frame(rows: list[dict]):
    """The rows as a pandas DataFrame (the one helper; pandas is imported only here)."""
    import pandas as pd
    return pd.DataFrame(rows)


# ── The options data (G8): ``options.duckdb`` ─────────────────────────────────────────────────────────────────

DATASET_OF_TABLE.update({**{t: "uw/option_list" for t in ("option_eod", "option_strike", "option_expiry",
                                                          "option_daily", "option_print", "iv_daily")},
                         "option_screener_daily": "uw/option_screener", "er_options": "uw/option_screener"})
KIND_OF_TABLE.update({t: "options" for t in ("option_eod", "option_strike", "option_expiry", "option_daily",
                                             "option_print", "iv_daily", "option_screener_daily", "er_options")})
# Rows a table shares between datasets: the earnings set (``set_name`` earnings) is uw/option_screener's, the IBKR
# year of ``iv_daily`` (``source`` ibkr) is ibkr/iv_history's. Their ``status`` is that dataset's newest check.
_SHARED_STATUS = (("set_name", "earnings", "uw/option_screener"), ("source", "ibkr", "ibkr/iv_history"))


def _ticker_list(tickers: Iterable[str] | str) -> list[str]:
    if isinstance(tickers, str):
        tickers = [tickers]
    return sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()})


def _option_rows(table: str, sql: str, params: list) -> list[dict]:
    rows = _rows(table, sql, params)
    for column, value, dataset in _SHARED_STATUS:
        if any(r.get(column) == value for r in rows):
            status = dataset_status(dataset)
            for r in rows:
                if r.get(column) == value:
                    r["status"] = status
    return rows


def _session_clause(table: str, session_date, ticker_param: str) -> tuple[str, list]:
    """``session_date = ?`` for a given day, else the newest session the table holds for the ticker."""
    if session_date is not None:
        return "session_date = ?", [_day(session_date)]
    return f"session_date = (SELECT max(session_date) FROM {table} WHERE ticker = ?)", [ticker_param]


def option_chain(ticker: str, session_date=None) -> list[dict]:
    """``option_eod``: every contract of one list name (SPX or the option watch list) at one session's close, the
    newest served when ``session_date`` is None (the file serves the last 30), by expiry, strike and type."""
    t = str(ticker).strip().upper()
    where, params = _session_clause("option_eod", session_date, t)
    return _option_rows("option_eod", f"SELECT * FROM option_eod WHERE ticker = ? AND {where} "
                                      "ORDER BY expiry, strike, option_type", [t, *params])


def option_strikes(ticker: str, session_date, set_name: str = "list") -> list[dict]:
    """``option_strike``: spot and greek exposure by strike of one name and session, in the set ``list`` (SPX and the
    option watch list) or ``earnings`` (the evening before a report), by strike."""
    t = str(ticker).strip().upper()
    return _option_rows("option_strike", "SELECT * FROM option_strike WHERE ticker = ? AND session_date = ? "
                                         "AND set_name = ? ORDER BY strike", [t, _day(session_date), set_name])


def option_expiries(ticker: str, session_date, set_name: str = "list") -> list[dict]:
    """``option_expiry``: contracts, open interest, volume, greek exposure, flow and term structure by expiry."""
    t = str(ticker).strip().upper()
    return _option_rows("option_expiry", "SELECT * FROM option_expiry WHERE ticker = ? AND session_date = ? "
                                         "AND set_name = ? ORDER BY expiry", [t, _day(session_date), set_name])


def option_daily(tickers: Iterable[str] | str, start=None, end=None) -> list[dict]:
    """``option_daily``: the GEX levels, the day's option volume and premium and IV rank of list names, by ticker and
    session, ``start <= session_date <= end`` (either bound optional)."""
    names = _ticker_list(tickers)
    if not names:
        return []
    sql = f"SELECT * FROM option_daily WHERE ticker IN ({', '.join('?' * len(names))})"
    params: list = list(names)
    if start is not None:
        sql += " AND session_date >= ?"
        params.append(_day(start))
    if end is not None:
        sql += " AND session_date <= ?"
        params.append(_day(end))
    return _option_rows("option_daily", sql + " ORDER BY ticker, session_date", params)


def option_prints(ticker: str, session_date, set_name: str | None = None) -> list[dict]:
    """``option_print``: the last 50 prints of one name and session, newest first, with ``side`` (ask, bid, mid or
    NULL) and ``package``; of either set when ``set_name`` is None."""
    t = str(ticker).strip().upper()
    sql = "SELECT * FROM option_print WHERE ticker = ? AND session_date = ?"
    params: list = [t, _day(session_date)]
    if set_name is not None:
        sql += " AND set_name = ?"
        params.append(set_name)
    return _option_rows("option_print", sql + " ORDER BY executed_at DESC, print_id", params)


def iv_history(tickers: Iterable[str] | str, start=None, end=None, days: int | None = 30,
               source: str | None = None) -> list[dict]:
    """``iv_daily``: implied volatility of ``tickers`` at the tenor ``days`` (every tenor when None), by ticker,
    session and source. ``observed`` tells the day UW's daily run saw (``uw``) from IBKR's backfilled year (``ibkr``,
    30 days only); a board showing a past day's IV reads ``observed`` before calling it that day's."""
    names = _ticker_list(tickers)
    if not names:
        return []
    sql = f"SELECT * FROM iv_daily WHERE ticker IN ({', '.join('?' * len(names))})"
    params: list = list(names)
    for clause, value in (("days = ?", days), ("source = ?", source), ("session_date >= ?", _day(start)),
                          ("session_date <= ?", _day(end))):
        if value is not None:
            sql += f" AND {clause}"
            params.append(value)
    return _option_rows("iv_daily", sql + " ORDER BY ticker, session_date, source, days", params)


def option_screener(session_date=None, passing_only: bool = False) -> list[dict]:
    """``option_screener_daily``: every name reporting in the 28 days after the session, with its screener row and
    whether it passes the Earnings Radar gates, by ticker; the newest session when ``session_date`` is None."""
    if session_date is None:
        where, params = "session_date = (SELECT max(session_date) FROM option_screener_daily)", []
    else:
        where, params = "session_date = ?", [_day(session_date)]
    if passing_only:
        where += " AND passes_gates"
    return _option_rows("option_screener_daily",
                        f"SELECT * FROM option_screener_daily WHERE {where} ORDER BY ticker", params)


def earnings_options(start=None, end=None, tickers: Iterable[str] | None = None) -> list[dict]:
    """``er_options``: the earnings set, one row per name and the evening it was fetched (``session_date``), with
    the ``report_date`` it was fetched for, by session and ticker."""
    sql, params = "SELECT * FROM er_options WHERE true", []
    if tickers is not None:
        names = _ticker_list(tickers)
        if not names:
            return []
        sql += f" AND ticker IN ({', '.join('?' * len(names))})"
        params += names
    if start is not None:
        sql += " AND session_date >= ?"
        params.append(_day(start))
    if end is not None:
        sql += " AND session_date <= ?"
        params.append(_day(end))
    return _option_rows("er_options", sql + " ORDER BY session_date, ticker", params)

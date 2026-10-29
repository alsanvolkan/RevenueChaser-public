"""The read API of the market store (D4): the only place a board gets market data, and the only SQL it runs.

Four serving kinds: ``market`` (``market_series``, ``spx_expiry_strike``), since PR 5 the identity ``spine``
(``universe``, ``identity``, ``membership``), since PR 7 ``prices`` (``security_ids``, ``bars``, ``actions``,
``shares``, ``market_cap``, ``latest_market``) and, since PR 16, ``ownership`` (``holders_13f``, ``periods_13f``,
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


# ── The prices store (PR 7, G2) ───────────────────────────────────────────────────────────────────────────────
# ``prices.duckdb``, keyed on the security id (``gvkey:iid``, ``figi:...``, ``cik:...:TICKER``, ``sym:TICKER``;
# ``security_ids`` maps tickers to it). Bars are stored nominal; ``bars`` applies ``adj_factor`` on read. ``status``
# is the newest check of the daily source each table follows.

DATASET_OF_TABLE.update({"price_bar_daily": "alpaca/bars", "security_symbol": "alpaca/bars",
                         "market_cap_daily": "alpaca/bars", "corporate_action": "alpaca/corporate_actions",
                         "shares_class": "sec/cover_shares"})
KIND_OF_TABLE.update({t: "prices" for t in ("price_bar_daily", "security_symbol", "market_cap_daily",
                                            "corporate_action", "shares_class")})
BASES = ("nominal", "split", "total_return")
# The factor period of a bar: ``valid_from <= session_date < valid_to``, open at both ends (no row: factor 1).
_PERIOD = ("a.security_id = b.security_id AND b.session_date >= coalesce(a.valid_from, DATE '0001-01-01') "
           "AND (a.valid_to IS NULL OR b.session_date < a.valid_to)")


def _ids(security_ids: Iterable[str]) -> list[str]:
    return sorted({str(s) for s in security_ids if str(s or "").strip()})


def _bounded(sql: str, params: list, column: str, start, end) -> str:
    if start is not None:
        sql += f" AND {column} >= ?"
        params.append(_day(start))
    if end is not None:
        sql += f" AND {column} <= ?"
        params.append(_day(end))
    return sql


def security_ids(tickers: Iterable[str]) -> dict[str, str]:
    """``{ticker: security_id}`` for the tickers (upper-cased, the SEC form ``BRK-B``) a security trades under now;
    of two securities whose newest symbol is the ticker (a delisted one kept its last), the one with the newer bar."""
    wanted = sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()})
    if not wanted:
        return {}
    sql = (f"SELECT s.symbol, s.security_id FROM security_symbol s LEFT JOIN (SELECT security_id, "
           "max(session_date) AS last FROM price_bar_daily GROUP BY ALL) b USING (security_id) "
           f"WHERE s.valid_to IS NULL AND s.symbol IN ({', '.join('?' * len(wanted))}) "
           "QUALIFY row_number() OVER (PARTITION BY s.symbol ORDER BY b.last DESC NULLS LAST, s.security_id) = 1")
    return {r["symbol"]: r["security_id"] for r in _rows("security_symbol", sql, wanted)}


def bars(security_ids: Iterable[str], start=None, end=None, basis: str = "nominal") -> list[dict]:
    """Daily bars of ``security_ids`` with ``start <= session_date <= end`` (either bound optional), by security and
    session, on one ``basis``: ``nominal`` (as traded), ``split`` (open, high, low, close and vwap times the bar's
    ``split_factor``, volume and shares divided by it: today's share basis) or ``total_return`` (the prices times
    ``tr_factor``, which also takes out every cash dividend after the bar; volume and shares as ``split``). Each row
    carries ``basis``, ``split_factor`` and ``tr_factor``."""
    if basis not in BASES:
        raise ValueError(f"unknown basis {basis!r}: one of {', '.join(BASES)}")
    ids = _ids(security_ids)
    if not ids:
        return []
    price = {"nominal": "1.0", "split": "coalesce(a.split_factor, 1)", "total_return": "coalesce(a.tr_factor, 1)"}[basis]
    shares = "1.0" if basis == "nominal" else "coalesce(a.split_factor, 1)"
    sql = (f"SELECT b.security_id, b.session_date, b.symbol, b.cik, b.open * {price} AS open, b.high * {price} AS high, "
           f"b.low * {price} AS low, b.close * {price} AS close, b.volume / {shares} AS volume, "
           f"b.vwap * {price} AS vwap, b.trade_count, b.shares_outstanding / {shares} AS shares_outstanding, "
           f"'{basis}' AS basis, coalesce(a.split_factor, 1) AS split_factor, coalesce(a.tr_factor, 1) AS tr_factor, "
           "b.source, b.source_as_of, b.fetched_at, b.run_id "
           f"FROM price_bar_daily b LEFT JOIN adj_factor a ON {_PERIOD} "
           f"WHERE b.security_id IN ({', '.join('?' * len(ids))})")
    params: list = list(ids)
    sql = _bounded(sql, params, "b.session_date", start, end)
    return _rows("price_bar_daily", sql + " ORDER BY b.security_id, b.session_date", params)


def actions(security_ids: Iterable[str], start=None, end=None) -> list[dict]:
    """``corporate_action`` rows of ``security_ids`` with ``start <= ex_date <= end``, by security, ex-date and action;
    ``applied`` says whether the factors use it (``not_applied_why`` when not)."""
    ids = _ids(security_ids)
    if not ids:
        return []
    params: list = list(ids)
    sql = _bounded(f"SELECT * FROM corporate_action WHERE security_id IN ({', '.join('?' * len(ids))})", params,
                   "ex_date", start, end)
    return _rows("corporate_action", sql + " ORDER BY security_id, ex_date, action, source_id", params)


def shares(security_ids: Iterable[str], measure: str | None = None, start=None, end=None) -> list[dict]:
    """``shares_class`` rows of ``security_ids`` (``measure`` ``outstanding`` or ``float``, both when None) with
    ``start <= as_of <= end``, by security, measure, day and source. The counts are as reported on their day."""
    ids = _ids(security_ids)
    if not ids:
        return []
    params: list = list(ids)
    sql = f"SELECT * FROM shares_class WHERE security_id IN ({', '.join('?' * len(ids))})"
    if measure is not None:
        sql += " AND measure = ?"
        params.append(str(measure))
    sql = _bounded(sql, params, "as_of", start, end)
    return _rows("shares_class", sql + " ORDER BY security_id, measure, as_of, source", params)


def market_cap(security_ids: Iterable[str], start=None, end=None) -> list[dict]:
    """``market_cap_daily`` rows of ``security_ids`` with ``start <= session_date <= end``, by security and session:
    the nominal close times the shares of that day, and the issuer's sum over its listed classes."""
    ids = _ids(security_ids)
    if not ids:
        return []
    params: list = list(ids)
    sql = _bounded(f"SELECT * FROM market_cap_daily WHERE security_id IN ({', '.join('?' * len(ids))})", params,
                   "session_date", start, end)
    return _rows("market_cap_daily", sql + " ORDER BY security_id, session_date", params)


# The newest close and cap of a security count only this close to the store's newest session; an older one (a name
# Alpaca stopped answering, a history-only security) is no current value.
LATEST_WITHIN_DAYS = 14


def latest_market(tickers: Iterable[str]) -> dict[str, dict]:
    """``{ticker: row}`` for the tickers (upper-cased, the SEC form ``BRK-B``) a security trades under now: its newest
    nominal close (``price``, ``price_session``) and its newest market cap (``market_cap``, the class's own;
    ``issuer_market_cap``, the sum over the issuer's listed classes; ``cap_session``), each only when it is at most
    ``LATEST_WITHIN_DAYS`` before the store's newest session. A bar with no shares has a price and no cap (None). Of
    two securities whose newest symbol is the ticker, the one with the newer bar, as ``security_ids``."""
    wanted = sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()})
    if not wanted:
        return {}
    sql = f"""
        WITH since AS (SELECT max(session_date) - INTERVAL {int(LATEST_WITHIN_DAYS)} DAY AS d FROM price_bar_daily
                       WHERE security_id NOT LIKE 'index:%'),
             bar AS (SELECT security_id, arg_max(close, session_date) AS price, max(session_date) AS price_session
                     FROM price_bar_daily WHERE close IS NOT NULL AND session_date >= (SELECT d FROM since)
                     GROUP BY ALL),
             cap AS (SELECT security_id, arg_max(market_cap, session_date) AS market_cap,
                            arg_max(issuer_market_cap, session_date) AS issuer_market_cap,
                            max(session_date) AS cap_session
                     FROM market_cap_daily WHERE session_date >= (SELECT d FROM since) GROUP BY ALL)
        SELECT s.symbol AS ticker, s.security_id, bar.price, bar.price_session, cap.market_cap, cap.issuer_market_cap,
               cap.cap_session
        FROM security_symbol s JOIN bar USING (security_id) LEFT JOIN cap USING (security_id)
        WHERE s.valid_to IS NULL AND s.symbol IN ({', '.join('?' * len(wanted))})
        QUALIFY row_number() OVER (PARTITION BY s.symbol ORDER BY bar.price_session DESC, s.security_id) = 1"""
    return {r["ticker"]: r for r in _rows("market_cap_daily", sql, wanted)}


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


def as_frame(rows: list[dict]):
    """The rows as a pandas DataFrame (the one helper; pandas is imported only here)."""
    import pandas as pd
    return pd.DataFrame(rows)

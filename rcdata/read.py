"""The read API of the market store (D4): the only place a board gets market data, and the only SQL it runs.

Four serving kinds: ``market`` (``market_series``, ``spx_expiry_strike``), since PR 5 the identity ``spine``
(``universe``, ``identity``, ``membership``), since PR 7 the ``prices`` store (``bars``, ``actions``, ``shares``,
``market_cap``) and since PR 10 the forecast ``ledger`` (``forecast_ledger``, ``forecast_outcomes``,
``forecast_baselines``).

Every row comes back with its provenance (``session_date``, ``source``, ``fetched_at``, ``source_as_of``, ``run_id``)
and ``status``, the newest ``dataset_check`` result of its dataset (``never_checked`` before the first). Each call
opens the live serving file read-only and closes it again (``serving.open_read``), and counts one read of its table
in the read log. No serving file yet means no rows, not an error: the status says why.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from . import ops, serving

NEVER_CHECKED = "never_checked"
DATASET_OF_TABLE = {"market_series": "market_series", "spx_gamma_expiry_strike": "uw/spot_expiry_strike",
                    "security": "spine/security", "index_membership": "funds/index_holdings"}
KIND_OF_TABLE = {"market_series": "market", "spx_gamma_expiry_strike": "market", "security": "spine",
                 "index_membership": "spine"}


def dataset_status(dataset: str) -> str:
    check = ops.latest_check(dataset)
    return check["result"] if check else NEVER_CHECKED


def _rows(table: str, sql: str, params: list, status_key: str = "status") -> list[dict]:
    """The rows, each with its dataset's newest check result under ``status_key`` (``dataset_status`` for a table
    whose own ``status`` column is data, the forecast outcomes and baselines)."""
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
        r[status_key] = status
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


# ── The price boards' by-ticker reads (G3: PR 8 Co-movement, PR 9 Sector Rotation, PR 11 Asymmetric Growth) ─────────
# A board names tickers; a ticker is the security trading under it now (``security_symbol``, valid_to NULL; of two,
# the one with the newer bar, as ``security_ids``), so a renamed name keeps its bars from before the rename. The index
# level (``index:SPX``) is never a ticker here. Each read is one query over the live file.

def _tickers(tickers: Iterable[str]) -> list[str]:
    if isinstance(tickers, str):
        tickers = [tickers]
    return sorted({str(t).strip().upper() for t in tickers or () if str(t or "").strip()})


def _now_cte(n: int) -> str:
    """``now (ticker, security_id)``: the security each of ``n`` asked tickers (the first ``n`` parameters) is."""
    return ("now AS (SELECT s.symbol AS ticker, s.security_id FROM security_symbol s LEFT JOIN (SELECT security_id, "
            "max(session_date) AS last FROM price_bar_daily GROUP BY ALL) l USING (security_id) "
            f"WHERE s.valid_to IS NULL AND s.security_id NOT LIKE 'index:%' AND s.symbol IN ({', '.join('?' * n)}) "
            "QUALIFY row_number() OVER (PARTITION BY s.symbol ORDER BY l.last DESC NULLS LAST, s.security_id) = 1)")


def _frame(table: str, sql: str, params: list, columns: list[str]):
    """The rows of ``sql`` as a pandas DataFrame (DuckDB's own conversion: a bar read can be a million rows), with
    ``status``; an empty frame with ``columns`` when there is no file yet."""
    import pandas as pd
    ops.read_log.bump(table)
    try:
        con = serving.open_read(KIND_OF_TABLE[table])
    except serving.ServingFileMissing:
        return pd.DataFrame(columns=[*columns, "status"])
    try:
        frame = con.execute(sql, params).df()
    finally:
        con.close()
    frame["status"] = dataset_status(DATASET_OF_TABLE[table])
    return frame


TICKER_BAR_COLUMNS = ["ticker", "security_id", "session_date", "open", "high", "low", "close", "volume", "source",
                      "fetched_at", "basis"]


def ticker_bars(tickers: Iterable[str], start=None, end=None, basis: str = "split"):
    """Daily bars of ``tickers`` with ``start <= session_date <= end`` as a DataFrame (``TICKER_BAR_COLUMNS``), by
    ticker and session, on one of ``bars``'s bases. ``session_date`` is a pandas datetime (midnight, no zone).

    ``split`` is the price as the old D01 store served it as "nominal" (splits applied, dividends not: the level a
    chart shows); ``total_return`` its "adjusted" (every cash dividend after the bar taken out too: the returns)."""
    if basis not in BASES:
        raise ValueError(f"unknown basis {basis!r}: one of {', '.join(BASES)}")
    import pandas as pd
    names = _tickers(tickers)
    if not names:
        return pd.DataFrame(columns=TICKER_BAR_COLUMNS)
    price = {"nominal": "1.0", "split": "coalesce(a.split_factor, 1)", "total_return": "coalesce(a.tr_factor, 1)"}[basis]
    shares = "1.0" if basis == "nominal" else "coalesce(a.split_factor, 1)"
    sql = (f"WITH {_now_cte(len(names))} SELECT now.ticker, b.security_id, CAST(b.session_date AS TIMESTAMP) AS "
           f"session_date, b.open * {price} AS open, b.high * {price} AS high, b.low * {price} AS low, "
           f"b.close * {price} AS close, b.volume / {shares} AS volume, b.source, b.fetched_at, '{basis}' AS basis "
           f"FROM price_bar_daily b JOIN now USING (security_id) LEFT JOIN adj_factor a ON {_PERIOD} WHERE true")
    params: list = list(names)
    sql = _bounded(sql, params, "b.session_date", start, end)
    frame = _frame("price_bar_daily", sql + " ORDER BY now.ticker, b.session_date", params, TICKER_BAR_COLUMNS)
    return frame.drop(columns=["status"])


def last_sessions(tickers: Iterable[str]) -> dict[str, date]:
    """``{ticker: newest session with a close}`` of the ``tickers`` the store has a bar of."""
    names = _tickers(tickers)
    if not names:
        return {}
    sql = (f"WITH {_now_cte(len(names))} SELECT now.ticker, max(b.session_date) AS d FROM price_bar_daily b "
           "JOIN now USING (security_id) WHERE b.close IS NOT NULL AND NOT isnan(b.close) GROUP BY ALL")
    return {r["ticker"]: r["d"] for r in _rows("price_bar_daily", sql, names)}


def newest_session() -> date | None:
    """The store's newest session with a close of any security (the index level left out); None when empty."""
    rows = _rows("price_bar_daily", "SELECT max(session_date) AS d FROM price_bar_daily WHERE close IS NOT NULL "
                                    "AND NOT isnan(close) AND security_id NOT LIKE 'index:%'", [])
    return rows[0]["d"] if rows else None


def changed_at(tickers: Iterable[str]) -> float:
    """When the store last changed under ``tickers``: the newest ``fetched_at`` of their bars and actions (0.0 when
    none). An action revises every bar before it without adding one, so a board's stored run older than this is
    stale even when the newest session has not moved."""
    names = _tickers(tickers)
    if not names:
        return 0.0
    sql = (f"WITH {_now_cte(len(names))} SELECT greatest("
           "coalesce((SELECT max(fetched_at) FROM price_bar_daily WHERE security_id IN (SELECT security_id FROM now)), 0), "
           "coalesce((SELECT max(fetched_at) FROM corporate_action WHERE security_id IN (SELECT security_id FROM now)), "
           "0)) AS t")
    rows = _rows("price_bar_daily", sql, names)
    return float((rows[0]["t"] if rows else None) or 0.0)


def bar_sessions(tickers: Iterable[str], start=None) -> list[date]:
    """The sessions, oldest first, on which any of ``tickers`` has a close (from ``start`` on when given)."""
    names = _tickers(tickers)
    if not names:
        return []
    sql = (f"WITH {_now_cte(len(names))} SELECT DISTINCT b.session_date AS d FROM price_bar_daily b "
           "JOIN now USING (security_id) WHERE b.close IS NOT NULL AND NOT isnan(b.close)")
    params: list = list(names)
    sql = _bounded(sql, params, "b.session_date", start, None)
    return [r["d"] for r in _rows("price_bar_daily", sql + " ORDER BY d", params)]


def bar_facts(tickers: Iterable[str], start, flat_run: int) -> dict:
    """What a windowed read cannot see, over the bars of ``tickers`` from ``start`` on (Co-movement, #379 COM-3):

    - ``first`` / ``last``: each ticker's first and last session with a close (``YYYY-MM-DD``);
    - ``sessions``: the sessions on which any of them has one, oldest first (``YYYY-MM-DD``);
    - ``flat``: the tickers with ``flat_run`` steps in a row on which the close did not move. The bars are as traded,
      so a step counts as unmoved when the stored close is equal OR an applied action has its ex-date inside it (its
      adjusted closes can be equal while the stored ones are not); the caller judges these candidates on their
      adjusted closes. A split needs no rule of its own here: a bar is as traded whenever it was fetched.
    """
    names = _tickers(tickers)
    empty = {"first": {}, "last": {}, "sessions": [], "flat": []}
    if not names or start is None:
        return empty
    run = max(2, int(flat_run))
    lo = _day(start)
    bars = (f"WITH {_now_cte(len(names))}, bars AS (SELECT now.ticker, b.security_id, b.session_date AS d, b.close "
            "FROM price_bar_daily b JOIN now USING (security_id) WHERE b.session_date >= ? AND b.close IS NOT NULL "
            "AND NOT isnan(b.close))")
    sql = (bars + ", ex AS (SELECT DISTINCT security_id, ex_date FROM corporate_action WHERE applied AND ex_date > ? "
           "AND security_id IN (SELECT security_id FROM now)), "
           "steps AS (SELECT ticker, security_id, d, close, lag(close) OVER w AS pc, lag(d) OVER w AS pd FROM bars "
           "WINDOW w AS (PARTITION BY ticker ORDER BY d)), "
           "moves AS (SELECT s.ticker, s.d, CASE WHEN s.pc IS NULL THEN NULL WHEN s.close = s.pc THEN 0 "
           "WHEN EXISTS (SELECT 1 FROM ex WHERE ex.security_id = s.security_id AND ex.ex_date > s.pd "
           "AND ex.ex_date <= s.d) THEN 0 ELSE 1 END AS moved FROM steps s), "
           "runs AS (SELECT ticker, d, count(moved) OVER r = ? AND sum(moved) OVER r = 0 AS flat FROM moves "
           f"WINDOW r AS (PARTITION BY ticker ORDER BY d ROWS BETWEEN {run - 1} PRECEDING AND CURRENT ROW)) "
           "SELECT ticker, CAST(min(d) AS VARCHAR) AS first, CAST(max(d) AS VARCHAR) AS last, "
           "bool_or(coalesce(flat, false)) AS flat FROM runs GROUP BY ticker")
    rows = _rows("price_bar_daily", sql, [*names, lo, lo, run])
    if not rows:
        return empty
    sessions = _rows("price_bar_daily", bars + " SELECT DISTINCT CAST(d AS VARCHAR) AS d FROM bars ORDER BY d",
                     [*names, lo])
    return {"first": {r["ticker"]: r["first"] for r in rows},
            "last": {r["ticker"]: r["last"] for r in rows},
            "sessions": [r["d"] for r in sessions],
            "flat": sorted(r["ticker"] for r in rows if r["flat"])}


def as_frame(rows: list[dict]):
    """The rows as a pandas DataFrame (the one helper; pandas is imported only here)."""
    import pandas as pd
    return pd.DataFrame(rows)


# ── The screeners' reads (PR 10, G4) ──────────────────────────────────────────────────────────────────────────
# Swing and Fundamentals ask by ticker, and a ticker names the security G3's ``_now_cte`` picks: the one trading
# under it now, of two the one with the newer bar.

def last_bars(tickers: Iterable[str]) -> dict[str, dict]:
    """``{ticker: row}`` for the tickers with a bar: ``security_id``, ``last_session`` (a date, the newest bar's New
    York session) and ``last_fetched_at`` (that bar's ``fetched_at``: when the builder read it, so a test of "written
    after the session's close + 30 min" can be made on it). A ticker with no bar is absent."""
    wanted = _tickers(tickers)
    if not wanted:
        return {}
    sql = (f"WITH {_now_cte(len(wanted))} SELECT now.ticker, now.security_id, max(b.session_date) AS last_session, "
           "arg_max(b.fetched_at, b.session_date) AS last_fetched_at FROM price_bar_daily b JOIN now USING "
           "(security_id) GROUP BY ALL")
    return {r["ticker"]: r for r in _rows("price_bar_daily", sql, wanted)}


def bars_by_ticker(tickers: Iterable[str], start=None, end=None) -> list[dict]:
    """``bars`` of the securities the tickers name, nominal, each row with its ``ticker`` (the name asked), by ticker
    and session. The factors (``split_factor``, ``tr_factor``) come with every row, so a caller derives the split and
    the total-return basis from one read."""
    ids = security_ids(tickers)
    if not ids:
        return []
    ticker_of = {sid: t for t, sid in ids.items()}
    rows = bars(ids.values(), start=start, end=end, basis="nominal")
    for r in rows:
        r["ticker"] = ticker_of[r["security_id"]]
    return sorted(rows, key=lambda r: (r["ticker"], r["session_date"]))


# The prefilter's average volume: the mean of a name's last this many sessions (Yahoo's averageVolume, which the old
# quote row carried, is a 3-month mean: about 63 sessions).
LIQUIDITY_SESSIONS = 63


def liquidity(tickers: Iterable[str], sessions: int = LIQUIDITY_SESSIONS) -> dict[str, dict]:
    """``{ticker: row}``: ``price`` (the newest nominal close) and ``price_session``, and ``avg_volume``, the mean
    volume over the name's last ``sessions`` bars on today's share basis (divided by ``split_factor``), with
    ``volume_sessions``, how many bars that mean rests on. Like ``latest_market``, only a close at most
    ``LATEST_WITHIN_DAYS`` before the store's newest session counts: an older one is no current price."""
    wanted = _tickers(tickers)
    if not wanted:
        return {}
    sql = (f"WITH {_now_cte(len(wanted))}, "
           f"since AS (SELECT max(session_date) - INTERVAL {int(LATEST_WITHIN_DAYS)} DAY AS d FROM price_bar_daily "
           "WHERE security_id NOT LIKE 'index:%'), "
           "recent AS (SELECT b.security_id, b.session_date, b.close, b.volume / coalesce(a.split_factor, 1) AS volume, "
           "row_number() OVER (PARTITION BY b.security_id ORDER BY b.session_date DESC) AS n "
           f"FROM price_bar_daily b LEFT JOIN adj_factor a ON {_PERIOD} "
           "WHERE b.security_id IN (SELECT security_id FROM now)) "
           "SELECT now.ticker, now.security_id, "
           "arg_max(recent.close, recent.session_date) FILTER (WHERE recent.close IS NOT NULL) AS price, "
           "max(recent.session_date) FILTER (WHERE recent.close IS NOT NULL) AS price_session, "
           "avg(recent.volume) FILTER (WHERE recent.n <= ?) AS avg_volume, "
           "count(recent.volume) FILTER (WHERE recent.n <= ?) AS volume_sessions "
           "FROM now JOIN recent USING (security_id) GROUP BY ALL "
           "HAVING max(recent.session_date) FILTER (WHERE recent.close IS NOT NULL) >= (SELECT d FROM since)")
    return {r["ticker"]: r for r in _rows("price_bar_daily", sql, [*wanted, int(sessions), int(sessions)])}


def tickers_on(day) -> list[str]:
    """The tickers of the securities with a usable close on ``day`` (an index not counted), each by the symbol it
    trades under now (``security_symbol`` with no ``valid_to``; a delisted security keeps its last), sorted.

    The forecast baselines' universe of a freeze day (PR 10): whatever traded that day, delisted names included, so
    it is not survivorship-biased. shortcut: two securities that now share a symbol count once, and a security whose
    symbols all closed is left out; upgrade when the baselines read bars by ``security_id`` (no ticker round trip)."""
    rows = _rows("price_bar_daily",
                 "SELECT DISTINCT s.symbol FROM price_bar_daily b JOIN security_symbol s USING (security_id) "
                 "WHERE b.session_date = ? AND b.close > 0 AND s.valid_to IS NULL "
                 "AND b.security_id NOT LIKE 'index:%' ORDER BY s.symbol", [_day(day)])
    return [r["symbol"] for r in rows]


# ── the forecast ledger (PR 10, G4) ───────────────────────────────────────────────────────────────────────────────

DATASET_OF_TABLE.update({"forecast_ledger": "ledger/freeze", "forecast_outcomes": "ledger/outcomes",
                         "forecast_baselines": "ledger/baselines"})
KIND_OF_TABLE.update({"forecast_ledger": "ledger", "forecast_outcomes": "ledger", "forecast_baselines": "ledger"})


def forecast_ledger(source: str | None = None, start=None, end=None, target_type: str | None = None) -> list[dict]:
    """``forecast_ledger`` rows (of ``source`` and ``target_type`` when given) with ``start <= forecast_date <= end``
    (either bound optional), by source, forecast date and rank. No ledger file yet is no rows."""
    sql, params = "SELECT * FROM forecast_ledger WHERE true", []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    if target_type is not None:
        sql += " AND target_type = ?"
        params.append(target_type)
    sql = _bounded(sql, params, "forecast_date", start, end)
    return _rows("forecast_ledger", sql + " ORDER BY source, forecast_date, rank, subject", params)


def ledger_day_frozen(source: str, forecast_date, target_type: str = "pick") -> bool:
    """True when ``forecast_ledger`` already holds ``source``'s ``target_type`` rows for ``forecast_date``."""
    return bool(_rows("forecast_ledger", "SELECT 1 AS hit FROM forecast_ledger WHERE source = ? AND forecast_date = ? "
                      "AND target_type = ? LIMIT 1", [source, _day(forecast_date), target_type]))


def forecast_outcomes(source: str | None = None, start=None, end=None) -> list[dict]:
    """``forecast_outcomes`` rows (of ``source`` when given) with ``start <= forecast_date <= end``, by source,
    forecast date, subject and horizon: what happened after each frozen pick (``core/outcomes.py``). ``status`` is the
    row's own (open, closed, no_bars, split_check); the dataset's check result is ``dataset_status``."""
    sql, params = "SELECT * FROM forecast_outcomes WHERE true", []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql = _bounded(sql, params, "forecast_date", start, end)
    return _rows("forecast_outcomes", sql + " ORDER BY source, forecast_date, subject, horizon_days", params,
                 status_key="dataset_status")


def forecast_baselines(start=None, end=None) -> list[dict]:
    """``forecast_baselines`` rows with ``start <= forecast_date <= end``, by forecast date, horizon and basis: what
    the day's universe did over the same windows (``core/baselines.py``). ``status`` is the row's own (open, closed);
    the dataset's check result is ``dataset_status``."""
    params: list = []
    sql = _bounded("SELECT * FROM forecast_baselines WHERE true", params, "forecast_date", start, end)
    return _rows("forecast_baselines", sql + " ORDER BY forecast_date, horizon_days, basis", params,
                 status_key="dataset_status")

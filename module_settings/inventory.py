"""Store-only data-health inventory — zero network.

Reads the ``MANIFEST`` tables through ``core.rawstore`` (which routes each table
to its DuckDB file: ``raw.duckdb``, ``edgar.duckdb``, ``history.duckdb`` or ``prices.duckdb``) and reports, per dataset,
the row count, the newest data date, and the honest hole count. A table that is missing or empty
shows ``—`` rather than a guess. The Freshness column is the one exception: it never prints a bare
dash, but one of the named states the registry justifies (S5b, G3). See ``docs/data_health.md`` §2.
"""
from __future__ import annotations

import inspect
import json
import os
import threading
import time
from datetime import datetime, timezone

from RevenueChaser.core import automations, datahealth, datasets, fiscal_year, ibkr_login, rawstore
from RevenueChaser.core.gics_map import canonical_sector
from RevenueChaser.core.identity import refresh as identity_refresh
from RevenueChaser.core.identity import types as identity_types
# a dedicated alias for the identity-health code, so it does not lean on the shared universe import below
from RevenueChaser.core.universe import DEFAULT_MC_MIN as _MC_FLOOR
from RevenueChaser.module_settings import boards
from RevenueChaser.core.universe import DEFAULT_MC_MIN, classification_universe

DASH = "—"
# The drill's value for a security_master column this store does not carry yet (CUSIP / ISIN arrive later).
IDENTIFIER_NA = "n/a"
# The Settings banner reads this board's last recorded run (#80). Settings itself makes no provider call.
BOARD = "module_settings"
# Warn once ``security_master.updated_at`` is older than this many days (#345, #381).
IDENTITY_MASTER_STALE_DAYS = 7

# The two ways a coverage row has no number (S7, #124). A failed read is never a zero, and a
# population the store holds no denominator for is said in words instead of guessed.
COVERAGE_UNREADABLE = "unreadable"
COVERAGE_UNMEASURED = "unmeasured"

# The excluded security types in the board's own words. The groups themselves are
# ``core.universe.TYPE_GROUPS`` (together exactly ``identity.types.EXCLUDED_TYPES``); only the
# labels differ, because those are written for code.
SECURITY_TYPE_WORDS = {
    "funds_etfs": "Funds and exchange-traded funds",
    "spacs": "Special purpose acquisition companies",
    "other_securities": "Preferred shares, warrants and units",
}

# The tables ``module_sec_13f`` creates before it reads. With them all present its readers are
# store-only (no table is created by asking for a number), so this page may ask it for the CUSIP
# map's counts.
SEC13F_TABLES = ("sec13f_holdings", "sec13f_submissions", "sec13f_cusip_names", "sec13f_ingested_zips")


def _db_of(name: str) -> str:
    return (rawstore.MANIFEST.get(name) or {}).get("db", "raw")


# Per-call cache of existing table names, keyed by db. Only alive between
# ``_begin_call()`` and ``_end_call()`` (i.e. inside one ``collect()`` /
# ``ticker_detail()``), so a table created later shows up on the next Refresh.
# Thread-local so concurrent requests never share or clear each other's cache.
_CALL = threading.local()


def _begin_call() -> None:
    _CALL.tables = {}
    _CALL.columns = {}
    _CALL.errors = []
    _CALL.summaries = {}
    _CALL.max_values = {}
    _CALL.failed_dbs = set()


def _end_call() -> None:
    _CALL.tables = None
    _CALL.columns = None
    _CALL.errors = None
    _CALL.summaries = None
    _CALL.max_values = None
    _CALL.failed_dbs = None


def _summary(key: str, fn, *args):
    """One health summary per collect, reused by every row that asks the same store question.

    The dataset rows already ask ``prices.health_summary`` and ``quotes.health_summary``; the
    identity section below needs the same two answers, and a second full sweep of either store
    file would spend the page's 2.5 s budget on a question already answered. Outside
    ``collect()`` there is no cache and this is the plain call.
    """
    cache = getattr(_CALL, "summaries", None)
    if cache is None:
        return fn(*args)
    if key not in cache:
        cache[key] = fn(*args)
    return cache[key]


class _RepeatedReadError(RuntimeError):
    """A read this call already failed, served again from its cache: the first catcher recorded it."""


def _record(table: str | None, where: str, exc: Exception) -> None:
    """Remember a caught query failure for this call (F4).

    A failed query must not look like "no data": ``collect()`` returns these as
    ``payload["errors"]`` and the board shows them. A *missing table* is not a
    failure and never comes through here, and neither is a cached failure served
    again (``_RepeatedReadError``): one failed read is one entry.
    """
    if isinstance(exc, _RepeatedReadError):
        return
    errors = getattr(_CALL, "errors", None)
    if errors is not None:
        errors.append({"table": table, "where": where, "error": str(exc)})


# Every ``(table, column)`` this module's SQL names (besides ``*``/``COUNT``).
# ``test_queried_columns_exist_in_the_manifest`` pins each one to
# ``rawstore.MANIFEST``, so a renamed column fails a test instead of silently
# rendering ``—``. Add a pair here whenever a query starts reading a column.
QUERIED_COLUMNS: list[tuple[str, str]] = [
    ("equity_universe", "ticker"), ("equity_universe", "name"),
    ("equity_universe", "market_cap"),
    ("equity_universe_provenance", "key"), ("equity_universe_provenance", "generated_at"),
    ("equity_universe_provenance", "refreshed_by"),
    ("sec_company_sic", "ticker"),
    # index lists against their floors (#302)
    ("index_constituents", "slug"), ("index_constituents_changes", "run_id"),
    ("index_constituents_changes", "run_at"), ("sec_exchange_file", "ticker"), ("sec_exchange_file", "exchange"),
    ("sec_exchange_file", "status"), ("sec_exchange_file", "run_at"), ("sec_exchange_file", "cik"),
    ("sec_mf_file", "run_at"),
    ("chain_filings", "ticker"), ("chain_filings", "filed"),
    ("edgar_documents", "filed"), ("edgar_documents", "form"), ("edgar_documents", "fetched_at"),
    # SEC submissions (#299, D15)
    ("sec_submissions", "cik"), ("sec_submissions", "filing_date"), ("sec_submissions", "fetched_at"),
    ("source_checks", "key"), ("source_checks", "run_id"),
    ("edgar_financials", "ticker"),
    ("swing_ohlc", "ticker"), ("swing_ohlc", "ts"),
    ("price_bars_daily", "ticker"), ("price_bars_daily", "session_date"), ("price_bars_daily", "source"),
    ("price_actions", "ex_date"),
    ("price_checks", "dataset"), ("price_checks", "source"), ("price_checks", "checked_at"),
    # SPX daily levels (D03, #293)
    ("index_levels_daily", "symbol"), ("index_levels_daily", "session_date"), ("index_levels_daily", "source"),
    ("vendor_financials", "fetched_at"), ("vendor_valuation", "session_date"),
    ("quote_snapshot", "ticker"), ("quote_snapshot", "session_date"), ("quote_snapshot", "source"),
    ("quote_snapshot", "market_cap"),
    ("sec13f_submissions", "period"),
    ("oipd_meta", "ticker"), ("oipd_meta", "as_of"),
    ("etf_holdings", "ticker"), ("etf_holdings", "entry"), ("etf_holdings", "provider"), ("etf_holdings", "fetched_at"),
    ("rotation_holdings", "ticker"), ("rotation_holdings", "rows"),
    ("relatedness_profiles", "ticker"), ("relatedness_profiles", "fetched_at"),
    # identity health (issue #89)
    ("security_master", "ticker"), ("security_master", "security_type"), ("security_master", "sector_source"),
    ("security_master", "updated_at"),
    ("identity_runs", "run_id"), ("identity_runs", "started_at"), ("identity_runs", "finished_at"),
    ("identity_runs", "status"), ("identity_runs", "sources"), ("identity_runs", "counts"),
    ("identity_conflicts", "ticker"), ("identity_conflicts", "kind"), ("identity_conflicts", "field"),
    ("identity_conflicts", "side_a"), ("identity_conflicts", "side_b"), ("identity_conflicts", "note"),
    ("identity_conflicts", "last_seen"),
    ("result_runs", "session_date"),
    ("identity_source_cache", "source"), ("identity_source_cache", "ticker"),
    ("identity_source_cache", "fetched_at"), ("identity_source_cache", "status"),
    ("watchlist_changes", "run_at"), ("board_settings_changes", "run_at"),
    # the drill's scoring datasets and identifiers (S10, #126)
    ("vendor_financials", "ticker"), ("vendor_financials", "period_end"),
    ("ownership_snapshot", "ticker"), ("ownership_snapshot", "session_date"), ("ownership_snapshot", "source"),
    ("security_master", "cik"), ("security_master", "figi"),
    # identity coverage (S7, #124)
    ("security_master", "cusip"), ("security_master", "isin"),
]


def _existing_tables(db: str) -> set[str]:
    cache = getattr(_CALL, "tables", None)
    if cache is not None and db in cache:
        return cache[db]
    try:
        rows = rawstore.query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'", db=db)
        names = {r["table_name"] for r in rows}
    except Exception as exc:  # noqa: BLE001 - recorded (F4), degrades to "no data"
        _record(f"db:{db}", "_existing_tables", exc)
        names = set()
        failed = getattr(_CALL, "failed_dbs", None)
        if failed is not None:   # the drill reads every table in this file as unknown, not absent (#143)
            failed.add(db)
    if cache is not None:
        cache[db] = names
    return names


def _table_exists(name: str) -> bool:
    """Whether ``name`` exists in its DuckDB file (missing == no data, not error)."""
    if name not in rawstore.MANIFEST:
        return False
    return name in _existing_tables(_db_of(name))


def _file_unreadable(name: str) -> bool:
    """Whether this call could not read the table list of ``name``'s store file (#143).

    A locked or broken file fails on its table list first, so ``_table_exists`` is False for every
    table in it; the drill must read those tables as unknown, never as absent.
    """
    return name in rawstore.MANIFEST and _db_of(name) in (getattr(_CALL, "failed_dbs", None) or ())


def _absent(table: str, empty: dict) -> dict:
    """The presence record of a table ``_table_exists`` did not find: absent, or unknown when its
    file's table list could not be read."""
    return dict(empty, error=True) if _file_unreadable(table) else empty


def _existing_columns(table: str) -> set[str]:
    """Column names ``table`` carries in this store, cached for one call.

    Read the same way as ``_existing_tables``: a column that arrives in a later release is
    absent here, so the drill can say ``n/a`` instead of raising on a ``SELECT`` of it.
    """
    cache = getattr(_CALL, "columns", None)
    key = (_db_of(table), table)
    if cache is not None and key in cache:
        return cache[key]
    try:
        rows = rawstore.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name = ?", [table], db=key[0])
        names = {r["column_name"] for r in rows}
    except Exception as exc:  # noqa: BLE001 - recorded (F4), degrades to "the column is not there"
        _record(table, "_existing_columns", exc)
        names = set()
    if cache is not None:
        cache[key] = names
    return names


def _has_column(table: str, column: str) -> bool:
    """Whether ``table`` carries ``column`` today (a later-release column reads absent, never raises)."""
    return _table_exists(table) and column in _existing_columns(table)


def _count(name: str) -> int | None:
    if not _table_exists(name):
        return None
    try:
        return rawstore.count(name)
    except Exception as exc:  # noqa: BLE001
        _record(name, "_count", exc)
        return None


def _sum_counts(tables: list[str]) -> int | None:
    counts = [_count(t) for t in tables]
    if all(c is None for c in counts):
        return None
    return sum(c or 0 for c in counts)


def _count_where(table: str, where: str) -> int | None:
    if not _table_exists(table):
        return None
    try:
        rows = rawstore.query(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", db=_db_of(table))
        return int(rows[0]["n"]) if rows else 0
    except Exception as exc:  # noqa: BLE001
        _record(table, "_count_where", exc)
        return None


def _max_value_or_raise(table: str, column: str):
    """Newest value of ``column`` in ``table``; ``None`` when the table is not in this store.

    A failed query raises, so the caller can tell a broken read from a table with no rows
    (``_max_value`` is the swallowing form the row builders use). Inside one call the answer,
    or the failure, is kept: the row's ``last_date`` and its Fresh cell are one read (#150). A kept
    failure is raised as a new ``_RepeatedReadError`` caused by the first, so it is recorded once.
    """
    cache = getattr(_CALL, "max_values", None)
    if cache is not None and (table, column) in cache:
        value, exc = cache[(table, column)]
        if exc is not None:
            raise _RepeatedReadError(str(exc)) from exc
        return value
    try:
        if not _table_exists(table):
            value = None
        else:
            rows = rawstore.query(
                f"SELECT MAX({column}) AS m FROM {table}", db=_db_of(table))
            value = rows[0].get("m") if rows else None
    except Exception as exc:
        if cache is not None:
            cache[(table, column)] = (None, exc)
        raise
    if cache is not None:
        cache[(table, column)] = (value, None)
    return value


def _max_value(table: str, column: str):
    try:
        return _max_value_or_raise(table, column)
    except Exception as exc:  # noqa: BLE001
        _record(table, "_max_value", exc)
        return None


def _fmt_date(value) -> str:
    """Newest date/timestamp as ``YYYY-MM-DD``; ``—`` when absent/unparseable.

    Epoch floats (``fetched_at`` on the text-free tables) are read as UTC
    seconds; ISO strings are trimmed to their day.
    """
    if value is None:
        return DASH
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return DASH
    text = str(value).strip()
    if not text:
        return DASH
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10] if len(text) >= 10 else DASH


def _max_date(*pairs: tuple[str, str]) -> str:
    """Newest of several ``(table, column)`` dates, formatted."""
    dates = [_fmt_date(_max_value(t, c)) for t, c in pairs]
    dates = [d for d in dates if d and d != DASH]
    return max(dates) if dates else DASH


def _missing(tickers: list[str], table: str, exclude: set[str] | None = None) -> int | None:
    """Classification-universe tickers with no row in ``table`` (cross-file safe).

    ``exclude`` holds upper-cased tickers the caller has already settled as absent on purpose;
    they are not holes (#381, DF97: a recorded 10-K / 20-F miss has no ``chain_filings`` row).
    """
    if not _table_exists(table):
        return None
    try:
        rows = rawstore.query(f"SELECT DISTINCT ticker FROM {table}", db=_db_of(table))
    except Exception as exc:  # noqa: BLE001
        _record(table, "_missing", exc)
        return None
    present = {r.get("ticker") for r in rows}
    skip = exclude or frozenset()
    return sum(1 for t in tickers if t not in present and str(t).upper() not in skip)


def _index_last_change() -> tuple[str, int | None]:
    """``(date, rows)`` of the newest change run of ``index_constituents`` (#302); ``(DASH, None)`` when none."""
    if not _table_exists("index_constituents_changes"):
        return DASH, None
    try:
        rows = rawstore.query(
            "SELECT max(run_at) AS at, count(*) AS n FROM index_constituents_changes WHERE run_id = "
            "(SELECT run_id FROM index_constituents_changes ORDER BY run_at DESC LIMIT 1)")
    except Exception as exc:  # noqa: BLE001
        _record("index_constituents_changes", "_index_last_change", exc)
        return DASH, None
    at = rows[0].get("at") if rows else None
    return (_fmt_date(at), int(rows[0]["n"])) if at is not None else (DASH, None)


def _sec_nasdaq_count() -> int | None:
    """Tickers whose latest SEC exchange file row (D21) is present on Nasdaq; None until that file is stored."""
    if not _count("sec_exchange_file"):
        return None
    try:
        rows = rawstore.query(
            "SELECT count(*) AS n FROM (SELECT exchange, status FROM sec_exchange_file QUALIFY row_number() OVER "
            "(PARTITION BY ticker ORDER BY run_at DESC) = 1) WHERE status = 'present' AND lower(exchange) = 'nasdaq'")
    except Exception as exc:  # noqa: BLE001
        _record("sec_exchange_file", "_sec_nasdaq_count", exc)
        return None
    return int(rows[0]["n"]) if rows else 0


def _index_note() -> str:
    """Each stored index list against its floor, the last change run and Nasdaq vs the SEC exchange file (#302)."""
    from RevenueChaser.core import index_registry
    counts: dict[str, int] = {}
    if _table_exists("index_constituents"):
        try:
            counts = {r["slug"]: int(r["n"]) for r in rawstore.query(
                "SELECT slug, count(*) AS n FROM index_constituents GROUP BY slug")}
        except Exception as exc:  # noqa: BLE001
            _record("index_constituents", "_index_note", exc)
    lists = " · ".join(f"{slug} {counts.get(slug, 0):,} / {'floor ' if i == 0 else ''}"
                       f"{index_registry.MIN_INDEX[slug]:,}" for i, slug in enumerate(index_registry.STORED))
    when, n = _index_last_change()
    change = f"last change {when} (+{n:,})" if n is not None else "no change recorded"
    sec = _sec_nasdaq_count()
    nasdaq = (f"{counts.get('nasdaq', 0):,} / {sec:,}" if sec is not None else "not stored yet")
    return f"{lists}; {change}; Nasdaq vs SEC exchange file: {nasdaq}"


def _price_bars_row(now: datetime) -> dict:
    """The "Daily price bars" row (#156): counts plus the ``prices.health_summary`` note.

    A missing table or a calendar error is a note, never an exception (the ``_missing`` pattern).
    """
    default = {
        "key": "price_bars", "label": "Daily price bars",
        "tables": ["price_bars_daily", "price_actions"],
        "rows": _count("price_bars_daily"),
        "last_date": _max_date(("price_bars_daily", "session_date")),
        "holes": None, "hole_label": "behind latest session", "fetch": None,
        "note": "",
    }
    from RevenueChaser.core import prices
    try:
        h = _summary("prices", prices.health_summary, now)
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("price_bars_daily", "_price_bars_row", exc)
        default["note"] = "no bars yet (the attended fill has not run)"
        return default
    rows = default["rows"]
    default["holes"] = h.get("behind")
    if not rows:
        default["note"] = "no bars yet (the attended fill has not run)"
        return default
    if h.get("calendar_error"):
        default["note"] = "bars stored, but the latest complete session is unavailable (calendar error)"
        return default
    share = h.get("source_share") or {}
    check = h.get("last_check")
    check_txt = _fmt_date(check) if check is not None else DASH
    default["note"] = (
        f"newest {h.get('newest_session') or DASH} vs latest complete "
        f"{h.get('latest_complete') or DASH}; Unusual Whales {int(share.get('uw', 0))} / "
        f"yfinance {int(share.get('yfinance', 0))}; {int(h.get('actions_last_5', 0))} actions in the "
        f"last 5 sessions; last check {check_txt}")
    return default


def _levels_behind(newest: str | None, latest: str | None) -> int | None:
    """Complete sessions the stored levels are missing, through ``core.prices.sessions_behind`` so the calendar
    guard lives in one place: the holiday table starts at ``CALENDAR_START``, and a newest session older than
    that answers ``None`` instead of walking off the table (#293 review). The column is a count, so a 0/1 flag
    would read as "one hole" for any size of gap."""
    from datetime import date as _date

    from RevenueChaser.core import prices
    if not newest or not latest:
        return None
    try:
        return prices.sessions_behind(_date.fromisoformat(newest), _date.fromisoformat(latest))
    except ValueError:
        return None


def _index_levels_row(now: datetime) -> dict:
    """The "SPX daily levels" row (D03, #293): the newest stored ``^GSPC`` session against the latest complete
    session, the row count and the source. A missing table or a calendar error is a note, never an exception."""
    from RevenueChaser.core import index_levels
    row = {
        "key": "index_levels", "label": "SPX daily levels",
        "tables": ["index_levels_daily"],
        "rows": 0, "last_date": DASH,
        "holes": None, "hole_label": "behind the latest complete session", "fetch": None,
        "note": "no index levels yet (the daily bars --index-levels step writes them)",
    }
    # A store without the table is the normal "nothing yet" case, not an error: every other row builder
    # checks _table_exists before reading, and health_summary would raise and be recorded as a failure.
    if not _table_exists("index_levels_daily"):
        return row
    try:
        row["last_date"] = _max_date(("index_levels_daily", "session_date"))
        h = index_levels.health_summary(now)
        # health_summary counts the one stored symbol; the whole-table count would diverge the day a second
        # symbol is written, since the key is (symbol, session_date) (#293 review N-1).
        rows = int(h.get("rows") or 0)
        row["rows"] = rows
        if not rows:
            return row
        if h.get("calendar_error"):
            row["note"] = "levels stored, but the latest complete session is unavailable (calendar error)"
            return row
        newest, latest = h.get("newest_session"), h.get("latest_complete")
        row["holes"] = _levels_behind(newest, latest)
        row["note"] = (f"newest {newest or DASH} vs latest complete {latest or DASH}; {rows:,} rows; "
                       f"source {h.get('source') or DASH}")
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("index_levels_daily", "_index_levels_row", exc)
    return row


# The eight fields G2-29 measured per source (phase2/06_interface.md §6); the note lists their fill.
_VENDOR_FILL_FIELDS = ("revenue", "gross_profit", "operating_income", "ebitda", "net_income", "interest_expense",
                       "total_assets", "operating_cash_flow")
_VENDOR_SOURCE_LABELS = {"uw": "Unusual Whales", "yfinance": "yfinance"}


def _vendor_row(now: datetime) -> dict:
    """The "Vendor statements & valuation" row (#297, D12): valuation tickers on the newest session, the newest
    run that appended statement rows, fill % per field per source. A failure is a note, never an exception."""
    row = {
        "key": "vendor", "label": "Vendor statements & valuation",
        "tables": ["vendor_financials", "vendor_valuation"],
        "rows": _count("vendor_financials"),
        "last_date": _max_date(("vendor_financials", "fetched_at"), ("vendor_valuation", "session_date")),
        "holes": None, "hole_label": "", "fetch": None, "note": "",
    }
    from RevenueChaser.core import vendor_data
    try:
        h = vendor_data.health_summary(now)
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("vendor_financials", "_vendor_row", exc)
        row["note"] = "no vendor rows yet (an AG, Earnings or Financials Fetch stores them)"
        return row
    if not h["statement_rows"] and not h["valuation_tickers"]:
        row["note"] = "no vendor rows yet (an AG, Earnings or Financials Fetch stores them)"
        return row
    parts = [f"valuation {h['valuation_tickers']} tickers on {h['valuation_session'] or DASH} "
             f"(latest complete {h['latest_complete'] or DASH})",
             f"newest statement change: {h['last_change_appended']} rows on {h['last_change_session'] or DASH}"]
    for source in sorted(h["fill"]):
        fill = h["fill"][source]
        label = _VENDOR_SOURCE_LABELS.get(source, source)
        parts.append(", ".join(f"{label} {f.replace('_', ' ')} {fill[f]:.0f}%"
                               for f in _VENDOR_FILL_FIELDS if f in fill))
    row["note"] = "; ".join(parts)
    return row


def _quotes_row() -> dict:
    """The "Quotes and market cap" row (#294): ``quote_snapshot`` per source against the universe.

    Holes are the universe tickers with no Yahoo row in the newest session (Yahoo leads, 2.6-2). A failed summary is
    a note and a recorded error, never an exception.
    """
    default = {
        "key": "quotes", "label": "Quotes and market cap",
        "tables": ["quote_snapshot"],
        "rows": _count("quote_snapshot"),
        "last_date": _max_date(("quote_snapshot", "session_date")),
        "holes": None, "hole_label": "no Yahoo cap in newest session",
        "fetch": {"route": "/run/universe", "label": "Fetch universe"},
        "note": "",
    }
    if default["rows"] is None:
        default["note"] = "no quote rows yet (the universe run writes them)"
        return default
    from RevenueChaser.core import quotes
    try:
        h = _summary("quotes", quotes.health_summary)
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("quote_snapshot", "_quotes_row", exc)
        default["note"] = "quote summary unavailable"
        return default
    default["holes"] = h["missing"].get(quotes.SOURCE_YAHOO)
    if not default["rows"]:
        default["note"] = "no quote rows yet (the universe run writes them)"
        return default
    rows = h["rows"]
    note = (f"newest {h['newest_session']}: yfinance {rows.get(quotes.SOURCE_YAHOO, 0)} / "
            f"Unusual Whales {rows.get(quotes.SOURCE_UW, 0)} of {h['universe']} universe; {h['gaps']} caps more than "
            f"{quotes.CAP_GAP_PCT}% apart")
    if h["gap_examples"]:
        note += f" ({', '.join(h['gap_examples'])})"
    if h["rate_limit_stop"] is not None:
        note += f"; last Yahoo rate-limit stop {_fmt_date(h['rate_limit_stop'])}"
    default["note"] = note
    return default


_HOLDINGS_PROVIDERS = (("uw", "Unusual Whales"), ("yfinance", "yfinance"))


def _etf_holdings_row() -> dict:
    """The "Rotation ETF holdings" row (#303, D25): ETFs fetched in the newest session vs the 34 the holdings job
    resolves, the provider share, the ETFs with no holdings and the theme ETFs with stored RS. A failure is a note."""
    row = {
        "key": "etf_holdings", "label": "Rotation ETF holdings",
        "tables": ["etf_holdings", "etf_holdings_changes"],
        "rows": _count("etf_holdings"),
        "last_date": _max_date(("etf_holdings", "fetched_at")),
        "holes": None, "hole_label": "ETFs with no holdings", "fetch": None, "note": "",
    }
    from RevenueChaser.module_sector_rotation import etf_holdings
    try:
        h = etf_holdings.health_summary()
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("etf_holdings", "_etf_holdings_row", exc)
        row["note"] = "holdings summary failed (see the errors list)"
        return row
    row["holes"] = len(h["missing"])
    if not h["stored"]:
        row["note"] = "no holdings yet (the holdings job stores them)"
        return row
    known = dict(_HOLDINGS_PROVIDERS)
    share = " / ".join(f"{known.get(p, p)} {h['providers'][p]}"
                       for p in [k for k, _ in _HOLDINGS_PROVIDERS] + sorted(set(h["providers"]) - set(known))
                       if p in h["providers"])
    parts = [f"{h['in_newest']} of {h['expected']} ETFs on session {h['newest_session'] or DASH}",
             share or "no provider recorded",
             f"relative strength stored {h['rs_stored']} of {h['themes']} theme ETFs"]
    if h["missing"]:
        named = ", ".join(h["missing"][:8])
        parts.append(f"no holdings: {named}" + (f" and {len(h['missing']) - 8} more" if len(h["missing"]) > 8 else ""))
    row["note"] = "; ".join(parts)
    return row


def _ibkr_login_url() -> str:
    """``RC_IBKR_LOGIN_URL``: where the IBKR gateway's login screen is reachable, or "" when unset.

    Read per payload build rather than at import, so setting the variable needs no restart, and
    kept out of the code so no host is pinned here. The page validates it before linking to it
    (``report._ibkr_login_href``); nothing here or there sends it a request.
    """
    return os.getenv("RC_IBKR_LOGIN_URL", "").strip()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _inputs_row() -> dict:
    """The "Your inputs" row (#306, D36): the present watchlist and settings rows (the live tables) and the newest
    change of either, a removal included (``<table>_changes``). Nothing here can be fetched: the user enters it."""
    watched, overrides = _count("watchlist"), _count("board_settings")
    last = _max_date(("watchlist_changes", "run_at"), ("board_settings_changes", "run_at"))
    row = {
        "key": "inputs", "label": "Your inputs (watchlist, board settings)",
        "tables": ["watchlist", "board_settings"],
        "rows": None if watched is None and overrides is None else (watched or 0) + (overrides or 0),
        "last_date": last, "holes": None, "hole_label": "", "fetch": None,
        "note": "no inputs yet (pin a ticker on LEAPS or save a board setting)",
    }
    if last != DASH or watched or overrides:
        row["note"] = (f"watchlist {_plural(watched or 0, 'ticker')}, "
                       f"{_plural(overrides or 0, 'setting override')}; last change {last}")
    return row


def _provenance() -> dict[str, str]:
    if not _table_exists("equity_universe_provenance"):
        return {}
    try:
        rows = rawstore.query(
            "SELECT key, generated_at, refreshed_by FROM equity_universe_provenance")
    except Exception as exc:  # noqa: BLE001
        _record("equity_universe_provenance", "_provenance", exc)
        return {}
    out = {r["key"]: r.get("generated_at") for r in rows}
    for r in rows:
        # The last pull's outcome rides on the ``__fetch__`` row (#146): say so when it was partial.
        if r["key"] == "__fetch__" and r.get("refreshed_by") == "partial":
            out["__fetch__"] = f"{out['__fetch__']} partial"
    return out


def _exists_row(table: str, ticker: str) -> bool | None:
    """Whether ``table`` has a row for ``ticker`` (cross-file safe, no network).

    ``None`` when the read failed (recorded): unknown, never "no row" (#143, the G3 rule).
    """
    if not _table_exists(table):
        return None if _file_unreadable(table) else False
    try:
        rows = rawstore.query(
            f"SELECT 1 FROM {table} WHERE ticker = ? LIMIT 1",
            [ticker], db=_db_of(table))
        return bool(rows)
    except Exception as exc:  # noqa: BLE001
        _record(table, "_exists_row", exc)
        return None


def _swing_presence(ticker: str) -> dict:
    """Bar count + newest ``ts`` for ``ticker`` in ``swing_ohlc`` (the SPY-fallback cache)."""
    empty = {"bars": 0, "newest": None}
    if not _table_exists("swing_ohlc"):
        return _absent("swing_ohlc", empty)
    try:
        rows = rawstore.query(
            "SELECT COUNT(*) AS n, MAX(ts) AS newest FROM swing_ohlc WHERE ticker = ?",
            [ticker], db=_db_of("swing_ohlc"))
    except Exception as exc:  # noqa: BLE001
        _record("swing_ohlc", "_swing_presence", exc)
        return dict(empty, error=True)
    row = rows[0] if rows else {}
    return {"bars": int(row.get("n") or 0), "newest": row.get("newest")}


def _d01_presence(ticker: str) -> dict:
    """Bar count and newest session for ``ticker`` in D01, the price of record.

    D01 is ``price_bars_daily`` plus ``price_actions`` in ``prices.duckdb``.
    Coverage is the stored bar count; actions adjust those bars on read
    (``core.prices.daily_bars``) and do not add sessions, so the count and the
    newest session both come from ``price_bars_daily``. ``swing_ohlc`` is not
    this number.
    """
    empty = {"bars": 0, "newest": None}
    if not _table_exists("price_bars_daily"):
        return _absent("price_bars_daily", empty)
    try:
        rows = rawstore.query(
            "SELECT COUNT(*) AS n, MAX(session_date) AS newest FROM price_bars_daily WHERE ticker = ?",
            [ticker], db=_db_of("price_bars_daily"))
    except Exception as exc:  # noqa: BLE001
        _record("price_bars_daily", "_d01_presence", exc)
        return dict(empty, error=True)
    row = rows[0] if rows else {}
    newest = row.get("newest")
    if newest is not None:
        newest = _fmt_date(newest)
        newest = None if newest == DASH else newest
    return {"bars": int(row.get("n") or 0), "newest": newest}


def _presence(table: str, ticker: str, *, date_column: str, count_key: str = "rows",
              source_column: str | None = None) -> dict:
    """Row count and newest ``date_column`` for ``ticker`` in ``table``: one query, store only.

    The same shape as :func:`_d01_presence` — the count under ``count_key``, ``newest`` as
    ``YYYY-MM-DD`` — plus the newest row's ``source_column`` when asked. A table this store does
    not carry reads as no rows; a failed query is recorded and carries ``error: True``, so it reads
    "could not read", never as no rows (#143). Neither raises: a board's absence must not break the drill.
    """
    empty = {count_key: 0, "newest": None}
    if source_column:
        empty["source"] = None
    if not _table_exists(table):
        return _absent(table, empty)
    source = f", arg_max({source_column}, {date_column}) AS src" if source_column else ""
    try:
        rows = rawstore.query(
            f"SELECT COUNT(*) AS n, MAX({date_column}) AS newest{source} FROM {table} WHERE ticker = ?",
            [ticker], db=_db_of(table))
    except Exception as exc:  # noqa: BLE001
        _record(table, "_presence", exc)
        return dict(empty, error=True)
    row = rows[0] if rows else {}
    newest = _fmt_date(row.get("newest"))
    out = {count_key: int(row.get("n") or 0), "newest": None if newest == DASH else newest}
    if source_column:
        out["source"] = row.get("src")
    return out


def _quote_presence(ticker: str) -> dict:
    """The newest stored quote for ``ticker``, read by ``core.quotes``' own reader.

    ``latest_caps`` is the rule a board uses (newest session, Yahoo's row where both sources have
    one), so the source the drill shows is the one a reader would take. One query; absent = no rows.
    """
    empty = {"rows": 0, "newest": None, "source": None}
    if not _table_exists("quote_snapshot"):
        return _absent("quote_snapshot", empty)
    from RevenueChaser.core import quotes
    try:
        got = quotes.latest_caps([ticker]).get(ticker) or {}
    except Exception as exc:  # noqa: BLE001
        _record("quote_snapshot", "_quote_presence", exc)
        return dict(empty, error=True)
    if not got:
        return empty
    newest = _fmt_date(got.get("session_date"))
    return {"rows": 1, "newest": None if newest == DASH else newest, "source": got.get("source")}


def _edgar_presence(ticker: str) -> dict:
    """Annual period count and newest fiscal period end of the ``edgar_financials`` row.

    One row per ticker, holding the extracted annual/quarterly lists. ``periods`` counts the annual
    records the financials boards read and ``newest`` is the latest of their period ends
    (``core.fiscal_year.period_end``, the same end a score anchors on) — read across the list, not
    off its last position. A row with no annual extract falls back to its quarterly periods, so a
    stored row never reads as absent.
    """
    empty = {"periods": 0, "newest": None}
    if not _table_exists("edgar_financials"):
        return _absent("edgar_financials", empty)
    try:
        row = rawstore.get("edgar_financials", ticker)
    except Exception as exc:  # noqa: BLE001
        _record("edgar_financials", "_edgar_presence", exc)
        return dict(empty, error=True)
    if not row:
        return empty
    annual = [r for r in (row.get("annual") or []) if isinstance(r, dict)]
    if annual:
        ends = [end for end in (fiscal_year.period_end(r) for r in annual) if end is not None]
        return {"periods": len(annual), "newest": max(ends).isoformat() if ends else None}
    quarterly = [r for r in (row.get("quarterly") or []) if isinstance(r, dict)]
    periods = [str(r["period"])[:10] for r in quarterly if r.get("period")]
    return {"periods": len(quarterly), "newest": max(periods) if periods else None}


def _master_identifier(master: dict | None, column: str):
    """One ``security_master`` identifier: its value, or ``n/a`` when the column is not in this store."""
    if not _has_column("security_master", column):
        return IDENTIFIER_NA
    return (master or {}).get(column)


# ── G7 (#124): which boards can score this name ────────────────────────────────────────────────
# The drill measures a name's presence in these store tables. ``boards.BOARD_INPUTS`` lists the
# tables each board reads, so a board is decided by the tables this drill measured — and a board
# that reads any other table is "not checked for this board", never a guess.
# ``equity_universe`` is read as the list the boards themselves apply (cap floor and security
# type, ``classification_universe``), not merely as "a row exists".
_PRESENCE_KEY = {"equity_universe": "in_classification_universe"}
_DRILL_TABLE_LABELS = {
    "equity_universe": "equity universe membership",
    "chain_filings": "10-K / 20-F filing",
    "price_bars_daily": "daily price bars",
    "quote_snapshot": "quote",
    "vendor_financials": "vendor financials",
    "edgar_financials": "EDGAR facts",
    "ownership_snapshot": "ownership",
    "swing_ohlc": "legacy daily open/high/low/close cache (SPY fallback)",
    "oipd_meta": "Implied Probability chain",
    "relatedness_profiles": "Relatedness profile",
}


def _rows_stored(record) -> bool:
    """Whether a presence record says the name has a row: a count above zero, or ``True`` itself."""
    if isinstance(record, bool):
        return record
    if not isinstance(record, dict):
        return False
    return any(int(record.get(key) or 0) for key in ("bars", "rows", "periods"))


def _unreadable(presence: dict, key: str) -> bool:
    """Whether the drill's read of ``key`` failed: a record flagged ``error`` or a row check that
    answered ``None`` (#143). A key the presence does not carry is not a failure."""
    if key not in presence:
        return False
    record = presence[key]
    return record is None or (isinstance(record, dict) and bool(record.get("error")))


def _board_fit(presence: dict) -> list[dict]:
    """One entry per registered board: can it score this name, from the presence the drill read.

    ``can`` when every table the board reads is measured by this drill and has a row; ``cannot``
    names the measured tables without a row, in plain words; ``not_checked`` when the board reads
    a table this drill does not measure (or reads none), because the drill does not know;
    ``unreadable`` names the measured tables whose read failed (#143): a failed read is not a
    missing row, so it is never "can" or "cannot" on its own; a board that also misses a readable
    table still says ``cannot``, because that much is known. Nav order. One pass over
    ``presence``: no table is queried again here.
    """
    measured = {table: _rows_stored(presence.get(_PRESENCE_KEY.get(table, table)))
                for table in _DRILL_TABLE_LABELS}
    failed = {table for table in _DRILL_TABLE_LABELS
              if _unreadable(presence, _PRESENCE_KEY.get(table, table))}
    out = []
    for entry in boards.BOARD_INPUTS:
        tables = list(entry.get("tables") or [])
        all_measured = bool(tables) and all(table in measured for table in tables)
        missing = [_DRILL_TABLE_LABELS[table] for table in tables
                   if table in measured and table not in failed and not measured[table]]
        unreadable = [_DRILL_TABLE_LABELS[table] for table in tables if table in failed]
        # A readable table with no row is known: the board cannot score the name, whatever else failed.
        if all_measured and missing:
            out.append({"key": entry.get("key"), "label": entry.get("label"),
                        "state": "cannot", "missing": missing})
            continue
        # Otherwise a failed read is said, even on a board the drill cannot fully check: it is the fact.
        if unreadable:
            out.append({"key": entry.get("key"), "label": entry.get("label"),
                        "state": "unreadable", "missing": [], "unreadable": unreadable})
            continue
        if not all_measured:
            out.append({"key": entry.get("key"), "label": entry.get("label"),
                        "state": "not_checked", "missing": []})
            continue
        out.append({"key": entry.get("key"), "label": entry.get("label"),
                    "state": "cannot" if missing else "can", "missing": missing})
    return out


def _universe_tickers() -> list[dict]:
    """``[{ticker, name}]`` for the ticker combobox — one store read, no network."""
    try:
        rows = rawstore.query("SELECT ticker, name FROM equity_universe ORDER BY ticker")
    except Exception as exc:  # noqa: BLE001 - recorded (F4); empty picker
        _record("equity_universe", "_universe_tickers", exc)
        return []
    return [{"ticker": r["ticker"], "name": r.get("name") or r["ticker"]}
            for r in rows if r.get("ticker")]


# The tables the drill's unknown-name check reads: only a failed read of one of these (or of its file's table list)
# makes a name in no dataset unknown rather than absent.
_UNKNOWN_NAME_TABLES = ("equity_universe", "security_master", "sec_exchange_file", "sec_company_sic", "chain_filings",
                        "edgar_financials", "price_bars_daily", "quote_snapshot", "vendor_financials",
                        "ownership_snapshot", "swing_ohlc", "oipd_meta", "relatedness_profiles")


def _read_failed(tables) -> bool:
    """Whether this call recorded a failed read of one of ``tables``, or of the table list of a file holding one."""
    names = set(tables) | {f"db:{_db_of(t)}" for t in tables}
    return any(e.get("table") in names for e in getattr(_CALL, "errors", None) or ())


def ticker_detail(ticker: str) -> dict | None:
    _begin_call()
    try:
        return _ticker_detail(ticker)
    finally:
        _end_call()


def _read_sector(stored):
    """A stored sector as the UW name: a pre-rename GICS spelling (a row ``/run/identity`` has not re-applied yet)
    maps onto its Morningstar name; a missing one stays ``None``."""
    return canonical_sector(stored) if stored else stored


def _ticker_detail(ticker: str) -> dict | None:
    """Per-ticker drill: identifiers, cap, sector and industry, SIC, latest filing, the
    presence of the datasets that decide whether a board can score the name — read straight
    from ``.store`` with zero network, one query per table — and which boards can score the
    name, decided from that presence and ``boards.BOARD_INPUTS`` (no query per board).

    Unknown ticker (absent from every dataset it reads) returns ``None`` so the route can
    answer 404. A NULL ``market_cap`` stays ``None`` — never coerced.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return None

    universe = rawstore.get("equity_universe", t) if _table_exists("equity_universe") else None
    master = rawstore.get("security_master", t) if _table_exists("security_master") else None
    sec = (_sec_file_row(t) if _table_exists("sec_exchange_file") else None)
    sic = rawstore.get("sec_company_sic", t) if _table_exists("sec_company_sic") else None
    filing = rawstore.get("chain_filings", t) if _table_exists("chain_filings") else None
    # The five datasets a board needs to score a name; each is a count plus its newest date.
    d01 = _d01_presence(t)
    quote = _quote_presence(t)
    vendor = _presence("vendor_financials", t, date_column="period_end")
    edgar = _edgar_presence(t)
    ownership = _presence("ownership_snapshot", t, date_column="session_date", source_column="source")
    swing = _swing_presence(t)
    # Read once: the unknown-name test below and the presence both use them (one failure, one entry).
    oipd = _exists_row("oipd_meta", t)
    related = _exists_row("relatedness_profiles", t)

    if not any(x is not None for x in (universe, master, sec, sic, filing)) \
            and not edgar["periods"] \
            and d01["bars"] == 0 \
            and not quote["rows"] \
            and not vendor["rows"] \
            and not ownership["rows"] \
            and swing["bars"] == 0 \
            and not oipd \
            and not related \
            and not _read_failed(_UNKNOWN_NAME_TABLES):   # a failed read of one of them is not "absent" (#143)
        return None

    # The identity spine's CIK is the current one; the older tables stay the fallback.
    cik = (master or {}).get("cik") or next(
        (src["cik"] for src in (sec, sic, filing) if src and src.get("cik")), None)
    cap = universe.get("market_cap") if universe else None
    presence = {
        "edgar_financials": edgar,
        # D01 is the price of record. swing_ohlc is the SPY-fallback cache.
        "price_bars_daily": d01,
        "quote_snapshot": quote,
        "vendor_financials": vendor,
        "ownership_snapshot": ownership,
        "swing_ohlc": swing,
        "oipd_meta": oipd,
        "relatedness_profiles": related,
        # The latest stored annual filing, the row the drill shows as "Last 10-K / 20-F".
        "chain_filings": filing is not None,
        # One universe definition: the very list the boards read (cap floor and
        # security type), not a second copy of the rule.
        "in_classification_universe": t in set(classification_universe()),
    }
    return {
        "ticker": t,
        "name": (universe or {}).get("name") or (sec or {}).get("name") or t,
        "market_cap": cap,
        "price": (universe or {}).get("price"),
        "sector": _read_sector((universe or {}).get("sector")),
        "industry": (universe or {}).get("industry"),
        "cik": cik,
        "figi": (master or {}).get("figi"),
        # CUSIP / ISIN are not in the manifest yet: n/a, not a made-up value, until the columns arrive.
        "cusip": _master_identifier(master, "cusip"),
        "isin": _master_identifier(master, "isin"),
        "sic": (sic or {}).get("sic"),
        "sic_description": (sic or {}).get("sic_description"),
        "filing": ({"form": filing.get("form"), "filed": filing.get("filed"),
                    "accession": filing.get("accession"),
                    "quality": filing.get("quality")} if filing else None),
        "presence": presence,
        # Which boards can score this name, from the presence just read — no query per board.
        "boards": _board_fit(presence),
        # The reads that failed (F4, #143), so a "could not read" names its table and its error.
        "errors": list(getattr(_CALL, "errors", None) or []),
    }


# ── Identity health (issue #89): the Identity spine row and the Settings card ──────────────────────
# Everything below reads ``security_master`` / ``identity_*`` from the store; nothing here fetches. A table
# that does not exist yet gives an empty section, never an exception. "Current" conflicts are the rows the
# latest ok/partial run saw: ``refresh._write_conflicts`` stamps ``last_seen`` with the run's ``started_at``.
TOP_CONFLICTS = 50
_IDENTITY_BUCKETS = ("official", "provider", "SIC", "type rule", "unclassified")   # + "other" when non-zero (manual)


def _json_cell(value):
    """A JSON column as a Python value (the store returns JSON text); anything unreadable is ``None``."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _source_bucket(source) -> str:
    """The sector-source bucket of a ``security_master.sector_source`` value (names: ``core/identity/gics.py``)."""
    s = source or "none"
    if s == "none":
        return "unclassified"
    if s == "sp1500":
        return "official"                           # the S&P 1500 list: its names' sector (#272)
    if s.startswith("provider"):
        return "provider"
    if s == "sic_explicit":
        return "SIC"                                # the explicit SIC table, the last sector fallback (#272)
    if s.startswith("type_rule"):
        return "type rule"
    return "other"                                  # e.g. ``manual``


def _identity_runs() -> list[dict]:
    """Every ``identity_runs`` row, newest first, with ``sources`` and ``counts`` parsed."""
    if not _table_exists("identity_runs"):
        return []
    try:
        rows = rawstore.query(
            "SELECT run_id, started_at, finished_at, status, sources, counts FROM identity_runs "
            "ORDER BY started_at DESC, run_id DESC")
    except Exception as exc:  # noqa: BLE001 - recorded (F4), degrades to "no run"
        _record("identity_runs", "_identity_runs", exc)
        return []
    for r in rows:
        r["sources"] = _json_cell(r.get("sources")) or {}
        r["counts"] = _json_cell(r.get("counts")) or {}
    return rows


def _last_run(runs: list[dict]) -> dict | None:
    if not runs:
        return None
    r = runs[0]
    sources = {name: {"status": s.get("status"), "rows": s.get("rows"), "fetched": s.get("fetched"),
                      "error": s.get("error"), "selected": s.get("selected")}
               for name, s in r["sources"].items() if isinstance(s, dict)}
    return {"run_id": r["run_id"], "status": r["status"], "started_at": r["started_at"],
            "finished_at": r["finished_at"], "sources": sources, "counts": r["counts"]}


def _master_mix(group_expr: str) -> list[tuple]:
    """``[(key, all, at_floor)]`` over ``security_master`` grouped by ``group_expr`` (floor: cap >= $300M).

    The cap comes from ``equity_universe``; a master ticker with no universe row counts in ``all`` only.
    """
    if not _table_exists("security_master"):
        return []
    has_cap = _table_exists("equity_universe")
    cap = "u.market_cap" if has_cap else "NULL"
    join = "LEFT JOIN equity_universe u ON u.ticker = m.ticker" if has_cap else ""
    try:
        rows = rawstore.query(
            f"SELECT {group_expr} AS k, COUNT(*) AS n, SUM(CASE WHEN {cap} >= ? THEN 1 ELSE 0 END) AS f "
            f"FROM security_master m {join} GROUP BY 1", [_MC_FLOOR])
    except Exception as exc:  # noqa: BLE001
        _record("security_master", "_master_mix", exc)
        return []
    return [(r["k"], int(r["n"] or 0), int(r["f"] or 0)) for r in rows]


def _type_mix() -> list[dict]:
    out = [{"type": k, "all": n, "floor": f, "excluded": k in identity_types.EXCLUDED_TYPES}
           for k, n, f in _master_mix("COALESCE(m.security_type, 'unknown')")]
    return sorted(out, key=lambda d: (-d["all"], d["type"]))


def _source_mix() -> list[dict]:
    rows = _master_mix("m.sector_source")
    if not rows:
        return []
    mix = {b: [0, 0] for b in (*_IDENTITY_BUCKETS, "other")}
    for source, n, f in rows:
        mix[_source_bucket(source)][0] += n
        mix[_source_bucket(source)][1] += f
    return [{"bucket": b, "all": a, "floor": f} for b, (a, f) in mix.items() if a or b in _IDENTITY_BUCKETS]


def _unclassified_holes() -> int | None:
    """Master tickers with no sector source at or above the $300M floor (``sector_source`` none / NULL)."""
    if not (_table_exists("security_master") and _table_exists("equity_universe")):
        return None
    try:
        rows = rawstore.query(
            "SELECT COUNT(*) AS n FROM security_master m JOIN equity_universe u ON u.ticker = m.ticker "
            "WHERE (m.sector_source = 'none' OR m.sector_source IS NULL) AND u.market_cap >= ?",
            [_MC_FLOOR])
    except Exception as exc:  # noqa: BLE001
        _record("security_master", "_unclassified_holes", exc)
        return None
    return int(rows[0]["n"]) if rows else 0


def _conflicts(runs: list[dict]) -> dict:
    """Current conflicts by kind, the top ones by market cap, and how many are older (not seen lately)."""
    out = {"as_of": None, "by_kind": [], "top": [], "older": {"count": 0, "since": None}}
    if not _table_exists("identity_conflicts"):
        return out
    as_of = next((r["started_at"] for r in runs if r["status"] in ("ok", "partial")), None)
    out["as_of"] = as_of
    has_cap = _table_exists("equity_universe")
    try:
        if as_of is not None:
            out["by_kind"] = [{"kind": r["kind"], "count": int(r["n"])} for r in rawstore.query(
                "SELECT kind, COUNT(*) AS n FROM identity_conflicts WHERE last_seen = ? "
                "GROUP BY kind ORDER BY n DESC, kind", [as_of])]
            cap = "u.market_cap" if has_cap else "NULL"
            join = "LEFT JOIN equity_universe u ON u.ticker = c.ticker" if has_cap else ""
            # the "*" row (a source-level conflict) has no ticker and no cap: always last
            out["top"] = [dict(r) for r in rawstore.query(
                f"SELECT c.ticker, c.kind, c.field, c.side_a, c.side_b, c.note, c.last_seen, {cap} AS market_cap "
                f"FROM identity_conflicts c {join} WHERE c.last_seen = ? "
                f"ORDER BY {cap} DESC NULLS LAST, (c.ticker = '*'), c.ticker, c.kind, c.field "
                f"LIMIT {TOP_CONFLICTS}", [as_of])]
            old = rawstore.query(
                "SELECT COUNT(*) AS n, MAX(last_seen) AS since FROM identity_conflicts "
                "WHERE last_seen IS NULL OR last_seen <> ?", [as_of])
        else:
            old = rawstore.query("SELECT COUNT(*) AS n, MAX(last_seen) AS since FROM identity_conflicts")
        if old:
            out["older"] = {"count": int(old[0]["n"] or 0), "since": old[0]["since"]}
    except Exception as exc:  # noqa: BLE001
        _record("identity_conflicts", "_conflicts", exc)
    return out


def _stale(now: datetime) -> list[dict]:
    """Per cached source: newest fetch and (for the windowed sources) how many universe tickers are past it.

    ``rows`` and ``past_window`` count only tickers in ``equity_universe``; ``newest`` covers every cached
    row. "Past the window" is exactly ``refresh._stale`` with the refresh job's own windows, so this card
    can never disagree with what the next run will refetch. The sources that refetch on every run have no
    window.
    """
    if not _table_exists("identity_source_cache"):
        return []
    R = identity_refresh
    windows = {
        "sec_submissions": (f"{R.SEC_DAYS} days", lambda status, ts: R._stale(ts, now, R.SEC_DAYS)),
        "openfigi": (f"{R.FIGI_DAYS} days (miss {R.FIGI_MISS_DAYS})",
                     lambda status, ts: R._stale(ts, now, R.FIGI_MISS_DAYS if status == "no_match" else R.FIGI_DAYS)),
        "yahoo_sector": (f"{R.YAHOO_DAYS} days", lambda status, ts: R._stale(ts, now, R.YAHOO_DAYS)),
        "fdic": (f"{R.FDIC_DAYS} days", lambda status, ts: R._stale(ts, now, R.FDIC_DAYS)),
    }
    try:
        newest = {r["source"]: r["newest"] for r in rawstore.query(
            "SELECT source, MAX(fetched_at) AS newest FROM identity_source_cache GROUP BY source")}
        in_universe = rawstore.query(
            "SELECT c.source, c.fetched_at, c.status FROM identity_source_cache c "
            "JOIN equity_universe u ON u.ticker = c.ticker") if _table_exists("equity_universe") else []
    except Exception as exc:  # noqa: BLE001
        _record("identity_source_cache", "_stale", exc)
        return []
    # a cached source that is no longer in SOURCES stays in the table but is not listed
    names = [s for s in R.SOURCES if s in newest or s in windows]
    out = []
    for name in names:
        mine = [r for r in in_universe if r["source"] == name]
        label, past = windows.get(name, ("every run", None))
        out.append({"source": name, "newest": newest.get(name), "rows": len(mine),
                    "past_window": sum(1 for r in mine if past(r["status"], r["fetched_at"])) if past else None,
                    "window": label})
    return out


def _spdr_state(runs: list[dict]) -> dict:
    """The SPDR sector check as the latest run that ran it saw it (never / ok / error + last good).

    A run "ran" the check when SPDR was selected and either failed or left its counts. A run that stopped
    before the check still lists SPDR as selected with its initial ``ok`` and no counts: it is skipped.
    """
    def ran(r) -> bool:
        s = r["sources"].get("spdr")
        return isinstance(s, dict) and bool(s.get("selected")) and (
            s.get("status") != "ok" or "spdr_checked" in r["counts"])
    mine = [r for r in runs if ran(r)]
    if not mine:
        return {"state": "never"}
    latest, s = mine[0], mine[0]["sources"]["spdr"]
    if s.get("status") == "ok":
        c = latest["counts"]
        return {"state": "ok", "checked": c.get("spdr_checked"), "agree": c.get("spdr_agree"),
                "date": latest["started_at"]}
    good = next((r for r in mine if r["sources"]["spdr"].get("status") == "ok"
                 and "spdr_checked" in r["counts"]), None)
    return {"state": "error", "error": s.get("error") or s.get("status"), "date": latest["started_at"],
            "last_good": {"date": good["started_at"], "checked": good["counts"].get("spdr_checked"),
                          "agree": good["counts"].get("spdr_agree")} if good else None}


def _unknown_master_age() -> dict:
    return {"text": "Identity master: last refreshed unknown", "warn": False, "date": None, "days": None}


def _parse_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _identity_master_age(now: datetime) -> dict:
    """How old ``security_master`` is: the newer of ``MAX(security_master.updated_at)`` and the newest
    ``finished_at`` of an ``ok``/``partial`` identity run (both build and write the master). Read-only; never raises.

    ``updated_at`` moves only when a row changes (``core/identity/master.py``), so a run that changed nothing
    would otherwise read as stale. No timestamp is "unknown". A query error is recorded and the page still renders.
    """
    stamps = []
    try:
        if _table_exists("security_master"):
            rows = rawstore.query(
                "SELECT MAX(updated_at) AS m FROM security_master", db=_db_of("security_master"))
            stamps.append(rows[0].get("m") if rows else None)
        if _table_exists("identity_runs"):
            rows = rawstore.query(
                "SELECT MAX(finished_at) AS m FROM identity_runs WHERE status IN ('ok', 'partial')",
                db=_db_of("identity_runs"))
            stamps.append(rows[0].get("m") if rows else None)
    except Exception as exc:  # noqa: BLE001 - recorded (F4); the page must still render
        _record("security_master", "_identity_master_age", exc)
        return _unknown_master_age()
    known = [w for w in map(_parse_utc, stamps) if w is not None]
    if not known:
        return _unknown_master_age()
    when = max(known)
    days = max((now - when).days, 0)
    date = when.strftime("%Y-%m-%d")
    return {
        "text": f"Identity master: last refreshed {date} ({days} {'day' if days == 1 else 'days'} ago)",
        "warn": days > IDENTITY_MASTER_STALE_DAYS,
        "date": date,
        "days": days,
    }


def _identity_health(now: datetime) -> dict:
    """The Settings Identity health card's data: last run, type and sector-source mix, conflicts, staleness, SPDR."""
    runs = _identity_runs()
    return {"last_run": _last_run(runs), "floor": _MC_FLOOR, "type_mix": _type_mix(),
            "source_mix": _source_mix(), "conflicts": _conflicts(runs), "stale": _stale(now),
            "spdr": _spdr_state(runs)}


def _ibkr_login_state(now: datetime) -> dict | None:
    """The IBKR Gateway login STATE card's data (#162): the login check's own record, nothing else.

    No socket and no store read. A probe here would make a Refresh contact the gateway, which is a
    side effect the page must not have and, on a dead gateway, a timeout on every look. A failed read
    is recorded (F4) and the card is left out: it must never look logged in or vanish without a trace.
    """
    try:
        return ibkr_login.collect(now=now)
    except Exception as exc:  # noqa: BLE001 - recorded (F4), the card is omitted
        _record("ibkr", "_ibkr_login_state", exc)
        return None


def _gb_text(nbytes: int, limit_gb: float, env: str) -> tuple[str, str]:
    """``(state, text)``: "warn" when ABOVE ``limit_gb`` (GB = 10^9 bytes), else "ok"."""
    gb = nbytes / 1e9
    if gb > limit_gb:
        return "warn", f"{gb:.2f} GB, above {limit_gb:g} GB ({env})"
    return "ok", f"{gb:.2f} GB"


def _int_or_none(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _backups(now: datetime) -> dict:
    """The Backups card (#309, avo 2026-10-16): the backup job's own records, plus the read-only cleanup dry run.

    Reads ``<backup dir>/.backup_sizes.json`` (both folder sizes, written by the nightly run after the off-machine
    copy) and ``.integrity_scan.json`` (the last read-only self-check of the local folder). Neither needs a folder
    walk, and the off-machine (Drive) folder is never touched: walking it took ~20 min (2026-10-16). The one folder
    read here is the LOCAL backup folder, listed read-only by ``backup_store.expired_backups`` (#124 G9) to say what
    the retention rule marks expired; nothing is deleted from it. A failure to read is recorded (F4) and the card
    shows "unknown"; it never fails the page."""
    from RevenueChaser.core import config
    from RevenueChaser.scripts import backup_store
    try:
        facts = backup_store.backup_facts()
    except Exception as exc:  # noqa: BLE001 - recorded (F4), the card says unknown
        _record(None, "_backups", exc)
        facts = {"folder": None, "sizes": None, "scan": None}
    sizes, scan = facts.get("sizes") or {}, facts.get("scan") or {}

    local_bytes = _int_or_none(sizes.get("local"))
    local = {"state": "unknown", "bytes": None, "limit_gb": config.RAW_STORE_BACKUP_WARN_GB,
             "text": "unknown: no backup run has recorded sizes yet"}
    if local_bytes is not None:
        state, text = _gb_text(local_bytes, config.RAW_STORE_BACKUP_WARN_GB, "RAW_STORE_BACKUP_WARN_GB")
        local.update(state=state, text=text, bytes=local_bytes, files=_int_or_none(sizes.get("local_files")),
                     duckdb_bytes=_int_or_none(sizes.get("duckdb_bytes")),
                     duckdb_files=_int_or_none(sizes.get("duckdb_files")),
                     docs_zip_bytes=_int_or_none(sizes.get("docs_zip_bytes")),
                     docs_zip_files=_int_or_none(sizes.get("docs_zip_files")))

    off_bytes = _int_or_none(sizes.get("offmachine"))
    off = {"state": "unknown", "bytes": None, "limit_gb": config.RAW_STORE_BACKUP_MIRROR_WARN_GB,
           "text": "unknown: no off-machine copy has been recorded yet"}
    if not str(config.RAW_STORE_BACKUP_MIRROR).strip():
        off.update(state="not set", text="not set: RAW_STORE_BACKUP_MIRROR is empty, so no copy leaves this machine")
    elif off_bytes is not None:
        state, text = _gb_text(off_bytes, config.RAW_STORE_BACKUP_MIRROR_WARN_GB, "RAW_STORE_BACKUP_MIRROR_WARN_GB")
        off.update(state=state, text=text, bytes=off_bytes, files=_int_or_none(sizes.get("offmachine_files")))

    checkpoints = {"state": "unknown",
                   "text": "unknown, not recorded: the backup job does not write the checkpoints folder (they are "
                           "made by hand), and this page never walks the off-machine folder; not in the total above"}

    scan_out = {"state": "unknown", "last_day": None, "failed": None, "checked": None, "next_due": None,
                "text": "unknown: no integrity scan has run yet"}
    if scan.get("last_day"):
        failed = _int_or_none(scan.get("failed")) or 0
        ok = bool(scan.get("ok")) and failed == 0
        scan_out.update(state="ok" if ok else "bad", last_day=str(scan.get("last_day")), failed=failed,
                        checked=_int_or_none(scan.get("checked")),
                        next_due=str(scan["next_due"]) if scan.get("next_due") else None,
                        text=(f"OK, {_int_or_none(scan.get('checked')) or 0:,} files match the manifest" if ok
                              else f"{failed} file{'s do' if failed != 1 else ' does'} not match (see the backup log)"))
    # #124 G9: what the retention rule marks expired, from one read-only listing of the local folder. A scan that
    # could not read it keeps the counts None, so the card reads "unknown" and never an empty-looking zero.
    expired_out = {"state": "unknown", "count": None, "bytes": None,
                   "why": "the local backup folder could not be read"}
    try:
        expired = backup_store.expired_backups()
    except Exception as exc:  # noqa: BLE001 - recorded (F4), the card says unknown
        _record(None, "_backups", exc)
    else:
        expired_out.update(state=expired.state, count=expired.count, bytes=expired.bytes, why=expired.why)
    return {"folder": facts.get("folder"), "recorded_at": sizes.get("recorded_at") or None,
            # G8 (#124): the off-machine location the job itself copies to. A setting read as text; the folder is
            # never listed, walked or stat-ed from this page.
            "offmachine_path": str(config.RAW_STORE_BACKUP_MIRROR or "").strip() or None,
            "local": local, "offmachine": off, "checkpoints": checkpoints, "scan": scan_out,
            "expired": expired_out}


def _provider_health() -> dict | None:
    """The banner's health: ``history.last_run(BOARD)["provider_health"]``. Store-only; None when nothing is recorded."""
    from RevenueChaser.core import history

    try:
        row = history.last_run(BOARD)
    except Exception:  # noqa: BLE001 - a missing or locked history file never fails the inventory
        return None
    return (row or {}).get("provider_health")


def collect(now: datetime | None = None) -> dict:
    """The Settings payload. ``now`` is the clock for the staleness windows (tests pin it)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    _begin_call()
    # G5 (#124): how long the read took, so the board can print it instead of guessing.
    started = time.perf_counter()
    try:
        with rawstore.session(), datahealth.memo_scope():   # one open per store file, not one per query
            result = _collect(now)
    finally:
        _end_call()
    result["timing"] = {"collect_seconds": round(time.perf_counter() - started, 2)}
    return result


def _corpus_note(tickers: list[str]) -> str:
    """#194: recorded 10-K / 20-F misses and the no-CIK count. Store only, no network."""
    from RevenueChaser.module_value_chain import engine as vc_engine

    if not _table_exists("chain_meta"):
        return ""
    s = vc_engine.corpus_miss_summary(tickers)
    if s is None:  # unreadable store: show nothing rather than a wrong number (F4)
        return ""
    no_cik = s["no_cik"]  # None == no CIK map stored: unknown, not "all of them"
    if not s["recorded"] and not no_cik:
        return ""
    note = (f"{s['recorded']} recorded misses ({s['no_filing']} no 10-K/20-F, "
            f"{s['form_40f']} 40-F; {s['due']} due for retry)")
    if no_cik:
        note += f" - {no_cik} no CIK"
    return note


def _recorded_misses() -> set[str] | None:
    """#381 (DF97): the tickers the Value Chain settled as "no usable annual filing", upper-cased.

    Store only, read through the function-level import ``_corpus_note`` already uses, so Settings
    gains no module-level dependency on a board package. ``None`` when the store cannot be read:
    the corpus holes then keep their old meaning rather than silently dropping to 0.
    """
    from RevenueChaser.module_value_chain import engine as vc_engine

    if not _table_exists("chain_meta"):
        return None
    try:
        return {ticker.upper() for ticker in vc_engine.stored_misses()}
    except Exception as exc:  # noqa: BLE001 - a failed read must not shrink the count
        _record("chain_meta", "_recorded_misses", exc)
        return None


def _documents_note() -> str:
    """D16 (#299): stored documents per form and the newest ``fetched_at``. Store only."""
    if not _table_exists("edgar_documents"):
        return ""
    try:
        rows = rawstore.query("SELECT form, COUNT(*) AS n FROM edgar_documents GROUP BY form ORDER BY n DESC, form",
                              db=_db_of("edgar_documents"))
    except Exception as exc:  # noqa: BLE001
        _record("edgar_documents", "_documents_note", exc)
        return ""
    if not rows:
        return ""
    forms = ", ".join(f"{r['form'] or DASH} {int(r['n']):,}" for r in rows)
    return f"documents {forms}; newest fetched {_fmt_date(_max_value('edgar_documents', 'fetched_at'))}"


def _sec_file_row(ticker: str) -> dict | None:
    """One ticker's newest present row in the SEC exchange file (#301 D21); None when it is not there."""
    from RevenueChaser.core import sec_files
    rows = sec_files.latest("exchange", keys=[ticker])
    return rows[0] if rows else None


def _universe_ciks() -> set[str] | None:
    """CIKs of every ``equity_universe`` ticker, via the stored SEC exchange file; None when unreadable."""
    if not (_table_exists("equity_universe") and _table_exists("sec_exchange_file")):
        return None
    from RevenueChaser.core.sec_submissions import norm_cik
    try:
        rows = rawstore.query(
            "SELECT DISTINCT s.cik FROM equity_universe u JOIN (SELECT ticker, cik, status FROM sec_exchange_file "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY run_at DESC) = 1) s "
            "ON s.ticker = u.ticker WHERE s.status = 'present' AND s.cik IS NOT NULL")
        return {norm_cik(r["cik"]) for r in rows if str(r["cik"]).strip().isdigit()}
    except Exception as exc:  # noqa: BLE001
        _record("sec_exchange_file", "_universe_ciks", exc)
        return None


def _sec_submissions_row() -> dict:
    """The "SEC submissions" row (#299, D15): universe CIKs the last sweep refreshed, the ones it did not
    (failed, or new since), the newest filing date and fetch. A failure is a note, never an exception."""
    row = {
        "key": "sec_submissions", "label": "SEC submissions",
        "tables": ["sec_submissions", "sec_submissions_cik"],
        "rows": _count("sec_submissions"),
        "last_date": _max_date(("sec_submissions", "filing_date")),
        "holes": None, "hole_label": "universe companies not refreshed in the last run (failed or new)",
        "fetch": {"route": "/run/edgar-sync", "label": "Fetch 10-K sync"},
        "note": "no submissions yet (the daily EDGAR sync stores them)",
    }
    from RevenueChaser.core import sec_submissions
    try:
        h = sec_submissions.health_summary()
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("sec_submissions", "_sec_submissions_row", exc)
        return row
    if not h["last_run_id"] and not h["filings"]:
        return row
    universe = _universe_ciks()
    parts = []
    if universe is not None:
        refreshed = len(universe & h["last_run_checked"])
        row["holes"] = len(universe - h["last_run_checked"])
        parts.append(f"last run {_fmt_date(h['last_run_at'])}: {refreshed:,} of {len(universe):,} universe companies "
                     f"refreshed")
    else:
        parts.append(f"last run {_fmt_date(h['last_run_at'])}: {h['last_run_ciks']:,} companies refreshed")
    parts.append(f"{h['ciks']:,} CIKs with filings; newest filing {_fmt_date(h['newest_filing_date'])}, "
                 f"newest fetched {_fmt_date(h['newest_fetched_at'])}")
    row["note"] = "; ".join(parts)
    return row


def _sec_files_row() -> dict:
    """The "SEC exchange & fund files" row (#301 D21 + D8): present rows against the file, members against the
    floor, the last pull's appends and the file's Last-Modified. Store plus the pull receipts, zero network."""
    row = {
        "key": "sec_files", "label": "SEC exchange & fund files",
        "tables": ["sec_exchange_file", "sec_mf_file"],
        "rows": _count("sec_exchange_file"),
        "last_date": _max_date(("sec_exchange_file", "run_at"), ("sec_mf_file", "run_at")),
        "holes": None, "hole_label": "", "fetch": None,
        "note": "no SEC file stored yet (RC-Daily-Bars writes it on a weekday night)",
    }
    from RevenueChaser.core import sec_files
    try:
        h = sec_files.health_summary()
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        _record("sec_exchange_file", "_sec_files_row", exc)
        return row
    if not h["exchange"]["present"] and not h["mf"]["present"]:
        return row

    def outcome(d: dict) -> str:
        """The last pull's own outcome, so a stale row is never read as a file that simply did not change
        (avo 2026-10-22). The table appends on change only, so "appended 0" alone cannot say which happened."""
        if d.get("last_pull") == "failed":
            night = d.get("session") or DASH
            good = d.get("good_session")
            tail = f", last good pull {good}" if good else ""
            return f"pull FAILED {night} ({d.get('error') or 'no reason recorded'}){tail}"
        if d.get("last_pull") == "unknown":
            return "no pull recorded yet"
        session = d.get("session") or DASH
        appended = d.get("appended")
        if appended is None:
            return f"pulled {session}"
        if int(appended) == 0 and not int(d.get("absent") or 0):
            return f"pulled {session}, unchanged"
        return f"pulled {session}, appended {int(appended):,} (+{int(d.get('absent') or 0):,} absent)"

    def one(d: dict) -> str:
        file_rows = f"{int(d['file_rows']):,}" if d["file_rows"] is not None else DASH
        text = (f"{int(d['present']):,} present vs {file_rows} in the file, "
                f"{int(d['members']):,} members / floor {int(d['floor']):,}")
        return f"{text}, {outcome(d)}, Last-Modified {d['last_modified'] or DASH}"

    row["note"] = f"exchange: {one(h['exchange'])}; funds: {one(h['mf'])}"
    return row


# ── The registry on each row (S5 of the Data Health plan, #124) ─────────────────────────────────
# ``_collect`` keeps building the rows it always built. Below, ``_attach_registry`` adds what
# ``core.datasets.REGISTRY`` knows about that dataset — source, writer, coverage, freshness — and
# ``sort_key`` orders the table worst coverage first. A row's own counts, holes, action and note
# are never touched or replaced here.

def _takes_now(fn) -> bool:
    """True when ``fn`` accepts a ``now`` argument (the registry's summary callables do; a bare one may not)."""
    try:
        return "now" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# What a coverage with no definable population reads. Never "0%": a percentage of nothing is a lie.
COVERAGE_NA = "n/a"


def _coverage_record(spec: datasets.DatasetSpec, row: dict, now: datetime | None = None) -> dict:
    """One dataset's coverage as a renderable record. Never raises.

    ``label`` is ``n/a`` when there is no definable population (no ``expected``, zero ``expected``,
    or no ``covered``), the counts and the rounded percentage when there is one, ``unknown`` when
    the callable returned no ``Health``, ``error`` when it raised — recorded on this row's table,
    so the page shows it in the row's own errors rather than failing the whole board.
    """
    empty = {"covered": None, "expected": None, "fraction": None, "newest": None, "problems": []}
    coverage = spec.coverage
    if coverage is None or isinstance(coverage, datasets.CoverageSpec):
        # The two-query form has no population/presence evaluator here yet (#124): say n/a.
        return dict(empty, label=COVERAGE_NA)
    try:
        health = coverage(now=now) if _takes_now(coverage) else coverage()
    except Exception as exc:  # noqa: BLE001 - a summary failure must not break the page
        # The summary that just failed is the one the row's own builder already reported: one
        # entry for one cause, so the error list names each failure once.
        table = (row.get("tables") or [None])[0]
        seen = getattr(_CALL, "errors", None) or []
        if not any(e.get("table") == table and e.get("error") == str(exc) for e in seen):
            _record(table, "_coverage", exc)
        return dict(empty, label="error", problems=[str(exc)[:120]])
    if not isinstance(health, datahealth.Health):
        return dict(empty, label="unknown")
    ratio = health.coverage
    newest = _fmt_date(health.newest)
    return {
        "covered": health.covered,
        "expected": health.expected,
        "fraction": ratio,
        "label": (COVERAGE_NA if ratio is None
                  else f"{health.covered:,} / {health.expected:,} ({ratio * 100:.0f}%)"),
        "newest": None if newest == DASH else newest,
        "problems": list(health.problems) + list(health.consistency_problems()),
    }


# ── The Fresh cell's named states (S5b, G3, #124) ────────────────────────────────────────────────
# The Fresh column printed a bare dash for four different situations, so "no time column", "the
# query failed" and "this table holds no rows" looked identical. Each state now carries its own
# words, and a failed query is a state of its own: it can never render as an empty dataset.
FRESH_DATE = "date"
FRESH_EMPTY = "empty"
FRESH_NO_TIME_COLUMN = "no_time_column"
FRESH_RUN_LOG = "run_log"
FRESH_RETIRED = "retired"
FRESH_BOOKKEEPING = "bookkeeping"
FRESH_FAILED = "failed"

NO_TIME_COLUMN_TEXT = "no time column - see the provenance rows"
EMPTY_TEXT = "no date stored yet"
RUN_LOG_TEXT = "taken from the run log"
RETIRED_TEXT = "retired - no freshness claim made"
BOOKKEEPING_TEXT = "store bookkeeping - no freshness claim"
QUERY_FAILED_TEXT = "query failed - this is not an empty dataset"


def _fresh(state: str, text: str, date: str | None = None) -> dict:
    """One Fresh-cell record: the state, the date where there is one, and the words to print."""
    return {"state": state, "date": date, "text": text}


def _run_log_dates() -> dict[str, str]:
    """The newest finished run per ``job_runs.registry_name``, read once for the whole table (S5b part 3).

    ``registry_name`` is the task's ``automations.REGISTRY`` name, the same name a ``from_run_log``
    dataset carries as its ``writer``, so ``_attach_registry`` serves every such dataset from one
    query instead of one per dataset. An unreadable run log is recorded and reads as no run: the
    words still stand and no date is invented.
    """
    dates: dict[str, str] = {}
    if not _table_exists("job_runs"):
        return dates
    try:
        rows = rawstore.query(
            "SELECT registry_name, MAX(finished_at) AS at FROM job_runs "
            "WHERE registry_name IS NOT NULL AND finished_at IS NOT NULL "
            "GROUP BY registry_name", db=_db_of("job_runs"))
    except Exception as exc:  # noqa: BLE001 - recorded (F4); the words stand without a date
        _record("job_runs", "_run_log_dates", exc)
        return dates
    for r in rows:
        found = _fmt_date(r.get("at"))
        if found and found != DASH:
            dates[str(r["registry_name"])] = found
    return dates


def _no_claim_state(spec: datasets.DatasetSpec) -> tuple[str, str]:
    """Which "no claim" this dataset makes: retired, or the store's own bookkeeping (S5b part 4).

    Read off the store's own ``MANIFEST`` retired flag: a dataset that lists a finished table reads
    as retired, never as stale, and nothing has to be re-listed here.
    """
    retired = any((rawstore.MANIFEST.get(t) or {}).get("retired") for t in spec.tables)
    return (FRESH_RETIRED, RETIRED_TEXT) if retired else (FRESH_BOOKKEEPING, BOOKKEEPING_TEXT)


def _fresh_record(spec: datasets.DatasetSpec, run_dates: dict[str, str] | None = None) -> dict:
    """The Fresh cell's record for one dataset: ``{state, date, text}`` (S5b, G3, #124).

    Four kinds of table, four kinds of claim: a ``(table, column)`` pair whose read returned a
    date; a dataset the registry names no time column for; a derived table whose freshness is the
    run that wrote it; and a retired or bookkeeping table that makes no claim. A pair whose tables
    hold no row reads ``empty``. A failed query is ``failed``: recorded, and never rendered like a
    table that holds no rows.

    ``run_dates`` is ``_run_log_dates()`` for this page, so every ``from_run_log`` dataset is served
    by the one query the caller made. Read here when it is not supplied.
    """
    if spec.freshness == datasets.FROM_RUN_LOG:
        dates = _run_log_dates() if run_dates is None else run_dates
        date = dates.get(str(spec.writer)) if spec.writer else None
        if date:
            return _fresh(FRESH_RUN_LOG, f"{RUN_LOG_TEXT}, {date}", date)
        return _fresh(FRESH_RUN_LOG, RUN_LOG_TEXT)
    if spec.freshness == datasets.NO_CLAIM:
        state, text = _no_claim_state(spec)
        return _fresh(state, text)
    pairs = spec.freshness_pairs()
    if not pairs:
        return _fresh(FRESH_NO_TIME_COLUMN, NO_TIME_COLUMN_TEXT)
    dates = []
    for table, column in pairs:
        try:
            dates.append(_fmt_date(_max_value_or_raise(table, column)))
        except Exception as exc:  # noqa: BLE001 - a failed read is its own state, not an empty table
            _record(table, "_fresh", exc)
            return _fresh(FRESH_FAILED, QUERY_FAILED_TEXT)
    known = [d for d in dates if d and d != DASH]
    if not known:
        return _fresh(FRESH_EMPTY, EMPTY_TEXT)
    date = max(known)
    return _fresh(FRESH_DATE, date, date)


def _retired_dataset(spec: datasets.DatasetSpec) -> bool:
    """True when every table the dataset owns is MANIFEST ``retired`` (G11: "nothing, by design")."""
    return all(bool((rawstore.MANIFEST.get(table) or {}).get("retired")) for table in spec.tables)


def _attach_registry(rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Add the registry's source, writer, coverage, freshness and gap to each collected row (S5, #124).

    A row whose key is not in ``datasets.REGISTRY`` keeps everything it carries and gains
    ``source: None`` — the table renders a dash in the three registry cells. Nothing is invented.

    The run log is read at most once, and only when a row on this page dates itself by it.

    G11 (#124) adds three facts of its own: ``gap_kind`` (the kind's name or None), ``next_step``
    (the written sentence, or None) and ``retired`` (every owned table is MANIFEST retired, so the
    detail row says "nothing, by design"). ``gap_kind`` is None when nothing is written down; the
    renderer must not read that as complete.

    ``now`` is the collect's clock, handed to each coverage that takes one, so a dated summary is asked
    the same question the row builder asked and is served from the same memo (#150).
    """
    run_dates: dict[str, str] | None = None
    for row in rows:
        try:
            spec = datasets.by_key(str(row.get("key") or ""))
        except KeyError:
            row["source"] = None
            continue
        # The store file the dataset is written to (G2): the table's store pill names it.
        row["db"] = spec.db
        row["source"] = spec.source.value
        row["writer"] = spec.writer
        row["writer_note"] = spec.writer_note
        row["coverage"] = _coverage_record(spec, row, now)
        if spec.freshness == datasets.FROM_RUN_LOG and run_dates is None:
            run_dates = _run_log_dates()
        row["fresh"] = _fresh_record(spec, run_dates)
        # The registry's own caveat (the #82/#83 wording), shown in the detail row, never invented here.
        row["fresh_note"] = spec.freshness_note
        row["gap_kind"] = spec.gap_kind.value if spec.gap_kind is not None else None
        row["next_step"] = spec.next_step
        row["retired"] = _retired_dataset(spec)
    return rows


# ── "Other store tables" (S6 of the Data Health plan, #124) ─────────────────────────────────────
# The backstop for a MANIFEST table no dataset claims. Normally empty: ``core.datasets`` claims
# every table, and ``core/tests/test_datasets.py`` pins that. If a table ever escapes the
# registry, this section says so in plain words instead of the table vanishing from the board.
#
# The column names this store writes a date or a timestamp into. An unclaimed table shows its
# newest value only when it carries one of them; a table with none of them says "no time column"
# rather than guessing at a date.
TIME_COLUMNS: tuple[str, ...] = (
    "run_at", "fetched_at", "checked_at", "generated_at", "updated_at", "started_at", "finished_at",
    "last_seen", "session_date", "filed", "period_end", "period", "as_of", "ex_date", "ts",
)


def unclaimed_tables() -> list[dict]:
    """Every MANIFEST table no dataset claims (S6, #124): store file, row count, newest date.

    One entry per table, ordered by name, read per store file through the usual helpers. ``newest``
    is ``None`` when the table carries no known time column. With nothing unclaimed this asks the
    store nothing at all: an empty section must not cost a query.
    """
    names = datasets.unclaimed(rawstore.MANIFEST)
    if not names:
        return []
    out = []
    for table in sorted(names):
        columns = [c for c in TIME_COLUMNS if c in _existing_columns(table)]
        newest = _max_date(*[(table, c) for c in columns]) if columns else DASH
        out.append({"table": table, "db": _db_of(table), "rows": _count(table),
                    "newest": None if newest == DASH else newest})
    return out


# ── S7 (#124): identity coverage ──────────────────────────────────────────────
# Three objects, counted over the same population (the names the boards screen):
#
# 1. the universe funnel — the SEC ticker and exchange file's present rows, down through the
#    listed venues, the stored universe, the $300M floor and the excluded security types to
#    ``universe.classification_universe()``. The steps are the rules the rest of the store already
#    applies (``sec_files.latest``, ``universe_fetch.SEC_LIST_EXCHANGES``, ``DEFAULT_MC_MIN``,
#    ``universe.TYPE_GROUPS``); this block only counts them, in one read per store file.
# 2. the inputs each surviving name carries.
# 3. the identifier matrix, each row naming its own denominator.
#
# A failed read is ``COVERAGE_UNREADABLE`` and the page says "could not be read" — never a zero.
# ``COVERAGE_UNMEASURED`` is for a number the store holds no definition for ("not measured").


def _coverage(key: str, label: str, denominator: str, *, covered: int | None,
              expected: int | None, state: str = "", detail: str = "") -> dict:
    """One row of the inputs or identifier table. ``covered is None`` means unknown, never zero."""
    return {"key": key, "label": label, "covered": covered, "expected": expected,
            "denominator": denominator, "state": state, "detail": detail}


def _present_file_rows() -> list[dict] | None:
    """The SEC ticker and exchange file's present rows (``sec_files.latest`` is the one reader).

    ``None`` when the read failed; ``[]`` when nothing is stored yet (a fresh store, not a failure).
    """
    from RevenueChaser.core import sec_files
    try:
        return sec_files.latest("exchange")
    except Exception as exc:  # noqa: BLE001 - a failed read is said, never counted as nothing
        _record("sec_exchange_file", "identity_coverage", exc)
        return None


def _universe_caps() -> dict[str, float] | None:
    """``ticker -> stored market cap`` from ``equity_universe``; ``None`` when it cannot be read."""
    if not _table_exists("equity_universe"):
        return {}
    try:
        rows = rawstore.query("SELECT ticker, market_cap FROM equity_universe")
    except Exception as exc:  # noqa: BLE001
        _record("equity_universe", "identity_coverage", exc)
        return None
    return {r["ticker"]: r.get("market_cap") for r in rows if r.get("ticker")}


def _session_tickers(table: str, db: str, session) -> set[str] | None:
    """Tickers with a row on ``session`` in ``table``; ``None`` when the read failed."""
    try:
        rows = rawstore.query(f"SELECT DISTINCT ticker FROM {table} WHERE session_date = ?",
                              [session], db=db)
    except Exception as exc:  # noqa: BLE001
        _record(table, "identity_coverage", exc)
        return None
    return {r["ticker"] for r in rows if r.get("ticker")}


def _spine_identifiers() -> dict[str, dict] | None:
    """``ticker -> {cusip, isin, figi}`` from ``security_master``; ``None`` when the read failed.

    A column this store does not carry yet reads as absent (the drill's ``n/a`` rule), never as an
    error. One query, whatever subset of the three columns exists.
    """
    if not _table_exists("security_master"):
        return {}
    columns = [c for c in ("cusip", "isin", "figi") if _has_column("security_master", c)]
    if not columns:
        return {}
    try:
        rows = rawstore.query(f"SELECT ticker, {', '.join(columns)} FROM security_master")
    except Exception as exc:  # noqa: BLE001
        _record("security_master", "identity_coverage", exc)
        return None
    return {r["ticker"]: r for r in rows if r.get("ticker")}


def _value(row: dict | None, column: str) -> str:
    """One identifier of one ``security_master`` row, stripped; ``""`` for NULL or a missing column."""
    return str((row or {}).get(column) or "").strip()


def _covered_from_missing(kept: list[str], table: str) -> int | None:
    """How many of ``kept`` have a row in ``table`` — the ``_missing`` pattern, one query.

    A table this store does not carry stores nothing, so it covers none of them (0). A table that
    is there but cannot be read is ``None``: the page says "could not be read", never 0.
    """
    if not _table_exists(table):
        return 0
    missing = _missing(kept, table)
    return None if missing is None else len(kept) - missing


def _universe_funnel(kept: set[str], present: list[dict] | None,
                     caps: dict | None) -> tuple[list[dict], str]:
    """``(steps, note)``: the funnel from the SEC file to the names the boards screen.

    ``note`` holds the words to show instead when the funnel cannot be drawn (a read failed, or
    the file is not stored yet): a funnel that does not add up is not a funnel.
    """
    from RevenueChaser.core import funnel as funnel_mod
    from RevenueChaser.core import universe as spine
    from RevenueChaser.core.universe_fetch import SEC_LIST_EXCHANGES

    if present is None or caps is None:
        return [], "The funnel could not be read from the store."
    if not present:
        return [], "The SEC ticker and exchange file is not stored yet, so there is nothing to count."

    floor = float(_MC_FLOOR)
    listed = {str(r["ticker"]).upper() for r in present
              if str(r.get("exchange") or "").strip().casefold() in SEC_LIST_EXCHANGES}
    in_universe = listed & set(caps)
    below = {t for t in in_universe
             if caps[t] is not None and float(caps[t]) < floor}
    no_cap = {t for t in in_universe if caps[t] is None}
    # The type rule is ``universe``'s own (``TYPE_GROUPS``, together ``EXCLUDED_TYPES``); untyped
    # names stay, exactly as ``classification_universe`` keeps them.
    types = spine._security_types()
    by_group = {key: set() for key, _label, _members in spine.TYPE_GROUPS}
    for ticker in in_universe - below - no_cap:
        group = next((key for key, _label, members in spine.TYPE_GROUPS
                      if types.get(ticker) in members), None)
        if group:
            by_group[group].add(ticker)
    remaining = in_universe - below - no_cap - set().union(*by_group.values())

    steps = [
        funnel_mod.step("file", "Companies in the SEC ticker and exchange file", len(present), "start"),
        funnel_mod.step("venues", "Not on Nasdaq, the New York Stock Exchange or Cboe",
                        len(present) - len(listed), "drop", by_design=True),
        # A listed name the stored universe does not hold is the universe being behind the SEC file
        # (a new listing since the last Fetch), not a rule: the row says so.
        funnel_mod.step("universe", "Not in the stored equity universe",
                        len(listed) - len(in_universe), "drop", by_design=False),
        funnel_mod.step("floor", f"Market cap below ${floor / 1e6:,.0f} million",
                        len(below), "drop", by_design=True),
        # A missing cap is not a rule: it is the store's own hole, and the row says so.
        funnel_mod.step("no_cap", "No market cap stored", len(no_cap), "drop", by_design=False),
    ]
    steps += [funnel_mod.step(key, SECURITY_TYPE_WORDS.get(key, label), len(by_group[key]),
                              "drop", by_design=True)
              for key, label, _members in spine.TYPE_GROUPS]
    # A kept name the file does not list is still screened: a note, outside the arithmetic.
    unreached = kept - in_universe
    if unreached:
        steps.append(funnel_mod.step("unreached", "Names the boards screen that the SEC file does not list",
                                     len(unreached), "note"))
    steps.append(funnel_mod.step("kept", "Names the boards screen", len(remaining), "result"))
    if funnel_mod.validate(steps):
        return [], "The funnel does not add up on this store, so it is not shown."
    return steps, ""


def _cusip_map_counts() -> dict | None:
    """The CUSIP map's numbers (#207, D19) as the section states them; ``None`` when unreadable.

    * ``asked`` — CUSIPs ``identity_cusip`` holds an answer for (the map's whole population);
    * ``mapped`` — of those, the ones whose newest answer names a ticker;
    * ``empty`` — asked and answered with no ticker: the store's own "unmatched" count;
    * ``never_asked`` — CUSIPs the 13F filings need a ticker for that the map has not asked about,
      from the map step's own queue (``module_sec_13f.engine.map_candidates``);
    * ``board_left_out`` — what the 13F board drops after its own filters (newest period, long
      equity holdings of the active filings, ``engine.unmatched_summary``). The period's own CUSIP
      total is not stored, so this count has no denominator to count against.

    The map, its queue and the board's summary are three reads. Each carries its own state, so one
    failing read leaves the other two answering, and the failed one says so instead of a zero.
    """
    from RevenueChaser.core import identity_cusip

    out = {"asked": 0, "mapped": 0, "empty": 0,
           "never_asked": 0, "never_asked_state": "",
           "board_left_out": 0, "board_state": ""}
    map_read = _table_exists(identity_cusip.TABLE)
    if map_read:
        try:
            latest = identity_cusip.latest()
        except Exception as exc:  # noqa: BLE001
            _record(identity_cusip.TABLE, "identity_coverage", exc)
            return None
        out["asked"] = len(latest)
        out["mapped"] = sum(1 for row in latest.values()
                            if row.get("status") == identity_cusip.PRESENT and row.get("ticker"))
        out["empty"] = out["asked"] - out["mapped"]
    if _count("sec13f_holdings") and all(_table_exists(t) for t in SEC13F_TABLES):
        from RevenueChaser.module_sec_13f import engine as sec_13f
        if map_read:
            try:
                out["never_asked"] = len(sec_13f.map_candidates())
            except Exception as exc:  # noqa: BLE001
                _record("sec13f_holdings", "identity_coverage", exc)
                out["never_asked"], out["never_asked_state"] = None, COVERAGE_UNREADABLE
        else:
            # The queue excludes the CUSIPs the map holds, so without its table the count has no
            # definition here — said, never drawn as "nothing is waiting".
            out["never_asked"], out["never_asked_state"] = None, COVERAGE_UNMEASURED
        try:
            summary = sec_13f.unmatched_summary(sec_13f.load_cusip_ticker())
            out["board_left_out"] = int((summary or {}).get("cusips") or 0)
        except Exception as exc:  # noqa: BLE001
            _record("sec13f_holdings", "identity_coverage", exc)
            out["board_left_out"], out["board_state"] = None, COVERAGE_UNREADABLE
    return out


def _count_text(n: int, word: str) -> str:
    """``3 names`` / ``1 name``: a count with its word, thousands kept apart and the plural right."""
    return f"{int(n):,} {word}" if int(n) == 1 else f"{int(n):,} {word}s"


def identity_coverage(tickers: list[str], now: datetime | None = None) -> dict:
    """S7 (#124): the universe funnel, the inputs the kept names carry, and identifier coverage.

    ``tickers`` is ``classification_universe()`` — computed once by ``_collect`` and reused here, so
    the tables count the population the boards screen rather than a second definition of it. A
    stored universe that is ahead of the SEC file is named in the funnel as a note, never dropped.
    Store only, one read per store file.
    """
    kept = sorted({str(t).upper() for t in (tickers or []) if str(t or "").strip()})
    n_kept = len(kept)
    denominator = f"{_count_text(n_kept, 'name')} the funnel keeps"

    present = _present_file_rows()
    funnel, funnel_note = _universe_funnel(set(kept), present, _universe_caps())

    # ── the inputs ──
    inputs = []
    if present is None:
        inputs.append(_coverage("cik", "SEC company number (CIK)", denominator, covered=None,
                                expected=n_kept, state=COVERAGE_UNREADABLE))
    else:
        # The rule ``_universe_ciks`` counts: a present row whose CIK is numeric.
        with_cik = {str(r["ticker"]).upper() for r in present
                    if str(r.get("cik") or "").strip().isdigit()}
        inputs.append(_coverage("cik", "SEC company number (CIK)", denominator,
                                covered=sum(1 for t in kept if t in with_cik), expected=n_kept))

    from RevenueChaser.core import prices, quotes
    covered, state = None, COVERAGE_UNREADABLE
    try:
        newest = _summary("quotes", quotes.health_summary)["newest_session"]
        if newest is None:
            covered, state = 0, ""          # no quote row stored at all: a real zero
        else:
            have = _session_tickers("quote_snapshot", "raw", newest)
            if have is not None:
                covered, state = sum(1 for t in kept if t in have), ""
    except Exception as exc:  # noqa: BLE001
        _record("quote_snapshot", "identity_coverage", exc)
    inputs.append(_coverage("quotes", "Quote and market cap, newest session", denominator,
                            covered=covered, expected=n_kept, state=state))

    covered, state = None, COVERAGE_UNREADABLE
    try:
        summary = _summary("prices", prices.health_summary, now)
        if summary.get("calendar_error"):
            covered, state = None, COVERAGE_UNMEASURED   # no complete session to count against
        elif summary.get("newest_session") is None:
            covered, state = 0, ""                        # no bar stored at all
        else:
            have = _session_tickers("price_bars_daily", "prices", summary["latest_complete"])
            if have is not None:
                covered, state = sum(1 for t in kept if t in have), ""
    except Exception as exc:  # noqa: BLE001
        _record("price_bars_daily", "identity_coverage", exc)
    inputs.append(_coverage("bars", "Daily price bars, latest completed session", denominator,
                            covered=covered, expected=n_kept, state=state))

    spine = _spine_identifiers()
    for key, label, column in (("cusip", "CUSIP", "cusip"), ("figi", "FIGI", "figi")):
        if spine is None:
            inputs.append(_coverage(key, label, denominator, covered=None, expected=n_kept,
                                    state=COVERAGE_UNREADABLE))
        else:
            inputs.append(_coverage(key, label, denominator,
                                    covered=sum(1 for t in kept if _value(spine.get(t), column)),
                                    expected=n_kept))

    for key, label, table in (("facts", "EDGAR company facts", "edgar_financials"),
                              ("annual", "Annual report (10-K or 20-F)", "chain_filings"),
                              ("ownership", "Ownership", "ownership_snapshot")):
        covered = _covered_from_missing(kept, table)
        inputs.append(_coverage(key, label, denominator, covered=covered, expected=n_kept,
                                state="" if covered is not None else COVERAGE_UNREADABLE))

    # ── the identifier matrix ──
    identifiers = []
    if spine is None:
        identifiers.append(_coverage("isin", "ISIN", "names with a CUSIP", covered=None,
                                     expected=None, state=COVERAGE_UNREADABLE))
    else:
        with_cusip = sum(1 for t in kept if _value(spine.get(t), "cusip"))
        with_isin = sum(1 for t in kept if _value(spine.get(t), "isin"))
        identifiers.append(_coverage(
            "isin", "ISIN", f"{_count_text(with_cusip, 'name')} with a CUSIP", covered=with_isin,
            expected=with_cusip, detail="Derived from the CUSIP; never fetched."))

    cusips = _cusip_map_counts()
    if cusips is None:
        for key, label in (("cusip_mapped", "CUSIP mapped to a ticker"),
                           ("cusip_empty", "CUSIP the store has no ticker for"),
                           ("cusip_never_asked",
                            "CUSIP the 13F filings hold that the store has never asked about")):
            identifiers.append(_coverage(key, label, "CUSIPs the store has asked about",
                                         covered=None, expected=None, state=COVERAGE_UNREADABLE))
        identifiers.append(_coverage("sec13f_left_out", "CUSIP the 13F board leaves out of its totals",
                                     "not measured", covered=None, expected=None,
                                     state=COVERAGE_UNREADABLE))
    else:
        asked, waiting = cusips["asked"], cusips["never_asked"]
        asked_text = f"{_count_text(asked, 'CUSIP')} the store has asked about"
        identifiers.append(_coverage("cusip_mapped", "CUSIP mapped to a ticker", asked_text,
                                     covered=cusips["mapped"], expected=asked))
        identifiers.append(_coverage(
            "cusip_empty", "CUSIP the store asked about and has no ticker for (the store's unmatched count)",
            asked_text, covered=cusips["empty"], expected=asked))
        # The waiting count is a second read: when it failed (or has no definition here) its
        # denominator is not a number either, and the row says so instead of counting to zero.
        need = asked + waiting if isinstance(waiting, int) else None
        need_text = (f"{_count_text(need, 'CUSIP')} that need a ticker (asked plus waiting)"
                     if need is not None else "CUSIPs that need a ticker (asked plus waiting)")
        identifiers.append(_coverage(
            "cusip_never_asked", "CUSIP the 13F filings hold that the store has never asked about",
            need_text, covered=waiting, expected=need, state=cusips["never_asked_state"]))
        identifiers.append(_coverage(
            "sec13f_left_out", "CUSIP the 13F board leaves out of its totals", "not measured",
            covered=cusips["board_left_out"], expected=None, state=cusips["board_state"],
            detail="Newest 13F period only: stock holdings (not options), in filings still in force."))

    return {"names_kept": n_kept, "funnel": funnel, "funnel_note": funnel_note,
            "inputs": inputs, "identifiers": identifiers}


def sort_key(row: dict) -> tuple:
    """The table's default order: worst coverage first, a row with none after the known ones.

    A row counts as known when its coverage has a ``fraction``; ``n/a``, ``unknown`` and ``error``
    all have none and follow. Ties break case-insensitively by label, and ``sorted`` is stable, so
    two rows alike in both keep the order ``_collect`` built them in.
    """
    coverage = row.get("coverage") or {}
    fraction = coverage.get("fraction")
    label = str(row.get("label") or row.get("key") or "").lower()
    if fraction is None:
        return (1, 0.0, label)
    return (0, float(fraction), label)


def _collect(now: datetime) -> dict:
    """Inventory of every dataset. Zero network, store only."""
    tickers = classification_universe()
    prov = _provenance()

    present_keys = [k for k in ("__meta__", "__stats__", "__fetch__") if k in prov]
    missing_keys = [k for k in ("__meta__", "__stats__", "__fetch__") if k not in prov]
    if present_keys:
        prov_note = "provenance: " + ", ".join(f"{k} {prov[k]}" for k in present_keys)
    else:
        prov_note = "provenance: none"
    if missing_keys:
        prov_note += " · missing " + ", ".join(missing_keys)

    universe_note = "provenance " + (", ".join(present_keys) if present_keys else "none")
    if missing_keys:
        universe_note += f" (missing {', '.join(missing_keys)})"

    identity = _identity_health(now)
    last_run = identity["last_run"]
    identity_note = f"last run {last_run['status']}" if last_run else "no identity run yet"
    master_age = _identity_master_age(now)

    corpus_note = _corpus_note(tickers)
    recorded_misses = _recorded_misses()  # #381 (DF97): settled names are not corpus holes

    profiles_count = _count("relatedness_profiles")
    filings_count = _count("chain_filings")
    relatedness_note = (
        f"profiles {profiles_count if profiles_count is not None else DASH} "
        f"vs filings {filings_count if filings_count is not None else DASH}")

    rows = [
        {
            "key": "universe",
            "label": "Equity universe",
            "tables": ["equity_universe"],
            "rows": _count("equity_universe"),
            "last_date": DASH,
            "holes": _count_where("equity_universe", "market_cap IS NULL"),
            "hole_label": "null market cap",
            "fetch": {"route": "/run/universe-stats", "label": "Fetch caps"},
            "fetch_alt": {"route": "/run/universe", "label": "Fetch universe"},
            "note": universe_note,
        },
        {
            "key": "universe_provenance",
            "label": "Universe provenance",
            "tables": ["equity_universe_provenance"],
            "rows": _count("equity_universe_provenance"),
            "last_date": _max_date(("equity_universe_provenance", "generated_at")),
            "holes": None,
            "hole_label": "",
            "fetch": None,
            "note": prov_note,
        },
        {
            "key": "identity",
            "label": "Identity spine",
            "tables": ["security_master", "identity_conflicts", "identity_source_cache", "identity_runs"],
            "rows": _count("security_master"),
            "last_date": _max_date(("identity_runs", "finished_at")),
            "holes": _unclassified_holes(),
            "hole_label": "unclassified ≥$300M",
            "fetch": {"route": "/run/identity", "label": "Fetch identity"},
            "note": identity_note,
            "master_age": master_age,
        },
        {
            "key": "corpus",
            "label": "10-K / 20-F corpus",
            "tables": ["chain_filings", "edgar_documents"],
            "rows": _sum_counts(["chain_filings", "edgar_documents"]),
            "last_date": _max_date(("chain_filings", "filed"),
                                   ("edgar_documents", "filed")),
            "holes": _missing(tickers, "chain_filings", exclude=recorded_misses),
            "hole_label": "no filing, excluding recorded misses" if recorded_misses is not None else "no filing",
            "fetch": {"route": "/run/edgar-sync", "label": "Fetch 10-K sync"},
            "note": "; ".join(n for n in (corpus_note, _documents_note()) if n),
        },
        _sec_submissions_row(),
        _sec_files_row(),
        {
            "key": "companyfacts",
            "label": "Companyfacts",
            "tables": ["edgar_financials"],
            "rows": _count("edgar_financials"),
            "last_date": DASH,
            "holes": _missing(tickers, "edgar_financials"),
            "hole_label": "no facts",
            "fetch": None,
            "note": "",
        },
        {
            "key": "ohlc",
            "label": "Legacy daily open/high/low/close cache (SPY fallback)",
            "tables": ["swing_ohlc"],
            "rows": _count("swing_ohlc"),
            "last_date": _max_date(("swing_ohlc", "ts")),
            "holes": _missing(tickers, "swing_ohlc"),
            "hole_label": "no bars",
            "fetch": None,
            "note": "",
        },
        _price_bars_row(now),
        _index_levels_row(now),
        _vendor_row(now),
        _quotes_row(),
        {
            "key": "index",
            "label": "Index constituents",
            "tables": ["index_constituents", "index_constituents_changes"],
            "rows": _count("index_constituents"),
            "last_date": _index_last_change()[0],
            "holes": None,
            "hole_label": "",
            "fetch": None,
            "note": _index_note(),
        },
        {
            "key": "sec13f",
            "label": "13F",
            "tables": ["sec13f_totals", "sec13f_holdings"],
            "rows": _sum_counts(["sec13f_totals", "sec13f_holdings"]),
            "last_date": _max_date(("sec13f_submissions", "period")),
            "holes": None,
            "hole_label": "",
            "fetch": None,
            "note": "",
        },
        {
            "key": "oipd",
            "label": "Implied Probability chain",
            "tables": ["oipd_chain", "oipd_meta"],
            "rows": _sum_counts(["oipd_chain", "oipd_meta"]),
            "last_date": _max_date(("oipd_meta", "as_of")),
            "holes": None,
            "hole_label": "",
            "fetch": None,
            "note": "",
        },
        _etf_holdings_row(),
        {
            "key": "relatedness",
            "label": "Relatedness profiles",
            "tables": ["relatedness_profiles", "relatedness_scores"],
            "rows": _sum_counts(["relatedness_profiles", "relatedness_scores"]),
            "last_date": _max_date(("relatedness_profiles", "fetched_at")),
            "holes": None,
            "hole_label": "",
            "fetch": None,
            "note": relatedness_note,
        },
        {
            "key": "result_history",
            "label": "Result history",
            "tables": ["result_runs"],
            "rows": _count("result_runs"),
            "last_date": _max_date(("result_runs", "session_date")),
            "holes": None,
            "hole_label": "",
            "fetch": None,
            "note": "history.duckdb (alsanvolkan/RevenueChaser#287): one header row per recorded board run",
        },
        _inputs_row(),
    ]
    tickers_list = _universe_tickers()
    board_cards = boards.board_cards()
    return {
        # Worst coverage first: the rows that need a Fetch are at the top (#124).
        "rows": sorted(_attach_registry(rows, now), key=sort_key),
        "tickers": tickers_list,
        "boards": board_cards,
        "identity": identity,
        # Fixed registry fields only: the live state is polled from /api/automations.
        "automations": automations.static_rows(),
        # #162: the IBKR Gateway login state, read from the login check's record. Never probed here.
        # Named to pair with ``ibkr_login_url`` above it, the shortcut that opens the login screen.
        "ibkr_login_state": _ibkr_login_state(now),
        "backups": _backups(now),
        "ibkr_login_url": _ibkr_login_url(),
        # S6 (#124): the backstop for a MANIFEST table no dataset claims. Built before the errors
        # list, so a failed read here is reported like any other.
        "other_tables": unclaimed_tables(),
        # S7 (#124): the funnel, the inputs and the identifiers, over the same population ``rows``
        # above use (``tickers`` is ``classification_universe()``).
        "identity_coverage": identity_coverage(tickers, now),
        "errors": list(getattr(_CALL, "errors", None) or []),
        "generated_at": now.isoformat(timespec="seconds"),
        "provider_health": _provider_health(),
    }

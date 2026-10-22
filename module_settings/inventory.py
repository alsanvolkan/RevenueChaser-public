"""Store-only data-health inventory — zero network.

Reads the ``MANIFEST`` tables through ``core.rawstore`` (which routes each table
to its DuckDB file: ``raw.duckdb``, ``edgar.duckdb``, ``history.duckdb`` or ``prices.duckdb``) and reports, per dataset,
the row count, the newest data date, and the honest hole count. A table that is
missing, empty, or has no date column shows ``—`` rather than a guess. See
``docs/data_health.md`` §2.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from RevenueChaser.core import automations, rawstore
from RevenueChaser.core.gics_map import canonical_sector
from RevenueChaser.core.identity import refresh as identity_refresh
from RevenueChaser.core.identity import types as identity_types
# a dedicated alias for the identity-health code, so it does not lean on the shared universe import below
from RevenueChaser.core.universe import DEFAULT_MC_MIN as _MC_FLOOR
from RevenueChaser.module_discord import readiness as discord_readiness
from RevenueChaser.module_settings import boards
from RevenueChaser.core.universe import DEFAULT_MC_MIN, classification_universe

DASH = "—"
# Warn once ``security_master.updated_at`` is older than this many days (#345, #381).
IDENTITY_MASTER_STALE_DAYS = 7


def _db_of(name: str) -> str:
    return (rawstore.MANIFEST.get(name) or {}).get("db", "raw")


# Per-call cache of existing table names, keyed by db. Only alive between
# ``_begin_call()`` and ``_end_call()`` (i.e. inside one ``collect()`` /
# ``ticker_detail()``), so a table created later shows up on the next Refresh.
# Thread-local so concurrent requests never share or clear each other's cache.
_CALL = threading.local()


def _begin_call() -> None:
    _CALL.tables = {}
    _CALL.errors = []


def _end_call() -> None:
    _CALL.tables = None
    _CALL.errors = None


def _record(table: str | None, where: str, exc: Exception) -> None:
    """Remember a caught query failure for this call (F4).

    A failed query must not look like "no data": ``collect()`` returns these as
    ``payload["errors"]`` and the board shows them. A *missing table* is not a
    failure and never comes through here.
    """
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
    if cache is not None:
        cache[db] = names
    return names


def _table_exists(name: str) -> bool:
    """Whether ``name`` exists in its DuckDB file (missing == no data, not error)."""
    if name not in rawstore.MANIFEST:
        return False
    return name in _existing_tables(_db_of(name))


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


def _max_value(table: str, column: str):
    if not _table_exists(table):
        return None
    try:
        rows = rawstore.query(
            f"SELECT MAX({column}) AS m FROM {table}", db=_db_of(table))
    except Exception as exc:  # noqa: BLE001
        _record(table, "_max_value", exc)
        return None
    return rows[0].get("m") if rows else None


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
        h = prices.health_summary(now)
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
        f"{h.get('latest_complete') or DASH}; UW {int(share.get('uw', 0))} / "
        f"yfinance {int(share.get('yfinance', 0))}; {int(h.get('actions_last_5', 0))} actions in the "
        f"last 5 sessions; last check {check_txt}")
    return default


# The eight fields G2-29 measured per source (phase2/06_interface.md §6); the note lists their fill.
_VENDOR_FILL_FIELDS = ("revenue", "gross_profit", "operating_income", "ebitda", "net_income", "interest_expense",
                       "total_assets", "operating_cash_flow")
_VENDOR_SOURCE_LABELS = {"uw": "UW", "yfinance": "yfinance"}


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
        h = quotes.health_summary()
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
            f"UW {rows.get(quotes.SOURCE_UW, 0)} of {h['universe']} universe; {h['gaps']} caps more than "
            f"{quotes.CAP_GAP_PCT}% apart")
    if h["gap_examples"]:
        note += f" ({', '.join(h['gap_examples'])})"
    if h["rate_limit_stop"] is not None:
        note += f"; last Yahoo rate-limit stop {_fmt_date(h['rate_limit_stop'])}"
    default["note"] = note
    return default


_HOLDINGS_PROVIDERS = (("uw", "UW"), ("fmp", "FMP"), ("yfinance", "yfinance"))


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
             f"RS stored {h['rs_stored']} of {h['themes']} theme ETFs"]
    if h["missing"]:
        named = ", ".join(h["missing"][:8])
        parts.append(f"no holdings: {named}" + (f" and {len(h['missing']) - 8} more" if len(h["missing"]) > 8 else ""))
    row["note"] = "; ".join(parts)
    return row


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


def _exists_row(table: str, ticker: str) -> bool:
    """Whether ``table`` has a row for ``ticker`` (cross-file safe, no network)."""
    if not _table_exists(table):
        return False
    try:
        rows = rawstore.query(
            f"SELECT 1 FROM {table} WHERE ticker = ? LIMIT 1",
            [ticker], db=_db_of(table))
        return bool(rows)
    except Exception as exc:  # noqa: BLE001
        _record(table, "_exists_row", exc)
        return False


def _swing_presence(ticker: str) -> dict:
    """Bar count + newest ``ts`` for ``ticker`` in ``swing_ohlc``."""
    empty = {"bars": 0, "newest": None}
    if not _table_exists("swing_ohlc"):
        return empty
    try:
        rows = rawstore.query(
            "SELECT COUNT(*) AS n, MAX(ts) AS newest FROM swing_ohlc WHERE ticker = ?",
            [ticker], db=_db_of("swing_ohlc"))
    except Exception as exc:  # noqa: BLE001
        _record("swing_ohlc", "_swing_presence", exc)
        return empty
    row = rows[0] if rows else {}
    return {"bars": int(row.get("n") or 0), "newest": row.get("newest")}


def _universe_tickers() -> list[dict]:
    """``[{ticker, name}]`` for the ticker combobox — one store read, no network."""
    try:
        rows = rawstore.query("SELECT ticker, name FROM equity_universe ORDER BY ticker")
    except Exception as exc:  # noqa: BLE001 - recorded (F4); empty picker
        _record("equity_universe", "_universe_tickers", exc)
        return []
    return [{"ticker": r["ticker"], "name": r.get("name") or r["ticker"]}
            for r in rows if r.get("ticker")]


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
    """Per-ticker drill: identity, cap, sector and industry, SIC, latest filing and dataset
    presence — read straight from ``.store`` with zero network.

    Unknown ticker (absent from every dataset) returns ``None`` so the route can
    answer 404. A NULL ``market_cap`` stays ``None`` — never coerced.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return None

    universe = rawstore.get("equity_universe", t) if _table_exists("equity_universe") else None
    sec = (_sec_file_row(t) if _table_exists("sec_exchange_file") else None)
    sic = rawstore.get("sec_company_sic", t) if _table_exists("sec_company_sic") else None
    filing = rawstore.get("chain_filings", t) if _table_exists("chain_filings") else None
    swing = _swing_presence(t)

    if not any(x is not None for x in (universe, sec, sic, filing)) \
            and not _exists_row("edgar_financials", t) \
            and swing["bars"] == 0 \
            and not _exists_row("oipd_meta", t) \
            and not _exists_row("relatedness_profiles", t):
        return None

    cik = None
    for src in (sec, sic, filing):
        if src and src.get("cik"):
            cik = src["cik"]
            break
    cap = universe.get("market_cap") if universe else None
    return {
        "ticker": t,
        "name": (universe or {}).get("name") or (sec or {}).get("name") or t,
        "market_cap": cap,
        "price": (universe or {}).get("price"),
        "sector": _read_sector((universe or {}).get("sector")),
        "industry": (universe or {}).get("industry"),
        "cik": cik,
        "sic": (sic or {}).get("sic"),
        "sic_description": (sic or {}).get("sic_description"),
        "filing": ({"form": filing.get("form"), "filed": filing.get("filed"),
                    "accession": filing.get("accession"),
                    "quality": filing.get("quality")} if filing else None),
        "presence": {
            "edgar_financials": _exists_row("edgar_financials", t),
            "swing_ohlc": swing,
            "oipd_meta": _exists_row("oipd_meta", t),
            "relatedness_profiles": _exists_row("relatedness_profiles", t),
            # One universe definition: the very list the boards read (cap floor and
            # security type), not a second copy of the rule.
            "in_classification_universe": t in set(classification_universe()),
        },
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


def _discord_readiness() -> dict | None:
    """The Discord export readiness card's data (#242): environment and the CLI file only, no store, no network.

    The version probe starts the exe at most once per exe file state (``exporter.cli_version``). A failed check is
    recorded (F4) and the card is left out: it must never look like "ready" or be dropped without a trace.
    """
    try:
        return discord_readiness.collect()
    except Exception as exc:  # noqa: BLE001 - recorded (F4), the card is omitted
        _record("discord", "_discord_readiness", exc)
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
    """The Backups card (#309, avo 2026-10-16): the backup job's own records only, a missing one is "unknown".

    Reads ``<backup dir>/.backup_sizes.json`` (both folder sizes, written by the nightly run after the off-machine
    copy) and ``.integrity_scan.json`` (the last read-only self-check of the local folder). No folder is walked and
    the off-machine (Drive) folder is never touched: walking it took ~20 min (2026-10-16). A failure to read is
    recorded (F4) and the card shows "unknown"; it never fails the page."""
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
    return {"folder": facts.get("folder"), "recorded_at": sizes.get("recorded_at") or None,
            "local": local, "offmachine": off, "checkpoints": checkpoints, "scan": scan_out}


def collect(now: datetime | None = None) -> dict:
    """The Settings payload. ``now`` is the clock for the staleness windows (tests pin it)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    _begin_call()
    try:
        return _collect(now)
    finally:
        _end_call()


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
        "holes": None, "hole_label": "universe CIKs not refreshed in the last run (failed or new)",
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
        parts.append(f"last run {_fmt_date(h['last_run_at'])}: {refreshed:,} of {len(universe):,} universe CIKs "
                     f"refreshed")
    else:
        parts.append(f"last run {_fmt_date(h['last_run_at'])}: {h['last_run_ciks']:,} CIKs refreshed")
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
            "label": "Daily OHLC",
            "tables": ["swing_ohlc"],
            "rows": _count("swing_ohlc"),
            "last_date": _max_date(("swing_ohlc", "ts")),
            "holes": _missing(tickers, "swing_ohlc"),
            "hole_label": "no bars",
            "fetch": None,
            "note": "",
        },
        _price_bars_row(now),
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
            "label": "OIPD chain",
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
            "note": "history.duckdb (#287): one header row per recorded board run",
        },
        _inputs_row(),
    ]
    tickers_list = _universe_tickers()
    board_cards = boards.board_cards()
    return {
        "rows": rows,
        "tickers": tickers_list,
        "boards": board_cards,
        "identity": identity,
        # Fixed registry fields only: the live state is polled from /api/automations.
        "automations": automations.static_rows(),
        "discord": _discord_readiness(),
        "backups": _backups(now),
        "errors": list(getattr(_CALL, "errors", None) or []),
        "generated_at": now.isoformat(timespec="seconds"),
    }

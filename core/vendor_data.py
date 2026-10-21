"""Vendor statements and valuation (#297, D12): the one writer of ``vendor_financials`` and ``vendor_valuation``.

The jobs that already fetch these answers store them here as they go; no extra request is made:

- ``universe`` / Asymmetric Growth: the yfinance payload of each AG name it fetches (statements only for a name
  without EDGAR years, #150; ``.info`` always), ``module_asymmetric_growth/engine.py``;
- ``earnings``: UW ``/stock/{t}/financials`` of each scored ticker, ``module_earnings_radar/pipeline.py``;
- ``quick`` (Financials): the ticker's yfinance payload and the ``.info`` of its comps peers, ``module_financials/run.py``.

A worker hands its answer to a :class:`Capture`; the job thread flushes it through :func:`record` with the job's
``run_id``. A Refresh fetches nothing, so it writes nothing (#313).

**Statements** (``vendor_financials``, change history, rule A of ``phase2/06_interface.md`` §2): one row per
(ticker, source, statement, report_type, period_end) and run. A row is appended only when the typed fields or the
payload differ from the newest stored row of that key; ``valid_from`` is the run's session (rule B; an intraday run
may append a change), ``status`` is 'present'. UW's ``inserted_at``/``updated_at`` are not a change. A period that
leaves the vendor's window (UW 8 quarters, Yahoo about 4 years) is not closed with an 'absent' row: the vendor no
longer serving an old period says nothing about its value (``06_interface/ddl_292.md`` §5).

**Valuation** (``vendor_valuation``, session snapshot, rule B): one row per (ticker, source, session_date). Only a
complete-session run writes it (an intraday ``.info`` carries intraday prices), and the first write of a session
wins. The full ``.info`` is the zlib-compressed ``payload`` (D-1 A).

Which source a reader takes first per field is the readers' rule (``06_interface.md`` §6, "D12 order per field"):
this module stores every source as served and changes no board.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date, datetime

from RevenueChaser.core import calendar, ledger, ownership, rawstore

logger = logging.getLogger(__name__)

STATEMENTS = "vendor_financials"
VALUATION = "vendor_valuation"
PRESENT = "present"

# Typed columns of vendor_financials, by statement. A row carries only its own statement's fields; the others
# are NULL (ddl_292.md §5).
FIELDS = {
    "income": ("revenue", "gross_profit", "operating_income", "ebitda", "net_income", "interest_expense"),
    "balance": ("total_assets", "inventory", "receivables", "deferred_revenue", "rpo", "total_debt"),
    "cash_flow": ("operating_cash_flow",),
}
TYPED = tuple(f for fs in FIELDS.values() for f in fs)

# yfinance: column -> the canonical key of ``core/providers/yfinance.get_financials``. ``operating_income`` is
# yfinance's ``ebit`` (labels "EBIT", then "Operating Income"), the field G2-29 measured as "ebit".
_YF_COLUMNS = {
    "income": {"revenue": "revenue", "gross_profit": "gross_profit", "operating_income": "ebit",
               "ebitda": "ebitda", "net_income": "net_income", "interest_expense": "interest_expense"},
    "balance": {"total_assets": "total_assets", "inventory": "inventory", "receivables": "receivables",
                "total_debt": "total_debt"},
    "cash_flow": {"operating_cash_flow": "operating_cash_flow"},
}
# The payload of a yfinance row: every canonical key of that statement (``yfinance._INCOME_FIELDS`` etc.).
_YF_PAYLOAD = {
    "income": ("revenue", "cost_of_revenue", "gross_profit", "ebit", "ebitda", "pretax_income", "tax_provision",
               "net_income", "interest_expense", "diluted_shares", "basic_shares", "rd_expense"),
    "balance": ("total_assets", "total_equity", "cash", "receivables", "inventory", "payables", "current_assets",
                "current_liabilities", "total_debt"),
    "cash_flow": ("operating_cash_flow", "capital_expenditure", "depreciation"),
}

# UW: statement -> (list in the /financials body, column -> served keys in order). The fallbacks are the ones
# ``UWClient._map_financials`` reads; ``operating_cashflow`` is the field docs/openapi.yaml and G2-10 name.
# UW has a debt total (``short_long_term_debt_total``) that no code or study names, so ``total_debt`` stays NULL.
_UW = {
    "income": ("income_statements", {
        "revenue": ("total_revenue",), "gross_profit": ("gross_profit",), "operating_income": ("operating_income",),
        "ebitda": ("ebitda",), "net_income": ("net_income",), "interest_expense": ("interest_expense",)}),
    "balance": ("balance_sheets", {
        "total_assets": ("total_assets",), "inventory": ("inventory",),
        "receivables": ("current_net_receivables", "net_receivables"),
        "deferred_revenue": ("deferred_revenue", "customer_deposits"),
        "rpo": ("rpo", "remaining_performance_obligation")}),
    "cash_flow": ("cash_flows", {"operating_cash_flow": ("operating_cashflow", "operating_cash_flow")}),
}
# Served bookkeeping, not values: a change here alone appends nothing.
_VOLATILE = frozenset({"inserted_at", "updated_at"})

# vendor_valuation column -> ``.info`` key (``module_financials/engines/comps_engine.py:56-61``,
# ``core/providers/yfinance.py:229-231``, the boards' ``longBusinessSummary``).
_INFO_COLUMNS = {
    "enterprise_value": "enterpriseValue", "ebitda": "ebitda", "total_revenue": "totalRevenue",
    "net_income_to_common": "netIncomeToCommon", "trailing_pe": "trailingPE", "beta": "beta",
}

EMPTY_RESULT = {"statements_appended": 0, "statements_unchanged": 0, "valuations_written": 0,
                "valuations_kept": 0, "valuations_skipped": 0, "valuation_skip_reason": None}


# ── small helpers ────────────────────────────────────────────────────────────

def _clock() -> float:
    """Epoch seconds; the one clock seam of this module (tests pin it)."""
    return time.time()


def _num(v) -> float | None:
    """A finite float, else None (never 0; a NaN or inf would make the JSON payload invalid)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean(v):
    """``v`` with every non-finite float replaced by None, as JSON would read it back."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v.strip()) >= 10:
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _first(row: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = _num(row.get(k))
        if v is not None:
            return v
    return None


def _row(ticker: str, source: str, statement: str, report_type: str, period_end: date, values: dict,
         payload: dict) -> dict:
    out = {"ticker": ticker, "source": source, "statement": statement, "report_type": report_type,
           "period_end": period_end, "payload": _clean(payload)}
    out.update({f: None for f in TYPED})
    out.update(values)
    return out


# ── the mappers (pure) ───────────────────────────────────────────────────────

def statement_rows_yfinance(ticker: str, payload: dict | None) -> list[dict]:
    """Statement rows of a ``yfinance.get_financials`` payload: annual, one row per statement and fiscal year.

    A record without ``period_end`` (a payload stored before #297) and a statement with no value that year are
    skipped."""
    annual = (payload or {}).get("annual") if isinstance(payload, dict) else None
    if not isinstance(annual, list):
        return []
    ticker = ticker.upper()
    rows = []
    for rec in annual:
        if not isinstance(rec, dict):
            continue
        period_end = _date(rec.get("period_end"))
        if period_end is None:
            continue
        for statement, columns in _YF_COLUMNS.items():
            body = {k: _num(rec.get(k)) for k in _YF_PAYLOAD[statement]}
            if all(v is None for v in body.values()):
                continue
            values = {col: body[key] for col, key in columns.items()}
            rows.append(_row(ticker, "yfinance", statement, "annual", period_end, values,
                             {"fiscal_year": rec.get("fiscal_year"), **body}))
    return rows


def statement_rows_uw(ticker: str, fin: dict | None) -> list[dict]:
    """Statement rows of a UW ``/stock/{t}/financials`` body, every row as served (annual and quarterly).

    The payload is the served row; a row without ``fiscal_date_ending`` is skipped, one without ``report_type`` is
    stored as 'unknown'."""
    if not isinstance(fin, dict):
        return []
    ticker = ticker.upper()
    rows = []
    for statement, (body_key, columns) in _UW.items():
        served = fin.get(body_key)
        if not isinstance(served, list):
            continue
        for rec in served:
            if not isinstance(rec, dict):
                continue
            period_end = _date(rec.get("fiscal_date_ending"))
            if period_end is None:
                continue
            report_type = str(rec.get("report_type") or "").strip().lower() or "unknown"
            values = {col: _first(rec, keys) for col, keys in columns.items()}
            rows.append(_row(ticker, "uw", statement, report_type, period_end, values, rec))
    return rows


def valuation_row_yfinance(ticker: str, info: dict | None) -> dict | None:
    """The ``vendor_valuation`` row of a yfinance ``.info`` dict (None when it is empty)."""
    if not isinstance(info, dict) or not info:
        return None
    row = {"ticker": ticker.upper(), "source": "yfinance", "payload": dict(info)}
    row.update({col: _num(info.get(key)) for col, key in _INFO_COLUMNS.items()})
    currency = info.get("currency")
    summary = info.get("longBusinessSummary")
    row["currency"] = str(currency) if currency else None
    row["long_business_summary"] = str(summary) if summary else None
    return row


# ── the writer ───────────────────────────────────────────────────────────────

def _key(r: dict) -> tuple:
    return (r["ticker"], r["source"], r["statement"], r["report_type"], r["period_end"])


def _comparable(r: dict) -> tuple:
    payload = r.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k not in _VOLATILE}
    # One JSON form for both sides: what the store read back and what a fresh fetch built.
    text = json.dumps(_clean(payload), sort_keys=True, default=str)
    return tuple(_num(r.get(f)) for f in TYPED) + (text,)


def _placeholders(n: int) -> str:
    return ", ".join(["?"] * n)


def _latest_rows(tx, tickers: list[str]) -> dict[tuple, tuple]:
    """``{key: comparable}`` of the newest stored row of every key of ``tickers``."""
    if not tickers:
        return {}
    cur = tx.execute(
        f"SELECT ticker, source, statement, report_type, period_end, {', '.join(TYPED)}, payload "
        f"FROM {STATEMENTS} WHERE ticker IN ({_placeholders(len(tickers))}) "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, source, statement, report_type, period_end "
        f"ORDER BY run_at DESC) = 1", tickers)
    cols = [d[0] for d in cur.description]
    out = {}
    for values in cur.fetchall():
        r = dict(zip(cols, values))
        out[_key(r)] = _comparable(r)
    return out


def _valuation_session(run_at: float) -> tuple[date | None, str | None]:
    """(session_date, None) when a valuation may be written at ``run_at``, else (None, why not)."""
    try:
        stamp = calendar.session_for(run_at)
    except ValueError:
        return None, "no session"
    if stamp.quality != calendar.QUALITY_COMPLETE:
        return None, "intraday"
    return stamp.session_date, None


def record(statements=(), valuations=(), *, run_id: str, run_at: float | None = None,
           fetched_at: float | None = None) -> dict:
    """Store statement rows (append on change) and valuation rows (first write per complete session), all or
    nothing in one transaction. Rows come from the mappers above; a row's own ``fetched_at`` wins over the
    argument. Returns the counts (``EMPTY_RESULT`` keys). Raises on a store error."""
    run_at = _clock() if run_at is None else float(run_at)
    fetched_at = run_at if fetched_at is None else float(fetched_at)
    statements = [r for r in statements if r]
    valuations = [r for r in valuations if r]
    res = dict(EMPTY_RESULT)
    if not statements and not valuations:
        return res
    try:
        valid_from = calendar.session_for(run_at).session_date
    except ValueError:
        valid_from = None                      # outside the holiday table: the row keeps run_at only
    session, why = _valuation_session(run_at)
    stamps = {"run_id": run_id, "run_at": run_at}

    def _write(tx):
        out = dict(EMPTY_RESULT)
        tx.ensure_table(STATEMENTS)
        tx.ensure_table(VALUATION)
        latest = _latest_rows(tx, sorted({r["ticker"] for r in statements}))
        new_rows, seen = [], set()
        for r in statements:
            k = _key(r)
            if k in seen:
                continue                       # the key twice in one batch: the first answer wins, on every run
            seen.add(k)
            if latest.get(k) == _comparable(r):
                out["statements_unchanged"] += 1
                continue
            new_rows.append({**r, "valid_from": valid_from, "status": PRESENT,
                             "fetched_at": r.get("fetched_at") or fetched_at, **stamps})
        tx.write_rows(STATEMENTS, new_rows)
        out["statements_appended"] = len(new_rows)
        if valuations and session is None:
            out["valuations_skipped"], out["valuation_skip_reason"] = len(valuations), why
        elif valuations:
            by_key = {}
            for v in valuations:
                by_key.setdefault((v["ticker"], v["source"]), v)
            tickers = sorted({t for t, _ in by_key})
            taken = {(t, s) for t, s in tx.execute(
                f"SELECT ticker, source FROM {VALUATION} WHERE session_date = ? "
                f"AND ticker IN ({_placeholders(len(tickers))})", [session, *tickers]).fetchall()}
            fresh = [{**v, "session_date": session, "fetched_at": v.get("fetched_at") or fetched_at, **stamps}
                     for k, v in by_key.items() if k not in taken]
            tx.write_rows(VALUATION, fresh)
            out["valuations_written"] = len(fresh)
            out["valuations_kept"] = len(by_key) - len(fresh)
        return out

    res.update(rawstore.atomic(_write))
    return res


# ── the capture a job fills ──────────────────────────────────────────────────

class Capture:
    """What a job's workers fetched, collected thread-safely and stored by :meth:`flush` on the job thread (once
    per run, or once per batch: AG flushes before each preload batch and at the end).

    ``run_at`` is the run's time (default: when the capture is made), the same for every row of the run.
    ``ownership=True`` (the universe / AG Fetch only, #300 D20) also stores the ownership fields of each fresh
    yfinance ``.info`` in ``ownership_snapshot`` (``core/ownership.py``)."""

    def __init__(self, run_at: float | None = None, ownership: bool = False):
        self.run_at = _clock() if run_at is None else float(run_at)
        self._noted = False                    # a store that keeps failing gives one job note per run
        self._owner_noted = False
        self._lock = threading.Lock()
        self._statements: list[dict] = []
        self._valuations: list[dict] = []
        self._ownership = [] if ownership else None

    def _add(self, statements: list[dict], valuation: dict | None, owner: dict | None = None) -> None:
        now = _clock()
        with self._lock:
            self._statements.extend({**r, "fetched_at": now} for r in statements)
            if valuation:
                self._valuations.append({**valuation, "fetched_at": now})
            if owner and self._ownership is not None:
                self._ownership.append({**owner, "fetched_at": now})

    def add_yfinance(self, ticker: str, payload: dict | None) -> None:
        """A fresh ``yfinance.get_financials`` or ``get_info`` payload. A ``_stale`` one (an old payload handed
        back by ``cache.get_or_fetch``) was not fetched now and is ignored."""
        if not isinstance(payload, dict) or payload.get("_stale"):
            return
        info = payload.get("info")
        self._add(statement_rows_yfinance(ticker, payload), valuation_row_yfinance(ticker, info),
                  ownership.row_yfinance(ticker, info))

    def add_uw(self, ticker: str, fin: dict | None) -> None:
        """A UW ``/stock/{t}/financials`` body."""
        self._add(statement_rows_uw(ticker, fin), None)

    def add_valuation(self, ticker: str, info: dict | None) -> None:
        """A bare yfinance ``.info`` (a comps peer)."""
        self._add([], valuation_row_yfinance(ticker, info))

    def flush(self, *, run_id: str, label: str = "Vendor") -> dict:
        """Store and clear what was collected. A store failure is a job note and an ``error`` key, never an
        exception: the board the job builds does not depend on these rows."""
        with self._lock:
            statements, self._statements = self._statements, []
            valuations, self._valuations = self._valuations, []
            owners = None
            if self._ownership is not None:
                owners, self._ownership = self._ownership, []
        res = dict(EMPTY_RESULT)
        if statements or valuations:
            try:
                res = record(statements, valuations, run_id=run_id, run_at=self.run_at)
            except Exception as exc:  # noqa: BLE001 - see docstring
                logger.exception("%s vendor statements and valuation were not stored", label)
                text = f"{type(exc).__name__}: {exc}"
                if not self._noted:
                    self._noted = True
                    ledger.add_note(f"{label} vendor statements were not stored: {text}")
                res = {**EMPTY_RESULT, "error": text}
        if owners:
            res.update(self._flush_ownership(owners, run_id, label))
        return res

    def _flush_ownership(self, owners: list[dict], run_id: str, label: str) -> dict:
        """D20 (#300): its own transaction, so a failure here never loses the D12 rows; one note per run."""
        try:
            done = ownership.record(owners, run_id=run_id, run_at=self.run_at)
            return {"ownership_written": done["written"], "ownership_kept": done["kept"]}
        except Exception as exc:  # noqa: BLE001 - see flush
            logger.exception("%s ownership snapshot was not stored", label)
            text = f"{type(exc).__name__}: {exc}"
            if not self._owner_noted:
                self._owner_noted = True
                ledger.add_note(f"{label} ownership snapshot was not stored: {text}")
            return {"ownership_written": 0, "ownership_kept": 0, "ownership_error": text}


# ── Data Health ──────────────────────────────────────────────────────────────

def health_summary(now: datetime | None = None) -> dict:
    """Read-only summary for the Data Health row, zero network.

    ``valuation_tickers``: tickers with a valuation row on the newest stored session. ``last_change_*``: the
    newest run that appended statement rows (a run that changed nothing leaves no row): its id, row count and
    session. ``fill``: % of the newest period of each (ticker, source, statement)
    whose field is filled, per source and field (the G2-10/G2-29 measure: the newest record)."""
    out: dict = {"valuation_session": None, "valuation_tickers": 0, "latest_complete": None,
                 "statement_rows": 0, "last_change_run_id": None, "last_change_appended": 0,
                 "last_change_session": None, "fill": {},
                 "newest_fetched_at": None}
    try:
        out["latest_complete"] = calendar.latest_complete_session(now).isoformat()
    except ValueError:
        pass
    v = rawstore.query(f"SELECT session_date AS s, COUNT(DISTINCT ticker) AS n FROM {VALUATION} "
                       f"GROUP BY session_date ORDER BY session_date DESC LIMIT 1")
    if v:
        out["valuation_session"] = v[0]["s"].isoformat()
        out["valuation_tickers"] = int(v[0]["n"])
    n = rawstore.query(f"SELECT COUNT(*) AS n, MAX(fetched_at) AS f FROM {STATEMENTS}")
    out["statement_rows"] = int(n[0]["n"]) if n else 0
    out["newest_fetched_at"] = n[0]["f"] if n else None
    last = rawstore.query(f"SELECT run_id, COUNT(*) AS n, MAX(valid_from) AS s FROM {STATEMENTS} WHERE run_at = "
                          f"(SELECT MAX(run_at) FROM {STATEMENTS}) GROUP BY run_id")
    if last:
        out["last_change_run_id"] = last[0]["run_id"]
        out["last_change_appended"] = int(last[0]["n"])
        out["last_change_session"] = last[0]["s"].isoformat() if last[0]["s"] else None
    fill: dict = {}
    for statement, fields in FIELDS.items():
        filled = ", ".join(f"AVG(CASE WHEN {f} IS NULL THEN 0 ELSE 100 END) AS {f}" for f in fields)
        rows = rawstore.query(
            f"SELECT source, {filled} FROM (SELECT * FROM {STATEMENTS} WHERE statement = ? "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, source, statement "
            f"ORDER BY period_end DESC, run_at DESC) = 1) GROUP BY source", [statement])
        for r in rows:
            fill.setdefault(r["source"], {}).update(
                {f: round(float(r[f]), 1) for f in fields if r[f] is not None})
    out["fill"] = fill
    return out

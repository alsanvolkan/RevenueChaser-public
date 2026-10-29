"""``uw/financials``: UW's numbers of a quarter a company has reported but not yet filed, as ``preliminary`` facts.

**Off unless ``RC_UW_PRELIMINARY=1``.** Whether the layer serves UW's numbers ahead of the SEC is an open decision of
the plan; off, a run asks nothing and the tables build serves none of the stored rows. Not verified against UW
whether ``/financials`` holds a quarter before the SEC does (UW's fundamentals are Alpha Vantage's).

A company is asked when it has reported and not filed: a report date (``earnings_event``, the calendar or an 8-K
item 2.02) in the last ``WINDOW_DAYS`` days, after the newest SEC statement filing, of a company that files
quarterly statements at the SEC. ``GET <RC_UW_BASE>/stock/<ticker>/financials``, one call per company and day, up
to ``MAX_PER_RUN``, against the ``uw`` cap; the company's shortest ticker (HEI, not HEI-A). No
``UNUSUAL_WHALES_API_KEY``: nothing is asked, a ``missing_key`` issue.

What is kept of the answer: the **quarterly** rows of the three statements (``report_type`` read on each row before
any date is used, the Earnings Radar review's S3: an annual and a fourth-quarter row share a date), in USD, the
fields of ``FIELD_MAP``. No ``total_debt``: UW's ``short_term_debt`` may hold the current portion of long-term debt
already, so total debt is the SEC's only; no EPS (UW's reported EPS may be an adjusted one).

**Served only until the SEC has the quarter.** ``fundamentals/tables`` serves a preliminary row only for a period
ending more than ``MARGIN_DAYS`` after the company's newest SEC quarter (a margin for the 52/53-week years, whose SEC
period end is a few days off the month end UW gives); the 10-Q's facts replace it in the next build. Accession and
filed date are NULL (the table's check allows that only on a preliminary row).

Raw: ``raw/uw/financials/<ticker>/<as-of>.json.gz`` (request, CIK, fetch time, run, body; the resume marker).
Parquet: ``parquet/uw/financials/cik=<cik>/<as-of>.parquet``, a snapshot per read, all kept. Staging:
``stg_uw_financials``, the newest snapshot's value per company, field and quarter.
"""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from .. import base
from . import KIND, common, facts

NAME = "uw/financials"
PROVIDER = "uw"
KEY = "unusual_whales"
FLAG = "RC_UW_PRELIMINARY"
WINDOW_DAYS = 14
MARGIN_DAYS = 30
MAX_PER_RUN = 300
STAGING = "stg_uw_financials"
# Served field <- the UW statement row's key (docs/openapi.yaml: Income Statement, Balance Sheet, Cash Flow Statement).
FIELD_MAP: dict[str, dict[str, str]] = {
    "income_statements": {"revenue": "total_revenue", "cost_of_revenue": "cost_of_revenue",
                          "gross_profit": "gross_profit", "ebit": "operating_income", "net_income": "net_income",
                          "rd_expense": "research_and_development"},
    "balance_sheets": {"total_assets": "total_assets", "total_equity": "total_shareholder_equity",
                       "cash": "cash_and_cash_equivalents", "short_term_investments": "short_term_investments",
                       "lt_debt": "long_term_debt_noncurrent", "lt_debt_current": "current_long_term_debt",
                       "receivables": "current_net_receivables", "inventory": "inventory",
                       "payables": "current_accounts_payable"},
    "cash_flows": {"operating_cash_flow": "operating_cashflow", "capex": "capital_expenditures",
                   "share_based_compensation": "stock_based_compensation",
                   "depreciation": "depreciation_depletion_and_amortization"},
}


def enabled() -> bool:
    return os.getenv(FLAG, "").strip() == "1"


def endpoint(ticker: str) -> str:
    from ....core.providers.unusual_whales import _seg
    return f"/stock/{_seg(ticker)}/financials"


def raw_path(ticker: str, as_of: date) -> Path:
    return common.raw_dir(NAME) / ticker / f"{as_of.isoformat()}.json.gz"


def _num(v) -> float | None:
    if v is None or isinstance(v, bool) or str(v).strip() in ("", "None", "-"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def rows_of(body, cik: str) -> tuple[list[dict], dict[str, int]]:
    """The fact rows of one ``/financials`` answer (no stamps); ``counts`` of the rows left out and why."""
    data = body.get("data", body) if isinstance(body, dict) else {}
    out: dict[tuple[str, date], dict] = {}
    counts: dict[str, int] = {}
    for statement, fields in FIELD_MAP.items():
        for row in (data.get(statement) if isinstance(data, dict) else None) or []:
            if not isinstance(row, dict):
                continue
            if row.get("report_type") != facts.QUARTERLY:
                counts["not_quarterly"] = counts.get("not_quarterly", 0) + 1
                continue
            if (row.get("reported_currency") or "USD").upper() != "USD":
                counts["not_usd"] = counts.get("not_usd", 0) + 1
                continue
            end = _day(row.get("fiscal_date_ending"))
            if end is None:
                continue
            for field, key in fields.items():
                value = _num(row.get(key))
                if value is not None:
                    out[(field, end)] = {
                        "cik": cik, "field": field, "period_type": facts.QUARTERLY, "fiscal_year": None,
                        "period_start": None, "period_end": end, "value": value, "unit": "USD", "accn": None,
                        "filed": None, "form": None, "derived": False, "preliminary": True, "source": NAME}
    return list(out.values()), counts


def candidates(as_of: date) -> list[tuple[str, str]]:
    """``(cik, ticker)`` of the companies that reported in the window and have not filed since, from the live
    ``fundamentals.duckdb``; [] before its first build."""
    from ... import serving
    try:
        con = serving.open_read(KIND)
    except serving.ServingFileMissing:
        return []
    try:
        rows = con.execute(f"""
            WITH sec AS (SELECT cik, max(filed) AS newest_filed FROM fundamental_fact
                         WHERE NOT preliminary GROUP BY cik HAVING bool_or(period_type = '{facts.QUARTERLY}')),
                 reported AS (SELECT cik, max(report_date) AS report_date FROM earnings_event
                              WHERE report_date BETWEEN ?::DATE - {WINDOW_DAYS} AND ?::DATE GROUP BY cik)
            SELECT r.cik, list(c.ticker ORDER BY length(c.ticker), c.ticker)[1]
            FROM reported r JOIN sec s ON s.cik = r.cik JOIN fundamental_company c ON c.cik = r.cik
            WHERE r.report_date > s.newest_filed
            GROUP BY r.cik ORDER BY r.cik""", [as_of, as_of]).fetchall()
    finally:
        con.close()
    return [(str(cik), str(ticker)) for cik, ticker in rows]


class UWFinancials:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        if not enabled():
            ctx.notes.append(f"off ({FLAG} is not 1: serving UW's preliminary numbers is an open decision); "
                             "nothing asked, none served")
            return []
        due = candidates(as_of)
        if not due:
            ctx.notes.append("no company reported in the window without a filing since")
            return []
        if not credentials.key_for(KEY):
            ctx.issue("missing_key", KEY, credentials.missing_message(KEY))
            ctx.notes.append("no UNUSUAL_WHALES_API_KEY: nothing asked")
            return []
        out, failed = [], []
        for cik, ticker in due[:MAX_PER_RUN]:
            raw = raw_path(ticker, as_of)
            if raw.exists():
                out.append(raw)
                continue
            ctx.check_cap(PROVIDER)
            res = ctx.uw_client().get_result(endpoint(ticker))
            if res.status != "ok":
                failed.append(ticker)
                ctx.issue("fetch_failed", ticker, f"UW {endpoint(ticker)}: {res.error}")
                continue
            out.append(base.write_raw_json(raw, {
                "request": {"endpoint": endpoint(ticker)}, "ticker": ticker, "cik": cik, "as_of": as_of.isoformat(),
                "fetched_at": time.time(), "run_id": ctx.run_id, "body": res.data}))
        if failed and not out:
            raise base.SourceError(f"UW /financials: every request failed ({len(failed)}, first {failed[0]})")
        ctx.notes.append(f"{len(out)} reported, not yet filed, companies read"
                         + (f", {len(failed)} failed" if failed else "")
                         + (f", {len(due) - MAX_PER_RUN} left for the next run" if len(due) > MAX_PER_RUN else ""))
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # UW's answer holds every quarter; only the ones after the SEC's newest are served, so a backfill is the
        # daily rule at its end date.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out, left = [], {}
        for raw in raws:
            doc = base.read_raw_json(raw)
            cik, as_of = common.norm_cik(doc["cik"]), date.fromisoformat(doc["as_of"])
            rows, counts = rows_of(doc.get("body"), cik)
            for k, n in counts.items():
                left[k] = left.get(k, 0) + n
            stamps = {"source_as_of": as_of, "fetched_at": float(doc["fetched_at"]), "run_id": doc["run_id"]}
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", facts.FACT, [{**r, **stamps} for r in rows])
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY field, period_end",
                                              common.parquet_dir(NAME) / f"cik={cik}" / f"{as_of.isoformat()}.parquet"))
            finally:
                con.close()
        if left:
            ctx.notes.append("rows left out: " + ", ".join(f"{k} {n}" for k, n in sorted(left.items())))
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        common.stage_glob(con, STAGING, facts.FACT, common.parquet_dir(NAME), "cik=*/*.parquet",
                          select="SELECT * FROM {src} QUALIFY row_number() OVER (PARTITION BY cik, field, "
                                 "period_type, period_end ORDER BY source_as_of DESC, fetched_at DESC) = 1")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "field", "period_end", "value"]
        where = f"source = '{NAME}'"
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM fundamental_fact WHERE {where}",
                                             "fundamental_fact", cols, where=where)
        quarters, companies = con.execute(f"SELECT count(DISTINCT (cik, period_end)), count(DISTINCT cik) "
                                          f"FROM fundamental_fact WHERE {where}").fetchone()
        ctx.notes.append(f"{quarters} preliminary quarter(s) of {companies} companies served")
        as_of = con.execute(f"SELECT max(source_as_of) FROM {STAGING}").fetchone()[0]
        on = ctx.as_of or self.default_as_of()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or on).isoformat(), "rows": common.count(con, STAGING), "added": added,
                "removed": removed}


DATASET = UWFinancials()

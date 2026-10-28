"""``alphavantage/earnings_calendar``: the coming report dates of the next three months, from Alpha Vantage.

``GET <RC_ALPHAVANTAGE_BASE>/query?function=EARNINGS_CALENDAR&horizon=3month&apikey=...`` answers CSV, one row per
expected report: ``symbol, name, reportDate, fiscalDateEnding, estimate, currency`` (and ``timeOfTheDay`` where Alpha
Vantage sends it). **Not verified against the real source** (the column names follow its documentation; read by
header name, an unknown column ignored, a missing required one a failed run). The free tier allows 25 requests a day
(``caps.DAY_CAPS``); this is one request a run. Its rate-limit and error answers are JSON (``Note``, ``Information``,
``Error Message``) with HTTP 200: a failed run that names the key, never the body (it could echo the request). No
``ALPHAVANTAGE_API_KEY``: nothing is asked, a ``missing_key`` build issue. The key travels in the query string,
which is never logged or stored.

The other report-date source is the SEC: an 8-K with item 2.02 ("Results of Operations and Financial Condition")
in a company's filing list (``sec/filings``) is a report that happened, served as a confirmed event. UW's earnings
calendar is the options data group's dataset; ``earnings_event`` will read it once that lands.

Raw: ``raw/alphavantage/earnings_calendar/<as-of>.csv.gz`` with a ``.meta.json`` (URL without its query). Parquet:
``parquet/alphavantage/earnings_calendar/date=<as-of>/part-0.parquet``, one snapshot per run, every one kept (a report
date in the past leaves the calendar; its last snapshot keeps it). Staging: ``stg_av_calendar``, the newest snapshot's
row per symbol and fiscal period.
"""
from __future__ import annotations

import csv
import io
import os
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from ....core.identity.sources import norm_ticker
from .. import base
from . import KIND, common

NAME = "alphavantage/earnings_calendar"
PROVIDER = "alphavantage"
KEY = "alphavantage"
HORIZON = "3month"
STAGING = "stg_av_calendar"
REQUIRED = ("symbol", "reportDate", "fiscalDateEnding")
SCHEMA = [("symbol", "TEXT"), ("name", "TEXT"), ("report_date", "DATE"), ("fiscal_date_ending", "DATE"),
          ("estimate", "DOUBLE"), ("currency", "TEXT"), ("time_of_day", "TEXT"), ("source_as_of", "DATE"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


def base_url() -> str:
    return (os.getenv("RC_ALPHAVANTAGE_BASE") or "https://www.alphavantage.co").rstrip("/")


def raw_path(as_of: date) -> Path:
    return common.raw_dir(NAME) / f"{as_of.isoformat()}.csv.gz"


def _num(text) -> float | None:
    try:
        return float(text) if str(text or "").strip() not in ("", "None", "-") else None
    except ValueError:
        return None


def _day(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


def parse(text: str) -> list[dict]:
    """The calendar's rows; ``SourceError`` for a JSON answer (a rate limit or error) or a CSV without the
    required columns."""
    body = (text or "").lstrip()
    if body.startswith("{"):
        import json
        try:
            keys = sorted(json.loads(body))
        except ValueError:
            keys = ["?"]
        raise base.SourceError(f"alphavantage earnings calendar: a JSON answer, not the calendar ({', '.join(keys)})")
    reader = csv.DictReader(io.StringIO(body))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        # Named from our list, never echoed from the answer (it could hold the request).
        raise base.SourceError(f"alphavantage earnings calendar: missing columns {', '.join(missing)}")
    out = []
    for r in reader:
        symbol = norm_ticker(r.get("symbol"))
        report = _day(r.get("reportDate"))
        if not symbol or report is None:
            continue
        out.append({"symbol": symbol, "name": (r.get("name") or "").strip() or None, "report_date": report,
                    "fiscal_date_ending": _day(r.get("fiscalDateEnding")), "estimate": _num(r.get("estimate")),
                    "currency": (r.get("currency") or "").strip() or None,
                    "time_of_day": (r.get("timeOfTheDay") or "").strip() or None})
    return out


class EarningsCalendar:
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
        raw = raw_path(as_of)
        if raw.exists():
            return [raw]
        key = credentials.key_for(KEY)
        if not key:
            ctx.issue("missing_key", KEY, credentials.missing_message(KEY))
            ctx.notes.append("no ALPHAVANTAGE_API_KEY: nothing asked")
            return []
        url = f"{base_url()}/query"
        r = common.get(ctx, PROVIDER, url, params={"function": "EARNINGS_CALENDAR", "horizon": HORIZON, "apikey": key})
        rows = parse(r.text)                      # refuse a rate-limit answer before it is kept
        base.write_raw_json(common.meta_path(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                                    "run_id": ctx.run_id, "url": url, "rows": len(rows)})
        return [base.write_raw(raw, r.content)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # Alpha Vantage serves only the coming calendar: a backfill is one fetch.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(common.meta_path(raw))
            as_of = date.fromisoformat(meta["as_of"])
            stamps = {"source_as_of": as_of, "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
            rows = [{**r, **stamps} for r in parse(base.read_raw(raw).decode("utf-8", "replace"))]
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY report_date, symbol",
                                              common.parquet_dir(NAME) / f"date={as_of.isoformat()}" /
                                              "part-0.parquet"))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        common.stage_glob(con, STAGING, SCHEMA, common.parquet_dir(NAME), "date=*/part-0.parquet",
                          select="SELECT * FROM {src} QUALIFY row_number() OVER (PARTITION BY symbol, "
                                 "coalesce(fiscal_date_ending, report_date) ORDER BY source_as_of DESC) = 1")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "report_date", "fiscal_period_end", "eps_estimate"]
        where = f"source = '{NAME}'"
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM earnings_event WHERE {where}",
                                             "earnings_event", cols, where=where)
        as_of = con.execute(f"SELECT max(source_as_of) FROM {STAGING}").fetchone()[0]
        on = ctx.as_of or self.default_as_of()
        tier_a = con.execute("SELECT count(DISTINCT cik) FROM fundamental_company").fetchone()[0]
        dated = con.execute(f"SELECT count(DISTINCT cik) FROM earnings_event WHERE source = '{NAME}' "
                            f"AND report_date >= DATE '{on.isoformat()}'").fetchone()[0]
        ctx.notes.append(f"{dated} of {tier_a} Tier A companies have a coming report date")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or on).isoformat(), "rows": common.count(con, STAGING), "added": added,
                "removed": removed}


DATASET = EarningsCalendar()

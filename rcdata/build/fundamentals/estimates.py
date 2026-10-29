"""``yfinance/estimates``: analyst estimates per Tier A company, asked weekly (Earnings Radar's D14 fields, #323).

One request per company (``core/providers/yfinance.get_earnings_trend``: Yahoo's ``earningsTrend``, the four blocks
``earnings_estimate``, ``revenue_estimate``, ``eps_trend``, ``eps_revisions`` for the periods ``0q``, ``+1q``,
``0y``, ``+1y``), for its shortest ticker (HEI, not HEI-A), when its newest answer is ``WEEK_DAYS`` old or there is
none; the companies longest without an answer first. Mapped to columns by Earnings Radar's own D14 mapping
(``module_earnings_radar/inputs/estimates.py``: ``trend_rows``, read, not changed), so a value is the value the board
reads today; a field Yahoo does not give, or gives as NaN, stays NULL. Counted against the ``yfinance`` cap;
``FAIL_STREAK`` failures in a row stop the run (Yahoo throttling or down), and a run where every request failed is a
failed run. A company that failed is asked again by the next run.

Raw: ``raw/yfinance/estimates/<as-of>/<ticker>.json.gz`` (the blocks, NaN as null; the resume marker). Parquet:
``parquet/yfinance/estimates/date=<as-of>/part-0.parquet``, every answer of that day (rewritten from the day's raw
files, so a resumed day loses none), every day kept. Staging: ``stg_yf_estimates``, every snapshot.

Served: ``estimate_latest``, the newest snapshot per company, ticker and period; the view ``estimate_snapshots``
gives it the old ``raw.duckdb`` shape (``session_date`` the day it was asked).
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, common

NAME = "yfinance/estimates"
PROVIDER = "yfinance"
WEEK_DAYS = 7
FAIL_STREAK = 5
STAGING = "stg_yf_estimates"


def _d14():
    # Earnings Radar's one mapping of the four blocks to the ``estimate_snapshots`` columns.
    from ....module_earnings_radar.inputs import estimates as d14
    return d14


def value_columns() -> tuple[str, ...]:
    return tuple(_d14().VALUE_COLUMNS)


def schema() -> list[tuple[str, str]]:
    return ([("cik", "TEXT"), ("ticker", "TEXT"), ("period", "TEXT")] + [(c, "DOUBLE") for c in value_columns()]
            + [("payload", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")])


def day_dir(as_of: date | str) -> Path:
    return common.raw_dir(NAME) / str(as_of)


def _newest(ticker: str) -> date | None:
    root = common.raw_dir(NAME)
    days = sorted(d.name for d in root.iterdir() if (d / f"{ticker}.json.gz").exists()) if root.is_dir() else []
    return date.fromisoformat(days[-1]) if days else None


def companies() -> list[tuple[str, str]]:
    """``(cik, ticker)`` per Tier A company: its shortest ticker."""
    return [(cik, sorted(tickers, key=lambda t: (len(t), t))[0])
            for cik, tickers in common.tickers_by_cik(common.universe()).items()]


class Estimates:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "weekly"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        from ....core.providers import yfinance as yf_provider
        from ....module_earnings_radar.inputs._common import json_safe
        due = []
        for cik, ticker in companies():
            newest = _newest(ticker)
            if newest is None or (as_of - newest).days >= WEEK_DAYS:
                due.append((newest or date.min, ticker, cik))
        due.sort()
        out, failed, streak, not_asked = [], [], 0, 0
        for n, (_, ticker, cik) in enumerate(due):
            raw = day_dir(as_of) / f"{ticker}.json.gz"
            if streak >= FAIL_STREAK:
                not_asked = len(due) - n
                break
            ctx.check_cap(PROVIDER)
            ctx.count(PROVIDER, calls=1)
            res = yf_provider.get_earnings_trend(ticker)
            if res.status != "ok":
                ctx.count(PROVIDER, failures=1)
                failed.append(ticker)
                streak += 1
                ctx.issue("fetch_failed", ticker, f"yfinance earningsTrend: {(res.error or {}).get('error')}")
                continue
            streak = 0
            out.append(base.write_raw_json(raw, {"ticker": ticker, "cik": cik, "as_of": as_of.isoformat(),
                                                 "fetched_at": time.time(), "run_id": ctx.run_id,
                                                 "blocks": json_safe(res.data)}))
        # The day's earlier answers too (a resumed day): its Parquet is rewritten from all of them.
        out = sorted(set(out) | set(day_dir(as_of).glob("*.json.gz")))
        if failed and not out:
            raise base.SourceError(f"yfinance estimates: every request failed ({len(failed)}, first {failed[0]})"
                                   + (f"; {FAIL_STREAK} in a row, {not_asked} not asked" if not_asked else ""))
        ctx.notes.append(f"{len(out)} companies read, {len(failed)} failed"
                         + (f"; {FAIL_STREAK} failures in a row, {not_asked} not asked" if not_asked else "")
                         + (f"; {len(companies()) - len(due)} read within {WEEK_DAYS} days" if due else
                            f"; every company read within {WEEK_DAYS} days"))
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # Yahoo serves only the current estimates: a backfill is the weekly rule at its end day.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        import json
        d14 = _d14()
        out = []
        for day in sorted({raw.parent.name for raw in raws}):
            rows = []
            for raw in sorted(day_dir(day).glob("*.json.gz")):
                doc = base.read_raw_json(raw)
                stamps = {"source_as_of": date.fromisoformat(doc["as_of"]), "fetched_at": float(doc["fetched_at"]),
                          "run_id": doc["run_id"]}
                for r in d14.trend_rows(doc["ticker"], doc.get("blocks") or {}, stamps["source_as_of"], {}):
                    rows.append({**r, "cik": common.norm_cik(doc["cik"]), "payload": json.dumps(r["payload"]),
                                 **stamps})
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", schema(), rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY ticker, period",
                                              common.parquet_dir(NAME) / f"date={day}" / "part-0.parquet"))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        common.stage_glob(con, STAGING, schema(), common.parquet_dir(NAME), "date=*/part-0.parquet")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "ticker", "period", "source_as_of"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM estimate_latest",
                                             "estimate_latest", cols)
        on = ctx.as_of or self.default_as_of()
        tier_a = con.execute("SELECT count(DISTINCT cik) FROM fundamental_company").fetchone()[0]
        have, fresh = con.execute(f"""
            SELECT count(DISTINCT cik), count(DISTINCT cik) FILTER (WHERE source_as_of > DATE '{on.isoformat()}'
                                                                    - {WEEK_DAYS})
            FROM estimate_latest""").fetchone()
        ctx.notes.append(f"{have} of {tier_a} Tier A companies have estimates ({fresh} asked within "
                         f"{WEEK_DAYS} days)")
        as_of = con.execute(f"SELECT max(source_as_of) FROM {STAGING}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or on).isoformat(), "rows": common.count(con, STAGING), "added": added,
                "removed": removed, "coverage": round(have / tier_a, 4) if tier_a else None}


DATASET = Estimates()

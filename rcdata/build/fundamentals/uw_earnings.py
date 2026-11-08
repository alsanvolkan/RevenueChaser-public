"""``uw/earnings``: the EPS a company reported, as Unusual Whales serves it, served as ``earnings_reported``.

**Why it is not a fact.** The served statement values are the SEC's (``sec/companyfacts``), and ``fundamental_fact``
holds them with the filing each came from -- its own check refuses a non-preliminary row without an accession. UW's
number has no filing behind it: it is what the company said on its report day, before or beside the 10-Q. Writing it
there would mean marking it ``preliminary``, which every served view reads as "not the SEC's, do not show", so the
value would be stored and invisible. It gets a table of its own instead, where it can never be mistaken for the
SEC's GAAP EPS (avo, 5 Nov 2026: "ayri, kaynak etiketli bir kolon olarak, SEC GAAP EPS'iyle asla karismadan").

**What is kept.** ``GET <RC_UW_BASE>/stock/<ticker>/earnings`` answers statement-style rows; the **quarterly** ones
(``report_type``, read on each row before any date is used: an annual and a fourth-quarter row share a date) give
``reported_eps``, ``estimated_eps`` (UW's consensus of that quarter) and ``surprise_percentage`` as UW serves them,
never recomputed -- a surprise is the vendor's ratio, and recomputing it from two numbers of different bases is how
a wrong one is made. A row without a fiscal period end has no key and is skipped.

**Its basis is UW's, not the SEC's.** A reported EPS is usually the company's adjusted (non-GAAP) figure, which is
why the SEC's ``eps_diluted`` stays the only EPS in ``fundamental_fact``. The two are comparable only through the
``source`` column, which is why it is on every row.

**Weekly, one request per company** (the pattern of ``yfinance/estimates``): a company whose newest answer is
``WEEK_DAYS`` old, or that has none, is asked, the longest without an answer first; ``FAIL_STREAK`` provider-side
failures in a row stop the run (UW throttling or down) and a run where every request failed is a failed run. A
request UW rejects (``rejected``: an unknown ticker) writes a raw file with no rows once a later request answers,
so it ages like an answer; ``REJECT_STREAK`` rejected in a row stop and fail the run. Counted against the
``uw`` day cap; not paced, because UW publishes no request window. No ``UNUSUAL_WHALES_API_KEY``: nothing is asked,
a ``missing_key`` issue.

Raw: ``raw/uw/earnings/<as-of>/<ticker>.json.gz`` (the resume marker). Parquet:
``parquet/uw/earnings/date=<as-of>/part-0.parquet``, rewritten from the day's raw files so a resumed day loses none;
every day kept. Staging: ``stg_uw_earnings``, the newest snapshot's row per company and quarter.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from .. import base
from .. import dirty as dirty_set
from . import KIND, common, facts

NAME = "uw/earnings"
PROVIDER = "uw"
KEY = "unusual_whales"
WEEK_DAYS = 7
FAIL_STREAK = 5
#: Rejected requests (``rejected``) in a row with no answer between that stop the run: one company's 4xx is its own,
#: this many in a row is the path. 5 of 5,304 were rejected on 6 Nov 2026, none of them adjacent.
REJECT_STREAK = 25
STAGING = "stg_uw_earnings"
#: One row per company and reported quarter: UW's three numbers as it serves them, and the day it reported.
SCHEMA = [("cik", "TEXT"), ("ticker", "TEXT"), ("period_end", "DATE"), ("report_date", "DATE"),
          ("eps_reported", "DOUBLE"), ("eps_estimate", "DOUBLE"), ("surprise_pct", "DOUBLE"),
          ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


def endpoint(ticker: str) -> str:
    from ....core.providers.unusual_whales import _seg, uw_symbol
    return f"/stock/{_seg(uw_symbol(ticker))}/earnings"


def day_dir(as_of: date | str) -> Path:
    return common.raw_dir(NAME) / str(as_of)


def _num(v) -> float | None:
    if v is None or isinstance(v, bool) or str(v).strip() in ("", "None", "-"):
        return None
    try:
        return float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def _newest(ticker: str) -> date | None:
    root = common.raw_dir(NAME)
    days = sorted(d.name for d in root.iterdir() if (d / f"{ticker}.json.gz").exists()) if root.is_dir() else []
    return date.fromisoformat(days[-1]) if days else None


def rejected(error: dict | None) -> bool:
    """Whether a failed UW answer is the request's own fault: a 4xx other than 401/403 (the key) and 429
    (throttling) -- an unknown ticker, a bad parameter. The client keeps those out of its breaker for the same
    reason (``unusual_whales.get_result``); a 5xx, a 429, a transport error or an open breaker is the provider's."""
    status = (error or {}).get("status")
    return isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 429)


def companies() -> list[tuple[str, str]]:
    """``(cik, ticker)`` per company of the universe: its shortest ticker (HEI, not HEI-A)."""
    return [(cik, sorted(tickers, key=lambda t: (len(t), t))[0])
            for cik, tickers in common.tickers_by_cik(common.universe()).items()]


def rows_of(body, cik: str, ticker: str) -> tuple[list[dict], dict[str, int]]:
    """The ``earnings_reported`` rows of one answer (no stamps); ``counts`` of the rows left out and why."""
    data = body if isinstance(body, list) else (body or {}).get("data") or []
    out: dict[date, dict] = {}
    counts: dict[str, int] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        if row.get("report_type") != facts.QUARTERLY:
            counts["not_quarterly"] = counts.get("not_quarterly", 0) + 1
            continue
        end = _day(row.get("fiscal_date_ending"))
        if end is None:
            counts["no_period_end"] = counts.get("no_period_end", 0) + 1
            continue
        eps = _num(row.get("reported_eps"))
        if eps is None:
            counts["no_reported_eps"] = counts.get("no_reported_eps", 0) + 1
            continue
        out[end] = {"cik": cik, "ticker": ticker, "period_end": end, "report_date": _day(row.get("report_date")),
                    "eps_reported": eps, "eps_estimate": _num(row.get("estimated_eps")),
                    "surprise_pct": _num(row.get("surprise_percentage")), "source": NAME}
    return [out[k] for k in sorted(out)], counts


class UWEarnings:
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
        every = companies()
        if not every:
            ctx.notes.append("no company in the universe yet (no spine build): nothing asked")
            return []
        if not credentials.key_for(KEY):
            ctx.issue("missing_key", KEY, credentials.missing_message(KEY))
            ctx.notes.append("no UNUSUAL_WHALES_API_KEY: nothing asked")
            return []
        due = []
        for cik, ticker in every:
            newest = _newest(ticker)
            if newest is None or (as_of - newest).days >= WEEK_DAYS:
                due.append((newest or date.min, ticker, cik))
        due.sort()
        out, failed, streak, not_asked, answered, n_rejected = [], [], 0, 0, 0, 0
        held: list[tuple[str, str, dict | None]] = []      # rejected, waiting for a later answer
        lost = False                                        # REJECT_STREAK rejected in a row: the path is gone
        earlier = list(day_dir(as_of).glob("*.json.gz"))
        for n, (_, ticker, cik) in enumerate(due):
            if streak >= FAIL_STREAK:
                not_asked = len(due) - n
                break
            ctx.check_cap(PROVIDER)
            # No pacer: UW publishes no request window (``caps.WINDOWS``), so there is none to keep. The day cap
            # and the client's own 429 backoff are the limits, as for every other UW dataset. The call itself is
            # counted by ``RunContext._on_uw_request``, which sees the retries too, so it is not counted here.
            res = ctx.uw_client().stock_earnings_result(ticker)
            if res.status != "ok":
                ctx.count(PROVIDER, failures=1)
                failed.append(ticker)
                ctx.issue("fetch_failed", ticker, f"UW {endpoint(ticker)}: {res.error}")
                if rejected(res.error):
                    # The request's own fault, not an outage: it does not stop the run, and a raw file with no
                    # rows lets the company age like any other instead of sorting first every run (#519 finding 1).
                    # Held until a later request answers: a 4xx on every request is the path gone, not each
                    # company's fault, and must hide nobody for a week (review of #541).
                    held.append((ticker, cik, res.error))
                    if len(held) >= REJECT_STREAK:
                        not_asked = len(due) - n - 1
                        lost = True
                        break
                    continue
                streak += 1
                continue
            for t, c, err in held:
                out.append(base.write_raw_json(day_dir(as_of) / f"{t}.json.gz", {
                    "request": {"endpoint": endpoint(t)}, "ticker": t, "cik": c, "as_of": as_of.isoformat(),
                    "fetched_at": time.time(), "run_id": ctx.run_id, "rejected": err, "body": None}))
            n_rejected += len(held)
            held = []
            answered += 1
            streak = 0
            out.append(base.write_raw_json(day_dir(as_of) / f"{ticker}.json.gz", {
                "request": {"endpoint": endpoint(ticker)}, "ticker": ticker, "cik": cik, "as_of": as_of.isoformat(),
                "fetched_at": time.time(), "run_id": ctx.run_id, "body": res.data}))
        # The day's earlier answers too (a resumed day): its Parquet is rewritten from all of them.
        out = sorted(set(out) | set(day_dir(as_of).glob("*.json.gz")))
        if lost:
            raise base.SourceError(f"UW /earnings: {REJECT_STREAK} requests in a row rejected (first {held[0][0]}: "
                                   f"{held[0][2]}), {not_asked} not asked; the path, not the companies")
        if failed and not answered and not earlier:
            raise base.SourceError(f"UW /earnings: every request failed ({len(failed)}, first {failed[0]})"
                                   + (f"; {FAIL_STREAK} in a row, {not_asked} not asked" if not_asked else ""))
        ctx.notes.append(f"{len(out) - n_rejected} companies read, {len(failed)} failed"
                         + (f" ({n_rejected} rejected by UW, kept as an empty answer)" if n_rejected else "")
                         + (f"; {FAIL_STREAK} failures in a row, {not_asked} not asked" if not_asked else "")
                         + f"; {len(every) - len(due)} read within {WEEK_DAYS} days")
        return out

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # One answer holds the company's whole print series, so a backfill is the weekly rule at its end day.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out, left = [], {}
        for day in sorted({raw.parent.name for raw in raws}):
            rows = []
            for raw in sorted(day_dir(day).glob("*.json.gz")):
                doc = base.read_raw_json(raw)
                stamps = {"source_as_of": date.fromisoformat(doc["as_of"]), "fetched_at": float(doc["fetched_at"]),
                          "run_id": doc["run_id"]}
                got, counts = rows_of(doc.get("body"), common.norm_cik(doc["cik"]), doc["ticker"])
                for k, n in counts.items():
                    left[k] = left.get(k, 0) + n
                rows += [{**r, **stamps} for r in got]
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY cik, period_end",
                                              common.parquet_dir(NAME) / f"date={day}" / "part-0.parquet"))
            finally:
                con.close()
        if left:
            ctx.notes.append("rows left out: " + ", ".join(f"{k} {n}" for k, n in sorted(left.items())))
        return out

    def dirty(self, ctx: base.RunContext):
        """Small and not partitioned by company: reloaded whole by every build, on the live copy too (#446)."""
        return dirty_set.ALL

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        common.stage_glob(con, STAGING, SCHEMA, common.parquet_dir(NAME), "date=*/part-0.parquet",
                          select="SELECT * FROM {src} QUALIFY row_number() OVER (PARTITION BY cik, period_end "
                                 "ORDER BY source_as_of DESC, fetched_at DESC) = 1")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["cik", "period_end", "eps_reported"]
        added, removed = common.compare_live(con, f"SELECT {', '.join(cols)} FROM earnings_reported",
                                             "earnings_reported", cols)
        on = ctx.as_of or self.default_as_of()
        companies_n = con.execute("SELECT count(DISTINCT cik) FROM fundamental_company").fetchone()[0]
        have, quarters = con.execute("SELECT count(DISTINCT cik), count(*) FROM earnings_reported").fetchone()
        ctx.notes.append(f"{have} of {companies_n} companies have a reported EPS ({quarters} quarters)")
        as_of = con.execute(f"SELECT max(source_as_of) FROM {STAGING}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or on).isoformat(), "rows": common.count(con, STAGING), "added": added,
                "removed": removed, "coverage": round(have / companies_n, 4) if companies_n else None}


DATASET = UWEarnings()

"""``alphavantage/earnings_transcripts``: the words spoken on each company's last reported earnings call.

``GET <RC_ALPHAVANTAGE_BASE>/query?function=EARNINGS_CALL_TRANSCRIPT&symbol=<t>&quarter=<YYYYQn>&apikey=...``
answers JSON: top-level ``symbol``, ``quarter`` and ``transcript``, a list of turns each with exactly ``speaker``,
``title``, ``content`` and ``sentiment``. Measured on the live key 21 Nov 2026 (#1214): 48 turns and ~48 KB of text
for one call, titles ``Analyst``, ``CEO``, ``CFO``, ``Operator`` and ``Vice President of Investor Relations``, and
**the free tier serves it** (no ``Note`` / ``Information`` / ``Error Message`` key on an answer that carries a call).

**The quarter is the company's fiscal quarter, not a calendar one** (measured 21 Nov 2026: ``MSFT`` ``2025Q1``
answers the call whose prepared remarks open "we are off to a solid start to our fiscal year" and report $65.6bn of
revenue -- Microsoft's fiscal Q1, the quarter that ended in September). So the label cannot be derived from a date
alone: it comes from ``earnings_report_history.fiscal_year`` and ``.fiscal_quarter``, which carry Compustat's
``fyearq`` and ``fqtr`` for the quarters Compustat dates. A company with no such row is **not asked about** -- its
transcript would need a guessed label -- and R8 abstains for it.

**One request per (ticker, fiscal quarter), and a raw file is the resume marker.** A past quarter's call does not
change, so a quarter already on disk is never fetched again; the free tier's 25 requests a day
(``caps.DAY_CAPS``) therefore buy ~22 *new* transcripts a day beside the spine's own three, and coverage climbs
day by day instead of costing anything per run. The run stops cleanly at the cap (``base.CapReached`` from
``ctx.check_cap``) and the names it did not reach are the next day's work. That shape is the owner's decision of
21 Nov 2026 (the free tier, not a paid plan).

``Information`` is **a refusal, not an empty call.** The same measurement asked ``NVDA`` ``2025Q4`` and got
``{"Information": ...}`` with no ``transcript``: that is the provider declining (a limit, or an endpoint it will
not serve), and treating it as "this company has no transcript" would write a false absence and never retry. So
``parse`` raises ``SourceError`` for it and no raw file is kept.

Raw: ``raw/alphavantage/earnings_transcripts/<ticker>/<quarter>.json.gz`` with a ``.meta.json`` (the URL without
its query -- the key travels in the query string and is never logged or stored). Parquet:
``parquet/alphavantage/earnings_transcripts/ticker=<t>/quarter=<q>/part-0.parquet``, one row per turn. Staging:
``stg_av_transcript``; ``fundamentals/tables`` builds ``earnings_transcript`` from it.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from ....core.identity.sources import norm_ticker
from .. import base
from .. import dirty as dirty_set
from . import KIND, common

NAME = "alphavantage/earnings_transcripts"
PROVIDER = "alphavantage"
KEY = "alphavantage"
STAGING = "stg_av_transcript"
FUNCTION = "EARNINGS_CALL_TRANSCRIPT"
#: The provider's own refusal keys. An answer carrying one is a refusal, never a call with no turns.
REFUSALS = ("Note", "Information", "Error Message")
#: A turn's own fields, in the order the provider sends them.
TURN_FIELDS = ("speaker", "title", "content", "sentiment")
SCHEMA = [("ticker", "TEXT"), ("quarter", "TEXT"), ("seq", "INTEGER"), ("speaker", "TEXT"), ("title", "TEXT"),
          ("content", "TEXT"), ("provider_sentiment", "DOUBLE"), ("source_as_of", "DATE"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


def base_url() -> str:
    return (os.getenv("RC_ALPHAVANTAGE_BASE") or "https://www.alphavantage.co").rstrip("/")


def quarter_label(fiscal_year, fiscal_quarter) -> str | None:
    """``YYYYQn`` for a fiscal year and quarter, or ``None`` when either is missing or out of range."""
    try:
        year, quarter = int(fiscal_year), int(fiscal_quarter)
    except (TypeError, ValueError):
        return None
    return f"{year}Q{quarter}" if 1900 <= year <= 2200 and 1 <= quarter <= 4 else None


def raw_path(ticker: str, quarter: str) -> Path:
    return common.raw_dir(NAME) / str(ticker) / f"{quarter}.json.gz"


def held(ticker: str, quarter: str) -> bool:
    """Whether this (ticker, quarter) is already on disk. A past quarter's call never changes, so this is the
    resume marker: it is what keeps a backfill from spending the day's budget on the same names again."""
    return raw_path(ticker, quarter).exists()


def parse(text: str) -> list[dict]:
    """The call's turns, in the order spoken; ``SourceError`` for a refusal or an answer of another shape."""
    try:
        body = json.loads(text or "")
    except ValueError as exc:
        raise base.SourceError(f"{NAME}: the answer is not JSON") from exc
    if not isinstance(body, dict):
        raise base.SourceError(f"{NAME}: the answer is not an object")
    refused = [k for k in REFUSALS if k in body]
    if refused:
        # Named from our own list, never echoed from the answer: it could repeat the request, and the key is in it.
        raise base.SourceError(f"{NAME}: the provider refused ({', '.join(refused)})")
    turns = body.get("transcript")
    if not isinstance(turns, list):
        raise base.SourceError(f"{NAME}: no transcript list in the answer")
    out = []
    for seq, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        out.append({"seq": seq,
                    "speaker": (str(turn.get("speaker") or "").strip() or None),
                    "title": (str(turn.get("title") or "").strip() or None),
                    "content": (str(turn.get("content") or "").strip() or None),
                    "provider_sentiment": _num(turn.get("sentiment"))})
    return out


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def wanted(rows: list[dict], tickers: dict[str, list[str]]) -> list[tuple[str, str]]:
    """``[(ticker, quarter label)]``: the last labelled reported quarter of each company we hold a ticker for.

    ``rows`` are ``earnings_report_history`` rows (keyed by CIK, which is why ``tickers`` -- the universe's
    ``{cik: [ticker]}`` -- is passed in; the table carries no ticker and the provider asks by symbol). The newest
    ``period_end`` of a company wins among the quarters that **have** a fiscal label: an unlabelled quarter cannot
    be asked for at all, so the one before it is the only thing on offer and is still the company's own words
    about a reported quarter. A company with no labelled quarter is left out, and R8 abstains for it.
    """
    best: dict[str, tuple[date, str]] = {}
    for r in rows:
        cik = common.norm_cik(r.get("cik"))
        label = quarter_label(r.get("fiscal_year"), r.get("fiscal_quarter"))
        period_end = r.get("period_end")
        held_tickers = tickers.get(cik or "") or []
        if not cik or label is None or not held_tickers or not isinstance(period_end, date):
            continue
        ticker = norm_ticker(held_tickers[0])
        if not ticker:
            continue
        if ticker not in best or period_end > best[ticker][0]:
            best[ticker] = (period_end, label)
    return sorted((t, q) for t, (_, q) in best.items())


#: How far back a reported quarter may be and still be the one R8 reads. A company reports every quarter, so a
#: newest labelled quarter older than this means the label source is behind, not that the call is the current one.
HISTORY_DAYS = 400


class EarningsTranscripts:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    feeds = ("earnings_transcript",)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def _due(self, ctx: base.RunContext, as_of: date) -> list[tuple[str, str]]:
        from ... import read                  # the live serving copy, as ``common.universe`` reads it
        rows = read.earnings_report_history(start=as_of - timedelta(days=HISTORY_DAYS), end=as_of)
        pairs = wanted(rows, common.tickers_by_cik(common.universe()))
        if not pairs:
            ctx.notes.append("no labelled reported quarter yet (no fundamentals file, or no Compustat dates)")
        return [(t, q) for t, q in pairs if not held(t, q)]

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        due = self._due(ctx, as_of)
        if not due:
            return []
        key = credentials.key_for(KEY)
        if not key:
            ctx.issue("missing_key", KEY, credentials.missing_message(KEY))
            ctx.notes.append("no ALPHAVANTAGE_API_KEY: nothing asked")
            return []
        out: list[Path] = []
        for ticker, quarter in due:
            try:
                ctx.check_cap(PROVIDER)              # before the request, so the cap is never overspent
                raw = self._read_one(ctx, ticker, quarter, key, as_of)
            except base.CapReached:
                # The day's budget is spent, here or inside the request. What is left is the next day's work and
                # not a failure, so the run ends cleanly and says how much is left.
                ctx.notes.append(f"day cap reached: {len(due) - len(out)} transcript(s) left for the next run")
                break
            if raw is not None:
                out.append(raw)
        ctx.notes.append(f"{len(out)} new transcript(s) of {len(due)} missing")
        return out

    def _read_one(self, ctx: base.RunContext, ticker: str, quarter: str, key: str,
                  as_of: date) -> Path | None:
        url = f"{base_url()}/query"
        r = common.get(ctx, PROVIDER, url, keyed=True,
                       params={"function": FUNCTION, "symbol": ticker, "quarter": quarter, "apikey": key})
        if r is None:                            # no ``missing`` status is passed, so this cannot happen today
            return None
        try:
            turns = parse(r.text)                 # refuse a provider refusal before it is kept as an empty call
        except base.SourceError as exc:
            ctx.issue("source", f"{ticker} {quarter}", str(exc))
            return None
        if not turns:
            # A real answer with no turns: the call is not published. Nothing is kept, so a later run asks again.
            ctx.notes.append(f"{ticker} {quarter}: no turns in the answer")
            return None
        raw = raw_path(ticker, quarter)
        base.write_raw_json(common.meta_path(raw), {"ticker": ticker, "quarter": quarter,
                                                   "as_of": as_of.isoformat(), "fetched_at": time.time(),
                                                   "run_id": ctx.run_id, "url": url, "turns": len(turns)})
        return base.write_raw(raw, r.content)

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # The provider serves one quarter per request, so a backfill is the same bounded pass as a run.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(common.meta_path(raw))
            ticker, quarter = str(meta["ticker"]), str(meta["quarter"])
            stamps = {"ticker": ticker, "quarter": quarter,
                      "source_as_of": date.fromisoformat(meta["as_of"]),
                      "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
            rows = [{**t, **stamps} for t in parse(base.read_raw(raw).decode("utf-8", "replace"))]
            con = base.scratch_con()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY seq",
                                              common.parquet_dir(NAME) / f"ticker={ticker}" /
                                              f"quarter={quarter}" / "part-0.parquet"))
            finally:
                con.close()
        return out

    def dirty(self, ctx: base.RunContext):
        """Keyed by ticker and quarter, not by company: the staging is reloaded whole, as the calendar's is."""
        return dirty_set.ALL

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        common.stage_glob(con, STAGING, SCHEMA, common.parquet_dir(NAME), "ticker=*/quarter=*/part-0.parquet")
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        names = con.execute(f"SELECT count(DISTINCT ticker) FROM {STAGING}").fetchone()[0]
        quarters = con.execute(f"SELECT count(DISTINCT quarter) FROM {STAGING}").fetchone()[0]
        calls = con.execute(f"SELECT count(*) FROM (SELECT DISTINCT ticker, quarter FROM {STAGING})").fetchone()[0]
        on = ctx.as_of or self.default_as_of()
        universe = len(common.tickers_by_cik(common.universe()))
        ctx.notes.append(f"{names} of {universe} companies have a transcript ({calls} calls, {quarters} quarters)")
        # A backfill that is still climbing is the expected state, so coverage is a note, never a failed check.
        return {"dataset": NAME, "result": "changed" if calls else "unchanged",
                "source_as_of": on.isoformat(), "rows": common.count(con, STAGING)}


DATASET = EarningsTranscripts()

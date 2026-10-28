"""The float: Yahoo's ``floatShares`` (``yfinance/float``) and Unusual Whales' ``total_float`` (``uw/total_float``).

**``yfinance/float``** asks Yahoo (the ``yfinance`` library, one ``Ticker.get_info`` per name) for ``SLICE`` Tier A
names a run: the names never asked first (by ticker), then the longest ago. The lookups are ``INTERVAL_S`` apart; a
401 or a 429 (Yahoo's refusal, or its rate limit) stops the run's lookups at once, and what was read before it is
kept: the next run goes on from there. ``yfinance`` (1.7.0, measured 28 Oct 2026) raises nothing for a refusal: it logs
the 401 or 429 on its ``yfinance`` logger and answers an empty info, so ``_lookup`` listens to that logger and raises
the refusal it logged when the answer is empty; an empty answer with no refusal logged is that name's error row
(``EMPTY_ANSWER``). Raw: ``raw/yfinance/float/<as_of>/batch-<n>.json.gz``, ``BATCH`` names a file;
a name a file of the day holds is not asked again that day. Parquet: ``parquet/yfinance/float/date=<as_of>/
part-0.parquet`` keyed on the ticker. ``_lookup`` is the one function that talks to Yahoo (the tests replace it).

**``uw/total_float``** asks UW's ``/short_screener`` for every Tier A name, 100 a request (the UW client's batching;
every request counted against UW's day cap): the daily check of the float. Raw: ``raw/uw/total_float/<as_of>.json.gz``.
Parquet: ``parquet/uw/total_float/date=<as_of>/part-0.parquet`` keyed on the ticker.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from . import KIND, Spacer, scope, sec_ticker, stage

YAHOO = "yfinance/float"
UW = "uw/total_float"
SLICE = 400
BATCH = 50
INTERVAL_S = 1.5
FIELDS = ("floatShares", "sharesOutstanding", "impliedSharesOutstanding", "quoteType", "currency", "exchange")
STOP = re.compile(r"\b(401|429)\b|rate.?limit|too many requests|unauthori[sz]ed|invalid crumb", re.I)
EMPTY_ANSWER = "empty answer: Yahoo returned none of the fields"
YAHOO_SCHEMA = [("ticker", "TEXT"), ("cik", "TEXT"), ("security_id", "TEXT"), ("float_shares", "DOUBLE"),
                ("shares_outstanding", "DOUBLE"), ("implied_shares", "DOUBLE"), ("quote_type", "TEXT"),
                ("currency", "TEXT"), ("error", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
                ("run_id", "TEXT")]
UW_SCHEMA = [("ticker", "TEXT"), ("cik", "TEXT"), ("security_id", "TEXT"), ("total_float", "DOUBLE"),
             ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]


class _Logged(logging.Handler):
    """The ``yfinance`` logger's messages during one lookup."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class YahooRefused(RuntimeError):
    """Yahoo refused a lookup (a 401 or 429 the library only logged)."""


def _lookup(ticker: str) -> dict:
    """Yahoo's fields of ``FIELDS`` for one ticker (the SEC file's form, which is Yahoo's: ``BRK-B``). An empty
    answer after a logged 401 or 429 raises ``YahooRefused`` with the logged text (the run's stop rule reads it)."""
    import yfinance as yf
    log, seen = logging.getLogger("yfinance"), _Logged()
    log.addHandler(seen)
    try:
        info = yf.Ticker(ticker).get_info() or {}
    finally:
        log.removeHandler(seen)
    out = {k: info.get(k) for k in FIELDS}
    refused = [m for m in seen.messages if STOP.search(m) and "continuing without crumb" not in m]
    if refused and all(v is None for v in out.values()):
        raise YahooRefused(refused[-1][:300])
    return out


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _tier_a() -> list[dict]:
    return [n for n in scope() if n.get("tier") == "A"]


def _partitions(*parts: str) -> list[Path]:
    return [f for _, f in stage.partitions(base.parquet_root().joinpath(*parts), "date")]


class YahooFloat:
    name = YAHOO
    kind = KIND
    providers = ("yfinance",)
    tables = ("shares_class",)
    cadence = "daily"
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def raw_dir(self, as_of: date) -> Path:
        return base.raw_root() / "yfinance" / "float" / as_of.isoformat()

    def _order(self, names: list[dict]) -> list[dict]:
        """Never asked first (by ticker), then the longest ago."""
        parts = _partitions("yfinance", "float")
        last: dict[str, float] = {}
        if parts:
            con = duckdb.connect()
            try:
                last = dict(con.execute("SELECT ticker, max(fetched_at) FROM read_parquet(["
                                        + ", ".join(base.sql_path(p) for p in parts) + "]) GROUP BY ticker").fetchall())
            finally:
                con.close()
        return sorted(names, key=lambda n: (n["ticker"] in last, last.get(n["ticker"], 0.0), n["ticker"]))

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        names = _tier_a()
        if not names:
            raise base.SourceError("the spine holds no Tier A name (spine.duckdb not built?): nothing to ask")
        folder = self.raw_dir(as_of)
        have = sorted(folder.glob("batch-*.json.gz")) if folder.is_dir() else []
        done = {t for p in have for t in base.read_raw_json(p)["rows"]}
        todo = [n for n in self._order(names) if n["ticker"] not in done][:max(0, SLICE - len(done))]
        spacer, out, batch, seq = Spacer(INTERVAL_S, ctx.clock, ctx.sleep), list(have), {}, len(have)

        def flush():
            nonlocal batch, seq
            if batch:
                out.append(base.write_raw_json(folder / f"batch-{seq:04d}.json.gz", {
                    "as_of": as_of.isoformat(), "rows": batch, "fetched_at": calendar._now().timestamp(),
                    "run_id": ctx.run_id}))
                seq, batch = seq + 1, {}
        for n in todo:
            ctx.check_cap("yfinance")
            spacer.wait()
            ctx.count("yfinance", calls=1)
            entry = {"cik": n.get("cik"), "security_id": n["security_id"]}
            try:
                entry.update(_lookup(n["ticker"]))
                if all(entry.get(k) is None for k in FIELDS):
                    ctx.count("yfinance", failures=1)
                    entry["error"] = EMPTY_ANSWER
            except Exception as e:  # noqa: BLE001 - one name's failure is that name's row, a refusal stops the run
                text = f"{type(e).__name__}: {e}"
                if STOP.search(text) or type(e).__name__ == "YFRateLimitError":
                    ctx.count("yfinance", failures=1, http_429=int("429" in text or "rate" in text.lower()))
                    ctx.notes.append(f"yfinance stopped at {n['ticker']}: {text[:120]}")
                    break
                ctx.count("yfinance", failures=1)
                entry["error"] = text[:200]
            batch[n["ticker"]] = entry
            if len(batch) >= BATCH:
                flush()
        flush()
        return out

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        days: dict[str, list[dict]] = {}
        for raw in raws:
            p = base.read_raw_json(raw)
            for ticker, e in p["rows"].items():
                days.setdefault(p["as_of"], []).append({
                    "ticker": ticker, "cik": e.get("cik"), "security_id": e.get("security_id"),
                    "float_shares": _num(e.get("floatShares")), "shares_outstanding": _num(e.get("sharesOutstanding")),
                    "implied_shares": _num(e.get("impliedSharesOutstanding")), "quote_type": e.get("quoteType"),
                    "currency": e.get("currency"), "error": e.get("error"), "source_as_of": date.fromisoformat(
                        p["as_of"]), "fetched_at": p["fetched_at"], "run_id": p["run_id"]})
        return [base.upsert_partition(rows, YAHOO_SCHEMA, ["ticker"], base.parquet_root() / "yfinance" / "float"
                                      / f"date={d}" / "part-0.parquet") for d, rows in sorted(days.items())]

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        stage.create(con, "stg_yahoo_float", YAHOO_SCHEMA, _partitions("yfinance", "float"))
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        return _float_check(ctx, con, YAHOO, "stg_yahoo_float", "float_shares", self.default_as_of())


class UWFloat:
    name = UW
    kind = KIND
    providers = ("uw",)
    tables = ("shares_class",)
    cadence = "daily"
    starts_by_hand = True

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def raw_path(self, as_of: date) -> Path:
        return base.raw_root() / "uw" / "total_float" / f"{as_of.isoformat()}.json.gz"

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        path = self.raw_path(as_of)
        if path.exists():
            return [path]
        names = {n["ticker"]: {"cik": n.get("cik"), "security_id": n["security_id"]} for n in _tier_a()}
        if not names:
            raise base.SourceError("the spine holds no Tier A name (spine.duckdb not built?): nothing to ask")
        ctx.check_cap("uw")
        res = ctx.uw_client().short_screener_result(sorted(names))
        if res.status == "failed":
            raise base.SourceError(f"uw /short_screener failed: {res.error}")
        if res.status == "partial":
            ctx.notes.append(f"uw /short_screener partial: {res.error}")
        rows = {}
        for symbol, item in (res.data or {}).items():
            t = sec_ticker(symbol)
            if t in names and isinstance(item, dict):
                rows[t] = {**names[t], "total_float": item.get("total_float")}
        return [base.write_raw_json(path, {"as_of": as_of.isoformat(), "rows": rows, "status": res.status,
                                           "fetched_at": calendar._now().timestamp(), "run_id": ctx.run_id})]

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            p = base.read_raw_json(raw)
            rows = [{"ticker": t, "cik": e.get("cik"), "security_id": e.get("security_id"),
                     "total_float": _num(e.get("total_float")), "source_as_of": date.fromisoformat(p["as_of"]),
                     "fetched_at": p["fetched_at"], "run_id": p["run_id"]} for t, e in sorted(p["rows"].items())]
            out.append(base.upsert_partition(rows, UW_SCHEMA, ["ticker"], base.parquet_root() / "uw" / "total_float"
                                             / f"date={p['as_of']}" / "part-0.parquet"))
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        stage.create(con, "stg_uw_float", UW_SCHEMA, _partitions("uw", "total_float"))
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        return _float_check(ctx, con, UW, "stg_uw_float", "total_float", self.default_as_of())


def _float_check(ctx, con, name: str, staging: str, column: str, default: date) -> dict:
    """The check row of a float source: its ``shares_class`` rows against the live file, and the share of Tier A
    with a value in its newest day."""
    cols = ["security_id", "measure", "as_of", "value", "source"]
    added, removed = stage.compare_live(con, "shares_class", cols, f"source = '{name}'")
    tier_a = {n["ticker"] for n in _tier_a()}
    have = {r[0] for r in con.execute(f"SELECT DISTINCT ticker FROM {staging} WHERE {column} IS NOT NULL")}
    as_of = ctx.as_of or default
    return {"dataset": name, "result": "changed" if added or removed else "unchanged",
            "source_as_of": as_of.isoformat(), "rows": stage.count(con, staging), "added": added, "removed": removed,
            "coverage": round(len(tier_a & have) / len(tier_a), 4) if tier_a else None}


YAHOO_FLOAT = YahooFloat()
UW_FLOAT = UWFloat()

"""``regsho/threshold``: the Reg SHO threshold lists, one file per market and day (keyless).

- Nasdaq: ``GET <RC_REGSHO_NASDAQ_BASE>/nasdaqth<YYYYMMDD>.txt``, header ``Symbol|Security Name|Market Category|Reg
  SHO Threshold Flag|Rule 3210|Filler``, one row per security, a last line with the file's time stamp
  (``20261027230025``); a day with no file is redirected to Nasdaq's not-found page (``Trader.aspx?id=http404``),
  read as a 404.
- NYSE: ``GET <RC_NYSE_BASE>/api/regulatory/threshold-securities/download?selectedDate=27-Oct-2026&market=<m>`` for
  ``NYSE``, ``NYSE American`` and ``NYSE Arca``, header ``Symbol|Security Name|Market Category|Reg SHO Threshold
  Flag|Filler|Filler``. A market with no threshold security answers the header alone. NYSE National, NYSE Texas and
  NYSE Chicago answered the header alone on every day tried (2026-10-28) and are not asked; Cboe's list is a
  JavaScript page and is not read (both known gaps).

A file is parsed by its header's names; an answer without that header (an error page) is refused and not kept. The
list of day D is published that evening, so a run's day is the session before today. An empty list or a 404 of one
of the last ``RECENT_SESSIONS`` sessions is not kept (asked again on the next run); of an older day, an empty list
is kept and a 404 is marked absent (``<day>.none``), never asked again.

A daily run asks the days from the newest ``DAILY_LOOKBACK`` sessions that have no file yet, never before the first
day stored (the first run asks its own day only); a backfill every session of its range.

Raw: ``raw/regsho/threshold/<market>/<day>.txt`` (as answered) with a ``.meta.json`` sidecar. Parquet:
``parquet/regsho/threshold/date=<day>/part-0.parquet`` (every market of the day). Served: ``regsho_threshold``.
"""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..spine.stage import partitions
from . import KIND, common

NAME = "regsho/threshold"
TABLE = "regsho_threshold"
NASDAQ_ENV, NYSE_ENV = "RC_REGSHO_NASDAQ_BASE", "RC_NYSE_BASE"
NASDAQ_DEFAULT, NYSE_DEFAULT = "https://www.nasdaqtrader.com/dynamic/symdir/regsho", "https://www.nyse.com"
NYSE_PATH = "/api/regulatory/threshold-securities/download"
# Our name of each market, and the provider its file counts against (with NYSE's own market name).
MARKETS = {"nasdaq": ("nasdaqtrader", None), "nyse": ("nyse", "NYSE"), "nyse_american": ("nyse", "NYSE American"),
           "nyse_arca": ("nyse", "NYSE Arca")}
RECENT_SESSIONS = 2
DAILY_LOOKBACK = 5
PER_MINUTE = 60
META_SUFFIX = ".meta.json"
SCHEMA = [("list_date", "DATE"), ("market", "TEXT"), ("symbol", "TEXT"), ("source_symbol", "TEXT"),
          ("security_name", "TEXT"), ("market_category", "TEXT"), ("threshold_flag", "TEXT"), ("rule_3210", "TEXT"),
          ("file_stamp", "TEXT"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
SERVED = SCHEMA[:3] + [("ticker", "TEXT"), ("ticker_source", "TEXT")] + SCHEMA[3:] + [("source", "TEXT")]
COLUMNS = {"security_name": "Security Name", "market_category": "Market Category",
           "threshold_flag": "Reg SHO Threshold Flag", "rule_3210": "Rule 3210"}


def raw_dir(market: str) -> Path:
    return base.raw_root() / "regsho" / "threshold" / market


def parquet_dir() -> Path:
    return base.parquet_root() / "regsho" / "threshold"


def raw_path(market: str, day: date) -> Path:
    return raw_dir(market) / f"{day.isoformat()}.txt"


def none_path(market: str, day: date) -> Path:
    return raw_dir(market) / f"{day.isoformat()}.none"


def _meta(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def request(market: str, day: date) -> tuple[str, str, dict | None]:
    """``(provider, url, params)`` of one market's list of ``day``."""
    provider, nyse_name = MARKETS[market]
    if nyse_name is None:
        root = (os.getenv(NASDAQ_ENV) or NASDAQ_DEFAULT).rstrip("/")
        return provider, f"{root}/nasdaqth{day:%Y%m%d}.txt", None
    root = (os.getenv(NYSE_ENV) or NYSE_DEFAULT).rstrip("/")
    return provider, root + NYSE_PATH, {"selectedDate": day.strftime("%d-%b-%Y"), "market": nyse_name}


def parse(market: str, body: bytes) -> tuple[list[dict], str | None]:
    """The list's rows by its header's names, and the file's time stamp; ``SourceError`` when the answer is not the
    list (no ``Symbol|...|Reg SHO Threshold Flag`` header)."""
    lines = [ln.strip() for ln in body.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    header = lines[0].split("|") if lines else []
    if not header or header[0] != "Symbol" or "Reg SHO Threshold Flag" not in header:
        raise base.SourceError(f"regsho {market}: the answer is not the threshold list ({(lines or [''])[0][:60]!r})")
    stamp, rows = None, []
    for ln in lines[1:]:
        if "|" not in ln:
            stamp = ln if ln.isdigit() else stamp
            continue
        rec = dict(zip(header, ln.split("|")))
        symbol = common.norm_symbol(rec.get("Symbol"))
        if not symbol:
            continue
        row = {"market": market, "symbol": symbol, "source_symbol": rec.get("Symbol")}
        for col, name in COLUMNS.items():
            v = (rec.get(name) or "").strip()
            row[col] = v or None
        rows.append(row)
    return rows, stamp


def done(market: str, day: date) -> bool:
    return raw_path(market, day).exists() or none_path(market, day).exists()


def first_day() -> date | None:
    days = [date.fromisoformat(p.name[:10]) for m in MARKETS if raw_dir(m).is_dir()
            for p in raw_dir(m).iterdir() if p.name.endswith((".txt", ".none"))]
    return min(days) if days else None


class RegShoThreshold:
    name = NAME
    kind = KIND
    providers = ("nasdaqtrader", "nyse")
    tables = (TABLE,)
    cadence = "daily"
    starts_by_hand = True             # the first run is the server thread's, after a backup

    def default_as_of(self) -> date:
        return common.session_before(calendar.today_ny())

    def _fetch_one(self, ctx: base.RunContext, market: str, day: date, as_of: date,
                   pacer: base.Pacer) -> Path | None:
        provider, url, params = request(market, day)
        pacer.wait()
        r = base.http_get(ctx, provider, url, params=params, accept=(404,))
        recent = day > common.session_before(as_of, RECENT_SESSIONS)
        # Nasdaq answers a day with no file by a redirect to its not-found page, never a 404 (seen 2026-10-28).
        if r.status_code == 404 or r.history:
            if not recent:
                base.write_raw_json(none_path(market, day), {"status": 404, "url": url, "run_id": ctx.run_id})
            return None
        rows, _ = parse(market, r.content)
        if not rows and recent:
            ctx.notes.append(f"{market} {day}: an empty list, asked again on the next run")
            return None
        raw = raw_path(market, day)
        base.write_raw_json(_meta(raw), {"day": day.isoformat(), "fetched_at": time.time(), "run_id": ctx.run_id,
                                         "url": url, "params": params})
        return base.write_raw(raw, r.content)

    def _walk(self, ctx: base.RunContext, days: list[date], as_of: date) -> list[Path]:
        pacer = base.Pacer(PER_MINUTE, clock=ctx.clock, sleep=ctx.sleep)
        out = []
        for day in days:
            for market in MARKETS:
                if not done(market, day):
                    raw = self._fetch_one(ctx, market, day, as_of, pacer)
                    if raw is not None:
                        out.append(raw)
        return out

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        first = first_day()
        if first is None:
            days = [as_of] if common.is_session(as_of) else []
        else:
            days = common.sessions(max(first, common.session_before(as_of, DAILY_LOOKBACK)), as_of)
        return self._walk(ctx, days, as_of)

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        days = common.sessions(start or end, end)
        ctx.notes.append(f"{len(days)} sessions to walk")
        self.to_parquet_all(ctx, self._walk(ctx, days, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for day in sorted({date.fromisoformat(r.name[:10]) for r in raws}):
            rows = []
            for market in MARKETS:
                raw = raw_path(market, day)
                if not raw.exists():
                    continue
                meta = base.read_raw_json(_meta(raw))
                parsed, stamp = parse(market, base.read_raw(raw))
                rows += [{**r, "list_date": day, "file_stamp": stamp, "fetched_at": meta["fetched_at"],
                          "run_id": meta["run_id"]} for r in parsed]
            final = parquet_dir() / f"date={day.isoformat()}" / "part-0.parquet"
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY market, symbol", final))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        files = [f for _, f in partitions(parquet_dir(), "date")]
        if not files:
            return {TABLE: common.create_table(con, TABLE, SERVED)}
        src = common.files_sql(files)
        common.symbol_map(con, f"SELECT symbol FROM {src}", ctx.notes, NAME)
        select = (f"SELECT p.*, m.ticker, m.ticker_source, '{NAME}' AS source FROM {src} p "
                  f"LEFT JOIN _symbol_map m USING (symbol) ORDER BY p.list_date, p.market, p.symbol")
        return {TABLE: common.create_table(con, TABLE, SERVED, select)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, KIND, TABLE, ["list_date", "market", "symbol", "ticker"])
        newest = con.execute(f"SELECT max(list_date) FROM {TABLE}").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, TABLE), "added": added, "removed": removed}


DATASET = RegShoThreshold()

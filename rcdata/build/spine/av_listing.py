"""``alphavantage/listing_status``: Alpha Vantage's list of every US listing, the ETF universe's first half (#346).

``GET <RC_ALPHAVANTAGE_BASE>/query?function=LISTING_STATUS&apikey=...`` answers CSV with the header
``symbol,name,exchange,assetType,ipoDate,delistingDate,status`` and the literal ``null`` for a missing value
(measured 1 Nov 2026: 14,521 rows, 5,907 ``ETF`` and 8,614 ``Stock``). A second call adds ``&state=delisted``; it
answered ``{}`` (2 bytes) that day, so a delisted answer that is not the CSV is a ``no_rows`` build issue and the
active list is kept. A JSON answer to the active call (a rate limit or an error, HTTP 200) fails the run and keeps
nothing; its body is never echoed (it can repeat the key). Two requests a run, inside the ``alphavantage`` day cap
it shares with the earnings calendar.

The key (``ALPHAVANTAGE_API_KEY``, through ``core.credentials``) travels in the query string, which is never stored
or logged: the meta keeps the URL without its query, and the builder silences httpx's request log
(``rcdata/build/__main__.py``). No key: nothing is asked, a ``missing_key`` build issue. Keyed, so it runs only in
the builder service.

Raw: ``raw/alphavantage/listing_status/<as-of>.csv.gz`` (the active list, the resume marker), its ``.meta.json``, and
``<as-of>.delisted.csv.gz`` when the delisted call answered the CSV. Parquet:
``parquet/alphavantage/listing_status/date=<as-of>/part-0.parquet``, both lists in one snapshot. Staging:
``stg_av_listing``, the newest snapshot only; ``spine/security`` reads it to build ``etf``.
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar, credentials
from ....core.identity.sources import norm_ticker
from ..fundamentals.earnings import base_url
from .. import base
from . import KIND, stage

NAME = "alphavantage/listing_status"
PROVIDER = "alphavantage"
KEY = "alphavantage"
STAGING = "stg_av_listing"
REQUIRED = ("symbol", "name", "exchange", "assetType", "ipoDate", "delistingDate", "status")
SCHEMA = [("ticker", "TEXT"), ("symbol", "TEXT"), ("name", "TEXT"), ("exchange", "TEXT"), ("asset_type", "TEXT"),
          ("ipo_date", "DATE"), ("delisting_date", "DATE"), ("status", "TEXT"), ("source_as_of", "DATE"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
META_SUFFIX = ".meta.json"


def raw_dir() -> Path:
    return base.raw_root() / "alphavantage" / "listing_status"


def parquet_dir() -> Path:
    return base.parquet_root() / "alphavantage" / "listing_status"


def raw_path(as_of: date) -> Path:
    return raw_dir() / f"{as_of.isoformat()}.csv.gz"


def delisted_path(as_of: date) -> Path:
    return raw_dir() / f"{as_of.isoformat()}.delisted.csv.gz"


def meta_path(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def _text(value) -> str | None:
    text = str(value or "").strip()
    return None if text in ("", "null") else text


def _day(value) -> date | None:
    try:
        return date.fromisoformat(_text(value) or "")
    except ValueError:
        return None


def parse(text: str) -> list[dict]:
    """The list's rows; ``SourceError`` for a JSON answer, a CSV without the documented columns, or no rows."""
    body = (text or "").lstrip()
    if body.startswith("{"):
        try:
            keys = sorted(json.loads(body))
        except ValueError:
            keys = ["?"]
        # The keys only, never the values: an error message can repeat the key.
        raise base.SourceError(f"alphavantage listing status: a JSON answer, not the list ({', '.join(keys)})")
    reader = csv.DictReader(io.StringIO(body))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise base.SourceError(f"alphavantage listing status: missing columns {', '.join(missing)}")
    out = []
    for r in reader:
        symbol = _text(r.get("symbol"))
        if not symbol:
            continue
        out.append({"ticker": norm_ticker(symbol), "symbol": symbol, "name": _text(r.get("name")),
                    "exchange": _text(r.get("exchange")), "asset_type": _text(r.get("assetType")),
                    "ipo_date": _day(r.get("ipoDate")), "delisting_date": _day(r.get("delistingDate")),
                    "status": _text(r.get("status"))})
    if not out:
        raise base.SourceError("alphavantage listing status: the list came back empty")
    return out


class AvListing:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # as every spine dataset: not due until a CLI run or a Fetch built it

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
        r = base.http_get(ctx, PROVIDER, url, params={"function": "LISTING_STATUS", "apikey": key}, keyed=True)
        rows = parse(r.text)                      # refuse a rate-limit answer before it is kept
        try:
            d = base.http_get(ctx, PROVIDER, url, keyed=True,
                              params={"function": "LISTING_STATUS", "state": "delisted", "apikey": key})
            parse(d.text)
            base.write_raw(delisted_path(as_of), d.content)
        except base.SourceError as e:
            ctx.issue("no_rows", "delisted", str(e))
            ctx.notes.append(f"delisted list not read ({e})")
        base.write_raw_json(meta_path(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "url": url, "rows": len(rows)})
        ctx.pending[NAME] = True
        return [base.write_raw(raw, r.content)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # Alpha Vantage serves only today's list: a backfill is one fetch.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(meta_path(raw))
            as_of = date.fromisoformat(meta["as_of"])
            rows = parse(base.read_raw(raw).decode("utf-8", "replace"))
            delisted = delisted_path(as_of)
            if delisted.exists():
                rows += parse(base.read_raw(delisted).decode("utf-8", "replace"))
            stamps = {"source_as_of": as_of, "fetched_at": float(meta["fetched_at"]), "run_id": meta["run_id"]}
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, [{**r, **stamps} for r in rows])
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY ticker, status",
                                              parquet_dir() / f"date={as_of.isoformat()}" / "part-0.parquet"))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        parts = stage.partitions(parquet_dir(), "date")
        stage.create(con, STAGING, SCHEMA, [parts[-1][1]] if parts else [])
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        etf = stage.count(con, STAGING, "asset_type = 'ETF' AND status = 'Active'")
        stock = stage.count(con, STAGING, "asset_type = 'Stock' AND status = 'Active'")
        gone = stage.count(con, STAGING, "status = 'Delisted'")
        as_of = con.execute(f"SELECT max(source_as_of) FROM {STAGING}").fetchone()[0]
        on = ctx.as_of or self.default_as_of()
        ctx.notes.append(f"{etf} ETF, {stock} Stock active; {gone} delisted")
        return {"dataset": NAME, "result": "changed" if ctx.pending.get(NAME) else "unchanged",
                "source_as_of": (as_of or on).isoformat(), "rows": stage.count(con, STAGING), "added": 0,
                "removed": 0}


DATASET = AvListing()

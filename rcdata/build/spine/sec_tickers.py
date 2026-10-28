"""``sec/company_tickers_exchange``: the SEC ticker and exchange file, weekly, with ``If-Modified-Since``.

``GET <RC_SEC_WWW_BASE>/files/company_tickers_exchange.json`` (``{"fields": ["cik", "name", "ticker", "exchange"],
"data": [[789019, "MICROSOFT CORP", "MSFT", "Nasdaq"], ...]}``) with the builder's User-Agent (the contact from
``RC_HTTP_CONTACT``; the SEC refuses a client without one). The request carries the ``Last-Modified`` of the newest
stored copy as ``If-Modified-Since``: a ``304`` writes nothing and the run is ``unchanged``. One request a run, inside
the sec day cap.

Raw: ``raw/sec/company_tickers_exchange/<as-of>.json.gz`` with a ``.meta.json`` sidecar (written first: the raw file
is the resume marker). Parquet: ``parquet/sec/company_tickers_exchange/date=<as-of>/part-0.parquet``, one snapshot per
fetch that changed, every snapshot kept (``security_history`` is built from them). Tickers are normalised as the
identity job does (``BRK.B`` -> ``BRK-B``); a repeated ticker keeps its first row. An empty or malformed body is a
failed run, never an empty snapshot.

Staging: ``stg_sec`` (the newest snapshot) and ``stg_sec_all`` (every snapshot).
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from ....core.identity.sources import norm_ticker
from .. import base
from . import KIND, stage

NAME = "sec/company_tickers_exchange"
PROVIDER = "sec"
PATH = "/files/company_tickers_exchange.json"
SCHEMA = [("ticker", "TEXT"), ("cik", "TEXT"), ("name", "TEXT"), ("exchange", "TEXT"), ("source_as_of", "DATE"),
          ("last_modified", "TEXT"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
META_SUFFIX = ".meta.json"


def url() -> str:
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_WWW_BASE.rstrip("/") + PATH


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "company_tickers_exchange"


def parquet_dir() -> Path:
    return base.parquet_root() / "sec" / "company_tickers_exchange"


def raw_path(as_of: date) -> Path:
    return raw_dir() / f"{as_of.isoformat()}.json.gz"


def meta_path(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def _newest_meta() -> dict | None:
    metas = sorted(raw_dir().glob(f"*.json.gz{META_SUFFIX}")) if raw_dir().is_dir() else []
    for m in reversed(metas):
        raw = m.with_name(m.name[: -len(META_SUFFIX)])
        if raw.exists():
            return base.read_raw_json(m)
    return None


def parse(body: dict) -> list[dict]:
    """The file's rows as ``{ticker, cik, name, exchange}``; ``SourceError`` for an empty or malformed body."""
    try:
        fields, data = body["fields"], body["data"]
        idx = {name: fields.index(name) for name in ("cik", "name", "ticker", "exchange")}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise base.SourceError(f"sec {PATH}: not the documented shape ({type(e).__name__}: {e})") from e
    rows: dict[str, dict] = {}
    for rec in data or []:
        ticker = norm_ticker(rec[idx["ticker"]])
        if not ticker or ticker in rows or rec[idx["cik"]] in (None, ""):
            continue
        exchange = rec[idx["exchange"]]
        rows[ticker] = {"ticker": ticker, "cik": str(int(rec[idx["cik"]])), "name": rec[idx["name"]],
                        "exchange": str(exchange).strip() if exchange not in (None, "") else None}
    if not rows:
        raise base.SourceError(f"sec {PATH}: the file came back empty")
    return list(rows.values())


class SecTickers:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = ()
    cadence = "weekly"
    backfill_needs_from = False

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        raw = raw_path(as_of)
        if raw.exists():
            return [raw]
        headers = {}
        prev = _newest_meta()
        if prev and prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]
        r = base.http_get(ctx, PROVIDER, url(), headers=headers or None)
        if r.status_code == 304:
            ctx.notes.append(f"not modified since {prev['last_modified']}")
            return []
        try:
            body = r.json()
        except ValueError as e:
            raise base.SourceError(f"sec {PATH}: the answer is not JSON") from e
        parse(body)                                       # refuse an empty or malformed file before it is kept
        base.write_raw_json(meta_path(raw), {"as_of": as_of.isoformat(), "fetched_at": time.time(),
                                             "run_id": ctx.run_id, "url": url(),
                                             "last_modified": r.headers.get("last-modified")})
        return [base.write_raw_json(raw, body)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # The SEC serves only today's file: a backfill is one fetch.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        out = []
        for raw in raws:
            meta = base.read_raw_json(meta_path(raw))
            as_of = date.fromisoformat(meta["as_of"])
            rows = [{**r, "source_as_of": as_of, "last_modified": meta.get("last_modified"),
                     "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]}
                    for r in parse(base.read_raw_json(raw))]
            final = parquet_dir() / f"date={as_of.isoformat()}" / "part-0.parquet"
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY ticker", final))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        parts = stage.partitions(parquet_dir(), "date")
        stage.create(con, "stg_sec_all", SCHEMA, [f for _, f in parts])
        stage.create(con, "stg_sec", SCHEMA, [parts[-1][1]] if parts else [])
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["ticker", "cik", "name", "exchange"]
        added, removed = stage.compare_live(con, "SELECT ticker, cik, name, exchange FROM security", "security", cols)
        as_of = con.execute("SELECT max(source_as_of) FROM stg_sec").fetchone()[0]
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, "stg_sec"), "added": added, "removed": removed}


DATASET = SecTickers()

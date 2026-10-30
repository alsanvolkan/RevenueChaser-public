"""``sec/company_tickers_mf``: the SEC fund file, the ETF universe's half of the spine, weekly.

``GET <RC_SEC_WWW_BASE>/files/company_tickers_mf.json`` (``{"fields": ["cik", "seriesId", "classId", "symbol"],
"data": [[2110, "S000009184", "C000024954", "LACAX"], ...]}``) — the equity file's shape with the fund file's four
fields, fetched the same way: the builder's User-Agent (the contact from ``RC_HTTP_CONTACT``), the ``Last-Modified``
of the newest stored copy as ``If-Modified-Since``, one request a run inside the sec day cap, a ``304`` that writes
nothing. Raw, Parquet and staging mirror ``sec_tickers`` exactly; the served table is ``fund``.

**Why the spine needs it.** ``security`` is built from ``company_tickers_exchange``, which lists only filers with
their own CIK and ticker. An ordinary ETF is a series of a trust, so it is not there: of the 34 ETFs the rotation
board syncs, three were in ``security`` when this was measured (30 Oct 2026). The fund file is where the other 31
are, one row per share class, with the series and class ids.

**The ETF rule, and that it is a rule.** The file carries no "this is an ETF" field. A symbol of five letters
ending in X is the classic open-end mutual-fund share class (22,797 of the 28,614 rows on 30 Oct 2026); dropping
those leaves 5,811 ETF candidates, the same order as a third-party count of US ETFs (5,394), which is the only
corroboration there is. So every row carries ``is_etf`` **with** ``is_etf_source`` (:data:`ETF_RULE`), never a bare
boolean, and the rule keeps its known false positives: a four-letter fund ending in X, and a five-letter ETF that
does not. A second signal — the ticker trading on an exchange, or the N-CEN ETF field — is a later step.

**The ETF universe is this table plus ``security``'s ``etp`` rows.** SPY is a unit investment trust and GLD a
commodity trust: neither has a series and a class, so neither is in the fund file. Both are rows of the equity file
(GLD, SLV and USO are its ``etp`` rows). A reader after the whole ETF universe reads both, never ``fund`` alone.

Malformed symbols exist (``(NWAGX)`` with its parentheses, ``n/a``, the empty string): they are normalised, and what
still does not parse is dropped and counted in the run's note, never guessed at.
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

import duckdb

from ....core import calendar
from ....core.identity.sources import norm_ticker
from .. import base
from . import KIND, stage

NAME = "sec/company_tickers_mf"
PROVIDER = "sec"
PATH = "/files/company_tickers_mf.json"
FIELDS = ("cik", "seriesId", "classId", "symbol")
#: What ``is_etf`` came from. A second signal gets its own value, so a reader can tell the two apart.
ETF_RULE = "length_rule_v1"
#: A ticker the spine will accept: what ``norm_ticker`` leaves of a symbol has to look like this or the row is
#: dropped. ``(NWAGX)``, ``n/a`` and the empty symbol are the file's real failures.
TICKER = re.compile(r"[A-Z0-9][A-Z0-9-]{0,9}")

SCHEMA = [("ticker", "TEXT"), ("ticker_raw", "TEXT"), ("cik", "TEXT"), ("series_id", "TEXT"), ("class_id", "TEXT"),
          ("is_etf", "BOOLEAN"), ("is_etf_source", "TEXT"), ("source_as_of", "DATE"), ("last_modified", "TEXT"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
#: The served table: the Parquet columns without ``last_modified``, which is the fetch's business, not a reader's.
FUND = [(c, t) for c, t in SCHEMA if c != "last_modified"]
TABLE = "fund"
META_SUFFIX = ".meta.json"


def url() -> str:
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_WWW_BASE.rstrip("/") + PATH


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "company_tickers_mf"


def parquet_dir() -> Path:
    return base.parquet_root() / "sec" / "company_tickers_mf"


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


def is_etf(ticker: str) -> bool:
    """The rule (:data:`ETF_RULE`), read off the **normalised** ticker: not a five-letter share class ending in X.

    The case matters: 25 of the file's symbols are written lower case, and reading the rule off the raw symbol
    keeps those five-letter share classes in the ETF set.
    """
    t = norm_ticker(ticker)
    return not (len(t) == 5 and t.isalpha() and t.endswith("X"))


def _parse(body: dict) -> tuple[list[dict], list[str]]:
    """``(rows, dropped)``: the file's rows typed, and the raw symbols that do not parse."""
    try:
        fields, data = body["fields"], body["data"]
        idx = {name: fields.index(name) for name in FIELDS}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise base.SourceError(f"sec {PATH}: not the documented shape ({type(e).__name__}: {e})") from e
    rows: dict[str, dict] = {}
    dropped: list[str] = []
    for rec in data or []:
        raw = rec[idx["symbol"]]
        ticker = norm_ticker(raw)
        class_id = str(rec[idx["classId"]] or "").strip()
        if not TICKER.fullmatch(ticker) or rec[idx["cik"]] in (None, ""):
            dropped.append("" if raw is None else str(raw))
            continue
        if class_id in rows:                                  # a class id is the file's key; keep its first row
            continue
        rows[class_id] = {"ticker": ticker, "ticker_raw": "" if raw is None else str(raw),
                          "cik": str(int(rec[idx["cik"]])), "series_id": str(rec[idx["seriesId"]] or "").strip(),
                          "class_id": class_id, "is_etf": is_etf(ticker), "is_etf_source": ETF_RULE}
    if not rows:
        raise base.SourceError(f"sec {PATH}: the file came back empty")
    return list(rows.values()), dropped


def parse(body: dict) -> list[dict]:
    """The file's rows as ``{ticker, ticker_raw, cik, series_id, class_id, is_etf, is_etf_source}``.

    ``SourceError`` for an empty or malformed body; a symbol that does not parse is dropped (``_parse`` counts it).
    """
    return _parse(body)[0]


def _create_fund(con: duckdb.DuckDBPyConnection) -> int:
    cols = ", ".join(f'"{c}" {t}' for c, t in FUND)
    names = ", ".join(f'"{c}"' for c, _ in FUND)
    con.execute(f"CREATE TABLE {TABLE} ({cols})")
    con.execute(f"INSERT INTO {TABLE} SELECT {names} FROM stg_sec_mf ORDER BY ticker, class_id")
    return stage.count(con, TABLE)


class SecTickersMf:
    name = NAME
    kind = KIND
    providers = (PROVIDER,)
    tables: tuple[str, ...] = (TABLE,)
    cadence = "weekly"
    backfill_needs_from = False
    starts_by_hand = True             # as every spine dataset: not due until a CLI run or a Fetch built it

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
        _parse(body)                                       # refuse an empty or malformed file before it is kept
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
            parsed, dropped = _parse(base.read_raw_json(raw))
            if dropped:
                ctx.notes.append(f"{len(dropped)} symbols did not parse and were dropped "
                                 f"(e.g. {', '.join(repr(d) for d in sorted(set(dropped))[:3])})")
            rows = [{**r, "source_as_of": as_of, "last_modified": meta.get("last_modified"),
                     "fetched_at": meta["fetched_at"], "run_id": meta["run_id"]} for r in parsed]
            final = parquet_dir() / f"date={as_of.isoformat()}" / "part-0.parquet"
            con = duckdb.connect()
            try:
                base.rows_table(con, "rows", SCHEMA, rows)
                out.append(base.write_parquet(con, "SELECT * FROM rows ORDER BY ticker, class_id", final))
            finally:
                con.close()
        return out

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        parts = stage.partitions(parquet_dir(), "date")
        stage.create(con, "stg_sec_mf_all", SCHEMA, [f for _, f in parts])
        stage.create(con, "stg_sec_mf", SCHEMA, [parts[-1][1]] if parts else [])
        return {TABLE: _create_fund(con)}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["ticker", "cik", "series_id", "class_id", "is_etf"]
        select = f"SELECT {', '.join(cols)} FROM stg_sec_mf"
        added, removed = stage.compare_live(con, select, TABLE, cols)
        as_of = con.execute("SELECT max(source_as_of) FROM stg_sec_mf").fetchone()[0]
        etfs = stage.count(con, "stg_sec_mf", "is_etf")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, "stg_sec_mf"), "added": added, "removed": removed,
                "note": f"{etfs} ETF candidates by {ETF_RULE}"}


DATASET = SecTickersMf()

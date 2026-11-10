"""``ibkr/news``: Dow Jones headlines from the IBKR Gateway, one pass an hour (#643 F5).

IBKR serves news per contract, so the names are the stock watch list (``ops.watch_list("stock")``, sorted) and then
``BASE_TICKERS``, at most ``MAX_TICKERS``; two requests a name (its contract id, then its ``DJ-N``/``DJ-RT``
headlines of the last ``WINDOW_S``). Headlines only, never the article body. A client id of its own (75): the IV
history client is 73 and the News board's own call 74, and the Gateway refuses a second client with one id (326).
A Gateway that is down is a ``failed`` check, which the hourly pass retries; a name it does not know is an issue.

One row a story and name: ``headline_id`` is made from the story's words (``DJ-N`` and ``DJ-RT`` carry one story
under two article ids), so the same story under two names is two rows with one id. IBKR's time is UTC
(``module_news/headlines.py`` reads it so; not checked against a known publish time, review M5).

Raw: ``raw/ibkr/news/<YYYY-MM-DD>/<HHMMSS>.json.gz`` (UTC fetch time; one file per fetch, so the hourly cadence is
not stopped by the day's first file). Parquet: ``parquet/ibkr/news/date=<YYYY-MM-DD>/part-0.parquet`` by the UTC
publish day, merged by key (the newest fetch wins a key). Serving: ``news_headline``, keyed
``(headline_id, ticker)``.

The name list, ``BASE_TICKERS`` and the headline cleaning are the News board's (``module_news/headlines.py``); the
builder does not import a board module, so they are copied here until the board reads this table (#643 PR 4).
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from ...core import calendar, ibkr_login
from ...core.providers import ibkr_tws
from .. import ops
from . import base, partitions
from . import dirty as dirty_set

NAME = "ibkr/news"
TABLE = "news_headline"
CLIENT_ID = 75
PROVIDERS = "DJ-N+DJ-RT"
SOURCE = "Dow Jones"
PER_TICKER = 20
MAX_TICKERS = 25
# shortcut: every pass asks the whole day back (the upsert makes the overlap harmless), 50 requests an hour; a
# mark from the newest stored headline would ask less, worth it only if the Gateway starts pacing these.
WINDOW_S = 24 * 3600
BASE_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "BRK.B", "LLY", "XOM")
SCHEMA = [("published_date", "DATE"), ("headline_id", "TEXT"), ("ticker", "TEXT"), ("published_at", "DOUBLE"),
          ("headline", "TEXT"), ("source", "TEXT"), ("provider", "TEXT"), ("source_as_of", "DATE"),
          ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
KEY = ["headline_id", "ticker"]
COMPARE = [c for c, _ in SCHEMA if c not in ("fetched_at", "run_id", "source_as_of")]

_META_PREFIX = re.compile(r"^\{[^}]*\}!?")
_IB_TIME = "%Y-%m-%d %H:%M:%S.0"


def tickers(watchlist) -> list[str]:
    """Watch-list names (sorted) first, then ``BASE_TICKERS``, no repeats, at most ``MAX_TICKERS``."""
    out: list[str] = []
    for t in [*sorted(str(x).upper() for x in watchlist or ()), *BASE_TICKERS]:
        if t and t not in out:
            out.append(t)
    return out[:MAX_TICKERS]


def clean(headline: str) -> str:
    """The headline without Dow Jones' ``{A:…:C:…}!`` metadata prefix."""
    return _META_PREFIX.sub("", str(headline or "")).strip()


def epoch(text) -> float | None:
    try:
        return datetime.strptime(str(text).strip(), _IB_TIME).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return None


def headline_id(text: str) -> str:
    return "ibkr:" + hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()[:16]


def day_dir(day: date) -> Path:
    return base.raw_root() / "ibkr" / "news" / day.isoformat()


def partition_path(day: date) -> Path:
    return base.parquet_root() / "ibkr" / "news" / f"date={day.isoformat()}" / "part-0.parquet"


class IBKRNews:
    name = NAME
    kind = "market"
    providers = ("ibkr",)
    tables = (TABLE,)
    cadence = "hourly"

    def default_as_of(self) -> date:
        return calendar._now().date()       # the UTC day: the partitions are UTC publish days

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        names = tickers(ops.watch_list("stock"))
        now = time.time()
        since = datetime.fromtimestamp(now - WINDOW_S, timezone.utc).strftime(_IB_TIME)
        host, port = ibkr_login.target()
        items, skipped = [], []
        try:
            with ibkr_tws.TWSClient(host, port, client_id=CLIENT_ID) as client:
                req = 0
                for name in names:
                    ctx.check_cap("ibkr")
                    ctx.count("ibkr", calls=2)
                    try:
                        req += 1
                        con_id = client.con_id(req, ibkr_tws.contract_for(name))
                        req += 1
                        got = client.news(req, con_id, PROVIDERS, since, "", PER_TICKER)
                    except ibkr_tws.HistoryError as e:
                        skipped.append(name)
                        ctx.issue("fetch_failed", name, str(e)[:300])
                        continue
                    items += [{**it, "ticker": name} for it in got]
        except ibkr_tws.TWSError as e:          # no session, or the gateway went away: nothing is written
            raise base.SourceError(f"IBKR gateway {host}:{port}: {e}") from e
        if names and len(skipped) == len(names):
            raise base.SourceError(f"IBKR gateway {host}:{port}: every name failed (first: {skipped[0]})")
        raw = day_dir(datetime.fromtimestamp(now, timezone.utc).date()) / \
            f"{datetime.fromtimestamp(now, timezone.utc):%H%M%S}.json.gz"
        return [base.write_raw_json(raw, {"names": names, "since": since, "providers": PROVIDERS,
                                          "skipped": skipped, "as_of": as_of.isoformat(), "fetched_at": now,
                                          "run_id": ctx.run_id, "items": items})]

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        as_of = date.fromisoformat(doc["as_of"])
        by_day: dict[date, list[dict]] = {}
        for it in doc.get("items") or []:
            text, at = clean(it.get("headline")), epoch(it.get("time"))
            if not text or at is None:
                continue
            day = datetime.fromtimestamp(at, timezone.utc).date()
            by_day.setdefault(day, []).append({
                "published_date": day, "headline_id": headline_id(text), "ticker": it["ticker"],
                "published_at": at, "headline": text, "source": SOURCE, "provider": it.get("provider"),
                "source_as_of": as_of, "fetched_at": doc["fetched_at"], "run_id": doc["run_id"]})
        # One partition write per day; a story listed twice under one name (DJ-N and DJ-RT) keeps one row.
        return [base.upsert_partition(rows, SCHEMA, KEY, partition_path(day)) for day, rows in sorted(by_day.items())]

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        return [p for raw in raws for p in self.to_parquet(ctx, raw)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        raise base.SourceError(f"{NAME} has no backfill: each pass already asks the last {WINDOW_S // 3600} hours")

    def dirty(self, ctx: base.RunContext):
        return dirty_set.keyed(ctx, self, "date")

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        """``partitions.load``, except that one story and name may sit in two day partitions (the same words for a
        name on two UTC days, or DJ-N and DJ-RT either side of midnight): the key is served once, from its first
        publish day. The 10 Nov 16:05 pass died on that primary key."""
        parts = {p.parent.name.split("=", 1)[1]: p
                 for p in (base.parquet_root() / "ibkr" / "news").glob("date=*/part-0.parquet")}
        keys = partitions.dirty_keys(ctx, NAME)
        if keys is None:
            cols = ", ".join(f'"{c}" {t}' for c, t in SCHEMA)
            con.execute(f"CREATE TABLE {TABLE} ({cols}, PRIMARY KEY ({', '.join(KEY)}))")
            files = [parts[k] for k in sorted(parts)]
        else:
            con.execute(f"DELETE FROM {TABLE} WHERE {partitions.where_any(keys, _where)}")
            files = [parts[k] for k in sorted(keys) if k in parts]
        if files:
            names = ", ".join(f'"{c}"' for c, _ in SCHEMA)
            on = " AND ".join(f't."{k}" = n."{k}"' for k in KEY)
            listed = ", ".join(base.sql_path(p) for p in files)
            con.execute(f"CREATE OR REPLACE TEMP TABLE _news_new AS SELECT {names} FROM read_parquet([{listed}], "
                        f"union_by_name = true) QUALIFY row_number() OVER (PARTITION BY {', '.join(KEY)} "
                        f"ORDER BY published_at, fetched_at DESC) = 1")
            # On the live copy a key may already be served from another day: the earlier day wins either way, as in
            # a whole build. (Days of one key never tie: one day's partition holds a key once.)
            con.execute(f"DELETE FROM {TABLE} t USING _news_new n WHERE {on} AND t.published_at > n.published_at")
            con.execute(f"INSERT INTO {TABLE} SELECT {names} FROM _news_new n "
                        f"WHERE NOT EXISTS (SELECT 1 FROM {TABLE} t WHERE {on})")
            con.execute("DROP TABLE _news_new")
        return {TABLE: int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = partitions.compare(ctx, con, self.kind, NAME, TABLE, COMPARE, _where)
        rows = int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
        as_of = ctx.as_of or self.default_as_of()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows, "added": added, "removed": removed,
                "coverage": None}


def _where(key: str) -> str:
    return f"published_date = DATE '{date.fromisoformat(key).isoformat()}'"


DATASET = IBKRNews()

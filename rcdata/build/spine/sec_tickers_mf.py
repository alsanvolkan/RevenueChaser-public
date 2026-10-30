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
boolean. A second signal — the ticker trading on an exchange, or the N-CEN ETF field — is a later step.

The rule is wrong in both directions, and a reader filtering on ``is_etf`` can see only one of them:

- **``is_etf`` true for a fund** — a fund whose symbol is not five letters ending in X: four letters and an X
  (``AAAX``), or five letters and no X (``QINSQ``). A reader meets these, so they can be told apart later.
- **``is_etf`` false for an ETF** — an ETF whose symbol *is* five letters ending in X. This one is invisible: the
  row is in ``fund`` but outside every ``is_etf`` filter, so nothing downstream can notice it. It is the error to
  weigh when the second signal is chosen, and the reason the rule is recorded rather than applied silently.

**The ETF universe is this table plus ``security``'s ``etp`` rows.** SPY is a unit investment trust and GLD a
commodity trust: neither has a series and a class, so neither is in the fund file. Both are rows of the equity file
(GLD, SLV and USO are its ``etp`` rows). A reader after the whole ETF universe reads both, never ``fund`` alone.

Malformed symbols exist (``(NWAGX)`` with its parentheses, ``n/a``, the empty string): they are normalised, and what
still does not parse is dropped and counted in the run's note, never guessed at.

**``fund`` is the kind's first source-owned served table, and its shrink guard is narrower than the others'.** If
this dataset's Parquet tree goes missing while ``fund`` is live and full, the build makes it empty and
``serving._verify_against_live`` refuses the swap — which is the guard working, and the live file is untouched. But
``base.shrink_scope`` relaxes a kind's *derived* tables for every dataset of the kind and a source's own table only
for that source, so the refusal stands for every spine dataset, the daily ``funds/index_holdings`` included, and
only a request on this dataset can clear it with ``allow_shrink``. Fetching this dataset again is the ordinary way
out; widening the hatch means letting the datasets declare which tables each source feeds, which is the upgrade
``shrink_scope``'s own docstring already anticipates and is not this PR's to make (#326).

``stg_sec_mf_all`` has **no reader yet.** The equity file keeps every snapshot because ``security_history`` is built
from them; nothing is built from these yet, and the obvious consumer — a share class that changed ticker or moved
series — is not written. It is here because the brief asked for the equity file's shape exactly, and it costs one
load of every stored snapshot (about 28.6k rows a week) on every spine build. Drop it or give it a reader; do not
leave it to grow on the strength of the symmetry alone.
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
#: dropped. Measured over the real file 30 Oct 2026: a letter first, then one to six letters or digits, so two to
#: seven characters (the file's lengths are 2 to 7, and ``PCOMRSI`` is a real seven); no symbol in it carries a dot
#: or a hyphen, but a share class could gain one the way ``BRK.B`` does, so the equity file's suffix is allowed
#: (``funds.py`` has the same shape one character shorter). This accepts all 28,608 real symbols and rejects
#: exactly the file's six failures: ``(NWAGX)`` and its three siblings, ``n/a``, and the empty symbol.
TICKER = re.compile(r"[A-Z][A-Z0-9]{1,6}(-[A-Z0-9]{1,3})?")

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


#: Why a row was dropped, in the order the reasons are read. Each one is counted on its own, because they are
#: different problems: a bad symbol is the file's known untidiness, a row with no CIK or no class id is not.
DROP_REASONS = ("the symbol does not parse", "no CIK", "no class id")


def _parse(body: dict) -> tuple[list[dict], dict[str, list[str]]]:
    """``(rows, dropped)``: the file's rows typed, and the raw symbols dropped, by reason (:data:`DROP_REASONS`).

    A class id is the file's key, so a repeated one keeps its first row — but a **blank** one is not a key, and
    keying on it would collapse every such row into one and say nothing about it, so it is a drop with its own
    count. The real file has 28,614 distinct class ids and none blank; this is the guard, not a fix for today.
    """
    try:
        fields, data = body["fields"], body["data"]
        idx = {name: fields.index(name) for name in FIELDS}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise base.SourceError(f"sec {PATH}: not the documented shape ({type(e).__name__}: {e})") from e
    rows: dict[str, dict] = {}
    dropped: dict[str, list[str]] = {reason: [] for reason in DROP_REASONS}
    for rec in data or []:
        raw = rec[idx["symbol"]]
        symbol = "" if raw is None else str(raw)
        ticker = norm_ticker(raw)
        class_id = str(rec[idx["classId"]] or "").strip()
        if not TICKER.fullmatch(ticker):
            dropped[DROP_REASONS[0]].append(symbol)
        elif rec[idx["cik"]] in (None, ""):
            dropped[DROP_REASONS[1]].append(symbol)
        elif not class_id:
            dropped[DROP_REASONS[2]].append(symbol)
        elif class_id not in rows:                            # a repeated class id keeps its first row
            rows[class_id] = {"ticker": ticker, "ticker_raw": symbol,
                              "cik": str(int(rec[idx["cik"]])), "series_id": str(rec[idx["seriesId"]] or "").strip(),
                              "class_id": class_id, "is_etf": is_etf(ticker), "is_etf_source": ETF_RULE}
    if not rows:
        raise base.SourceError(f"sec {PATH}: the file came back empty")
    return list(rows.values()), dropped


def drop_note(dropped: dict[str, list[str]]) -> str | None:
    """One line naming each reason that dropped a row, with a count and an example; None when nothing was dropped."""
    parts = [f"{len(syms)} {reason} (e.g. {', '.join(repr(s) for s in sorted(set(syms))[:3])})"
             for reason, syms in dropped.items() if syms]
    return "dropped: " + "; ".join(parts) if parts else None


def parse(body: dict) -> list[dict]:
    """The file's rows as ``{ticker, ticker_raw, cik, series_id, class_id, is_etf, is_etf_source}``.

    ``SourceError`` for an empty or malformed body; a row the rules above drop is counted by ``_parse``.
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
            note = drop_note(dropped)
            if note:
                ctx.notes.append(note)
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
        # The served table, not its staging: the day anything filters in ``_create_fund``, added/removed have to
        # describe what was served. ``is_etf_source`` is in the compare because the run that moves the rule to its
        # next value is the one run that most needs to read ``changed``.
        cols = ["ticker", "cik", "series_id", "class_id", "is_etf", "is_etf_source"]
        select = f"SELECT {', '.join(cols)} FROM {TABLE}"
        added, removed = stage.compare_live(con, select, TABLE, cols)
        as_of = con.execute(f"SELECT max(source_as_of) FROM {TABLE}").fetchone()[0]
        etfs = stage.count(con, TABLE, "is_etf")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (as_of or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, TABLE), "added": added, "removed": removed,
                "note": f"{etfs} ETF candidates by {ETF_RULE}"}


DATASET = SecTickersMf()

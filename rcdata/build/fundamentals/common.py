"""What the fundamentals datasets share: the Tier A companies, the SEC's User-Agent and pace, a GET that can say
"nothing there", staging from a Parquet tree, and the comparison with the live file.

**The companies** are avo's own list from the live spine (``my_universe``, its ``stock`` rows), or every Tier A row
where the spine holds no such list (:func:`universe`); read when a run starts and again when the kind is built (no
network: ``spine.duckdb`` is a local file). No spine file yet means no companies, so a fetch asks nothing and the
build serves empty tables.

**The SEC.** Every SEC request carries ``RC_SEC_USER_AGENT`` ("Name email", the SEC's rule) when it is set, else the
builder's own User-Agent with ``RC_HTTP_CONTACT``; each request takes a token of ``core/sec_throttle.py``'s
box-wide bucket (every SEC caller's, 9 a second against the SEC's 10, ``caps.WINDOWS``); when it cannot be used the
requests are spaced ``SEC_INTERVAL_S`` apart, this process's share (``sec_throttle.FALLBACK_RATE``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import httpx

from ....core import sec_throttle
from .. import base
from . import KIND

SEC = "sec"
SEC_UA_ENV = "RC_SEC_USER_AGENT"
SEC_INTERVAL_S = 1 / sec_throttle.FALLBACK_RATE   # this process's share of the SEC's 10/s if the bucket falls back
META_SUFFIX = ".meta.json"
_DIGITS = re.compile(r"^\d{1,10}$")


def sec_headers() -> dict[str, str]:
    """The SEC's User-Agent: ``RC_SEC_USER_AGENT`` when set, else the builder's (with ``RC_HTTP_CONTACT``)."""
    ua = (os.getenv(SEC_UA_ENV) or "").strip()
    return {"User-Agent": ua or base.user_agent()}


def sec_www() -> str:
    """``https://www.sec.gov`` or its test seam ``RC_SEC_WWW_BASE`` (read at call time)."""
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_WWW_BASE.rstrip("/")


def sec_data() -> str:
    """``https://data.sec.gov`` or its test seam ``RC_SEC_DATA_BASE`` (read at call time)."""
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_DATA_BASE.rstrip("/")


def cik10(cik) -> str:
    return f"{int(cik):010d}"


def norm_cik(value) -> str | None:
    """A CIK as the spine writes it (``789019``), from ``0000789019``, ``789019`` or an int; None otherwise."""
    text = str(value if value is not None else "").strip()
    return str(int(text)) if _DIGITS.match(text) else None


class Spacing:
    """One token of ``shared`` (the box-wide SEC bucket) before each call when it is given and can be used; otherwise
    at least ``interval`` seconds between two calls."""

    def __init__(self, interval: float, clock: Callable[[], float], sleep: Callable[[float], Any],
                 shared: Callable[[], Any] | None = None):
        self.interval, self._clock, self._sleep, self._shared = interval, clock, sleep, shared
        self._last: float | None = None

    def wait(self) -> None:
        on_bucket = self._shared is not None and self._shared()
        if not on_bucket and self._last is not None:
            gap = self.interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()


def sec_spacing(ctx: base.RunContext) -> Spacing:
    """One spacing per run, shared by every SEC dataset of the run (``ctx.pending``)."""
    spacing = ctx.pending.get("_sec_spacing")
    if spacing is None:
        spacing = ctx.pending["_sec_spacing"] = Spacing(SEC_INTERVAL_S, ctx.clock, ctx.sleep,
                                                         shared=lambda: sec_throttle.pace())
    return spacing


def get(ctx: base.RunContext, provider: str, url: str, *, params: dict | None = None, headers: dict | None = None,
        spacing: Spacing | None = None, missing: Iterable[int] = (), missing_object: bool = False,
        keyed: bool = False) -> httpx.Response | None:
    """GET with ``base.http_get``'s retry ladder and counting; a status in ``missing`` returns None (nothing there:
    a holiday's daily index, a company with no XBRL facts). Raises ``SourceError`` like ``http_get``.

    ``missing_object``: an answer that says the host holds no object for the URL (``base.is_missing``: a 404, or a
    bucket's XML ``AccessDenied``) returns None as well. An ``/Archives/`` URL needs this, because that bucket
    answers a missing object with a 403 and a plain 403 is a refusal that has to raise.

    ``params`` and ``headers`` may hold a key: neither is ever logged. ``keyed``: the request carries the provider's
    key (Alpha Vantage's ``apikey``), so a 403 points at the key and not at ``RC_HTTP_CONTACT``.
    """
    missing = set(missing)
    last = ""
    for attempt in range(1, base.HTTP_ATTEMPTS + 1):
        ctx.check_cap(provider)
        if spacing is not None:
            spacing.wait()
        ctx.count(provider, calls=1)
        try:
            r = ctx.http.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            last = type(e).__name__
        else:
            if r.status_code in missing or (missing_object and base.is_missing(r)):
                return None
            if r.status_code == 429:
                ctx.count(provider, http_429=1)
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code}" + (base.refusal_note(keyed) if r.status_code == 403 else "")
            if not (r.status_code == 429 or r.status_code >= 500):
                break
        if attempt < base.HTTP_ATTEMPTS:
            ctx.sleep(base.HTTP_BACKOFF_S * 2 ** (attempt - 1))
    ctx.count(provider, failures=1)
    raise base.SourceError(f"{provider} {url}: {last}")


def sec_get(ctx: base.RunContext, url: str, *, missing: Iterable[int] = (),
            missing_object: bool = False) -> httpx.Response | None:
    """``get`` for an SEC URL: the SEC's User-Agent and pace, and a 403 that points at ``RC_HTTP_CONTACT``."""
    return get(ctx, SEC, url, headers=sec_headers(), spacing=sec_spacing(ctx), missing=missing,
               missing_object=missing_object)


# ── The companies ─────────────────────────────────────────────────────────────────────────────────────────────

#: The tiers read when avo's list decides which companies to ask about: it may name a Tier D company (no exchange
#: in the SEC file), and the list is then the only filter. Without a list, Tier A alone, the old rule.
LIST_TIERS = ("A", "B", "C", "D")


def universe() -> list[dict]:
    """The companies this kind asks about, as ``security`` rows of the live spine, by ticker; [] before the first
    spine build.

    avo's own list when the live spine holds one (``my_universe``, avo 5 Nov 2026): its ``kind`` ``stock`` rows,
    the same names the boards that read ``fundamentals.duckdb`` screen (``core/universe.py``). A fund is never
    asked about -- an ETF has no statements to ask the SEC for -- and a company the list leaves out costs no fetch.
    Without that table, a spine built before schema 5, the old rule: every Tier A company.

    Either way a row needs a CIK, because every SEC dataset of the kind is keyed by one.
    """
    from ... import read
    kept = read.my_universe_tickers(("stock",))
    rows = read.universe(("A",) if kept is None else LIST_TIERS)
    if kept is not None:
        rows = [r for r in rows if r["ticker"] in kept]
    return [r for r in rows if norm_cik(r.get("cik"))]


def ciks(rows: Iterable[dict]) -> list[str]:
    """The distinct CIKs of ``rows``, in number order."""
    return sorted({c for r in rows if (c := norm_cik(r.get("cik")))}, key=int)


def tickers_by_cik(rows: Iterable[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in rows:
        cik = norm_cik(r.get("cik"))
        if cik:
            out.setdefault(cik, []).append(r["ticker"])
    return {c: sorted(t) for c, t in out.items()}


# ── Files ─────────────────────────────────────────────────────────────────────────────────────────────────────

def raw_dir(dataset: str) -> Path:
    """``raw/<source>/<name>`` of dataset ``<source>/<name>``."""
    return base.raw_root().joinpath(*dataset.split("/", 1))


def parquet_dir(dataset: str) -> Path:
    return base.parquet_root().joinpath(*dataset.split("/", 1))


def meta_path(raw: Path) -> Path:
    return raw.with_name(raw.name + META_SUFFIX)


def stage_glob(con: duckdb.DuckDBPyConnection, name: str, schema: list[tuple[str, str]], folder: Path,
               pattern: str, select: str | None = None, ciks: Iterable[str] | None = None) -> int:
    """``CREATE TEMP TABLE name`` with ``schema`` and load every Parquet file under ``folder`` matching ``pattern``
    (one ``read_parquet`` glob, not a list of thousands of paths); its row count. ``select`` shapes the load over
    ``{src}``, as in ``spine.stage.create``. No file yet: an empty, typed table.

    ``ciks`` (a build on the live copy, :func:`dirty_ciks`) loads only those companies' partitions: the
    ``cik=*`` of ``pattern`` is each of them in turn."""
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({cols})")
    names = ", ".join(f'"{c}"' for c, _ in schema)
    if ciks is not None:
        if "cik=*" not in pattern:
            raise ValueError(f"{pattern!r} is not partitioned by company")
        files = [p for c in sorted(ciks) if folder.is_dir()
                 for p in sorted(folder.glob(pattern.replace("cik=*", f"cik={c}")))]
        if files:
            src = f"read_parquet([{', '.join(base.sql_path(p) for p in files)}], union_by_name = true)"
            body = select.replace("{src}", src) if select else f"SELECT {names} FROM {src}"
            con.execute(f"INSERT INTO {name} SELECT {names} FROM ({body})")
        return count(con, name)
    if folder.is_dir() and next(folder.glob(pattern), None) is not None:
        src = f"read_parquet({base.sql_path(folder / pattern)}, union_by_name = true)"
        body = select.replace("{src}", src) if select else f"SELECT {names} FROM {src}"
        con.execute(f"INSERT INTO {name} SELECT {names} FROM ({body})")
    return count(con, name)


def count(con: duckdb.DuckDBPyConnection, table: str, where: str = "") -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")).fetchone()[0])


def compare_live(con: duckdb.DuckDBPyConnection, select_new: str, table: str, columns: list[str],
                 where: str = "", dirty_only: bool = False) -> tuple[int, int]:
    """``(added, removed)``: rows of ``select_new`` (over the build connection) against ``table`` of the live
    ``fundamentals.duckdb`` on ``columns``, the live rows filtered by ``where``. No live file or table: all added.

    ``dirty_only`` (a build on the live copy) compares only the dirty companies' rows (:data:`DIRTY_TABLE`): every
    other company's rows are the live file's own, so they cannot differ."""
    if dirty_only:
        only = f"cik IN (SELECT cik FROM {DIRTY_TABLE})"
        select_new = f"SELECT * FROM ({select_new}) WHERE {only}"
        where = f"({where}) AND {only}" if where else only
    from ... import serving
    cols = ", ".join(f'"{c}"' for c in columns)
    new_rows = int(con.execute(f"SELECT count(*) FROM ({select_new})").fetchone()[0])
    live = serving.live_path(KIND)
    if not live.exists():
        return new_rows, 0
    con.execute(f"ATTACH {base.sql_path(live)} AS live_fund (READ_ONLY)")
    try:
        known = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'live_fund'").fetchall()}
        if table not in known:
            return new_rows, 0
        old = f'SELECT {cols} FROM live_fund."{table}"' + (f" WHERE {where}" if where else "")
        new = f"SELECT {cols} FROM ({select_new})"
        added = con.execute(f"SELECT count(*) FROM ({new} EXCEPT ALL {old})").fetchone()[0]
        removed = con.execute(f"SELECT count(*) FROM ({old} EXCEPT ALL {new})").fetchone()[0]
        return int(added), int(removed)
    finally:
        con.execute("DETACH live_fund")


# ── #446: the build on the live copy ─────────────────────────────────────────────────────────────────────────

#: The datasets partitioned by company (``cik=<cik>``). On a build over the live copy their staging holds only the
#: dirty companies, and every served row of those companies is replaced; every other dataset is small and is
#: reloaded whole (``tables.py``).
KEYED = ("sec/companyfacts", "sec/filings", "uw/financials")
#: The build connection's TEMP table of the dirty companies, one ``cik`` a row.
DIRTY_TABLE = "_dirty_cik"


def dirty_ciks(ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> frozenset[str] | None:
    """The companies a build on the live copy reloads; None for a whole build.

    The keys of the changed partitions of every :data:`KEYED` dataset, and every company of the universe the live
    file serves no row of (one that joined the list: its partitions may be old, but it has no company row yet).
    Worked out once per build (the plan's memo) and written to :data:`DIRTY_TABLE`."""
    plan = getattr(ctx, "incremental", None)
    if plan is None:
        return None
    if "ciks" not in plan.memo:
        keys: set[str] = set()
        for name in KEYED:
            d = plan.dirty.get(name)
            if d is not None:
                keys |= set(d.keys)
        served = {r[0] for r in con.execute("SELECT DISTINCT cik FROM fundamental_company").fetchall()}
        if plan.fallback:
            keys |= _gone_companies(con)
        keys |= {c for c in ciks(universe()) if c not in served}
        con.execute(f"CREATE OR REPLACE TEMP TABLE {DIRTY_TABLE} (cik TEXT PRIMARY KEY)")
        if keys:
            con.execute(f"INSERT INTO {DIRTY_TABLE} SELECT unnest(?)", (sorted(keys),))
        plan.memo["ciks"] = frozenset(keys)
    return plan.memo["ciks"]


def _gone_companies(con: duckdb.DuckDBPyConnection) -> set[str]:
    """The companies the live copy serves rows of from a keyed source whose ``cik=<cik>`` folder is gone.

    Only for a live file built before the manifest: its ``built_at`` cannot show a deleted partition, and the
    manifest this build writes would never show it either (review of #554). A folder, not a single file, is what
    goes in practice. shortcut: a lone file deleted inside a folder that stays (``documents.parquet``) between the
    live file's build and this one is not seen, now or later; it can only happen once, on the first build after
    the manifest landed, and ``RC_BUILD_WHOLE=1`` clears it. Upgrade only if a source starts deleting single files."""
    from . import companyfacts, filings, uw_financials
    served = {
        companyfacts.NAME: f"SELECT DISTINCT cik FROM fundamental_fact WHERE source = '{facts_source()}'",
        filings.NAME: "SELECT DISTINCT cik FROM filing_item",
        uw_financials.NAME: f"SELECT DISTINCT cik FROM fundamental_fact WHERE source = '{uw_financials.NAME}'",
    }
    gone: set[str] = set()
    for name, sql in served.items():
        folder = parquet_dir(name)
        gone |= {r[0] for r in con.execute(sql).fetchall() if not (folder / f"cik={r[0]}").is_dir()}
    return gone


def facts_source() -> str:
    from . import facts
    return facts.SOURCE


def staged_value(ctx: base.RunContext, con: duckdb.DuckDBPyConnection, staging: str, folder: Path, pattern: str,
                 expr: str, select: str | None = None):
    """``expr`` (``max(source_as_of)``, ``count(*)``) over a keyed source's whole staging: the staging table itself
    on a whole build, the whole Parquet tree on a build over the live copy (whose staging holds only the dirty
    companies). ``select`` shapes the load as in :func:`stage_glob`."""
    if dirty_ciks(ctx, con) is None:
        return con.execute(f"SELECT {expr} FROM {staging}").fetchone()[0]
    if not (folder.is_dir() and next(folder.glob(pattern), None) is not None):
        return con.execute(f"SELECT {expr} FROM {staging} WHERE false").fetchone()[0]
    src = f"read_parquet({base.sql_path(folder / pattern)}, union_by_name = true)"
    body = select.replace("{src}", src) if select else f"SELECT * FROM {src}"
    return con.execute(f"SELECT {expr} FROM ({body})").fetchone()[0]

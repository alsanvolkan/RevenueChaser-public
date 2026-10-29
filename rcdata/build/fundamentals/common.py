"""What the fundamentals datasets share: the Tier A companies, the SEC's User-Agent and pace, a GET that can say
"nothing there", staging from a Parquet tree, and the comparison with the live file.

**The companies** are the Tier A rows of the live spine (``rcdata.read.universe``), read when a run starts and again
when the kind is built (no network: ``spine.duckdb`` is a local file). No spine file yet means no companies, so a
fetch asks nothing and the build serves empty tables.

**The SEC.** Every SEC request carries ``RC_SEC_USER_AGENT`` ("Name email", the SEC's rule) when it is set, else the
builder's own User-Agent with ``RC_HTTP_CONTACT``; requests are spaced ``SEC_INTERVAL_S`` apart, under 5 a second
(the plan's limit; the SEC's own is 10), and each is counted against the ``sec`` day cap.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import httpx

from .. import base
from . import KIND

SEC = "sec"
SEC_UA_ENV = "RC_SEC_USER_AGENT"
SEC_INTERVAL_S = 0.25
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
    """At least ``interval`` seconds between two calls: ``wait()`` before each one sleeps the rest of the gap."""

    def __init__(self, interval: float, clock: Callable[[], float], sleep: Callable[[float], Any]):
        self.interval, self._clock, self._sleep = interval, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()


def sec_spacing(ctx: base.RunContext) -> Spacing:
    """One spacing per run, shared by every SEC dataset of the run (``ctx.pending``)."""
    spacing = ctx.pending.get("_sec_spacing")
    if spacing is None:
        spacing = ctx.pending["_sec_spacing"] = Spacing(SEC_INTERVAL_S, ctx.clock, ctx.sleep)
    return spacing


def get(ctx: base.RunContext, provider: str, url: str, *, params: dict | None = None, headers: dict | None = None,
        spacing: Spacing | None = None, missing: Iterable[int] = ()) -> httpx.Response | None:
    """GET with ``base.http_get``'s retry ladder and counting; a status in ``missing`` returns None (nothing there:
    a holiday's daily index, a company with no XBRL facts). Raises ``SourceError`` like ``http_get``.

    ``params`` and ``headers`` may hold a key: neither is ever logged.
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
            if r.status_code in missing:
                return None
            if r.status_code == 429:
                ctx.count(provider, http_429=1)
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code}"
            if not (r.status_code == 429 or r.status_code >= 500):
                break
        if attempt < base.HTTP_ATTEMPTS:
            ctx.sleep(base.HTTP_BACKOFF_S * 2 ** (attempt - 1))
    ctx.count(provider, failures=1)
    raise base.SourceError(f"{provider} {url}: {last}")


def sec_get(ctx: base.RunContext, url: str, *, missing: Iterable[int] = ()) -> httpx.Response | None:
    return get(ctx, SEC, url, headers=sec_headers(), spacing=sec_spacing(ctx), missing=missing)


# ── The companies ─────────────────────────────────────────────────────────────────────────────────────────────

def universe() -> list[dict]:
    """The Tier A ``security`` rows of the live spine, by ticker; [] before the first spine build."""
    from ... import read
    return [r for r in read.universe(("A",)) if norm_cik(r.get("cik"))]


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
               pattern: str, select: str | None = None) -> int:
    """``CREATE TEMP TABLE name`` with ``schema`` and load every Parquet file under ``folder`` matching ``pattern``
    (one ``read_parquet`` glob, not a list of thousands of paths); its row count. ``select`` shapes the load over
    ``{src}``, as in ``spine.stage.create``. No file yet: an empty, typed table."""
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({cols})")
    if folder.is_dir() and next(folder.glob(pattern), None) is not None:
        names = ", ".join(f'"{c}"' for c, _ in schema)
        src = f"read_parquet({base.sql_path(folder / pattern)}, union_by_name = true)"
        body = select.replace("{src}", src) if select else f"SELECT {names} FROM {src}"
        con.execute(f"INSERT INTO {name} SELECT {names} FROM ({body})")
    return count(con, name)


def count(con: duckdb.DuckDBPyConnection, table: str, where: str = "") -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")).fetchone()[0])


def compare_live(con: duckdb.DuckDBPyConnection, select_new: str, table: str, columns: list[str],
                 where: str = "") -> tuple[int, int]:
    """``(added, removed)``: rows of ``select_new`` (over the build connection) against ``table`` of the live
    ``fundamentals.duckdb`` on ``columns``, the live rows filtered by ``where``. No live file or table: all added."""
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

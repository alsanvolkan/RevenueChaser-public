"""``uw/spot_expiry_strike``: SPX spot exposures by expiry and strike from Unusual Whales, one call per session.

``GET /stock/SPX/spot-exposures/expiry-strike?expirations[]=<session>&expirations[]=<next session>`` (``expirations[]``
is required; UW serves this from 16 Jan 2025). Every call passes ``date=<session>`` and that session's two
nearest expiries; a row whose ``time`` is not on the session's New York date is refused. The daily run between the
close and the next open is ``observed``; the backfill and any other run are not. One call answers one row per strike
and expiry: 488 on the live check of 27 Oct 2026 (241 and 247), under UW's maximum page of 500, so an ordinary
session is one call. A busier one (OPEX, most Thursdays and Fridays: 575 rows on 21 Mar 2025, 683 on 15 Oct 2026)
fills the page and is asked for ``page=1, 2, ...`` (0-based, same ``limit``) until a page is shorter than 500; the
day is kept only when every page arrived, and fails at ``MAX_PAGES`` full pages rather than loop.

Raw: ``raw/uw/spot_expiry_strike/<YYYY-MM-DD>/SPX.json.gz`` (UW's body with the request and its provenance). A
one-page day keeps UW's body as sent; a paged day keeps ``{"data": <every page's rows, in order>}`` and the row count
of each page in ``request.pages``. A key repeated across pages fails the day (the answer moved while paging). Either way one file per day, written once all its pages arrived.
Parquet: ``parquet/uw/spot_expiry_strike/date=<YYYY-MM-DD>/part-0.parquet``. Serving: ``spx_gamma_expiry_strike``.

``dte`` counts NYSE sessions from the session to the expiry (``core/calendar.py``), so a Friday's next expiry,
the Monday, is 1 DTE.
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from ...core import calendar
from ...core.config import to_float
from . import base

NAME = "uw/spot_expiry_strike"
TABLE = "spx_gamma_expiry_strike"
TICKER = "SPX"
ENDPOINT = f"/stock/{TICKER}/spot-exposures/expiry-strike"
PAGE_LIMIT = 500        # UW's documented maximum for this endpoint (docs/openapi.yaml, "Default 500 Max 500 Min 1")
# Pages per session before the day fails instead of asking on: the busiest day seen live was two pages (683 rows on
# 15 Oct 2026), so 20 pages (10,000 rows) is far past any real day and only stops an answer that never ends.
MAX_PAGES = 20
# The backfill's call estimate per missing session (Settings). An ordinary day is one call and a paged one two or
# three; the days a first backfill leaves missing are the paged ones (168 of 447 on homesrv, 28 Oct 2026), so two.
CALLS_PER_SESSION_ESTIMATE = 2
FIRST_SESSION = date(2025, 1, 16)
REJECTED_SUFFIX = ".rejected"
# An epoch below this (9 Sep 2001) is not a UW snapshot time: 0, or a date written without dashes read as seconds.
MIN_EPOCH = 1e9

# Every numeric field UW documents for this answer (docs/openapi.yaml, "Spot greek exposures by strike"): the four
# greeks, each for calls and puts, by open interest, volume, bid and ask; and ``price``, the spot of the snapshot.
# A numeric field UW adds later is reported as a build issue until a serving-schema bump gives it a column.
GREEK_FIELDS = tuple(f"{side}_{greek}_{basis}" for greek in ("gamma", "delta", "charm", "vanna")
                     for side in ("call", "put") for basis in ("oi", "vol", "bid", "ask"))
NUMERIC_FIELDS = GREEK_FIELDS + ("price",)
_NOT_EXTRA = {"expiry", "strike", "time", "date", "ticker"}

SCHEMA = ([("session_date", "DATE"), ("expiry", "DATE"), ("strike", "DOUBLE"), ("dte", "INTEGER")]
          + [(f, "DOUBLE") for f in NUMERIC_FIELDS]
          + [("snapshot_time", "TEXT"), ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
             ("run_id", "TEXT"), ("observed", "BOOLEAN")])
KEY = ("session_date", "expiry", "strike")
# What makes a row "changed": its data, not when or by which run it was fetched.
COMPARE = [c for c, _ in SCHEMA if c not in ("fetched_at", "run_id", "source_as_of")]


def raw_path(session: date) -> Path:
    return base.raw_root() / "uw" / "spot_expiry_strike" / session.isoformat() / f"{TICKER}.json.gz"


def partition_path(session: date) -> Path:
    return base.parquet_root() / "uw" / "spot_expiry_strike" / f"date={session.isoformat()}" / "part-0.parquet"


def dte(session: date, expiry: date) -> int:
    n, d = 0, session
    while d < expiry:
        d = calendar.next_trading_day(d)
        n += 1
    return n


class UWSpotExpiryStrike:
    name = NAME
    kind = "market"
    providers = ("uw",)
    tables = (TABLE,)

    def default_as_of(self) -> date:
        return calendar.latest_complete_session()

    # ── fetch ──
    def _fetch_session(self, ctx: base.RunContext, session: date, *, observed: bool) -> Path:
        # A session that has not finished (an explicit --as-of or backfill --to of today) would be stored as UW's
        # intraday snapshot for good: its raw file is the resume marker (review S-2 of #220). No call is sent.
        latest = calendar.latest_complete_session()
        if session > latest:
            raise base.SourceError(f"UW {ENDPOINT} {session}: the session is not complete yet (latest complete "
                                   f"session {latest}); not fetched")
        raw = raw_path(session)
        if raw.exists():
            return raw
        expiries = [session, calendar.next_trading_day(session)]
        # Every call names its session (B1): without date=, UW answers its newest snapshot, which during the next
        # session is that session's intraday one.
        params: dict = {"expirations[]": [e.isoformat() for e in expiries], "date": session.isoformat(),
                        "limit": PAGE_LIMIT}
        body = self._get_page(ctx, session, params, 0)
        rows = body.get("data", []) if isinstance(body, dict) else body
        if not isinstance(rows, list) or not rows:
            raise base.SourceError(f"UW {ENDPOINT} {session}: no rows")
        if not any(isinstance(r, dict) and snapshot_day(r.get("time")) in (session, None) for r in rows):
            raise base.SourceError(f"UW {ENDPOINT}: every row is stamped on a day not on {session}")
        request: dict = {"endpoint": ENDPOINT, "params": params}
        if len(rows) >= PAGE_LIMIT:
            # A full page may be a cut day (G3): ask the next pages until one is shorter than the limit (an empty
            # one after a page of exactly 500). Only a day whose every page arrived, each a list of rows on this
            # session and none repeating an earlier page's key, is written; the raw file is the resume marker, so a
            # failed page, a moved answer or the ceiling leaves no partial day.
            sizes, pages = [len(rows)], [rows]
            while sizes[-1] >= PAGE_LIMIT:
                if len(sizes) >= MAX_PAGES:
                    ctx.issue("truncated", session.isoformat(),
                              f"{MAX_PAGES} full pages of {PAGE_LIMIT} rows: the page ceiling, day not stored")
                    raise base.SourceError(f"UW {ENDPOINT} {session}: {MAX_PAGES} full pages of {PAGE_LIMIT} "
                                           f"rows reach the page ceiling {MAX_PAGES}; day not stored")
                n = len(sizes)
                page = self._get_page(ctx, session, params, n)
                # Only a ``data`` list (or a bare list) is a page: a 200 carrying ``{}`` or a message, read as an
                # empty page, would end the day early and store its first pages as the whole day.
                more = page.get("data") if isinstance(page, dict) else page
                if not isinstance(more, list):
                    raise base.SourceError(f"UW {ENDPOINT} {session} page {n}: no list of rows in the answer")
                if more and not any(isinstance(r, dict) and snapshot_day(r.get("time")) in (session, None)
                                    for r in more):
                    raise base.SourceError(f"UW {ENDPOINT} {session} page {n}: every row is stamped on a day "
                                           f"not on {session}")
                sizes.append(len(more))
                pages.append(more)
            _check_pages_disjoint(ctx, session, pages)
            request["pages"] = sizes
            body = {"data": [r for page in pages for r in page]}
        return base.write_raw_json(raw, {
            "request": request, "session_date": session.isoformat(),
            "observed": observed, "fetched_at": time_now(), "run_id": ctx.run_id, "body": body})

    def _get_page(self, ctx: base.RunContext, session: date, params: dict, page: int):
        """One page of the session's answer; page 0 is sent without ``page``, as before paging. Every page is a
        call against the day cap, and a failed page fails the day (the client's own retry ladder, nothing more)."""
        ctx.check_cap("uw")
        res = ctx.uw_client().get_result(ENDPOINT, dict(params, page=page) if page else params)
        if res.status != "ok":
            where = f" page {page}" if page else ""
            raise base.SourceError(f"UW {ENDPOINT} {session}{where}: {res.error}")
        return res.data

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """The daily run. ``observed`` only for the latest complete session taken between its close (plus the
        settle margin) and the next open; a run inside the next session, or for an older day, is not observed."""
        observed = as_of == calendar.latest_complete_session() and not calendar.in_us_session_window()
        return [self._fetch_session(ctx, as_of, observed=observed)]

    # ── type ──
    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        session = date.fromisoformat(doc["session_date"])
        body = doc["body"]
        items = body.get("data", []) if isinstance(body, dict) else body
        rows, seen, extras, bad = [], set(), set(), []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                expiry = date.fromisoformat(str(item["expiry"])[:10])
                strike = float(item["strike"])
            except (KeyError, TypeError, ValueError):
                bad.append(sorted(item)[:6])
                continue
            if snapshot_day(item.get("time")) not in (session, None):
                ctx.issue("wrong_session", f"{session} {expiry} {strike}", f"row stamped {item.get('time')}")
                continue
            key = (expiry, strike)
            if key in seen:
                ctx.issue("duplicate_key", f"{session} {expiry} {strike}", "second row for the key dropped")
                continue
            seen.add(key)
            extras |= {k for k, v in item.items()
                       if k not in NUMERIC_FIELDS and k not in _NOT_EXTRA and to_float(v) is not None}
            row = {"session_date": session, "expiry": expiry, "strike": strike, "dte": dte(session, expiry),
                   "snapshot_time": item.get("time"), "source": "uw", "source_as_of": session,
                   "fetched_at": doc["fetched_at"], "run_id": doc["run_id"], "observed": bool(doc["observed"])}
            row.update({f: to_float(item.get(f)) for f in NUMERIC_FIELDS})
            rows.append(row)
        if bad:                                     # one issue per answer, not one per row
            ctx.issue("bad_row", session.isoformat(),
                      f"{len(bad)} rows without a usable expiry/strike; first one's fields: {bad[0]}")
        if not rows:
            # An empty partition would mark the session done for good (S6), and so would the raw file, which is the
            # fetch's resume marker (N1): it is set aside as .rejected, kept for a look, and the next run asks again.
            # The run id in the name: a second rejection of the same day keeps the first one's file (F2).
            rejected = raw.with_name(f"{raw.name}.{ctx.run_id}{REJECTED_SUFFIX}")
            os.replace(raw, rejected)
            raise base.SourceError(f"UW {session}: no usable row in {raw.name} ({len(items)} rows answered); "
                                   f"set aside as {rejected.name}")
        if extras:
            ctx.issue("unknown_field", session.isoformat(), "numeric fields without a column: " + ", ".join(sorted(extras)))
        con = duckdb.connect()
        try:
            base.rows_table(con, "day", SCHEMA, rows)
            return [base.write_parquet(con, "SELECT * FROM day ORDER BY expiry, strike", partition_path(session))]
        finally:
            con.close()

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        return [part for raw in raws for part in self.to_parquet(ctx, raw)]

    # ── backfill ──
    def backfill(self, ctx: base.RunContext, start: date, end: date) -> None:
        """One call per past session without a partition, oldest first; resumable (a partition is the marker)."""
        start = max(start, FIRST_SESSION)
        done = skipped = 0
        for session in base.iter_sessions(start, end):
            if partition_path(session).exists():
                skipped += 1
                continue
            try:
                raw = self._fetch_session(ctx, session, observed=False)
            except base.CapReached as e:
                ctx.notes.append(f"backfill stopped at {session}: {e}")      # what it has is built and kept
                break
            except base.SourceError as e:
                if "breaker_open" in str(e):
                    raise
                ctx.issue("fetch_failed", session.isoformat(), str(e)[:300])
                continue
            try:
                self.to_parquet(ctx, raw)
            except base.SourceError as e:
                ctx.issue("type_failed", session.isoformat(), str(e)[:300])
                continue
            done += 1
        ctx.notes.append(f"backfill {start}..{end}: {done} typed, {skipped} already there")

    # ── build and check ──
    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        cols = ", ".join(f'"{c}" {t}' for c, t in SCHEMA)
        con.execute(f"CREATE TABLE {TABLE} ({cols}, PRIMARY KEY ({', '.join(KEY)}))")
        parts = sorted((base.parquet_root() / "uw" / "spot_expiry_strike").glob("date=*/part-0.parquet"))
        if parts:
            files = ", ".join(base.sql_path(p) for p in parts)
            names = ", ".join(f'"{c}"' for c, _ in SCHEMA)
            con.execute(f"INSERT INTO {TABLE} SELECT {names} FROM read_parquet([{files}], union_by_name = true)")
        return {TABLE: int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, self.kind, TABLE, COMPARE)
        rows = int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
        days = int(con.execute(f"SELECT count(DISTINCT session_date) FROM {TABLE}").fetchone()[0])
        as_of = ctx.as_of or self.default_as_of()
        try:
            expected = sum(1 for _ in base.iter_sessions(FIRST_SESSION, as_of))
        except ValueError as e:                     # an as-of past the calendar's table: no coverage, no failure
            ctx.issue("outside_calendar", NAME, str(e)[:300])
            expected = 0
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows, "added": added, "removed": removed,
                "coverage": round(days / expected, 4) if expected else None}


def snapshot_day(stamp) -> date | None:
    """The New York date of a UW ``time`` stamp; None when there is none or it cannot be read.

    An ISO time with an offset (``…Z``) is converted to New York; one without an offset is taken as UTC. A bare date
    (``2026-10-26``) is that calendar date, never midnight UTC (which is the day before in New York). Epoch seconds or
    milliseconds, as a number or a numeric string, are converted to New York.
    """
    if stamp is None or stamp == "" or isinstance(stamp, bool):
        return None
    epoch = stamp if isinstance(stamp, (int, float)) else None
    if epoch is None:
        text = str(stamp).strip()
        try:
            epoch = float(text)
        except ValueError:
            if len(text) == 10:
                try:
                    return date.fromisoformat(text)
                except ValueError:
                    return None
            try:
                t = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(calendar.NY).date()
    if not math.isfinite(epoch):                       # "nan", "inf": no stamp, never an exception (F1)
        return None
    if epoch < MIN_EPOCH:                              # 0 or "20261026" is not a snapshot time (F3)
        return None
    if epoch > 1e11:                                   # milliseconds
        epoch /= 1000.0
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(calendar.NY).date()
    except (ValueError, OverflowError, OSError):       # out of the platform's range (1e20 is OSError errno 75)
        return None


def _check_pages_disjoint(ctx: base.RunContext, session: date, pages: list[list]) -> None:
    """Fail the day when a key (expiry, strike) of one page is on an earlier page. Pages are offsets into an ordered
    set (``page × limit``); a repeat means the set moved between calls, and the same move the other way skips a row
    without a sign, so the day may be short and is not kept: the next run asks again. A repeat inside one page is
    left to ``to_parquet``, which keeps the first row of a key with an issue, as on a one-page day."""
    earlier: set = set()
    repeated = 0
    for page in pages:
        keys = set()
        for row in page:
            try:
                keys.add((str(row["expiry"])[:10], float(row["strike"])))
            except (KeyError, TypeError, ValueError):
                continue                        # to_parquet reports a row without a usable key (bad_row)
        repeated += len(keys & earlier)
        earlier |= keys
    if repeated:
        ctx.issue("duplicate_key", session.isoformat(),
                  f"{repeated} rows repeated across pages: the answer moved while paging, day not stored")
        raise base.SourceError(f"UW {ENDPOINT} {session}: {repeated} rows repeated across pages; the answer moved "
                               f"while paging and the day may be short, so it is not stored")


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


DATASET = UWSpotExpiryStrike()

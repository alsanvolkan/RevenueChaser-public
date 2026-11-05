"""``uw/greek_expiry_strike``: SPX greek exposure by strike for every expiry of the chain, from Unusual Whales.

``GET /stock/SPX/greek-exposure/strike-expiry?date=<session>&expiry=<expiry>``. ``expiry`` is required, takes one
expiry, and the endpoint serves no paging (docs/openapi.yaml, "Greek Exposure By Strike And Expiry"), so a session
is one call per expiry. The chain itself comes from ``GET /stock/SPX/greek-exposure/expiry?date=<session>``, one
more call, whose answer is one row per expiry: 54 expiries on the live check of 30 Oct 2026 (2026-10-30 through
2031-12-19), 14,367 strike rows in all. A day is kept only when every expiry of the chain answered, because a
partial chain would silently narrow a DTE band.

**One row per strike, both series summed.** Two option series share an expiry date -- SPX (AM-settled, the
standard monthly) and SPXW (PM-settled, the weekly) on a third Friday -- and UW answers one row per series with
no field to tell them apart: the answer carries ``expiry``, ``strike`` and the eight greeks, and the only things
to ask with are ``ticker``, ``date`` and ``expiry`` (``docs/openapi.yaml``, "Greek Exposure By Strike And
Expiry"). The exposure at that strike is the two together, so the rows are summed into one, a greek the second
row leaves out keeping the first row's value. Measured on the live answer of 2026-11-04: 14,551 rows answered,
12,981 strikes, and all 1,570 repeats fell on the nearest five third Fridays (2026-11-20, 2026-12-18,
2027-01-15, 2027-02-19, 2027-03-19) -- SPXW is listed about five months out, which is why the later third
Fridays carry one series each. Until this landed the second row was dropped, so those strikes served one
series' exposure.

``dte`` is UW's own field and counts **calendar** days (2026-11-02 answers 3 from the 2026-10-30 session,
2026-11-20 answers 21, 2027-10-15 answers 350). It is stored as answered, because the SPX Gamma board's bands
(``module_spx_gamma/levels.py``, ``DTE_BANDS``: 0-30, 31-90, 91-180, 181-365, 365+) are calendar-day bands. The
sibling dataset ``uw/spot_expiry_strike`` derives its own ``dte`` as NYSE sessions, so the same cell reads 1 there
and 3 here. The two are different measures of different endpoints' answers and are never compared or substituted:
measured on 2026-10-30, their put walls agree on the 1 DTE expiry (7280 both) and differ on the 0 DTE one
(7215 here, 7100 there). Counting sessions is also not open to this dataset: the chain runs past
``core/calendar.py``'s 1993-2028 holiday table (2029-12-21, 2030-12-20, 2031-12-19 on the 30 Oct chain), and that
table raises rather than guess, while the sibling never sees it because it asks only for the two nearest expiries.

Raw: ``raw/uw/greek_expiry_strike/<YYYY-MM-DD>/SPX.json.gz`` — ``{"chain": <the expiry answer's rows>, "expiries":
{<expiry>: <that expiry's rows>}}`` with the request and its provenance. One file per day, written once every
expiry arrived.
Parquet: ``parquet/uw/greek_expiry_strike/date=<YYYY-MM-DD>/part-0.parquet``. Serving: ``spx_greek_expiry_strike``.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from ...core import calendar
from ...core.config import to_float
from . import base

NAME = "uw/greek_expiry_strike"
TABLE = "spx_greek_expiry_strike"
TICKER = "SPX"
ENDPOINT = f"/stock/{TICKER}/greek-exposure/strike-expiry"
CHAIN_ENDPOINT = f"/stock/{TICKER}/greek-exposure/expiry"
# The chain's length on the live check of 30 Oct 2026 was 54, so a day is ~55 calls (the chain plus one per
# expiry). UW's day cap is 100,000 (``caps.DAY_CAPS``), so a daily run is nothing; a long backfill is not, which
# is what the Settings estimate below is for.
CALLS_PER_SESSION_ESTIMATE = 55
# A chain longer than this is not the SPX chain: refuse the day rather than send hundreds of calls. The live
# check saw 54 and the board's own fetch has seen the same order since August.
MAX_EXPIRIES = 120
FIRST_SESSION = date(2025, 1, 16)                   # the sibling dataset's first served session
REJECTED_SUFFIX = ".rejected"
# An answer with more than this share of its rows unusable (no expiry/strike, or stamped on another day) is
# refused like an empty one; the same figure as the sibling dataset and ``serving.MAX_SHRINK``.
MAX_DROPPED = 0.01

# Every numeric field UW documents for this answer: the four greeks, each for calls and puts. Unlike the spot
# exposures there is no open-interest/volume/bid/ask basis here — one value per greek per side.
GREEK_FIELDS = tuple(f"{side}_{greek}" for greek in ("gex", "delta", "charm", "vanna")
                     for side in ("call", "put"))
NUMERIC_FIELDS = GREEK_FIELDS
_NOT_EXTRA = {"expiry", "strike", "date", "dte", "ticker"}

SCHEMA = ([("session_date", "DATE"), ("expiry", "DATE"), ("strike", "DOUBLE"), ("dte", "INTEGER")]
          + [(f, "DOUBLE") for f in NUMERIC_FIELDS]
          + [("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
             ("run_id", "TEXT"), ("observed", "BOOLEAN")])
KEY = ("session_date", "expiry", "strike")
# What makes a row "changed": its data, not when or by which run it was fetched.
COMPARE = [c for c, _ in SCHEMA if c not in ("fetched_at", "run_id", "source_as_of")]


def raw_path(session: date) -> Path:
    return base.raw_root() / "uw" / "greek_expiry_strike" / session.isoformat() / f"{TICKER}.json.gz"


def partition_path(session: date) -> Path:
    return base.parquet_root() / "uw" / "greek_expiry_strike" / f"date={session.isoformat()}" / "part-0.parquet"


class UWGreekExpiryStrike:
    name = NAME
    kind = "market"
    providers = ("uw",)
    tables = (TABLE,)

    def default_as_of(self) -> date:
        return calendar.latest_complete_session()

    # ── fetch ──
    def _get(self, ctx: base.RunContext, session: date, endpoint: str, params: dict, where: str):
        """One call; every call counts against the day cap and a failed call fails the day."""
        ctx.check_cap("uw")
        res = ctx.uw_client().get_result(endpoint, params)
        if res.status != "ok":
            raise base.SourceError(f"UW {endpoint} {session}{where}: {res.error}")
        return res.data

    def _chain(self, ctx: base.RunContext, session: date) -> list[str]:
        """The session's expiry chain, oldest first. Refuses an empty chain and one past ``MAX_EXPIRIES``."""
        body = self._get(ctx, session, CHAIN_ENDPOINT, {"date": session.isoformat()}, "")
        rows = body.get("data") if isinstance(body, dict) else body
        if not isinstance(rows, list) or not rows:
            raise base.SourceError(f"UW {CHAIN_ENDPOINT} {session}: no expiry rows")
        expiries = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                expiries.append(date.fromisoformat(str(r["expiry"])[:10]).isoformat())
            except (KeyError, TypeError, ValueError):
                continue
        expiries = sorted(set(expiries))
        if not expiries:
            raise base.SourceError(f"UW {CHAIN_ENDPOINT} {session}: no readable expiry in {len(rows)} rows")
        if len(expiries) > MAX_EXPIRIES:
            raise base.SourceError(f"UW {CHAIN_ENDPOINT} {session}: {len(expiries)} expiries, past the ceiling "
                                   f"{MAX_EXPIRIES}; day not fetched")
        return expiries

    def _fetch_session(self, ctx: base.RunContext, session: date, *, observed: bool) -> Path:
        # A session that has not finished would be stored as UW's intraday snapshot for good: its raw file is the
        # resume marker, as in the sibling dataset. No call is sent.
        latest = calendar.latest_complete_session()
        if session > latest:
            raise base.SourceError(f"UW {ENDPOINT} {session}: the session is not complete yet (latest complete "
                                   f"session {latest}); not fetched")
        raw = raw_path(session)
        if raw.exists():
            return raw
        chain = self._chain(ctx, session)
        expiries: dict[str, list] = {}
        for expiry in chain:
            params = {"date": session.isoformat(), "expiry": expiry}
            body = self._get(ctx, session, ENDPOINT, params, f" expiry {expiry}")
            rows = body.get("data") if isinstance(body, dict) else body
            # A 200 carrying ``{}`` or a message is not an empty expiry: it would silently narrow a DTE band, so
            # the day fails and the next run asks again.
            if not isinstance(rows, list):
                raise base.SourceError(f"UW {ENDPOINT} {session} expiry {expiry}: no list of rows in the answer")
            _check_answer_day(rows, session, f" expiry {expiry}")
            expiries[expiry] = rows
        return base.write_raw_json(raw, {
            "request": {"endpoint": ENDPOINT, "chain_endpoint": CHAIN_ENDPOINT, "date": session.isoformat(),
                        "expiries": chain},
            "session_date": session.isoformat(), "observed": observed, "fetched_at": time_now(),
            "run_id": ctx.run_id, "body": {"expiries": expiries}})

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        """The daily run. ``observed`` only for the latest complete session taken between its close (plus the
        settle margin) and the next open; a run inside the next session, or for an older day, is not observed."""
        observed = as_of == calendar.latest_complete_session() and not calendar.in_us_session_window()
        return [self._fetch_session(ctx, as_of, observed=observed)]

    # ── type ──
    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        session = date.fromisoformat(doc["session_date"])
        by_expiry = (doc["body"] or {}).get("expiries") or {}
        rows, by_key, extras, bad, wrong = [], {}, set(), [], 0
        total = 0
        for items in by_expiry.values():
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                total += 1
                try:
                    expiry = date.fromisoformat(str(item["expiry"])[:10])
                    strike = float(item["strike"])
                except (KeyError, TypeError, ValueError):
                    bad.append(sorted(item)[:6])
                    continue
                answered = _answer_day(item.get("date"))
                if answered is not None and answered != session:
                    ctx.issue("wrong_session", f"{session} {expiry} {strike}", f"row stamped {item.get('date')}")
                    wrong += 1
                    continue
                key = (expiry, strike)
                kept = by_key.get(key)
                if kept is not None:
                    # Two option series share an expiry date -- SPX (AM, standard monthly) and SPXW (PM, weekly)
                    # on a third Friday -- and the answer has no field to tell them apart (docs/openapi.yaml,
                    # "Greek Exposure By Strike And Expiry": expiry, strike and the eight greeks, and only
                    # ticker/date/expiry to ask with). The strike's exposure is the two together, so they are
                    # summed. Measured on 2026-11-04: 1,570 of 14,551 rows, every one of them on the nearest
                    # five third Fridays, and each was dropped before this.
                    for f in NUMERIC_FIELDS:
                        v = to_float(item.get(f))
                        if v is not None:
                            kept[f] = v if kept[f] is None else kept[f] + v
                    continue
                extras |= {k for k, v in item.items()
                           if k not in NUMERIC_FIELDS and k not in _NOT_EXTRA and to_float(v) is not None}
                dte = to_float(item.get("dte"))
                row = {"session_date": session, "expiry": expiry, "strike": strike,
                       "dte": int(dte) if dte is not None else None,
                       "source": "uw", "source_as_of": session, "fetched_at": doc["fetched_at"],
                       "run_id": doc["run_id"], "observed": bool(doc["observed"])}
                row.update({f: to_float(item.get(f)) for f in NUMERIC_FIELDS})
                by_key[key] = row
                rows.append(row)
        if bad:                                     # one issue per answer, not one per row
            ctx.issue("bad_row", session.isoformat(),
                      f"{len(bad)} rows without a usable expiry/strike; first one's fields: {bad[0]}")
        dropped = len(bad) + wrong
        if not rows or dropped > MAX_DROPPED * total:
            # An empty partition would mark the session done for good, and so would the raw file, which is the
            # fetch's resume marker: it is set aside as .rejected, kept for a look, and the next run asks again.
            rejected = raw.with_name(f"{raw.name}.{ctx.run_id}{REJECTED_SUFFIX}")
            os.replace(raw, rejected)
            why = (f"no usable row in {raw.name} ({total} rows answered)" if not rows else
                   f"{dropped} of {total} rows unusable in {raw.name} ({len(bad)} without an expiry/strike, "
                   f"{wrong} stamped on another day), more than {MAX_DROPPED:.0%}")
            raise base.SourceError(f"UW {session}: {why}; set aside as {rejected.name}")
        if extras:
            ctx.issue("unknown_field", session.isoformat(),
                      "numeric fields without a column: " + ", ".join(sorted(extras)))
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
        """One chain plus one call per expiry for each past session without a partition, oldest first; resumable
        (a partition is the marker). A session costs ~``CALLS_PER_SESSION_ESTIMATE`` calls, so a long range is a
        long run: the cap stops it and what it has is kept."""
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
        parts = sorted((base.parquet_root() / "uw" / "greek_expiry_strike").glob("date=*/part-0.parquet"))
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


def _answer_day(stamp) -> date | None:
    """The session date a row names in its own ``date`` field; None when there is none or it cannot be read.

    This answer carries a plain market date, not the spot exposures' epoch ``time``, so there is one format to
    read and ``uw_spot_expiry_strike.snapshot_day`` is not reused here.
    """
    if stamp is None or stamp == "" or isinstance(stamp, bool):
        return None
    try:
        return date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None


def _check_answer_day(rows: list, session: date, where: str) -> None:
    """Refuse an answer none of whose rows is on ``session``. A row with no ``date`` at all still passes,
    ``date=`` being the first guard; an answer whose every row names another day is UW's newest snapshot rather
    than the day asked for, and storing it would date someone else's session as this one."""
    dicts = [r for r in rows if isinstance(r, dict)]
    if not dicts:
        return
    if any(_answer_day(r.get("date")) in (None, session) for r in dicts):
        return
    raise base.SourceError(f"UW {ENDPOINT} {session}{where}: every row is stamped on a day not on {session} "
                           f"(first: {dicts[0].get('date')!r})")


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


DATASET = UWGreekExpiryStrike()

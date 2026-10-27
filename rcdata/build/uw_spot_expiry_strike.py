"""``uw/spot_expiry_strike``: SPX spot exposures by expiry and strike from Unusual Whales, one call per session.

``GET /stock/SPX/spot-exposures/expiry-strike?expirations[]=<session>&expirations[]=<next session>`` (``expirations[]``
is required; UW serves this from 16 Jan 2025). The daily run asks the two nearest expiries after the close
(``observed`` true); the backfill adds ``date=<session>`` and the same two expiries of that session (``observed``
false). One call answers about 391 rows (26 Oct 2026: 155 at 0 DTE, 236 at 1 DTE, strikes 4,800 to 9,200), under the
500-row page, so one session is one call.

Raw: ``raw/uw/spot_expiry_strike/<YYYY-MM-DD>/SPX.json.gz`` (UW's body with the request and its provenance).
Parquet: ``parquet/uw/spot_expiry_strike/date=<YYYY-MM-DD>/part-0.parquet``. Serving: ``spx_gamma_expiry_strike``.

``dte`` counts NYSE sessions from the session to the expiry (``core/calendar.py``), so a Friday's next expiry,
the Monday, is 1 DTE.
"""
from __future__ import annotations

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
PAGE_LIMIT = 500
FIRST_SESSION = date(2025, 1, 16)

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
COMPARE = [c for c, _ in SCHEMA if c not in ("fetched_at", "run_id")]


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
        raw = raw_path(session)
        if raw.exists():
            return raw
        expiries = [session, calendar.next_trading_day(session)]
        params: dict = {"expirations[]": [e.isoformat() for e in expiries], "limit": PAGE_LIMIT}
        if not observed:
            params["date"] = session.isoformat()
        res = ctx.uw_client().get_result(ENDPOINT, params)
        if res.status != "ok":
            raise base.SourceError(f"UW {ENDPOINT} {session}: {res.error}")
        rows = res.data.get("data", []) if isinstance(res.data, dict) else res.data
        if not isinstance(rows, list) or not rows:
            raise base.SourceError(f"UW {ENDPOINT} {session}: no rows")
        if len(rows) >= PAGE_LIMIT:
            ctx.issue("possibly_truncated", session.isoformat(), f"{len(rows)} rows = the page limit")
        return base.write_raw_json(raw, {
            "request": {"endpoint": ENDPOINT, "params": params}, "session_date": session.isoformat(),
            "observed": observed, "fetched_at": time_now(), "run_id": ctx.run_id, "body": res.data})

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        return [self._fetch_session(ctx, as_of, observed=True)]

    # ── type ──
    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        session = date.fromisoformat(doc["session_date"])
        body = doc["body"]
        items = body.get("data", []) if isinstance(body, dict) else body
        rows, seen, extras = [], set(), set()
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                expiry = date.fromisoformat(str(item["expiry"])[:10])
                strike = float(item["strike"])
            except (KeyError, TypeError, ValueError):
                ctx.issue("bad_row", session.isoformat(), f"row without a usable expiry/strike: {sorted(item)[:6]}")
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
        if extras:
            ctx.issue("unknown_field", session.isoformat(), "numeric fields without a column: " + ", ".join(sorted(extras)))
        con = duckdb.connect()
        try:
            base.rows_table(con, "day", SCHEMA, rows)
            return [base.write_parquet(con, "SELECT * FROM day ORDER BY expiry, strike", partition_path(session))]
        finally:
            con.close()

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
            except base.SourceError as e:
                if "breaker_open" in str(e):
                    raise
                ctx.issue("fetch_failed", session.isoformat(), str(e)[:300])
                continue
            self.to_parquet(ctx, raw)
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
        expected = sum(1 for _ in base.iter_sessions(FIRST_SESSION, as_of))
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows, "added": added, "removed": removed,
                "coverage": round(days / expected, 4) if expected else None}


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


DATASET = UWSpotExpiryStrike()

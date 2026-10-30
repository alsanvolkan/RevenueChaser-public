"""``alpaca/bars``: Alpaca's daily bars of the scope, the last ``WINDOW`` sessions read again every run.

**One run** (``as_of``, default the latest complete New York session) asks ``/v2/stocks/bars`` for the scope
(``prices.scope``), ``CHUNK`` symbols a request, twice: ``adjustment=raw`` (the prices as traded, which the store
keeps) and ``adjustment=all`` (Alpaca's split and dividend adjusted closes, which only check ours). The raw window is
the ``WINDOW`` sessions ending on ``as_of``; the ``all`` window starts one session earlier, so every kept day has its
Alpaca day return from one answer. The query's end is never later than ``RECENT_MARGIN`` ago (the free plan has no
SIP data of the last 15 minutes). A run refuses an ``as_of`` whose session is not complete yet (16:30 New York,
``core/calendar.py``).

**Raw files**: ``raw/alpaca/bars/<as_of>/<adjustment>-<n>.json.gz``, one per request, holding the names it asked
(symbol, ticker, CIK, tier, the security id of the day), the query and every page. A file that exists is not asked
again; a run cut off mid-way asks only the names no file of its ``as_of`` holds yet. A chunk Alpaca refuses (400 or
422, a symbol it does not take) is split in halves until the one name it refuses is left, which is an issue.

**The key rules.** A bar's session is the New York date of its timestamp ``t``. A bar is kept only for a New York
session (another day is the issue ``not_a_session``) inside the run's window and complete when it was fetched
(``calendar.bars_complete``): never an intraday bar.

**Parquet**: ``parquet/alpaca/bars/date=<session>/part-0.parquet`` keyed ``(symbol, session_date)``, the newest
fetch winning a key. Before a key is replaced, a change of ``open``, ``high``, ``low``, ``close`` or ``volume`` keeps
the value it replaces in ``parquet/alpaca/bars_revision/date=<session>/part-<run_id>.parquet`` (``revised_at``, the
fetch that replaced it, and ``revised_by``, its run): the ``price_revision`` table of ``prices/serving``.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base
from ..sources import alpaca
from . import KIND, alpaca_symbol, scope, stage

NAME = "alpaca/bars"
WINDOW = 5
CHUNK = 100
ADJUSTMENTS = ("raw", "all")
FEED = "sip"
LIMIT = 10_000
RECENT_MARGIN = timedelta(minutes=16)
SOURCE_DIR = ("alpaca", "bars")
SCHEMA = [("symbol", "TEXT"), ("ticker", "TEXT"), ("cik", "TEXT"), ("tier", "TEXT"), ("security_id", "TEXT"),
          ("session_date", "DATE"), ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
          ("volume", "DOUBLE"), ("trade_count", "BIGINT"), ("vwap", "DOUBLE"), ("close_all", "DOUBLE"),
          ("all_return", "DOUBLE"), ("feed", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
          ("run_id", "TEXT")]
KEY = ["symbol", "session_date"]
COMPARE = ("open", "high", "low", "close", "volume")
REVISION_SCHEMA = SCHEMA + [("revised_at", "DOUBLE"), ("revised_by", "TEXT")]
NAME_FIELDS = ("symbol", "ticker", "cik", "tier", "security_id")


def raw_dir(as_of: date) -> Path:
    return base.raw_root().joinpath(*SOURCE_DIR, as_of.isoformat())


def partition_path(session: date) -> Path:
    return base.parquet_root().joinpath(*SOURCE_DIR, f"date={session.isoformat()}", "part-0.parquet")


def revision_path(session: date, run_id: str) -> Path:
    return base.parquet_root() / "alpaca" / "bars_revision" / f"date={session.isoformat()}" / f"part-{run_id}.parquet"


def window(as_of: date, n: int = WINDOW) -> list[date]:
    """The ``n`` New York sessions ending on ``as_of`` (a session), oldest first."""
    out = [as_of]
    while len(out) < n:
        out.insert(0, calendar.previous_trading_day(out[0] - timedelta(days=1)))
    return out


def session_of(t: str) -> date:
    """The New York date of an Alpaca bar timestamp (``2026-10-27T04:00:00Z``)."""
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(calendar.NY).date()


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refused(e: Exception) -> bool:
    """A 400 or 422: Alpaca refused something in the query (a symbol), not the key or the plan."""
    m = re.search(r"HTTP (\d{3})", str(e))
    return bool(m) and m.group(1) in ("400", "422")


def _changed(new, old) -> bool:
    if new is None or old is None:
        return (new is None) != (old is None)
    return abs(float(new) - float(old)) > 1e-9 * max(1.0, abs(float(old)))


class AlpacaBars:
    name = NAME
    kind = KIND
    providers = (alpaca.PROVIDER,)
    tables = ("price_bar_daily",)
    cadence = "daily"
    starts_by_hand = True             # the first run is the deploy's own request (POST /api/build/request/...)
    honours_scope = True              # a request's tiers and names are what it asks (#265)

    def default_as_of(self) -> date:
        return calendar.latest_complete_session()

    # ── fetch ─────────────────────────────────────────────────────────────────────────────────────────────────

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        now = calendar._now()
        if not calendar.is_trading_day(as_of):
            raise base.SourceError(f"{as_of} is not a New York session")
        if not calendar.bars_complete(as_of, now.timestamp()):
            raise base.SourceError(f"the session of {as_of} is not complete yet (16:30 New York): not fetched")
        names = scope(want=ctx.scope)
        if not ctx.scope.names and not any(n.get("tier") for n in names):
            raise base.SourceError("the spine holds no name of the asked tiers (spine.duckdb not built?): "
                                   "nothing to ask")
        folder = raw_dir(as_of)
        have = sorted(folder.glob("*.json.gz")) if folder.is_dir() else []
        covered = {(payload["adjustment"], n["symbol"]) for payload in map(base.read_raw_json, have)
                   for n in payload["names"]}
        sessions = window(as_of)
        end = min(datetime.combine(as_of, time(23, 59, 59), calendar.NY), now - RECENT_MARGIN)
        first = {"raw": sessions[0], "all": calendar.previous_trading_day(sessions[0] - timedelta(days=1))}
        pace = alpaca.pacer(ctx)
        out = list(have)
        for adj in ADJUSTMENTS:
            todo = [{**{k: n.get(k) for k in NAME_FIELDS[1:]}, "symbol": alpaca_symbol(n["ticker"])} for n in names]
            todo = [n for n in todo if (adj, n["symbol"]) not in covered]
            used = [int(p.name.split("-")[1].split(".")[0]) for p in have if p.name.startswith(adj + "-")]
            seq = [max(used, default=-1)]
            start = datetime.combine(first[adj], time(0, 0), calendar.NY)
            for i in range(0, len(todo), CHUNK):
                out += self._ask(ctx, folder, adj, todo[i:i + CHUNK], start, end, as_of, pace, seq)
        return out

    def _ask(self, ctx, folder: Path, adj: str, names: list[dict], start: datetime, end: datetime, as_of: date,
             pace, seq: list[int]) -> list[Path]:
        params = {"symbols": ",".join(n["symbol"] for n in names), "timeframe": "1Day", "start": _utc(start),
                  "end": _utc(end), "adjustment": adj, "feed": FEED, "limit": LIMIT, "sort": "asc"}
        try:
            pages = alpaca.get_pages(ctx, "/v2/stocks/bars", params, pace, "bars")
        except base.CapReached:
            raise
        except base.SourceError as e:
            if not _refused(e):
                raise
            if len(names) == 1:
                ctx.issue("symbol_refused", names[0]["symbol"], f"alpaca refused the symbol: {str(e)[-120:]}")
                return []
            half = len(names) // 2
            return (self._ask(ctx, folder, adj, names[:half], start, end, as_of, pace, seq)
                    + self._ask(ctx, folder, adj, names[half:], start, end, as_of, pace, seq))
        seq[0] += 1
        payload = {"as_of": as_of.isoformat(), "adjustment": adj, "feed": FEED, "start": params["start"],
                   "end": params["end"], "names": names, "pages": pages, "fetched_at": calendar._now().timestamp(),
                   "run_id": ctx.run_id}
        return [base.write_raw_json(folder / f"{adj}-{seq[0]:04d}.json.gz", payload)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        """The windows ending on every ``WINDOW``-th session from ``end`` back to ``start``, each a daily run of its
        own (its own raw folder, so a cut-off backfill resumes). Inside ``core/calendar.py``'s years only: Alpaca's
        history from 2016 is the Phase 8 fill."""
        sessions = list(base.iter_sessions(start, end))
        for as_of in reversed(sessions[::-1][::WINDOW]):
            self.to_parquet_all(ctx, self.fetch(ctx, as_of))

    # ── Parquet ───────────────────────────────────────────────────────────────────────────────────────────────

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        """Type a run's raw files, keep each changed value as a revision, merge each touched session once."""
        payloads = [base.read_raw_json(p) for p in raws]
        rows: dict[tuple[str, date], dict] = {}
        closes: dict[tuple[str, str], list[tuple[date, float]]] = {}
        dropped: dict[str, int] = {}
        asked: dict[str, dict[str, str | None]] = {}      # as_of -> symbol -> the tier it was asked under
        for p in payloads:
            as_of = date.fromisoformat(p["as_of"])
            keep = set(window(as_of))
            names = {n["symbol"]: n for n in p["names"]}
            if p["adjustment"] == "raw":
                asked.setdefault(p["as_of"], {}).update({s: n.get("tier") for s, n in names.items()})
            for page in p["pages"]:
                for symbol, bars in (page or {}).items():
                    for b in bars or []:
                        session = session_of(b["t"])
                        why = self._reject(session, keep, p["fetched_at"])
                        if p["adjustment"] == "all":
                            if why in (None, "outside the window"):
                                closes.setdefault((p["as_of"], symbol), []).append((session, b.get("c")))
                            continue
                        if why:
                            dropped[why] = dropped.get(why, 0) + 1
                            if why == "not a session":
                                ctx.issue("not_a_session", f"{symbol}:{session}", "a bar dated on no NYSE session")
                            continue
                        n = names.get(symbol) or {"symbol": symbol}
                        rows[(symbol, session)] = {
                            **{k: n.get(k) for k in NAME_FIELDS}, "symbol": symbol, "session_date": session,
                            "open": b.get("o"), "high": b.get("h"), "low": b.get("l"), "close": b.get("c"),
                            "volume": b.get("v"), "trade_count": b.get("n"), "vwap": b.get("vw"),
                            "close_all": None, "all_return": None, "feed": p.get("feed"), "source_as_of": as_of,
                            "fetched_at": p["fetched_at"], "run_id": p["run_id"]}
        for (as_of, symbol), series in closes.items():
            series.sort()
            for (d0, c0), (d1, c1) in zip([(None, None)] + series[:-1], series):
                row = rows.get((symbol, d1))
                if row is None or row["source_as_of"].isoformat() != as_of:
                    continue
                row["close_all"] = c1
                if d0 is not None and c0 and c1 is not None and \
                        calendar.previous_trading_day(d1 - timedelta(days=1)) == d0:
                    row["all_return"] = c1 / c0 - 1.0
        if dropped:
            ctx.notes.append("bars dropped: " + ", ".join(f"{n} {why}" for why, n in sorted(dropped.items())))
        self._coverage(ctx, rows, asked)
        by_day: dict[date, list[dict]] = {}
        for r in rows.values():
            by_day.setdefault(r["session_date"], []).append(r)
        out, revised = [], 0
        for session, day_rows in sorted(by_day.items()):
            final = partition_path(session)
            old = {(r["symbol"], r["session_date"]): r for r in stage.read_rows(final)}
            changes = [{**o, "revised_at": r["fetched_at"], "revised_by": ctx.run_id} for r in day_rows
                       for o in [old.get((r["symbol"], r["session_date"]))]
                       if o is not None and o["fetched_at"] < r["fetched_at"]
                       and any(_changed(r[c], o[c]) for c in COMPARE)]
            if changes:
                revised += len(changes)
                stage.write_rows(changes, REVISION_SCHEMA, revision_path(session, ctx.run_id), order="symbol")
            out.append(base.upsert_partition(day_rows, SCHEMA, KEY, final))
        if revised:
            ctx.notes.append(f"{revised} bars revised (the old values kept in price_revision)")
        return out

    @staticmethod
    def _reject(session: date, keep: set[date], fetched_at: float) -> str | None:
        try:
            if not calendar.is_trading_day(session):
                return "not a session"
        except ValueError:
            return "outside the calendar"
        if session not in keep:
            return "outside the window"
        if not calendar.bars_complete(session, fetched_at):
            return "not complete when fetched"
        return None

    @staticmethod
    def _coverage(ctx: base.RunContext, rows: dict, asked: dict[str, dict[str, str | None]]) -> None:
        """The run's coverage: Tier A's share, which is the acceptance, and a note per other tier beside it (#265).

        The scope is A+B now, and an OTC or fund name may have no Alpaca bar at all, so counting B into the one
        number would move an acceptance nobody changed. It is reported instead, or an empty tier goes unseen.
        """
        if not asked:
            return
        as_of = max(asked)
        tiers = asked[as_of]
        got = {s for (s, d) in rows if d.isoformat() == as_of and s in tiers}
        tier_a = {s for s, t in tiers.items() if t == "A"}
        ctx.pending[NAME] = {"as_of": as_of, "asked": len(tier_a), "with_bar": len(got & tier_a)}
        ctx.notes.append(f"{len(got & tier_a)} of {len(tier_a)} Tier A names with a bar on {as_of}")
        for tier in sorted({t for t in tiers.values() if t and t != "A"}):
            of_tier = {s for s, t in tiers.items() if t == tier}
            ctx.notes.append(f"{len(got & of_tier)} of {len(of_tier)} Tier {tier} names with a bar on {as_of}")

    # ── build ─────────────────────────────────────────────────────────────────────────────────────────────────

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        root = base.parquet_root()
        stage.create(con, "stg_alpaca_bars", SCHEMA, [f for _, f in stage.partitions(root.joinpath(*SOURCE_DIR),
                                                                                       "date")])
        stage.create(con, "stg_alpaca_bar_revisions", REVISION_SCHEMA,
                     sorted((root / "alpaca" / "bars_revision").glob("date=*/part-*.parquet")))
        return {}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["security_id", "session_date", "open", "high", "low", "close", "volume"]
        added, removed = stage.compare_live(con, "price_bar_daily", cols, f"source = '{NAME}'")
        p = ctx.pending.get(NAME) or {}
        as_of = p.get("as_of") or (ctx.as_of or self.default_as_of()).isoformat()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged", "source_as_of": as_of,
                "rows": stage.count(con, "stg_alpaca_bars"), "added": added, "removed": removed,
                "coverage": round(p["with_bar"] / p["asked"], 4) if p.get("asked") else None}


DATASET = AlpacaBars()

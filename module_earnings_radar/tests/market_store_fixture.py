"""A ``market.duckdb`` holding FRED observations, for the Earnings board's market-store reader tests.

Written the way the builder writes it: a FRED JSON answer under ``raw/fred/series/<as-of>/<SERIES>.json`` with its
metadata sidecar, typed to Parquet by the dataset's own ``to_parquet_all``, then the serving file built and swapped
in by ``base.build_kind``. No SQL is written into the serving file by hand.
"""
from __future__ import annotations

import json
from datetime import date

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base, market_series as ms
from RevenueChaser.rcdata.build.sources import fred

RUN_ID = "earnings-fixture-run"


def fred_body(observations: dict[str, float]) -> bytes:
    """FRED's own answer shape; a missing value is the dot FRED writes."""
    return json.dumps({"observations": [{"date": d, "value": "." if v is None else repr(float(v))}
                                        for d, v in sorted(observations.items())]}).encode()


def serve(observations: dict[str, float], *, series: str = "DGS10", as_of: date | None = None,
          check: str | None = "changed") -> None:
    """Build and swap in a ``market`` serving file holding ``observations`` (ISO day -> value) of ``series``."""
    as_of = as_of or date.fromisoformat(max(observations))
    ctx = base.RunContext(run_id=RUN_ID, dataset=ms.NAME, trigger="test", allow_shrink=True)
    path = base.raw_root() / fred.SOURCE / fred.DATASET_DIR / as_of.isoformat() / f"{series}.json"
    raw = ms.save_raw(ctx, path, fred_body(observations), as_of, fred.BASE, series=series)
    ms.DATASET.to_parquet_all(ctx, [raw])
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    base.build_kind(ctx, ms.DATASET)
    if check:
        ops.record_check(ms.NAME, check, run_id=RUN_ID, source_as_of=as_of.isoformat())


def serve_economic(sweeps: dict, *, fetched_at: float = 1.0, check: str | None = "changed") -> None:
    """Build ``market.economic_event`` from ``sweeps`` (snapshot day -> UW calendar items) the way the builder does: one
    raw fetch per day written at ``fetched_at``, typed by ``uw/economic_calendar``'s own ``to_parquet_all``, then the
    kind built and swapped in. A later call adds its days and replaces a day it names again (that day's newer fetch).
    An item may give ``event_ts`` (epoch seconds) in place of UW's ``time`` text."""
    from datetime import datetime, timezone

    from RevenueChaser.rcdata.build import uw_economic_calendar as econ

    ctx = base.RunContext(run_id=RUN_ID, dataset=econ.NAME, trigger="test", allow_shrink=True)
    raws = []
    for day, items in sorted(sweeps.items()):
        day = day if isinstance(day, date) else date.fromisoformat(str(day))
        body = [{**{k: v for k, v in i.items() if k != "event_ts"},
                 "time": i.get("time") or datetime.fromtimestamp(float(i["event_ts"]), timezone.utc).isoformat()}
                for i in items]
        raw = econ.day_dir(day) / f"{int(fetched_at * 1000):015d}.json.gz"
        raws.append(base.write_raw_json(raw, {"request": {"endpoint": econ.ENDPOINT}, "snapshot_date": day.isoformat(),
                                              "fetched_at": fetched_at, "run_id": RUN_ID, "body": {"data": body}}))
    econ.DATASET.to_parquet_all(ctx, raws)
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    base.build_kind(ctx, econ.DATASET)
    if check:
        ops.record_check(econ.NAME, check, run_id=RUN_ID, source_as_of=max(raws).parent.name)


def serve_estimates(rows: list[dict], *, as_of: date | None = None, check: str | None = "changed") -> None:
    """Build and swap in a ``fundamentals`` serving file whose ``estimate_latest`` holds ``rows``.

    The same way ``rcdata/tests/fund_fixture.spine`` builds a spine: the dataset's own schema, the columns a row
    leaves out written NULL. ``rows`` carry at least ``ticker`` and ``period``. ``as_of`` (default New York's today, so
    a reader's age limit sees a fresh row whenever the suite runs) is every row's ``source_as_of`` unless it names its own.
    """
    from RevenueChaser.core import calendar
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.fundamentals import estimates as est

    as_of = as_of or calendar.today_ny()
    schema = est.schema()
    full = [{**{c: None for c, _ in schema}, "cik": "1", "source_as_of": as_of, "fetched_at": 1.0,
             "run_id": RUN_ID, **r} for r in rows]
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    names = [c for c, _ in schema]
    with serving.build("fundamentals") as b:
        b.con.execute(f"CREATE TABLE estimate_latest ({cols})")
        if full:
            b.con.executemany(f"INSERT INTO estimate_latest VALUES ({', '.join('?' * len(names))})",
                              [[r[c] for c in names] for r in full])
        b.swap({"estimate_latest": len(full)})
    if check:
        ops.record_check(est.NAME, check, run_id=RUN_ID, source_as_of=as_of.isoformat())


def serve_short_interest(finra_rows: list[dict], shortable_rows: list[dict] = (), *,
                         check: str | None = "changed") -> None:
    """Build and swap in an ``ownership`` serving file with ``finra_short_interest`` and ``ibkr_shortable`` (#287).

    Each table has its dataset's own served schema, the columns a row leaves out written NULL. A FINRA row carries at
    least ``ticker`` and ``settlement_date`` (``symbol`` defaults to the ticker); a shortable row ``ticker`` and
    ``session_date``.
    """
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.ownership import finra, ibkr

    with serving.build("ownership") as b:
        counts = {}
        for table, schema, rows in ((finra.TABLE, finra.SERVED, finra_rows),
                                    (ibkr.TABLE, ibkr.SERVED, shortable_rows)):
            names = [c for c, _ in schema]
            full = [{**{c: None for c in names}, "symbol": r.get("ticker"), "fetched_at": 1.0, "run_id": RUN_ID, **r}
                    for r in rows]
            b.con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in schema)})")
            if full:
                b.con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(names))})",
                                  [[r[c] for c in names] for r in full])
            counts[table] = len(full)
        b.swap(counts)
    if check:
        ops.record_check(finra.NAME, check, run_id=RUN_ID)

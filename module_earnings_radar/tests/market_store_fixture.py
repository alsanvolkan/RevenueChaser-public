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


def serve_estimates(rows: list[dict], *, as_of: date | None = None, check: str | None = "changed") -> None:
    """Build and swap in a ``fundamentals`` serving file whose ``estimate_latest`` holds ``rows``.

    The same way ``rcdata/tests/fund_fixture.spine`` builds a spine: the dataset's own schema, the columns a row
    leaves out written NULL. ``rows`` carry at least ``ticker`` and ``period``.
    """
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.fundamentals import estimates as est

    as_of = as_of or date(2026, 10, 28)
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

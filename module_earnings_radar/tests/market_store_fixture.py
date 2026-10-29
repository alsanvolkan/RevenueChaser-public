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

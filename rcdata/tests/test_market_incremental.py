"""#446: ``market`` builds on a copy of the live file and reloads only the Parquet partitions that changed.

The proof is metamorphic, as for fundamentals and prices: whatever changed in Parquet, the build on the live copy
serves exactly what a whole build of the same tree serves (``_same_as_whole``).
"""
from __future__ import annotations

import shutil
from datetime import date

import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, dirty, market_series as ms
from RevenueChaser.rcdata.build import uw_greek_expiry_strike as greek, uw_spot_expiry_strike as spot
from RevenueChaser.rcdata.build.sources import fred, treasury

TABLES = (ms.TABLE, spot.TABLE, greek.TABLE)
D1, D2, D3 = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)


def _uw(mod, day: date, value: float = 1.0) -> None:
    rows = [{"session_date": day, "expiry": date(2026, 11, 20), "strike": 7000.0 + 100 * k, "dte": 18,
             "source": "uw", "source_as_of": day, "fetched_at": 1.0, "run_id": "r",
             **{f: value * (k + 1) for f in mod.NUMERIC_FIELDS if f != "price"}} for k in range(3)]
    base.upsert_partition(rows, mod.SCHEMA, list(mod.KEY), mod.partition_path(day))


def _series(src, series_id: str, year: int, value: float) -> None:
    rows = [{"series_id": series_id, "obs_date": date(year, 3, d), "source": src.SOURCE, "value": value + d,
             "frequency": "daily", "source_as_of": date(year, 3, 31), "fetched_at": 1.0, "run_id": "r"}
            for d in (2, 3, 4)]
    base.upsert_partition(rows, ms.SCHEMA, ms.KEY, ms.partition_path(src, year))


def _seed() -> None:
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    for mod in (spot, greek):
        _uw(mod, D1)
        _uw(mod, D2)
    _series(treasury, "DGS10_T", 2025, 4.0)
    _series(fred, "DGS10", 2025, 4.1)
    _series(fred, "DGS10", 2026, 4.2)
    for year in range(2020, 2025):                  # enough untouched files to stay under dirty.MAX_DIRTY_SHARE
        _series(fred, "DGS10", year, 3.0)


def _build(shrink: bool = False) -> tuple[dict, str]:
    """One build of ``market`` with every dataset a member (``shrink``: each may shrink); market_series' check."""
    ctxs = [(d, base.RunContext(run_id=f"t-{d.name}", dataset=d.name, allow_shrink=shrink, as_of=D3))
            for d in (ms.DATASET, spot.DATASET, greek.DATASET)]
    checks = base.build_kind_many("market", ctxs, lead=ctxs[0][1])
    return checks[ms.NAME], " ".join(ctxs[0][1].notes)


def _snapshot() -> dict[str, list]:
    with serving.open_read("market") as con:
        return {t: sorted(con.execute(f"SELECT * EXCLUDE (run_id) FROM {t}").fetchall(), key=repr) for t in TABLES}


def _same_as_whole(monkeypatch) -> None:
    incremental = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    _, note = _build(shrink=True)
    assert "built whole" in note, note
    monkeypatch.delenv(dirty.WHOLE_ENV)
    assert _snapshot() == incremental


def test_the_first_build_is_whole_and_the_next_one_with_nothing_changed_is_on_the_live_copy():
    _seed()
    first, note = _build()
    assert first["result"] != "failed" and "built whole" in note, note
    before = _snapshot()
    assert {t: len(r) for t, r in before.items()} == {ms.TABLE: 24, spot.TABLE: 6, greek.TABLE: 6}
    again, note = _build()
    assert "built on the live file: 0 Parquet file(s) changed, 0 key(s) dirty" in note, note
    assert again["result"] == "unchanged" and _snapshot() == before


def test_changed_new_and_gone_partitions_serve_what_a_whole_build_serves(monkeypatch):
    _seed()
    _build()
    _uw(spot, D3)                                   # a new session
    _uw(greek, D2, value=2.0)                       # a session re-typed with other values
    shutil.rmtree(greek.partition_path(D1).parent)  # a session gone
    _series(fred, "DGS10", 2026, 9.9)               # one source's year restated
    check, note = _build(shrink=True)
    assert "built on the live file: 4 Parquet file(s) changed, 4 key(s) dirty" in note, note
    assert check["added"] == 3 and check["removed"] == 3, check      # market_series: 3 values changed
    with serving.open_read("market") as con:
        assert con.execute(f"SELECT count(*) FROM {greek.TABLE} WHERE session_date = ?", (D1,)).fetchone() == (0,)
        assert con.execute(f"SELECT count(*) FROM {spot.TABLE} WHERE session_date = ?", (D3,)).fetchone() == (3,)
    _same_as_whole(monkeypatch)


def test_market_series_dirty_keys_name_the_source_and_the_year():
    ctx = base.RunContext(run_id="t", dataset=ms.NAME)
    ctx.changed = (ms.partition_path(fred, 2026), ms.partition_path(treasury, 2025),
                   spot.partition_path(D1))
    assert ms.DATASET.dirty(ctx).keys == {"fred/2026", "treasury/2025"}
    assert spot.DATASET.dirty(ctx).keys == {D1.isoformat()}
    assert greek.DATASET.dirty(ctx) is dirty.CLEAN
    with pytest.raises(ValueError):
        ms._where("nosuch/2026")

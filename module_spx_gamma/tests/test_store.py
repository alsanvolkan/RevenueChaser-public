"""The SPX Gamma board file, boards/spx_gamma.duckdb (SPX Gamma review 9 Nov 2026, D1-D5)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from RevenueChaser.core import rawstore
from RevenueChaser.module_spx_gamma import store

_PKG = Path(__file__).resolve().parents[1]
_REPO = _PKG.parent


def test_the_tables_live_in_the_board_file():
    for t in (store.RAW_DAYS, store.RAW_FILES, store.BRIER, store.GEX, store.META):
        assert rawstore._db_of(t) == "spx_gamma"
    assert "spx_gamma" in rawstore.BOARD_DBS


def test_a_raw_day_round_trips_byte_exact():
    body = '{"data": [{"strike": "7500", "x": 1.10}]}'.encode()
    store.put_raw_day("2026-11-05", {"requested_date": "2026-11-05", "spot": 7580.0, "files": {"a.json": {}}},
                      {"a.json": body, "b.json": b"[]"})
    assert store.has_day("2026-11-05") and store.stored_days() == ["2026-11-05"]
    assert store.latest_day() == "2026-11-05"
    assert store.raw_files("2026-11-05") == {"a.json": body.decode(), "b.json": "[]"}
    assert store.raw_file_hashes("2026-11-05")["a.json"] == hashlib.sha256(body).hexdigest()
    assert store.raw_manifest("2026-11-05")["spot"] == 7580.0


def test_a_replaced_day_keeps_no_old_file():
    store.put_raw_day("2026-11-05", {}, {"a.json": b"1", "old.json": b"2"})
    store.put_raw_day("2026-11-05", {}, {"a.json": b"3"})
    assert store.raw_files("2026-11-05") == {"a.json": "3"}


def test_a_scored_brier_record_is_never_changed():
    store.put_brier([{"date": "2026-11-02", "scenario_probs": {"Upside": 1.0}, "outcome": "Upside", "brier": 0.0}])
    store.put_brier([{"date": "2026-11-02", "scenario_probs": {"Upside": 0.0}, "outcome": None, "brier": None}])
    [rec] = store.brier_records()
    assert rec["date"] == "2026-11-02" and rec["outcome"] == "Upside" and rec["scenario_probs"] == {"Upside": 1.0}


def test_every_gex_day_is_kept():
    """D2: no 10-day prune."""
    for i in range(1, 13):
        store.put_gex(f"2026-10-{i:02d}", {"spot": float(i), "regime": "Long gamma"})
    days = store.gex_days()
    assert len(days) == 12 and days["2026-10-01"]["spot"] == 1.0


def test_meta_round_trips():
    store.put_meta("latest", {"asof_date": "2026-11-05"})
    assert store.latest() == {"asof_date": "2026-11-05"}


def test_only_the_store_module_writes_the_board_file():
    """One owner (review §5): no other module names an spx_ table in a write."""
    names = "|".join(("spx_raw_days", "spx_raw_files", "spx_brier", "spx_gex_daily", "spx_meta"))
    pattern = re.compile(rf"(write_rows|replace_all|execute|atomic|transaction)\([^)]*({names})")
    offenders = []
    for path in _REPO.rglob("*.py"):
        rel = path.relative_to(_REPO)
        if "tests" in rel.parts or rel == Path("module_spx_gamma/store.py") or rel.parts[0] == ".git":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text) or 'db="spx_gamma"' in text and "transaction" in text:
            offenders.append(str(rel))
    assert offenders == []


def test_a_fetch_manifest_spot_stores_its_value():
    """A Fetch's manifest carries ``spot`` as ``{"source", "value"}``; the column takes the number."""
    store.put_raw_day("2026-11-06", {"spot": {"source": "rcdata", "value": 6749.0}}, {"a.json": b"{}"})
    assert store._query("SELECT spot FROM spx_raw_days")[0]["spot"] == 6749.0
    assert store.raw_manifest("2026-11-06")["spot"] == {"source": "rcdata", "value": 6749.0}

"""The reads the Settings / Data control board adds to ``newdata.collect`` (design v28, avo 30 Oct 2026).

The universe bar's sets come from the spine's ``security`` (A, the funds of B, the rest of B, C, D); the Backfill
calendar's fill state is the number of names with a bar on each session of ``price_bar_daily``; the journey table
says where each dataset sits (the live file's size, the HDD raw and Parquet folders). Every one is a read: the
serving files through ``open_read``, the folders by a bounded walk, both cached by the hour.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import calendar, config
from RevenueChaser.module_settings import newdata
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build.prices import serving as prices_serving
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 14 SEC-file names

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)
    newdata.clear_sample_cache()
    yield
    newdata.clear_sample_cache()


def _bars(rows):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                    "build_id TEXT)")
        con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('prices', 1, 1761700000, 'test')")
        cols = ", ".join(f'"{c}" {t}' for c, t in prices_serving.SCHEMAS["price_bar_daily"])
        con.execute(f"CREATE TABLE price_bar_daily ({cols})")
        for sid, day in rows:
            con.execute("INSERT INTO price_bar_daily (security_id, session_date, close, source) VALUES "
                        "(?, ?, 1.0, 'alpaca/bars')", [sid, day])
    finally:
        con.close()


@pytest.mark.usefixtures("built")
def test_the_universe_sets_are_counted_from_the_spine_with_bs_tickers_split_by_type():
    u = newdata.collect(NOW)["universe"]
    c = u["counts"]
    assert (c["A"], c["C"], c["D"]) == (10, 1, 1)
    assert c["E"] + c["R"] == 2 and u["total"] == 14
    assert len(u["tickers"]["E"]) == c["E"] and len(u["tickers"]["R"]) == c["R"]
    con = serving.open_read("spine")
    try:
        typed = dict(con.execute("SELECT ticker, security_type FROM security WHERE tier = 'B'").fetchall())
    finally:
        con.close()
    assert all(typed[t] in ("etp", "cef") for t in u["tickers"]["E"])
    assert all(typed[t] not in ("etp", "cef") for t in u["tickers"]["R"])


def test_no_spine_means_no_universe_rather_than_a_universe_of_zero(market_roots):
    assert newdata.collect(NOW)["universe"] is None


def _spine(rows):
    """A live spine holding only ``security``: (ticker, tier, security_type, gvkey, iid) per name."""
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE security (ticker TEXT, cik TEXT, tier TEXT, security_type TEXT, gvkey TEXT, "
                    "iid TEXT, composite_figi TEXT, source_as_of DATE)")
        for ticker, tier, kind, gvkey, iid in rows:
            con.execute("INSERT INTO security VALUES (?, NULL, ?, ?, ?, ?, NULL, DATE '2026-10-28')",
                        [ticker, tier, kind, gvkey, iid])
    finally:
        con.close()


def test_the_calendar_reads_names_per_session_once_an_hour(market_roots, monkeypatch):
    _bars([("A1", "2026-10-27"), ("A1", "2026-10-28"), ("B2", "2026-10-28")])
    calls = []
    real = newdata._session_counts
    monkeypatch.setattr(newdata, "_session_counts", lambda con: calls.append(1) or real(con))
    assert newdata.collect(NOW)["sessions"] == {"2026-10-27": {"-": 1}, "2026-10-28": {"-": 2}}, \
        "without a spine no bar is in a tier set: it counts as off the spine"
    newdata.collect(NOW)
    assert calls == [1], "the second render in the same hour reads the cache"


def test_names_per_session_are_counted_per_tier_set_of_the_spine(market_roots):
    """Review M1: the calendar and the universe tile compare this with the picked sets, so it is counted by set, not
    over every bar in the file. A fund-only universe must not read Tier A's bars as its own."""
    _spine([("AAA", "A", "operating", "g1", "01"), ("ETF", "B", "etp", "g2", "01"), ("PRF", "B", "preferred", "g3", "01"),
            ("OTC", "C", "operating", "g4", "01")])
    _bars([("g1:01", "2026-10-27"), ("g1:01", "2026-10-28"), ("g2:01", "2026-10-28"), ("g3:01", "2026-10-28"),
           ("g4:01", "2026-10-28"), ("gone", "2026-10-28")])
    assert newdata.collect(NOW)["sessions"] == {
        "2026-10-27": {"A": 1}, "2026-10-28": {"A": 1, "E": 1, "R": 1, "C": 1, "-": 1}}


def test_no_prices_file_means_no_sessions(market_roots):
    assert newdata.collect(NOW)["sessions"] == {}


def test_storage_counts_each_datasets_raw_and_parquet_folders_and_the_live_file(market_roots):
    from RevenueChaser.rcdata.build import base
    raw = base.raw_root() / "alpaca" / "bars" / "2026-10-28"
    raw.mkdir(parents=True)
    (raw / "a-1.json.gz").write_bytes(b"x" * 10)
    (raw / "a-2.json.gz").write_bytes(b"y" * 5)
    _bars([("A1", "2026-10-28")])
    st = newdata.collect(NOW)["storage"]
    bars = st["datasets"]["alpaca/bars"]
    assert (bars["raw"]["files"], bars["raw"]["bytes"], bars["raw"]["capped"]) == (2, 15, False)
    assert bars["parquet"]["files"] is None, "a folder that does not exist is not an empty one"
    assert st["files"]["prices"]["bytes"] == os.path.getsize(serving.live_path("prices"))
    assert st["files"]["options"]["bytes"] is None
    assert st["roots"]["ssd"] == str(config.MARKET_DIR)


def test_a_walk_stops_at_its_cap_and_says_the_count_is_a_floor(market_roots, monkeypatch):
    from RevenueChaser.rcdata.build import base
    raw = base.raw_root() / "sec" / "companyfacts"
    raw.mkdir(parents=True)
    for i in range(5):
        (raw / f"{i}.json.gz").write_bytes(b"z")
    monkeypatch.setattr(newdata, "STORAGE_WALK_BUDGET", 3)
    got = newdata.collect(NOW)["storage"]["datasets"]["sec/companyfacts"]["raw"]
    assert got["capped"] is True and got["files"] == 3


def test_the_cap_is_one_budget_for_the_whole_walk_not_one_per_folder(market_roots, monkeypatch):
    """Review S3: the walk runs on a page render, on the HDD; ~38 datasets x 2 roots x a per-folder cap was millions
    of stat calls. One budget bounds the whole walk, and every folder past it says its count is a floor."""
    from RevenueChaser.rcdata.build import base
    for name in ("alpaca/bars", "sec/companyfacts", "sec/13f"):
        for root in (base.raw_root(), base.parquet_root()):
            folder = root.joinpath(*name.split("/"))
            folder.mkdir(parents=True)
            for i in range(3):
                (folder / f"{i}.bin").write_bytes(b"z")
    monkeypatch.setattr(newdata, "STORAGE_WALK_BUDGET", 4)
    st = newdata.collect(NOW)["storage"]["datasets"]
    raws = [st[n][r] for n in ("alpaca/bars", "sec/companyfacts", "sec/13f") for r in ("raw", "parquet")]
    assert sum(r["files"] or 0 for r in raws) <= 4
    assert sum(r["capped"] for r in raws) >= 2, "the folders past the budget say so"


def test_a_card_carries_its_last_three_checks_newest_first(market_roots):
    for day in ("2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"):
        ops.record_check("alpaca/bars", "changed", source_as_of=day, checked_on=day, added=5, calls=2)
    card = next(c for c in newdata.collect(NOW)["datasets"] if c["dataset"] == "alpaca/bars")
    assert [r["source_as_of"] for r in card["recent"]] == ["2026-10-28", "2026-10-27", "2026-10-26"]
    assert card["recent"][0]["added"] == 5 and card["recent"][0]["calls"] == 2


def test_every_served_tables_rows_and_newest_day_are_in_one_map(market_roots):
    _bars([("A1", "2026-10-28")])
    assert newdata.collect(NOW)["tables"]["price_bar_daily"] == {"rows": 1, "newest": "2026-10-28"}

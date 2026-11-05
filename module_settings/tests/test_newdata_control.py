"""The reads the Settings / Data control board adds to ``newdata.collect`` (design v28, avo 30 Oct 2026).

The universe bar's sets come from the spine's ``security`` (A, the funds of B, the rest of B, C, D); the Backfill
calendar's fill state is the number of names with a bar on each session of ``price_bar_daily``; the journey table
says where each dataset sits (the live file's size, the HDD raw and Parquet folders). Every one is a read: the
serving files through ``open_read``, the folders by a bounded walk, both cached by the hour.
"""
from __future__ import annotations

import itertools
import json
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


def test_the_calendar_counts_one_row_per_name_because_the_bars_key_says_so():
    """``_session_counts`` counts rows, not distinct names, which is only the same number because
    ``(security_id, session_date)`` is ``price_bar_daily``'s key. If that key ever changes, the calendar starts
    counting bars instead of names and nothing else would say so (the DISTINCT cost 8-10 s against 2 s on the live
    2.9 GB store, 5 Nov 2026, so it is not going back in as insurance)."""
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    assert prices_serving.KEYS["price_bar_daily"] == ("security_id", "session_date")


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


def test_the_walk_counts_each_datasets_raw_and_parquet_folders(market_roots):
    from RevenueChaser.rcdata.build import base
    raw = base.raw_root() / "alpaca" / "bars" / "2026-10-28"
    raw.mkdir(parents=True)
    (raw / "a-1.json.gz").write_bytes(b"x" * 10)
    (raw / "a-2.json.gz").write_bytes(b"y" * 5)
    picture = newdata.walk_storage()
    bars = picture["datasets"]["alpaca/bars"]
    assert (bars["raw"]["files"], bars["raw"]["bytes"], bars["raw"]["capped"]) == (2, 15, False)
    assert bars["parquet"]["files"] is None, "a folder that does not exist is not an empty one"
    assert picture["walked_at"], "the picture says when it was taken"


def test_the_render_stats_the_serving_files_live_and_reads_the_folders_from_the_picture(market_roots):
    """#434: four stats of the SSD are a live number and stay in the render; the HDD walk is a picture the job
    takes, and until it has the page must not read "no folder yet" as if it had looked."""
    _bars([("A1", "2026-10-28")])
    st = newdata.collect(NOW)["storage"]
    assert st["files"]["prices"]["bytes"] == os.path.getsize(serving.live_path("prices")), "stat'ed now"
    assert st["files"]["options"]["bytes"] is None
    assert st["roots"]["ssd"] == str(config.MARKET_DIR)
    assert (st["datasets"], st["walked_at"]) == ({}, None), "no picture here yet"

    newdata.STORAGE_PICTURE.parent.mkdir(parents=True, exist_ok=True)
    newdata.STORAGE_PICTURE.write_text(json.dumps(
        {"datasets": {"alpaca/bars": {"raw": {"files": 7, "bytes": 70, "capped": False}}},
         "walked_at": "2026-11-05T11:00:00+00:00"}), encoding="utf-8")
    st = newdata.collect(NOW)["storage"]
    assert st["datasets"]["alpaca/bars"]["raw"]["files"] == 7
    assert st["walked_at"] == "2026-11-05T11:00:00+00:00"


def test_a_missing_or_broken_picture_reads_as_not_walked_rather_than_as_empty_folders(market_roots):
    """A half-written or hand-edited file must not make every dataset look like it has no folder."""
    newdata.STORAGE_PICTURE.parent.mkdir(parents=True, exist_ok=True)
    for bad in ("{", "[]", '{"walked_at": "2026-11-05T11:00:00+00:00"}', '{"datasets": 3}'):
        newdata.STORAGE_PICTURE.write_text(bad, encoding="utf-8")
        st = newdata.collect(NOW)["storage"]
        assert (st["datasets"], st["walked_at"]) == ({}, None), bad


def test_a_card_carries_its_last_three_checks_newest_first(market_roots):
    for day in ("2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"):
        ops.record_check("alpaca/bars", "changed", source_as_of=day, checked_on=day, added=5, calls=2)
    card = next(c for c in newdata.collect(NOW)["datasets"] if c["dataset"] == "alpaca/bars")
    assert [r["source_as_of"] for r in card["recent"]] == ["2026-10-28", "2026-10-27", "2026-10-26"]
    assert card["recent"][0]["added"] == 5 and card["recent"][0]["calls"] == 2


def test_every_served_tables_rows_and_newest_day_are_in_one_map(market_roots):
    _bars([("A1", "2026-10-28")])
    assert newdata.collect(NOW)["tables"]["price_bar_daily"] == {"rows": 1, "newest": "2026-10-28"}


def test_the_walk_stops_when_it_runs_out_of_time_and_says_the_count_is_a_floor(market_roots, monkeypatch):
    """The ceiling is the job's now, not a render's (#434), but a pathological tree must still not hold the
    long-jobs worker for ever."""
    from RevenueChaser.rcdata.build import base
    raw = base.raw_root() / "sec" / "companyfacts"
    raw.mkdir(parents=True)
    (raw / "0.json.gz").write_bytes(b"z")
    monkeypatch.setattr(newdata, "STORAGE_WALK_SECONDS", -1.0)      # out of time at the first entry
    got = newdata.walk_storage()["datasets"]["sec/companyfacts"]["raw"]
    assert got["capped"] is True and got["files"] == 0


def test_the_walk_shares_one_deadline_and_a_folder_past_it_says_the_count_is_a_floor(market_roots, monkeypatch):
    """One deadline for the whole picture, not a slice per folder. #433 sliced it because the budget was a page
    render's 20 s and the first dataset walked could spend all of it; in a job the budget is 600 s against a
    measured 25 s walk, so rationing it only guaranteed three folders a floor for ever.

    The clock is faked at a sixth of the budget per read, so whichever folder is walked first spends the whole of
    it on its six entries and the two after it are out of time before their first entry. A slice per folder gave
    all three the same count; one shared deadline gives one of them everything. The assertion is about that and not
    about how fast this box's disk is, and it does not assume which folder ``_datasets()`` reaches first."""
    from RevenueChaser.rcdata.build import base
    names = ("alpaca/bars", "sec/companyfacts", "sec/13f")
    for name in names:
        folder = base.raw_root().joinpath(*name.split("/"))
        folder.mkdir(parents=True)
        for i in range(6):
            (folder / f"{i}.bin").write_bytes(b"z")
    step = newdata.STORAGE_WALK_SECONDS / 6
    ticks = itertools.count(step, step)
    monkeypatch.setattr(newdata.time, "monotonic", lambda: next(ticks))
    picture = newdata.walk_storage()["datasets"]
    got = [picture[n]["raw"] for n in names]
    assert sorted(f["files"] for f in got) == [0, 0, 6], "one shared budget, not three equal slices"
    assert [f["capped"] for f in got].count(False) == 1, "the folder that finished says its count is exact"

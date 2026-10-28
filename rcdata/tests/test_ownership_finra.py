"""``finra/short_interest``: the published dates read once a run, every page of a date until FINRA's
``record-total``, a short answer refused and not kept, only newer dates fetched on later runs, a backfill of the
missing ones, and each symbol named by the spine."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import finra

from . import ownership_fixture as ox

OLD, MID, NEW = "2026-09-15", "2026-09-30", "2026-10-15"


def _rows(day: str, n: int) -> list[dict]:
    named = [ox.finra_row("MSFT", day, 50_000_000), ox.finra_row("BFA", day, 120_000),
             ox.finra_row("BRK.B", day, 9_000_000)]
    return named + [ox.finra_row(f"Z{i:05d}", day, 1000 + i) for i in range(n - len(named))]


@pytest.fixture
def src(monkeypatch, http_mock):
    monkeypatch.setenv("RC_FINRA_API_BASE", "https://finra.test")
    feed = ox.Finra({OLD: _rows(OLD, 40), MID: _rows(MID, 40), NEW: _rows(NEW, 12_003)})
    feed.install(http_mock)
    ox.make_spine()
    return feed


def _run(day: str = "2026-10-28", **params) -> dict:
    return base.run_request({"dataset": finra.NAME, "params": {"as_of": day, **params}})


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def test_the_first_run_fetches_the_newest_date_page_by_page(src, http_mock):
    row = _run()
    assert row["result"] == "changed" and row["rows"] == 12_003
    assert len(http_mock.calls("/partitions/")) == 1
    pages = http_mock.calls("/data/")
    assert len(pages) == 3 and row["calls"] == 4          # 5,000 + 5,000 + 2,003 rows
    assert finra.stored_dates() == [date(2026, 10, 15)]
    assert _served("SELECT count(*) FROM finra_short_interest") == [(12_003,)]
    assert _served("SELECT settlement_date, current_short, days_to_cover, ticker, ticker_source "
                   "FROM finra_short_interest WHERE symbol = 'MSFT'") == [
        (date(2026, 10, 15), 50_000_000, 1.5, "MSFT", "spine:exact")]


def test_a_class_share_is_named_through_the_spine(src):
    _run()
    assert _served("SELECT symbol, ticker, ticker_source FROM finra_short_interest WHERE symbol IN ('BFA', 'BRK-B') "
                   "ORDER BY symbol") == [("BFA", "BF-A", "spine:squash"), ("BRK-B", "BRK-B", "spine:exact")]
    assert _served("SELECT count(*) FROM finra_short_interest WHERE ticker IS NULL") == [(12_000,)]


def test_a_date_whose_pages_do_not_add_up_is_refused_and_not_kept(src):
    src.total_override[NEW] = 12_500
    row = _run()
    assert row["result"] == "failed" and "record-total says 12500" in row["note"]
    assert finra.stored_dates() == []
    assert not serving.live_path("ownership").exists()


def test_a_later_run_fetches_only_the_dates_after_the_newest_stored(src, http_mock):
    _run()
    src.dates["2026-10-31"] = _rows("2026-10-31", 10)
    http_mock.requests.clear()
    row = _run("2026-11-12")
    assert [c.content for c in http_mock.calls("/data/")] and len(http_mock.calls("/data/")) == 1
    assert finra.stored_dates() == [date(2026, 10, 15), date(2026, 10, 31)]
    assert row["source_as_of"] == "2026-10-31"


def test_a_date_already_stored_is_not_fetched_again(src, http_mock):
    _run()
    http_mock.requests.clear()
    row = _run()
    assert http_mock.calls("/data/") == [] and row["calls"] == 1


def test_a_backfill_fetches_every_missing_date_of_its_range(src, http_mock):
    _run()
    row = base.run_request({"dataset": finra.NAME, "params": {"mode": "backfill", "from": "2026-09-20",
                                                              "to": "2026-10-28"}})
    assert row["result"] == "changed"
    assert finra.stored_dates() == [date(2026, 9, 30), date(2026, 10, 15)]
    assert _served("SELECT count(DISTINCT settlement_date) FROM finra_short_interest") == [(2,)]


def test_with_no_spine_no_symbol_is_named(src):
    serving.live_path("spine").unlink()
    row = _run()
    assert row["result"] == "changed" and "no spine file yet" in row["note"]
    assert _served("SELECT count(*) FROM finra_short_interest WHERE ticker IS NOT NULL") == [(0,)]


def test_a_stored_date_with_no_symbol_is_a_source_error_not_an_index_error(src):
    """Review N1: a raw file whose rows all lack a symbol (a corrupted file on disk) fails as a source error."""
    raw = base.write_raw_json(finra.raw_path(date(2026, 10, 15)), {
        "settlement_date": "2026-10-15", "pages": [[{**ox.finra_row("MSFT", "2026-10-15", 1_000), "symbolCode": ""}]]})
    with pytest.raises(base.SourceError, match="no row with a symbol"):
        finra.DATASET.to_parquet_all(base.RunContext(run_id="t", dataset=finra.NAME), [raw])

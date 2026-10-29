"""L5 (#377): a name comes from the spine first and from the recorded history second.

Before G9 a LEAPS Fetch resolved names through the UW screener and recorded them with every footprint
(``leaps_footprint_history``), so a Refresh read the last recorded session's names, with the 13F meta as the
fallback. G9 reads no provider: the spine (any tier, ``module_sec_13f.store_reads.meta``) names a ticker, and a ticker
the spine does not carry keeps the name an older Fetch recorded.
"""
from datetime import date, datetime, timezone

from RevenueChaser.core import config
from RevenueChaser.module_leaps import history as leaps_history
from RevenueChaser.module_leaps.scan import scan_leaps, scan_leaps_cached
from RevenueChaser.module_leaps.tests.options_store import pin
from RevenueChaser.module_sec_13f.tests.ownership_store import _write_spine

TODAY = date(2026, 10, 6)
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()


def _footprint(ticker, name):
    return {"ticker": ticker, "name": name, "snapshot": {"ticker": ticker, "label": "Quiet"},
            "inputs": {}, "fetch_status": "ok"}


def _spine(names: dict):
    config.MARKET_DIR.mkdir(parents=True, exist_ok=True)
    _write_spine(config.MARKET_DIR, {t: (n, "Energy") for t, n in names.items()})


def test_refresh_reads_names_from_the_recorded_history(rawstore_tmp_db):
    pin("ZZZZ")
    leaps_history.record([_footprint("ZZZZ", "Zed Corp")], run_id="r1", run_at=COMPLETE)
    watched, _ = scan_leaps_cached(today=TODAY)
    assert watched[0]["name"] == "Zed Corp"           # a ticker the spine does not carry


def test_the_spine_name_wins_over_an_older_recorded_name(rawstore_tmp_db):
    pin("ZZZZ")
    _spine({"ZZZZ": "Zed Spine"})
    leaps_history.record([_footprint("ZZZZ", "Zed Fetch")], run_id="r1", run_at=COMPLETE)
    for path in (scan_leaps, lambda *a, **k: scan_leaps_cached(**k)):
        assert path(None, today=TODAY)[0][0]["name"] == "Zed Spine"


def test_refresh_reads_the_spine_without_history(rawstore_tmp_db):
    pin("ZZZZ")
    _spine({"ZZZZ": "Zed Spine"})
    watched, _ = scan_leaps_cached(today=TODAY)
    assert watched[0]["name"] == "Zed Spine"


def test_a_blank_history_name_keeps_the_spine_name(rawstore_tmp_db):
    pin("ZZZZ")
    _spine({"ZZZZ": "Zed Spine"})
    leaps_history.record([_footprint("ZZZZ", "")], run_id="r1", run_at=COMPLETE)
    watched, _ = scan_leaps_cached(today=TODAY)
    assert watched[0]["name"] == "Zed Spine"


def test_the_fetch_records_the_name_it_shows(rawstore_tmp_db):
    pin("AAA")
    _spine({"AAA": "AAA Spine"})
    footprints: list = []
    scan_leaps(None, today=TODAY, footprints=footprints)
    assert footprints[0]["name"] == "AAA Spine"

"""C24 (#383 R2a): a Refresh shows the last Fetch's notes, read from its result-history header (K2).

A Fetch puts its notes on the board (LEAPS: the scan's notes; 13F: names not refreshed and the CUSIPs left out), and
the result history records them on the run's header (LEAPS ``warnings=notes``, 13F ``warnings=[unmatched]`` and the
names note as the partial reason). A Refresh used to render without them; now it shows them, marked as the last
Fetch's, with the time it ran. No recorded run, or one without notes: no line. A failed history read is no line,
never a failed Refresh. Offline, temp store and output folder.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.app import dashboard, runners
from RevenueChaser.app.tests.test_sec13f_job import GOOD, fake_uw, store  # noqa: F401
from RevenueChaser.core import history, ledger
from RevenueChaser.module_leaps import history as leaps_history
from RevenueChaser.module_sec_13f import engine as sec_13f, history as sec13f_history

TUE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()      # Tuesday 18:00 ET


@pytest.fixture
def leaps_out(rawstore_tmp_db, tmp_path, monkeypatch):
    import RevenueChaser.module_leaps.report as ld
    ledger.take_notes()
    monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(runners, "LEAPS_LATEST", tmp_path / "leaps_latest.html")
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    return tmp_path / "leaps_latest.html"


def test_a_leaps_refresh_shows_the_last_fetchs_notes(leaps_out):
    leaps_history.record([], run_id="f1", run_at=TUE, warnings=["UW flow <b>timed out</b> for 2 pages", "second"])
    html = runners.run_leaps_cached().read_text(encoding="utf-8")
    assert "Last Fetch (2026-10-06 22:00 UTC): UW flow &lt;b&gt;timed out&lt;/b&gt; for 2 pages second" in html


def test_a_leaps_refresh_with_no_recorded_fetch_or_no_notes_shows_none(leaps_out):
    assert "Last Fetch" not in runners.run_leaps_cached().read_text(encoding="utf-8")
    leaps_history.record([], run_id="f1", run_at=TUE)
    assert "Last Fetch" not in runners.run_leaps_cached().read_text(encoding="utf-8")


def test_a_failed_history_read_is_no_line_not_a_failed_refresh(leaps_out, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history locked")
    monkeypatch.setattr(history, "last_run", boom)
    assert "Last Fetch" not in runners.run_leaps_cached().read_text(encoding="utf-8")


def test_a_13f_refresh_shows_the_names_note_and_the_unmatched_cusips(store, tmp_path, monkeypatch):  # noqa: F811
    import RevenueChaser.module_sec_13f.report as sr
    ledger.take_notes()
    monkeypatch.setattr(sr, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(runners, "SEC_13F_LATEST", tmp_path / "sec_13f_latest.html")
    sec_13f.save_sec13f_meta({"AAPL": GOOD})
    sec_13f.recompute_totals()
    sec13f_history.record({"totals": sec_13f.load_totals()}, run_id="f1", run_at=TUE,
                          names_note="Names and sectors were not refreshed: key rejected.",
                          unmatched="7 CUSIPs with no ticker are left out")
    dashboard.app.config["TESTING"] = True
    r = dashboard.app.test_client().get("/view/module_sec_13f?refresh=1")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert ("Last Fetch (2026-10-06 22:00 UTC): Names and sectors were not refreshed: key rejected. "
            "7 CUSIPs with no ticker are left out") in html


def test_the_notice_reads_the_newest_run(rawstore_tmp_db):
    leaps_history.record([], run_id="old", run_at=TUE, warnings=["old note"])
    leaps_history.record([], run_id="new", run_at=TUE + 3600, warnings=["new note"])
    assert runners.last_fetch_notice("module_leaps") == "Last Fetch (2026-10-06 23:00 UTC): new note"


def test_the_notice_read_waits_only_briefly_for_another_process(rawstore_tmp_db, monkeypatch):
    """#383 C24 review (N4): the Refresh-time read runs inside ``rawstore.brief_wait()``, so another RevenueChaser
    process holding ``history.duckdb`` costs the Refresh ~0.35 s, not the ~29 s read budget."""
    from RevenueChaser.core import rawstore
    seen = []

    def last_run(*a, **k):
        seen.append(getattr(rawstore._LOCAL, "brief", False))
        return None
    monkeypatch.setattr(history, "last_run", last_run)
    assert runners.last_fetch_notice("module_leaps") is None
    assert seen == [True]

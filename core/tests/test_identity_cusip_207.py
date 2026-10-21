"""``identity_cusip`` (#207 F1, D19): the append-only change history of OpenFIGI's CUSIP answers (rule A)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import identity_cusip, rawstore

# 2026-10-14 21:00 UTC = 17:00 New York, after the close: session 2026-10-14.
RUN_AT = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc).timestamp()
LATER = RUN_AT + 86400


def _present(ticker, figi="F-" + "1", share_class_figi="S-1", name="CO"):
    return {"status": "present", "ticker": ticker, "figi": figi, "share_class_figi": share_class_figi,
            "share_class": None, "name": name, "exch_code": "US", "security_type": "Common Stock",
            "fetched_at": RUN_AT}


ABSENT = {"status": "absent", "ticker": None, "figi": None, "share_class_figi": None, "share_class": None,
          "name": None, "exch_code": None, "security_type": None, "fetched_at": RUN_AT}


def _rows():
    return rawstore.query("SELECT * FROM identity_cusip ORDER BY cusip, run_at")


@pytest.fixture
def store(rawstore_tmp_db):
    return rawstore_tmp_db


def test_a_first_answer_appends_present_and_absent_rows_with_the_run_stamps(store):
    out = identity_cusip.record({"02079K305": _present("GOOGL"), "999999999": ABSENT}, run_id="job-1", run_at=RUN_AT)
    assert out == {"answered": 2, "appended": 2, "unchanged": 0, "absent": 1}
    goog, missing = _rows()
    assert goog["cusip"] == "02079K305" and goog["status"] == "present" and goog["ticker"] == "GOOGL"
    assert goog["source"] == "openfigi" and goog["run_id"] == "job-1" and goog["run_at"] == RUN_AT
    assert goog["valid_from"] == date(2026, 10, 14) and goog["fetched_at"] == RUN_AT
    assert missing["status"] == "absent" and missing["ticker"] is None


def test_an_unchanged_answer_appends_nothing(store):
    identity_cusip.record({"02079K305": _present("GOOGL")}, run_id="job-1", run_at=RUN_AT)
    out = identity_cusip.record({"02079K305": _present("GOOGL")}, run_id="job-2", run_at=LATER)
    assert out["appended"] == 0 and out["unchanged"] == 1
    assert len(_rows()) == 1


def test_a_changed_answer_appends_a_row_and_the_old_one_stays(store):
    identity_cusip.record({"30303M102": _present("FB")}, run_id="job-1", run_at=RUN_AT)
    identity_cusip.record({"30303M102": _present("META")}, run_id="job-2", run_at=LATER)
    assert [r["ticker"] for r in _rows()] == ["FB", "META"]
    assert identity_cusip.current_map()["30303M102"]["ticker"] == "META"


def test_a_lost_match_appends_one_absent_row_and_leaves_the_map(store):
    identity_cusip.record({"111111111": _present("OLD")}, run_id="job-1", run_at=RUN_AT)
    identity_cusip.record({"111111111": ABSENT}, run_id="job-2", run_at=LATER)
    identity_cusip.record({"111111111": ABSENT}, run_id="job-3", run_at=LATER + 86400)
    assert [r["status"] for r in _rows()] == ["present", "absent"]
    assert "111111111" not in identity_cusip.current_map()


def test_the_current_map_is_the_newest_present_row_with_a_ticker(store):
    identity_cusip.record({"02079K305": _present("GOOGL", share_class_figi="S-A"),
                           "02079K107": _present("GOOG", share_class_figi="S-C"),
                           "000000001": _present(None), "999999999": ABSENT}, run_id="job-1", run_at=RUN_AT)
    current = identity_cusip.current_map()
    assert set(current) == {"02079K305", "02079K107"}
    assert current["02079K305"] == {"ticker": "GOOGL", "figi": "F-1", "share_class_figi": "S-A",
                                    "source": "openfigi", "run_at": RUN_AT}


def test_has_rows_says_whether_the_map_is_seeded(store):
    assert identity_cusip.has_rows() is False
    identity_cusip.record({"999999999": ABSENT}, run_id="job-1", run_at=RUN_AT)
    assert identity_cusip.has_rows() is True


def test_no_answers_write_nothing(store):
    assert identity_cusip.record({}, run_id="job-1", run_at=RUN_AT)["appended"] == 0
    assert _rows() == []


def test_a_run_outside_the_holiday_table_keeps_run_at_only(store):
    far = datetime(2031, 1, 6, 21, 0, tzinfo=timezone.utc).timestamp()
    identity_cusip.record({"111111111": _present("A")}, run_id="job-1", run_at=far)
    assert _rows()[0]["valid_from"] is None and _rows()[0]["run_at"] == far


def test_ever_mapped_lists_cusips_with_any_present_row_with_a_ticker(store):
    """The prefix fallback (#207 option B) never revives a CUSIP OpenFIGI once mapped and later dropped."""
    identity_cusip.record({"02079K305": _present("GOOGL"), "111111111": _present("GONE"), "999999999": ABSENT,
                           "222222222": _present(None)}, run_id="job-1", run_at=RUN_AT)
    identity_cusip.record({"111111111": ABSENT}, run_id="job-2", run_at=LATER)
    assert identity_cusip.ever_mapped() == {"02079K305", "111111111"}
    assert set(identity_cusip.current_map()) == {"02079K305"}


def test_ever_mapped_on_an_empty_table_is_empty(store):
    rawstore.ensure_table("identity_cusip")
    assert identity_cusip.ever_mapped() == set()

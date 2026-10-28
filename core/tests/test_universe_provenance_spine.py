"""The board header's universe line (``reporting_common.universe_provenance_html``) under the spine (build plan PR 6):
it names the spine, its sources and its last build, and keeps the old cache's "refreshed by" only for what that cache
still feeds (the market caps); on the old store the line is unchanged."""
from __future__ import annotations

import pytest

from RevenueChaser.core import universe
from RevenueChaser.core.reporting_common import universe_provenance_html
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names

WHEN = "2026-10-28T09:00:00+00:00"


@pytest.fixture
def spine(monkeypatch, built):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.universe_source() == "spine"


@pytest.mark.usefixtures("spine")
def test_the_line_names_the_spine_its_sources_and_its_build():
    last = ops.latest_unskipped_check("spine/security")
    assert universe.spine_provenance() == {"checked_on": last["checked_on"], "result": last["result"],
                                           "source_as_of": "2026-10-27", "rows": last["rows"]}
    line = universe_provenance_html(WHEN, "Swing Potential")
    assert "identity spine" in line and "SEC company tickers" in line
    word = "built" if last["result"] == "changed" else "checked"
    assert "SEC file of 2026-10-27" in line and f"{word} {last['checked_on']}" in line
    assert "Universe refreshed by" not in line                     # the old cache no longer picks the names
    assert "market caps refreshed by <b" in line and "Swing Potential" in line


@pytest.mark.usefixtures("spine")
def test_a_partial_pull_is_about_the_market_caps_only():
    line = universe_provenance_html(WHEN, "Swing Potential", partial=True)
    assert "partial fetch" not in line and "market caps partly stale" in line
    assert "identity spine" in universe_provenance_html("", "")    # no cache provenance: the spine is still named


@pytest.mark.usefixtures("spine")
def test_a_changed_build_says_built_and_an_unchanged_one_checked():
    ops.record_check("spine/security", "changed", source_as_of="2026-10-28", checked_on="2026-10-29")
    assert "built 2026-10-29" in universe_provenance_html(WHEN, "X")
    ops.record_check("spine/security", "unchanged", source_as_of="2026-10-28", checked_on="2026-10-30")
    assert "checked 2026-10-30" in universe_provenance_html(WHEN, "X")


@pytest.mark.usefixtures("spine")
def test_the_board_name_is_escaped():
    assert "<img" not in universe_provenance_html(WHEN, "<img src=x>")


def test_the_old_store_keeps_the_old_line(monkeypatch, built):
    monkeypatch.setenv(universe.SOURCE_ENV, "store")
    assert universe.spine_provenance() is None
    line = universe_provenance_html(WHEN, "Swing Potential", partial=True)
    assert "Universe refreshed by" in line and "partial fetch" in line and "spine" not in line

"""The board header's universe line (``reporting_common.universe_provenance_html``) under the spine (build plan PR 6):
it names the spine, its sources and its last build, and keeps the old cache's "refreshed by" only for what that cache
still feeds (the market caps); on the old store the line is unchanged."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import rawstore, universe
from RevenueChaser.core.reporting_common import universe_provenance_html
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names

WHEN = "2026-10-28T09:00:00+00:00"


@pytest.fixture
def spine(monkeypatch, built):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.universe_source() == "spine"


@pytest.fixture
def caps(spine):
    """A stored market cap: the old cache still feeds the caps, so "refreshed by" is about them."""
    rawstore.write_rows("equity_universe", [{"ticker": "MSFT", "market_cap": 3e12, "price": 400.0, "name": "MSFT"}])


def _built_at() -> str:
    """The live spine's build time as the line shows it, read straight from the serving file."""
    with serving.open_read("spine") as con:
        built = con.execute(f"SELECT max(built_at) FROM {serving.VERSION_TABLE}").fetchone()[0]
    return datetime.fromtimestamp(built, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@pytest.mark.usefixtures("caps")
def test_the_line_names_the_spine_its_sources_and_its_build():
    assert universe.spine_provenance() == {"built_at": _built_at(), "source_as_of": "2026-10-27"}
    line = universe_provenance_html(WHEN, "Swing Potential")
    assert "identity spine" in line and "SEC company tickers" in line
    assert "SEC file of 2026-10-27" in line and f"built {_built_at()}" in line
    assert "Universe refreshed by" not in line                     # the old cache no longer picks the names
    assert "market caps refreshed by <b" in line and "Swing Potential" in line


@pytest.mark.usefixtures("caps")
def test_a_partial_pull_is_about_the_market_caps_only():
    line = universe_provenance_html(WHEN, "Swing Potential", partial=True)
    assert "partial fetch" not in line and "market caps partly stale" in line
    assert "identity spine" in universe_provenance_html("", "")    # no cache provenance: the spine is still named


@pytest.mark.usefixtures("spine")
def test_the_build_time_is_the_spines_own_not_a_datasets_check():
    """Real-data check (28 Oct): ``spine/security`` reads "unchanged" when an earlier dataset of the kind already
    rebuilt the file, so its check says nothing about when the spine was built. A later check changes nothing.

    The check's day is a date no build can carry. It used to be 2026-10-30, which the line really did contain on
    30 Oct 2026 - the day the clock reached it - because the line carries the build's own day: the test failed on
    a date rather than on a change (CI run 40982614219, on a diff that does not touch this file).
    """
    later = "2099-01-01"
    before = universe_provenance_html(WHEN, "X")
    assert later not in before, "the sentinel day must be one the line can never carry"
    ops.record_check("spine/security", "unchanged", source_as_of="2026-10-28", checked_on=later)
    assert universe_provenance_html(WHEN, "X") == before and later not in before


@pytest.mark.usefixtures("spine")
def test_the_board_name_is_escaped():
    assert "<img" not in universe_provenance_html(WHEN, "<img src=x>")


def test_the_old_store_keeps_the_old_line(monkeypatch, built):
    monkeypatch.setenv(universe.SOURCE_ENV, "store")
    assert universe.spine_provenance() is None
    line = universe_provenance_html(WHEN, "Swing Potential", partial=True)
    assert "Universe refreshed by" in line and "partial fetch" in line and "spine" not in line


@pytest.mark.usefixtures("spine")
def test_no_market_cap_loaded_says_so_in_the_header():
    """Opus review N5: on the spine with no market cap stored (the host after the 27 Oct purge, until G2), the header
    says so, so an empty screened board explains itself without opening the funnel."""
    line = universe_provenance_html(WHEN, "Swing Potential")
    assert "market caps: none loaded yet" in line and "refreshed by" not in line
    rawstore.write_rows("equity_universe", [{"ticker": "MSFT", "market_cap": 3e12, "price": 400.0, "name": "MSFT"}])
    line = universe_provenance_html(WHEN, "Swing Potential")
    assert "none loaded" not in line and "market caps refreshed by <b" in line

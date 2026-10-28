"""PR 5 step 4: the spine on Settings' "New data system" section: a card per spine dataset (the attended WRDS ones
without Fetch or Backfill), and the identity funnel (SEC file -> listed -> Tier A -> with CUSIP / FIGI / GICS /
index membership) counted from ``spine.duckdb`` with the open build issues of ``spine/security`` by kind. It
replaces the hand-built identity coverage card of the old page (D9)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_settings import newdata, report
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names

NOW = datetime(2026, 10, 28, 3, 0, tzinfo=timezone.utc)
SPINE = ("sec/company_tickers_exchange", "wrds/sec_idhist", "wrds/co_hgic", "wrds/idxcst_his", "openfigi/map",
         "funds/index_holdings", "spine/security")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _card(data: dict, dataset: str) -> dict:
    return next(c for c in data["datasets"] if c["dataset"] == dataset)


def test_a_card_per_spine_dataset_with_its_cadence_and_the_attended_ones_marked():
    data = newdata.collect(NOW)
    assert set(SPINE) <= {c["dataset"] for c in data["datasets"]}
    assert {d: _card(data, d)["cadence"] for d in SPINE} == {
        "sec/company_tickers_exchange": "weekly", "wrds/sec_idhist": "monthly", "wrds/co_hgic": "monthly",
        "wrds/idxcst_his": "monthly", "openfigi/map": "weekly", "funds/index_holdings": "daily",
        "spine/security": "daily"}
    assert [d for d in SPINE if _card(data, d)["attended"]] == ["wrds/sec_idhist", "wrds/co_hgic", "wrds/idxcst_his"]
    assert _card(data, "spine/security")["coverage_target"] == 0.98
    assert all(not _card(data, d)["label"].startswith(d) for d in SPINE)


def test_no_spine_yet_says_so_instead_of_a_funnel():
    funnel = newdata.collect(NOW)["identity"]
    assert funnel["steps"] == [] and funnel["note"] == "the spine has not been built yet"


@pytest.mark.usefixtures("built")
def test_the_funnel_counts_the_spine_and_the_issues_come_by_kind():
    ops.record_issue("spine/security", "tier_a_no_cusip", "ZZZ", "<script>x</script>")
    funnel = newdata.collect(NOW)["identity"]
    steps = {s["key"]: s for s in funnel["steps"]}
    assert [(s["key"], s["count"]) for s in funnel["steps"]] == [
        ("sec", 14), ("listed", 12), ("tier_a", 10), ("cusip", 10), ("figi", 9), ("gics", 9), ("index", 7)]
    assert steps["listed"]["out_of"] == 14 and steps["figi"]["out_of"] == 10
    assert {d["key"]: d["count"] for d in funnel["drops"]} == {"otc": 1, "no_exchange": 1, "excluded_type": 2}
    assert funnel["indexes"] == {"sp500": 5, "sp400": 2, "sp600": 0, "ndx": 1}
    kinds = {k["kind"]: k["count"] for k in funnel["issue_kinds"]}
    assert kinds["tier_a_no_figi"] == 1 and kinds["tier_a_no_cusip"] == 1
    assert funnel["issues_open"] == sum(kinds.values())


@pytest.mark.usefixtures("built")
def test_the_funnel_and_the_cards_share_one_read_only_handle_per_kind(monkeypatch):
    seen = []
    real = serving.open_read
    def spy(kind):
        con = real(kind)                                  # raises for the market file, which this test never built
        seen.append(kind)
        return con
    monkeypatch.setattr(serving, "open_read", spy)
    newdata.collect(NOW)
    assert seen == ["spine"]                              # one handle on the spine for the cards and the funnel


@pytest.mark.usefixtures("built")
def test_the_page_shows_the_identity_card_and_no_longer_the_hand_built_one():
    ops.record_issue("spine/security", "tier_a_no_cusip", "ZZZ", "<script>x</script>")
    html = report.render_settings_dashboard({"newdata": newdata.collect(NOW), "rows": [], "identity_coverage": {
        "funnel": [], "funnel_note": "old", "inputs": [], "identifiers": []}})
    card = html[html.index('id="nd-identity"'):]
    card = card[:card.index("</section>")]
    assert "Identity" in card and "Tier A" in card and "With a CUSIP" in card and "S&amp;P 500" in card
    assert re.search(r"With a FIGI</span><span class=\"nd-v\">9 of 10", card)
    assert "<script>x" not in html and "&lt;script&gt;x&lt;/script&gt;" in card
    assert 'id="identity-coverage"' not in html and "Identity coverage" not in html


@pytest.mark.usefixtures("built")
def test_an_attended_card_has_no_fetch_or_backfill_button():
    html = report.render_settings_dashboard({"newdata": newdata.collect(NOW), "rows": []})
    wrds = html[html.index('data-dataset="wrds/sec_idhist" data-status'):]
    wrds = wrds[:wrds.index('<div class="nd-card', 10)]
    assert 'data-nd-act="fetch"' not in wrds and 'data-nd-act="backfill"' not in wrds
    assert "python -m RevenueChaser.rcdata.build backfill" in wrds and 'data-nd-act="pause"' in wrds
    assert "never queued by the schedule (attended)" in wrds

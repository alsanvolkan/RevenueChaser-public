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
        ("sec", 14), ("listed", 12), ("tier_a", 10), ("cusip", 10), ("figi", 10), ("gics", 9), ("index", 7)]
    assert steps["listed"]["out_of"] == 14 and steps["figi"]["out_of"] == 10
    assert {d["key"]: d["count"] for d in funnel["drops"]} == {"otc": 1, "no_exchange": 1, "excluded_type": 2}
    assert funnel["indexes"] == {"sp500": 5, "sp400": 2, "sp600": 0, "ndx": 1}
    kinds = {k["kind"]: k["count"] for k in funnel["issue_kinds"]}
    # NVR was the one Tier A name without a FIGI; the ticker lane found it, so no no-FIGI issue is open.
    assert "tier_a_no_figi" not in kinds and "tier_a_no_figi_by_ticker" not in kinds
    assert kinds["tier_a_no_cusip"] == 1
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


def test_a_never_built_spine_card_says_it_starts_by_hand():
    """Delta S6: until its first build, a starts-by-hand dataset is never due; its card says so (and the attended
    ones say they run from the CLI), instead of an unexplained "Next check due"."""
    from RevenueChaser.rcdata.build import due
    data = newdata.collect(NOW)
    assert _card(data, "funds/index_holdings")["next_check_due"] == due.BY_HAND_WHY
    assert _card(data, "wrds/co_hgic")["next_check_due"] == due.ATTENDED_WHY
    checks = {c["label"]: c for c in _card(data, "funds/index_holdings")["checks"]}
    assert checks["Next check due"]["detail"] == due.BY_HAND_WHY
    assert _card(data, "market_series")["next_check_due"] is None          # the market datasets are unchanged
    ops.record_check("funds/index_holdings", "changed", source_as_of="2026-10-27", next_check_due="2026-10-28")
    assert _card(newdata.collect(NOW), "funds/index_holdings")["next_check_due"] == "2026-10-28"

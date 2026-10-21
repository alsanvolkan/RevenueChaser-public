"""C16 (#383 R3): Swing and Fundamentals say why each name left the scan, in the shared funnel format.

Before, a Fetch counted its drops without names (``data_quality.dropped``) and folded the prefilter into one
"hygiene" number, so the board could only say "Eliminated: … data<252d · hygiene" (screeners review X-5). Now the
payload carries ``funnel_steps`` (``core.funnel``): the stored universe, then every typed reason a name has no card
(the excluded security types, the 7-A prefilter's three reasons on D04, the scorer's reasons on D01 and EDGAR, the
names that scored but passed none of the board's three screens), then the candidates. The steps add up
(``funnel.validate``), each drop lists at most 50 names, and a drop is marked "by design" (a rule) or not (a data
gap that needs a look). The panel itself is the board wave's (SW-S3); R3 builds the data. Offline: the scenario of
``core/tests/screen_parity.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from RevenueChaser.core import funnel, rawstore
from RevenueChaser.core import screen_common as sc
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import pipeline as sp

BOARDS = [pytest.param(sp, id="swing"), pytest.param(fp, id="fundamentals")]


@pytest.fixture
def scenario(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)


def _steps(payload) -> dict[str, dict]:
    return {s["key"]: s for s in payload["funnel_steps"]}


@pytest.mark.parametrize("mod", BOARDS)
def test_the_funnel_runs_from_the_stored_universe_to_the_candidates_and_adds_up(scenario, mod):
    payload = mod.run_scan()
    steps = payload["funnel_steps"]
    assert funnel.validate(steps) == []
    assert steps[0]["key"] == "universe" and steps[0]["count"] == len(scn.UNIVERSE)
    assert steps[-1]["key"] == "candidates" and steps[-1]["count"] == len(payload["candidates"]) > 0
    assert "unrecorded" not in _steps(payload)              # every name that left has its reason


@pytest.mark.parametrize("mod", BOARDS)
def test_the_excluded_types_and_the_prefilter_reasons_are_typed_steps(scenario, mod):
    steps = _steps(mod.run_scan())
    assert steps["excluded_types"]["count"] == 1 and steps["excluded_types"]["by_design"] is True
    assert steps["low_price"]["tickers"] == ["LOWPX"] and steps["low_price"]["by_design"] is True
    assert steps["low_price"]["label"] == "Price under $5.00"
    assert steps["low_dollar_volume"]["tickers"] == ["LOWVOL"] and steps["low_dollar_volume"]["by_design"] is True
    assert steps["low_dollar_volume"]["label"] == "Average daily dollar volume under $250,000"
    assert "no_quote" not in steps


@pytest.mark.parametrize("mod", BOARDS)
def test_the_scorer_reasons_name_their_tickers(scenario, mod):
    steps = _steps(mod.run_scan())
    assert steps["no_bars"]["tickers"] == ["NOBARS1"] and steps["no_bars"]["by_design"] is False
    assert steps["short_bars"]["tickers"] == ["SHORT1"] and steps["short_bars"]["by_design"] is True


def test_fundamentals_names_the_ticker_with_no_edgar_annual(scenario):
    steps = _steps(fp.run_scan())
    assert steps["no_edgar_annual"]["tickers"] == ["NOEDGAR"] and steps["no_edgar_annual"]["by_design"] is False


@pytest.mark.parametrize("mod", BOARDS)
def test_scored_names_that_pass_no_screen_are_their_own_by_design_step(scenario, mod):
    payload = mod.run_scan()
    steps = _steps(payload)
    scored = len(rawstore.query(f"SELECT ticker FROM {'swing' if mod is sp else 'fundamentals'}_scores"))
    passed_none = scored - len(payload["candidates"])
    assert passed_none > 0, "the scenario has scored names that pass none of the screens"
    assert steps["passed_no_screen"]["count"] == passed_none and steps["passed_no_screen"]["by_design"] is True


@pytest.mark.parametrize("mod", BOARDS)
def test_a_name_without_a_d04_quote_is_a_no_quote_drop_that_needs_a_look(scenario, mod):
    with rawstore.connect(db=rawstore.MANIFEST["quote_snapshot"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM quote_snapshot WHERE ticker = 'UP2'")
    payload = mod.run_scan()
    steps = _steps(payload)
    assert steps["no_quote"]["tickers"] == ["UP2"] and steps["no_quote"]["by_design"] is False
    assert funnel.validate(payload["funnel_steps"]) == []


@pytest.mark.parametrize("mod", BOARDS)
def test_a_ticker_that_raises_is_a_named_scoring_error(scenario, monkeypatch, mod):
    real = mod.load_ticker_info

    def info(ticker, *a, **k):
        if ticker == "UP3":
            raise ValueError("bad payload")
        return real(ticker, *a, **k)
    monkeypatch.setattr(mod, "load_ticker_info", info)
    payload = mod.run_scan()
    steps = _steps(payload)
    assert steps["compute_error"]["tickers"] == ["UP3"] and steps["compute_error"]["by_design"] is False
    assert funnel.validate(payload["funnel_steps"]) == []


@pytest.mark.parametrize("mod", BOARDS)
def test_a_scan_with_nothing_scored_still_says_where_every_name_went(scenario, monkeypatch, mod):
    def boom(ticker, *a, **k):
        raise ValueError("bad payload")
    monkeypatch.setattr(mod, "load_ticker_info", boom)
    payload = mod.run_scan()
    assert payload["no_data"] is True
    assert funnel.validate(payload["funnel_steps"]) == []
    assert payload["funnel_steps"][-1]["count"] == 0


@pytest.mark.parametrize("mod", BOARDS)
def test_an_explicit_top_n_cut_is_its_own_step_and_not_a_failed_screen(scenario, mod):
    full = _steps(mod.run_scan())
    cut = mod.run_scan(top_n=1)
    steps = _steps(cut)
    assert steps["beyond_top_n"]["count"] == full["candidates"]["count"] - 1 > 0
    assert steps["passed_no_screen"]["count"] == full["passed_no_screen"]["count"]
    assert steps["candidates"]["count"] == 1 and funnel.validate(cut["funnel_steps"]) == []


# ── the step builder on its own ───────────────────────────────────────────────

CFG = SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=250_000)


def test_a_reason_lists_at_most_50_names_sorted_and_counts_them_all():
    names = [f"T{i:03d}" for i in range(60, 0, -1)]
    steps = sc.funnel_steps(universe=60, excluded_types=0, prefiltered=sc.Prefiltered([], {}),
                            dropped={"no_bars": names}, scored=0, passed=0, shown=0, cfg=CFG)
    no_bars = {s["key"]: s for s in steps}["no_bars"]
    assert no_bars["count"] == 60 and len(no_bars["tickers"]) == funnel.MAX_TICKERS
    assert no_bars["tickers"] == sorted(names)[:funnel.MAX_TICKERS]
    assert funnel.validate(steps) == []


def test_names_no_reason_accounts_for_are_shown_as_unrecorded():
    steps = sc.funnel_steps(universe=10, excluded_types=1, prefiltered=sc.Prefiltered([], {"low_price": ["A"]}),
                            dropped={"no_bars": ["B"]}, scored=3, passed=3, shown=3, cfg=CFG)
    by_key = {s["key"]: s for s in steps}
    assert by_key["unrecorded"]["count"] == 10 - 1 - 1 - 1 - 3 and by_key["unrecorded"]["by_design"] is False
    assert funnel.validate(steps) == []


def test_a_reason_the_funnel_does_not_know_still_gets_its_own_step():
    steps = sc.funnel_steps(universe=2, excluded_types=0, prefiltered=sc.Prefiltered([], {}),
                            dropped={"no_ohlc": ["A"]}, scored=1, passed=1, shown=1, cfg=CFG)
    step = {s["key"]: s for s in steps}["no_ohlc"]
    assert step["label"] == "No cached price bars" and step["tickers"] == ["A"] and step["by_design"] is False
    assert funnel.validate(steps) == []


def test_names_cut_by_an_explicit_top_n_are_their_own_step():
    steps = sc.funnel_steps(universe=5, excluded_types=0, prefiltered=sc.Prefiltered([], {}), dropped={},
                            scored=5, passed=5, shown=2, cfg=CFG)
    by_key = {s["key"]: s for s in steps}
    assert by_key["beyond_top_n"]["count"] == 3 and by_key["candidates"]["count"] == 2
    assert funnel.validate(steps) == []

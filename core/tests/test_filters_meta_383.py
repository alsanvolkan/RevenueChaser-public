"""Common K1 (#383 R1): the filter contract's index meta, the sp600 label (#214) and its first-fill gate.

The S&P SmallCap 600 gets a board label but never joins ``INDEXES``: ``fetch_all_indexes`` walks ``INDEXES`` and
``fetch_index`` saves any list that clears its floor, while ``fetch_sp600`` makes no request, so joining would
fill sp600 from the dashboard path with no typed OK. ``fetch_index`` itself refuses a gated first fill; only the
weekly step with ``first_fill=["sp600"]`` writes it. Temp stores and fake fetchers only.
"""
from __future__ import annotations

import json
import re

import pytest

from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import rawstore
from RevenueChaser.core import reporting_common as rc


def _members(prefix: str, n: int) -> set[str]:
    return {f"{prefix}{i:04d}" for i in range(n)}


@pytest.fixture
def fetchers(rawstore_tmp_db, monkeypatch):
    """Fake fetchers that would return full lists (sp600: 601 names); ``calls`` records each fetch."""
    state = {"sp500": _members("S", 450), "dow": _members("D", 30), "nasdaq": _members("N", 1100),
             "sp600": _members("M", 601)}
    calls: list[str] = []

    def fetcher(slug):
        def f():
            calls.append(slug)
            return set(state[slug])
        return f

    monkeypatch.setattr(ir, "_FETCHERS", {s: fetcher(s) for s in state})
    return state, calls


def _stored(slug: str) -> int:
    return int(rawstore.query("SELECT count(*) AS n FROM index_constituents WHERE slug = ?", [slug])[0]["n"])


# ── the first-fill gate lives in fetch_index ──────────────────────────────────────────────────────────────────────
def test_fetch_index_refuses_a_gated_first_fill(fetchers):
    _, calls = fetchers
    assert ir.fetch_index("sp600") == set()
    assert ir.fetch_index("sp600", refresh=True) == set()
    assert _stored("sp600") == 0
    assert "sp600" not in calls                       # not even read


def test_fetch_all_indexes_on_an_empty_sp600_writes_nothing(fetchers):
    _, calls = fetchers
    ir.fetch_all_indexes()
    ir.fetch_all_indexes(refresh=True)
    assert _stored("sp600") == 0 and "sp600" not in calls
    assert _stored("sp500") == 450                    # the ungated lists still fill


def test_the_weekly_step_still_writes_the_named_first_fill(fetchers):
    res = ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    assert res["slugs"]["sp600"]["status"] == "written"
    assert _stored("sp600") == 601


def test_once_sp600_has_rows_fetch_index_may_refresh_it(fetchers):
    state, _ = fetchers
    ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    state["sp600"] = _members("M", 590)
    assert len(ir.fetch_index("sp600", refresh=True)) == 590


def test_sp600_never_joins_the_fetched_indexes():
    assert "sp600" not in dict(ir.INDEXES)
    assert set(ir.LABELS) == set(ir.STORED)            # every stored slug has a label
    assert ir.LABELS["sp600"] == "S&P SmallCap 600"


# ── labels and meta ───────────────────────────────────────────────────────────────────────────────────────────────
def test_a_stored_sp600_member_is_labelled_on_a_card(fetchers):
    ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    cards = ir.annotate_cards([{"ticker": "M0001"}, {"ticker": "S0001"}])
    assert cards[0]["index"] == ["S&P SmallCap 600"] and cards[1]["index"] == ["S&P 500"]


def test_filters_meta_lists_sp600_only_when_it_has_rows(fetchers):
    meta = rc.filters_meta("b")
    assert meta["indexes"] == ["S&P 500", "Dow Jones", "Nasdaq Composite"]
    assert meta["index_labels"] == {"Nasdaq Composite": "Nasdaq-listed"}
    ir.refresh_lists(run_id="r1", first_fill=["sp600"])
    assert rc.filters_meta("b")["indexes"] == ["S&P 500", "Dow Jones", "S&P SmallCap 600", "Nasdaq Composite"]


def test_filters_meta_carries_the_board_fields(rawstore_tmp_db):
    meta = rc.filters_meta("comovement", floor_mc=3e8, earnings_mode="mark")
    assert meta["board"] == "comovement" and meta["floor_mc"] == 3e8 and meta["earnings_mode"] == "mark"


def test_the_component_has_no_literal_index_list():
    js = (rc._STATIC / "rc_filters.js").read_text(encoding="utf-8")
    assert '"Dow Jones"' not in js and '"Nasdaq Composite"' not in js
    assert "meta.indexes || []" in js


def test_comovement_meta_carries_the_registry_indexes(rawstore_tmp_db):
    from RevenueChaser.module_comovement import report

    html = report.render_comovement_dashboard({"as_of": "2026-09-17T00:00:00+00:00", "clusters": {}, "cuts": []})
    m = re.search(r"var META = (.*?);\n", html)
    assert m, "META not found"
    meta = json.loads(m.group(1))
    assert meta["indexes"] == ["S&P 500", "Dow Jones", "Nasdaq Composite"]
    assert meta["board"] == "comovement"


# ── C23: one public theme-run lookup ─────────────────────────────────────────────────────────────────────────────
def test_current_theme_run_is_public_and_shared(rawstore_tmp_db):
    from RevenueChaser.core import universe
    from RevenueChaser.module_relatedness import themes

    assert universe.current_theme_run() is None
    rawstore.write_rows("theme_runs", [{"id": "r9", "is_current": 1}])
    assert universe.current_theme_run() == "r9"
    assert themes.current_run_id() == "r9"


def test_filter_fields_looks_up_the_theme_run_once(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import universe

    calls = []
    real = universe.current_theme_run
    monkeypatch.setattr(universe, "current_theme_run", lambda: calls.append(1) or real())
    universe.filter_fields(["AAA"])
    assert len(calls) == 1


def test_the_relatedness_guard_lookup_raises_on_a_failed_read(rawstore_tmp_db, monkeypatch):
    """A filter never breaks a board (None), but the guards that refuse to rebuild the current run must not read a
    failed lookup as "no current run"."""
    from RevenueChaser.core import universe
    from RevenueChaser.module_relatedness import themes

    def boom(*a, **k):
        raise RuntimeError("store locked")
    monkeypatch.setattr(rawstore, "query", boom)
    assert universe.current_theme_run() is None
    with pytest.raises(RuntimeError):
        themes.current_run_id()

"""Fable review S2 (SPX Gamma): a day filled by the 2-year history pull (manifest ``origin: history_pull``) was
served as a normal day. The pull asks only the calls that take a date (snapshot.HISTORY_ENDPOINTS), so the iron
butterfly's chain, IV rank, options volume and the per-expiry walls of the DTE bands are absent and Put Support
falls back to the provider's put wall, with no word on the board. The data-quality banner now says so. Offline."""
from __future__ import annotations

import json

from RevenueChaser.module_spx_gamma import render, snapshot as spx_snapshot
from .conftest import make_snapshot


def _day(root, asof, **manifest):
    day = root / "raw" / asof
    day.mkdir(parents=True, exist_ok=True)
    (day / "manifest.json").write_text(json.dumps({"warnings": [], **manifest}), encoding="utf-8")


def test_a_history_pull_day_says_what_it_does_not_hold(tmp_path):
    snap = make_snapshot()
    snap["quality"] = {k: True for k in snap["quality"]}
    _day(tmp_path, snap["asof_date"], origin=spx_snapshot.HISTORY_ORIGIN)
    notice = render.data_quality_notice(snap, tmp_path)
    assert notice and notice.startswith("History pull day:")
    for absent in ("iron butterfly", "IV rank", "options volume", "DTE-band walls", "Put Support"):
        assert absent in notice
    assert "History pull day:" in render.render_html(snap, data_root=tmp_path)


def test_a_fetched_day_has_no_history_line(tmp_path):
    snap = make_snapshot()
    snap["quality"] = {k: True for k in snap["quality"]}
    _day(tmp_path, snap["asof_date"])
    assert render.data_quality_notice(snap, tmp_path) is None


def test_the_line_names_only_calls_the_pull_does_not_make():
    assert {"iv_rank", "options_volume"}.isdisjoint(spx_snapshot.HISTORY_ENDPOINTS)
    assert "option_chain" not in spx_snapshot.HISTORY_ENDPOINTS

"""KL (#377, Card E): the shared filter kit on the LEAPS board.

Spec: plans/2026-10-15_card_e_filter_bars.md §1-2. Facets Index, Market cap, Sector, Theme; the kit composes
(AND) with the board's own controls; every tier is filtered, the Watchlist too (avo 2026-10-16); a failed ``filter_fields`` read
never fails a render.
"""
import ast
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

import RevenueChaser.module_leaps.report as ld


def _snap(t, score=70, name=None, label="Accumulating"):
    return {"ticker": t, "name": name or f"{t} Corp", "label": label, "score": score, "frequency": 4,
            "notional": 400000.0, "net_notional": 100000.0, "ask_share": 0.7, "near_term_event": False}


def _fields(tickers):
    return {t: {"ticker": t, "name": f"{t} Corp", "index": ["sp500"], "market_cap": 5e9,
                "gics": ["Technology", "Software"], "theme_ids": [], "earnings_date": None}
            for t in tickers}


@pytest.fixture
def render(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", _fields)
    monkeypatch.setattr("RevenueChaser.core.universe.theme_catalog", lambda *a, **k: {"t1": {"label": "AI"}})

    def go(watched, unwatched, **kw):
        return ld.render_leaps_dashboard(watched, unwatched, **kw).read_text(encoding="utf-8")
    return go


def _json_var(page, name):
    m = re.search(rf"var {name}\s*=\s*(.*?);\n", page)
    assert m, f"{name} not found"
    return json.loads(m.group(1))


def test_bar_mount_and_four_facets(render):
    page = render([_snap("WL1")], [_snap("U1"), _snap("U2", score=30)])
    assert 'id="rc-filters"' in page
    assert "RCFilters.mount(" in page
    meta = _json_var(page, "META")
    assert meta["board"] == "leaps"
    assert meta["facets"] == ["index", "mc", "gics", "theme"]


def test_data_has_one_row_per_board_ticker(render):
    page = render([_snap("WL1")], [_snap("U1"), _snap("U2", score=30)])
    data = _json_var(page, "DATA")
    assert sorted(r["ticker"] for r in data) == ["U1", "U2", "WL1"]
    assert {"ticker", "name", "index", "market_cap", "gics", "theme_ids"} <= set(data[0])


def test_every_tr_has_data_ticker(render):
    page = render([_snap("WL1")], [_snap("U1")])
    bodies = re.findall(r"<tbody>(.*?)</tbody>", page, re.S)
    trs = re.findall(r"<tr[^>]*>", "".join(bodies))
    assert trs and all("data-ticker=" in tr for tr in trs)


def test_the_kit_filters_every_tier_watchlist_included(render):
    page = render([_snap("WL1")], [_snap("U1")])
    # avo 2026-10-16 (KL card "Watchlist de"): the kit filters all four tiers, the Watchlist too
    # (static half; module_testing/e2e/test_board_leaps.py TestFilterKitInTheBrowser checks it in Chrome)
    assert "KIT_TIERS = ['watchlist','high','emerging','quiet']" in page
    assert "SCAN_TIERS" not in page


def test_failed_filter_fields_still_renders_with_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)

    def boom(_t):
        raise RuntimeError("store down")
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", boom)
    page = ld.render_leaps_dashboard([_snap("WL1")], [_snap("U1")]).read_text(encoding="utf-8")
    assert "could not be read" in page
    assert 'id="rc-filters"' in page
    data = _json_var(page, "DATA")
    assert sorted(r["ticker"] for r in data) == ["U1", "WL1"]
    assert all(set(r) == {"ticker", "name"} for r in data)


def test_hostile_ticker_and_name_are_escaped_in_data(render):
    bad = '</script><img src=x onerror=alert(1)>'
    page = render([], [_snap("U1", name=bad)])
    assert bad not in page


def test_count_line_and_tier_counts_present(render):
    page = render([_snap("WL1")], [_snap("U1")])
    assert 'id="kit-count"' in page
    assert 'class="n"' in page and "data-total=" in page


_COUNTS = Path(__file__).resolve().parents[2] / "scripts/research/383_card_e_kit_counts.py"


def _counts_mod():
    spec = importlib.util.spec_from_file_location("kit_counts", _COUNTS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_counts_script_imports_nothing_of_revenuechaser_before_the_store_root_points_at_the_copy():
    """Run as a file on the PC: the env seam (RC_STORE_DIR) only works before the first RevenueChaser import, and
    the package is importable only once the checkout's parent is on sys.path (429 pattern)."""
    src = _COUNTS.read_text(encoding="utf-8")
    top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in top if isinstance(n, ast.ImportFrom)]
    assert not [m for m in names if m.startswith("RevenueChaser")]
    assert "sys.path.insert(0, str(ROOT.parent))" in src
    assert 'RC_STORE_AUTO_ENSURE="0"' in src and "RC_STORE_DIR=" in src


def test_counts_script_copies_the_newest_snapshot_and_refuses_a_store_root_that_is_not_the_copy(
        tmp_path, monkeypatch):
    mod = _counts_mod()
    monkeypatch.setattr(mod, "OUT", tmp_path / "out")
    for k in ("RC_STORE_DIR", "RC_CACHE_DIR", "RC_STORE_AUTO_ENSURE", "RAW_STORE_BACKUP_MIRROR"):
        monkeypatch.setenv(k, os.environ.get(k, ""))          # restored after the test
    monkeypatch.setattr(sys, "path", list(sys.path))
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "raw_20261014T000000Z.duckdb").write_bytes(b"old")
    (backups / "raw_20261015T000000Z.duckdb").write_bytes(b"new")
    # This process imported config long ago, so its store root is not the copy: the script must refuse.
    with pytest.raises(SystemExit, match="refusing to read"):
        mod.main(["--board", "leaps", "--backups", str(backups)])
    assert (tmp_path / "out" / "store" / "raw.duckdb").read_bytes() == b"new"
    assert os.environ["RC_STORE_AUTO_ENSURE"] == "0"
    assert os.environ["RC_STORE_DIR"] == str(tmp_path / "out" / "store")


def test_counts_script_store_dir_must_be_a_copy_under_its_folder(tmp_path, monkeypatch):
    mod = _counts_mod()
    monkeypatch.setattr(mod, "OUT", tmp_path / "out")
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / "raw.duckdb").write_bytes(b"x")
    with pytest.raises(SystemExit):
        mod.main(["--board", "leaps", "--store-dir", str(tmp_path / "live")])


def test_counts_script_counts_the_rendered_rows_per_tier(render):
    page = render([_snap("WL1")], [_snap("U1"), _snap("U2", score=30), _snap("U3", score=5)])
    assert _counts_mod().rendered_rows(page) == {"watchlist": 1, "high": 1, "emerging": 1, "quiet": 1}


def test_counts_script_summarize():
    mod = _counts_mod()
    rows = [{"index": ["S&P 500"], "market_cap": 5e11, "gics": ["Technology", "x"], "theme_ids": ["a"]},
            {"index": [], "market_cap": None, "gics": ["Unclassified"], "theme_ids": []}]
    s = mod.summarize(rows)
    assert s["rows"] == 2 and s["market_cap"] == {"Mega": 1, "No cap": 1}
    assert s["theme"] == {"Themed": 1, "Unthemed": 1} and s["worked_example"]["rows"] == 1


def test_mount_wires_onchange_to_apply_kit(render):
    page = render([_snap("WL1")], [_snap("U1")])
    assert "onChange: applyKit" in page
    assert "applyKit(RCF)" in page          # the initial pass also applies a filter carried over from another board

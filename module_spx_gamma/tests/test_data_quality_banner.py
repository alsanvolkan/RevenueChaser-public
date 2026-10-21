"""The SPX board shows the day's Fetch manifest warnings and any non-ok quality flag (M-SPX-5, #376).

``warnings`` live in the raw day's manifest (``snapshot.py``); ``quality`` (booleans) in the
normalized snapshot (``normalize.py``). A clean day shows neither line.
"""
from __future__ import annotations

import json

from RevenueChaser.module_spx_gamma import render
from .conftest import make_snapshot

HOSTILE = '<img src=x onerror="alert(1)">'


def _write_manifest(root, asof, warnings):
    day = root / "raw" / asof
    day.mkdir(parents=True, exist_ok=True)
    (day / "manifest.json").write_text(json.dumps({"warnings": warnings}), encoding="utf-8")


def test_manifest_warning_is_shown_escaped(tmp_path):
    snap = make_snapshot()
    _write_manifest(tmp_path, snap["asof_date"], [HOSTILE])
    html = render.render_html(snap, data_root=tmp_path)
    assert "Fetch warnings" in html
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


def test_non_ok_quality_is_shown(tmp_path):
    snap = make_snapshot()
    snap["quality"]["vix_available"] = False
    html = render.render_html(snap, data_root=tmp_path)
    assert "Data unavailable" in html
    assert "VIX" in html


def test_clean_day_shows_no_quality_banner(tmp_path):
    snap = make_snapshot()
    snap["quality"] = {k: True for k in snap["quality"]}
    html = render.render_html(snap, data_root=tmp_path)
    assert "Fetch warnings" not in html
    assert "Data unavailable" not in html
    assert 'class="rc-notice"' not in html

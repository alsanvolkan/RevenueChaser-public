"""#379 COM-3: the Co-movement page sends each group's fields once, and the page rebuilds the same rows.

``DATA`` used to be one row per (cut, member) with the group's ten fields and the name's nine fields repeated on
every row (``scripts/research/379_payload_shape.py`` measures it). The page now gets ``{keys, groups, names,
members}``: each group once (keyed by its ``cluster_id``), each distinct name record once, and one
``[cluster_id, name, core]`` triple per member. The rule is "rendered rows identical": the page's own script
unpacks ``DATA`` and the result must equal ``_member_rows`` key for key and in key order.
"""
from __future__ import annotations

import json
import re
import shutil

import pytest

from RevenueChaser.module_comovement import report
from RevenueChaser.core.html_safe import script_json
from RevenueChaser.module_comovement.tests.test_comovement import (
    _report_cluster, _report_payload, _run_board_js_probe)


def _payload():
    """Two cuts, a catch-all, persistence verdicts, missing numbers and a name that sits in both cuts."""
    meta = {"AAA": {"industry": "Semiconductors", "sector": "Technology", "theme_ids": ["t1", "t2"],
                    "earnings_date": "2026-10-20", "index": ["sp500"]},
            "BBB": {"industry": "Banks - Regional", "sector": "Financial Services"},
            "CCC": {"industry": "Oil & Gas E&P", "sector": "Energy", "name": "C </script> & co"},
            "DDD": {"industry": "", "sector": None, "market_cap": None}}
    coarse = [
        _report_cluster("1", label="Semis 60%, ×3.0", tickers=("AAA", "BBB", "CCC"), significant=True,
                        baseline_r=0.05, persist={"jaccard": 0.5, "new": False, "held": True, "oos_r": 0.3},
                        member_meta=meta),
        _report_cluster("2", mean=None, last=None, tickers=("DDD",), significant=None, member_meta=meta),
    ]
    coarse[0]["catchall"] = True
    fine = [
        _report_cluster("1", tickers=("AAA", "BBB"), significant=False, baseline_r=0.09, member_meta=meta),
        _report_cluster("7", tickers=("CCC", "DDD"), significant=True, baseline_r=0.04, member_meta=meta),
    ]
    for c in coarse + fine:
        for m in c["members"]:
            m["core"] = m["ticker"] != "BBB"
    return _report_payload(cuts=[4, 20], clusters={"4": coarse, "20": fine}, eligible=4, universe_total=4)


def _packed(html: str) -> dict:
    return json.loads(re.search(r"var DATA = rcUnpackRows\((.*?)\);\n", html, re.S).group(1))


def test_the_page_sends_each_group_and_each_name_once():
    payload = _payload()
    rows = report._member_rows(payload, applied=True)
    packed = _packed(report.render_comovement_dashboard(payload))
    assert len(packed["members"]) == len(rows) == 8
    assert sorted(packed["groups"]) == ["20:1", "20:7", "4:1", "4:2"]
    assert len(packed["names"]) == 4                                 # AAA..DDD, once each across both cuts
    assert packed["keys"] == list(rows[0])
    assert all(len(m) == 3 for m in packed["members"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("applied", [True, False])
def test_the_page_rebuilds_the_rows_it_used_to_get(tmp_path, applied):
    payload = _payload()
    if not applied:
        payload.update(benchmark="off", benchmark_applied=False)
    rows = report._member_rows(payload, applied=applied)
    got = _run_board_js_probe(report.render_comovement_dashboard(payload), tmp_path, "DATA")
    assert got == json.loads(json.dumps(rows))
    assert [list(r) for r in got] == [list(r) for r in rows]            # key order too


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_an_empty_board_still_renders(tmp_path):
    payload = _report_payload(clusters={}, cuts=[])
    html = report.render_comovement_dashboard(payload)
    assert _packed(html) == {"keys": [], "groups": {}, "names": [], "members": []}
    assert "No clusters yet" in html and _run_board_js_probe(html, tmp_path, "DATA") == []


def test_the_page_is_smaller():
    payload = _payload()
    big = _report_payload(cuts=[4, 20], clusters={
        cut: [_report_cluster(str(i), label=f"Group {i} 50%, ×2.0", tickers=tuple(f"T{i}{j:02d}" for j in range(30)),
                              significant=True, baseline_r=0.05) for i in range(10)]
        for cut in ("4", "20")})
    for p in (payload, big):
        html = report.render_comovement_dashboard(p)
        rows = report._member_rows(p, applied=True)
        before = len(script_json(rows))
        after = len(re.search(r"var DATA = rcUnpackRows\((.*?)\);\n", html, re.S).group(1))
        assert after < before
    assert after < 0.5 * before                                       # 600 rows: the repeats dominate

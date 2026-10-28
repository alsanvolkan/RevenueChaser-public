"""#147 (27-3a): folding the two ``run_scan`` bodies into one skeleton changes nothing but U5.

The golden (``fixtures/screen_parity_147.json``) was captured on the commit just before the refactor (main
``ff54ba6`` plus #145 and #146; the first capture, on ``e83aa26``, differed from it only in the sector and industry
names the phase 1 build #266 changed), from the scenario in ``screen_parity.py``: the payload of a Fetch for Swing and
Fundamentals and the rows each Fetch persisted, timestamps masked. Any difference is a bug. (The golden also held a
Refresh re-score; K4, #383 R1, made a Refresh show the last Fetch instead, so those two snapshots were dropped.)
C12 (#383 R3) changed the scan on purpose and the golden was re-captured on its commit: the prefilter keeps FAR and
BELOW50 (no trend rules), so both are scored and the Swing ranks shift; NOBARS1 drops as ``no_bars`` (D01); the
scenario's bars are D01 rows (``screen_parity.seed``). The diff and its reasons:
``/mnt/project-files/383_r3_evidence/parity_golden_c12_diff.txt``. The #434 review fixes (S1-S4) add only
``data_quality.no_avg_volume`` (0 here: every scenario name has a Yahoo average); the rest is unchanged.
Re-captured for #384 Swing W2 and #386 Fundamentals W3 (2026-10-16): Swing gains UP2 (RS no longer gates Minervini;
the 40/30/30 quality, methods/bucket, the direction gate rows, slim gate rows), Fundamentals keeps its 5 candidates
with rules_passed/bucket instead of multiplier/tiers; both tables gain the additive columns.
Data Health S11 (#80, #124) re-captured it: the Swing and Fundamentals Fetch payloads gain the additive
``provider_health`` key (the provider-health banner); compared with the old golden with that key removed, nothing
else differed.
#49 A2 removed the Swing payload's dead ``is_fixture`` key (``demo_payload`` was its only setter); only that key left
the golden, edited by hand.
#49 finished it for Fundamentals (and ``no_data_payload``): its ``is_fixture: false`` left the golden the same way.
G1 (build plan PR 6) moved Fundamentals' ``selection_version`` to 7 (review N1, net cash); re-captured, it was the
only difference, edited by hand. Review F2 then added the additive ``data_quality.stale_submissions`` (0 here); the
re-capture differed from the old golden by that key only.
To re-capture on another commit, run this file with ``RC_PARITY_CAPTURE=<path>`` (it writes the snapshot there
instead of comparing).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.tests import screen_parity as scn

GOLDEN = Path(__file__).parent / "fixtures" / "screen_parity_147.json"


@pytest.fixture
def scenario(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)


def _dump(snap: dict) -> str:
    return json.dumps(snap, sort_keys=True, indent=1, ensure_ascii=False)


def test_the_scan_output_matches_the_golden_from_main(scenario):
    snap = scn.snapshot()
    capture = os.environ.get("RC_PARITY_CAPTURE")
    if capture:
        Path(capture).write_text(_dump(snap), encoding="utf-8")
        pytest.skip(f"captured to {capture}")
    assert json.loads(_dump(snap)) == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_scenario_exercises_every_branch_of_the_scan(scenario):
    """A golden that only held empty boards would prove nothing: each outcome the refactor must keep appears."""
    snap = scn.snapshot()
    for name in ("swing", "fundamentals"):
        p = snap[name]["fetch"]
        assert p["no_data"] is False
        assert p["candidates"], f"{name}: no candidate passed a gate"
        s = p["summary"]
        assert s["excluded_types"] == 1, "the ETP must be dropped by the identity exclusion (R5)"
        assert s["hygiene"] == 2, "LOWPX and LOWVOL fail the 7-A prefilter; FAR and BELOW50 pass it (C12)"
        assert p["universe_total"] == len(scn.UNIVERSE)
        assert snap[name]["tables"][scn.TABLES[name][0]], f"{name}: a Fetch must persist its scores"
    # Swing scores NOEDGAR (it needs no filings); Fundamentals drops it and says why.
    swing_tickers = {r["ticker"] for r in snap["swing"]["tables"]["swing_scores"]}
    fund_tickers = {r["ticker"] for r in snap["fundamentals"]["tables"]["fundamentals_scores"]}
    assert "NOEDGAR" in swing_tickers and "NOEDGAR" not in fund_tickers
    assert snap["fundamentals"]["fetch"]["data_quality"]["dropped"]["no_edgar_annual"] == 1
    assert snap["swing"]["fetch"]["data_quality"]["dropped"]["short_bars"] == 1
    assert snap["swing"]["fetch"]["data_quality"]["dropped"]["no_bars"] == 1      # NOBARS1: no D01 rows


def test_a_refresh_leaves_the_persisted_scores_alone(scenario):
    """The golden holds the tables after a Fetch only; this pins that a Refresh does not rewrite them (#145 D5)."""
    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_swing_potential import pipeline as sp
    sp.run_scan()
    fp.run_scan()
    before = {t: rawstore.query(f"SELECT * FROM {t} ORDER BY 1, 2") for t in
              ("swing_scores", "swing_gates", "fundamentals_scores", "fundamentals_gates")}
    assert all(before.values())
    rawstore.replace_all("swing_scores", [], allow_empty=True)
    rawstore.replace_all("fundamentals_scores", [], allow_empty=True)
    sp.run_scan(no_fetch=True)
    fp.run_scan(no_fetch=True)
    assert rawstore.count("swing_scores") == 0 and rawstore.count("fundamentals_scores") == 0

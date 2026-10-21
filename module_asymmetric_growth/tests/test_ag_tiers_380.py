"""#380 AG W1 S2 tiers, default view, freeze bucket (card 2, #66 tiering, #62 D1).

- A signal is strong when at least ``TIER_MIN_PASSES`` (2) of its 3 sub-criteria are known passes (the card's ✓).
  Tier 3 (Balanced) = three strong signals, tier 2 (Strong) = two, otherwise no tier. Server-side, from the default
  thresholds: the client sliders change points only.
- Sort: tier, then points, then growth capped at +100 % (unknown or suspicious last), then ticker.
- The ledger bucket carries tier and points; every card stays a pick.
- ``ticker_scores`` gains ``tier``, ``flags`` and ``model_version`` (additive; ``ag_scores_history`` mirrors them).
- The board opens on Balanced + Strong; "ALL" shows every card. Rows stored before this version (tier unknown)
  open on "ALL" under a "scored before" banner, never an empty board.
Offline, temp store.
"""
from __future__ import annotations

import gzip
import json

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import ledger, rawstore, ticker_store
from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_asymmetric_growth import engine, freeze, report
from RevenueChaser.module_asymmetric_growth.engine import score_ticker
from RevenueChaser.module_asymmetric_growth.tests.test_ag_payload_route_380 import _card, _result


def _sig(*passed):
    return [{"key": k, "name": k, "passed": p, "known": 3, "total": 3, "subs": []}
            for k, p in zip(("growth", "survival", "momentum"), passed)]


# ── the tier ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("passed, tier", [((3, 3, 3), 3), ((2, 2, 2), 3), ((3, 3, 1), 2), ((2, 2, 0), 2),
                                          ((3, 1, 1), None), ((1, 1, 1), None), ((0, 0, 0), None)])
def test_the_tier_counts_the_strong_signals(passed, tier):
    assert engine.tier_of(_sig(*passed)) == tier


def test_a_signal_with_two_known_passes_of_three_is_strong_whatever_is_unknown():
    sigs = _sig(2, 2, 0)
    sigs[0]["known"] = 2                                       # 2 passes, 1 unknown: still 2 of 3
    sigs[1]["known"] = 2
    assert engine.tier_of(sigs) == 2


def test_a_3_3_1_card_has_tier_2():
    from RevenueChaser.module_asymmetric_growth.tests.test_score_ticker import _fin, _tech
    card = score_ticker("TEST", _fin(), _tech(dist=0.30, vol=1.0))    # momentum 1/3
    assert [s["passed"] for s in card["signals"]] == [3, 3, 1]
    assert card["tier"] == 2


def test_a_full_card_has_tier_3():
    from RevenueChaser.module_asymmetric_growth.tests.test_score_ticker import _fin, _tech
    assert score_ticker("TEST", _fin(), _tech())["tier"] == 3


# ── the sort ─────────────────────────────────────────────────────────────────

def _c(ticker, tier, points, growth=0.3, flags=()):
    return {"ticker": ticker, "tier": tier, "total_points": points, "rev_growth": growth, "flags": list(flags)}


def test_tier_3_sorts_before_tier_2_whatever_the_points():
    cards = [_c("LOW", None, 9), _c("TWO", 2, 8), _c("THR", 3, 6)]
    engine._sort_cards(cards)
    assert [c["ticker"] for c in cards] == ["THR", "TWO", "LOW"]


def test_growth_is_capped_at_100_pct_and_unknown_or_suspicious_sorts_last():
    cards = [_c("UNK", 3, 9, None), _c("NEG", 3, 9, -0.4), _c("SUS", 3, 9, 6.0, ["suspicious_growth"]),
             _c("BIG", 3, 9, 3.0), _c("ABC", 3, 9, 1.5), _c("MID", 3, 9, 0.5)]
    engine._sort_cards(cards)
    # BIG (+300 %) and ABC (+150 %) both cap at +100 %: the ticker breaks the tie. SUS and UNK are unknown, by ticker.
    assert [c["ticker"] for c in cards] == ["ABC", "BIG", "MID", "NEG", "SUS", "UNK"]


def test_the_summary_counts_the_tiers():
    cards = [_c("A", 3, 9), _c("B", 2, 7), _c("C", 2, 6), _c("D", None, 3)]
    for c in cards:
        c.update(sector="Technology", themes=[], tech_breakthrough=False)
    s = engine._summary(cards, 0, {})
    assert (s["tier_3"], s["tier_2"], s["tier_none"]) == (1, 2, 1)


# ── the ledger ───────────────────────────────────────────────────────────────

def test_the_freeze_bucket_carries_tier_and_points():
    rows = freeze.picks([_c("A", 3, 9), _c("B", 2, 7), _c("C", None, 4)])
    assert [r["bucket"] for r in rows] == ["tier:3|pts_9", "tier:2|pts_8_7", "tier:none|pts_lt5"]
    assert [r["rank"] for r in rows] == [1, 2, 3]                         # every card stays a pick


def test_the_tier_rule_is_in_the_effective_config():
    assert freeze.effective_config()["RULES"]["tier_min_passes"] == engine.TIER_MIN_PASSES == 2


# ── the store ────────────────────────────────────────────────────────────────

def test_ticker_scores_keeps_tier_flags_and_model_version(rawstore_tmp_db):
    mv = freeze.model_version()
    ticker_store.write_from_cards([_card("AAA", tier=3, flags=["suspicious_growth"], model_version=mv),
                                   _card("BBB", tier=None, flags=[], model_version=mv)], asof="x")
    got = {c["ticker"]: c for c in ticker_store.all_cards()}
    assert (got["AAA"]["tier"], got["AAA"]["flags"], got["AAA"]["model_version"]) == (3, ["suspicious_growth"], mv)
    assert (got["BBB"]["tier"], got["BBB"]["flags"]) == (None, [])
    view = ticker_store.card_view(got["AAA"])
    assert (view["tier"], view["flags"], view["model_version"]) == (3, ["suspicious_growth"], mv)


def test_the_history_mirror_has_the_new_columns():
    cols = {c.split()[0] for c in rawstore.MANIFEST["ag_scores_history"]["columns"]}
    assert {"tier", "flags", "model_version"} <= cols


def test_the_hunter_stamps_the_model_version(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _QUOTE, _edgar
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    seed_momentum(["AAA"])                                    # the momentum signal comes from D01 (#380 S4)
    monkeypatch.setattr(engine, "fetch_universe_stats",
                        lambda *a, **k: ([{"ticker": "AAA", "quote": dict(_QUOTE), "market_cap": 5e9}], {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    assert [(c["tier"], c["model_version"]) for c in res["cards"]] == [(3, freeze.model_version())]  # 3 strong signals
    assert engine.scored_before(res["cards"]) is False


def test_scored_before_reads_the_rows_version():
    assert engine.scored_before([{"ticker": "A", "model_version": None}]) is True
    assert engine.scored_before([{"ticker": "A", "model_version": "4+b0bec6e4"}]) is True
    assert engine.scored_before([{"ticker": "A", "model_version": freeze.model_version()}]) is False
    assert engine.scored_before([]) is False


# ── the board ────────────────────────────────────────────────────────────────

def _fresh(*specs):
    mv = freeze.model_version()
    return [_card(t, points=p, tier=tier, flags=[], model_version=mv) for t, p, tier in specs]


_SHOW = "console.log(JSON.stringify({grid: _els['grid'].innerHTML, count: _els['count'].textContent, " \
        "t3: _els['b-tier3'].textContent, t2: _els['b-tier2'].textContent}));"


def test_the_board_opens_on_balanced_and_strong(tmp_path, rawstore_tmp_db):
    result = _result(_fresh(("BAL", 9, 3), ("STR", 7, 2), ("NON", 8, None)))
    html = report.render_asymmetric_growth_hunter_html(result)
    log = run_board_js(html, _SHOW, tmp_path, payload=report.board_payload(result))
    assert "BAL" in log["grid"] and "STR" in log["grid"] and "NON" not in log["grid"]
    assert log["count"].startswith("2 ") and (str(log["t3"]), str(log["t2"])) == ("1", "1")
    assert "scored before" not in html


def test_all_shows_every_card(tmp_path, rawstore_tmp_db):
    result = _result(_fresh(("BAL", 9, 3), ("STR", 7, 2), ("NON", 8, None)))
    html = report.render_asymmetric_growth_hunter_html(result)
    log = run_board_js(html, "setTier('all');\n" + _SHOW, tmp_path, payload=report.board_payload(result))
    assert all(t in log["grid"] for t in ("BAL", "STR", "NON")) and log["count"].startswith("3 ")
    log = run_board_js(html, "setTier('none');\n" + _SHOW, tmp_path, payload=report.board_payload(result))
    assert "NON" in log["grid"] and "BAL" not in log["grid"]


def test_the_card_shows_its_tier(tmp_path, rawstore_tmp_db):
    result = _result(_fresh(("BAL", 9, 3), ("STR", 7, 2)))
    log = run_board_js(report.render_asymmetric_growth_hunter_html(result), _SHOW, tmp_path,
                       payload=report.board_payload(result))
    assert "Balanced" in log["grid"] and "Strong" in log["grid"]


def test_rows_stored_before_this_version_show_every_card_under_a_banner(tmp_path, rawstore_tmp_db):
    """A Refresh after the merge reads rows a v4 Fetch wrote: tier unknown, no model version."""
    old = [_card(t, points=p) for t, p in (("OLD1", 9), ("OLD2", 4))]       # no tier, no model_version
    result = _result(old)
    html = report.render_asymmetric_growth_hunter_html(result)
    assert "scored before" in html and "next Fetch" in html
    log = run_board_js(html, _SHOW, tmp_path, payload=report.board_payload(result))
    assert "OLD1" in log["grid"] and "OLD2" in log["grid"] and log["count"].startswith("2 ")


# ── K4: the Refresh render is the Fetch render, tiers and flags included ─────

@pytest.fixture
def out(tmp_path, monkeypatch, rawstore_tmp_db):
    folder = tmp_path / "out"
    monkeypatch.setattr(runners, "OUTPUT_DIR", folder)
    monkeypatch.setattr(runners, "ASYMMETRIC_GROWTH_LATEST", folder / "asymmetric_growth_latest.html")
    monkeypatch.setattr(freeze, "freeze_result", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "fetch-1")
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "price": 10.0, "name": "A"}])
    return folder


def _stored_payload():
    return json.loads(gzip.decompress(rc.payload_path(runners.ASYMMETRIC_GROWTH_LATEST).read_bytes()))


def test_the_refresh_payload_is_the_fetch_payload_with_tiers_and_flags(out, monkeypatch):
    cards = _fresh(("BAL", 9, 3), ("STR", 7, 2), ("NON", 8, None))
    cards[1]["flags"] = ["mixed_share_basis"]
    for c in cards:
        c["index"] = []                       # the labels come from the index registry on both paths (empty here)
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: _result(cards))
    runners.run_asymmetric_growth(None)
    fetched_html, fetched = runners.ASYMMETRIC_GROWTH_LATEST.read_text(encoding="utf-8"), _stored_payload()
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert _stored_payload() == fetched
    assert [(c["ticker"], c["tier"], c["flags"]) for c in fetched["cards"]] == [
        ("BAL", 3, []), ("STR", 2, ["mixed_share_basis"]), ("NON", None, [])]
    assert "scored before" not in fetched_html


def test_the_model_version_is_the_ledgers():
    assert freeze.model_version() == ledger.model_version(engine.SELECTION_VERSION, freeze.effective_config())

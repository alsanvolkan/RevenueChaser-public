"""M-SPX-2 (#376): no-data signals, deltas that match the card numbers, the no-flow badge.

- DF17 (SPX-2): empty spot exposures used to read Vanna "Long" (a bearish vote) and Charm "Agree"
  (a bullish vote), so the stance could be a consensus of missing data.
- DF19 (SPX-6): the 1D/1W deltas and the Delta pill measured other numbers than the cards show.
- DF26: missing flow data used to read "Wall Holding".
"""
from __future__ import annotations

import copy

from RevenueChaser.module_spx_gamma import render, run as spx_run
from RevenueChaser.module_spx_gamma.flow_signals import evaluate_flow_signal
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
from RevenueChaser.module_spx_gamma.summary import build_summary, charm_card, signal_matrix, vanna_card

from .conftest import make_raw

DAY = "2026-08-14"
PREV = "2026-08-13"


def _cards(snapshot):
    return {c["title"]: c for c in build_summary(snapshot)}


def _snapshot_without_spot_exposures():
    raw = make_raw()
    raw["spot_exposure"] = {"data": []}
    return normalize_snapshot(raw, DAY)


# ── DF17: no rows, no vote ──────────────────────────────────────────────────
def test_empty_spot_exposures_read_no_data():
    cards = _cards(_snapshot_without_spot_exposures())
    assert cards["Vanna"]["signal"]["label"] == "No data"
    assert cards["Charm"]["signal"]["label"] == "No data"
    assert cards["Vanna"].get("lean") is None and cards["Charm"].get("lean") is None


def test_the_stance_counts_only_real_signals():
    cards = build_summary(_snapshot_without_spot_exposures())
    m = signal_matrix(cards)
    # only Delta can vote here (Gamma is context, Vanna and Charm have no data)
    assert m["total_directional"] == 1


def test_vanna_card_without_rows_has_no_data():
    c = vanna_card([], None, 7800.0)
    assert c["signal"]["label"] == "No data"


def test_charm_card_without_rows_has_no_data():
    c = charm_card([], {}, 7800.0)
    assert c["signal"]["label"] == "No data" and c["lean"] is None


def test_charm_two_zero_sums_do_not_agree():
    rows = [{"strike": 7800.0, "call_charm_vol": 0.0, "put_charm_vol": 0.0,
             "call_charm_oi": 0.0, "put_charm_oi": 0.0}]
    c = charm_card(rows, {}, 7800.0)
    assert c["lean"] is None and c["signal"]["label"] != "Agree"


# ── DF19: each delta measures the number its card shows ────────────────────
def _snapshot_with_scaled_vanna(factor: float):
    raw = make_raw()
    raw = copy.deepcopy(raw)
    for r in raw["spot_exposure"]["data"]:
        for k in ("call_vanna_vol", "put_vanna_vol", "call_charm_vol", "put_charm_vol"):
            r[k] = str(float(r[k]) * factor)
    return normalize_snapshot(raw, DAY)


def test_snapshot_carries_the_card_numbers():
    s = normalize_snapshot(make_raw(), DAY)
    cards = _cards(s)
    assert cards["Vanna"]["rows"][0][1] != "N/A"
    assert s["vanna_at_spot"] is not None and s["charm_live_sum"] is not None
    assert s["vanna_at_spot"] != s["net_vanna"]          # nearest-spot strike, not the all-row sum
    assert s["charm_live_sum"] != s["live_charm"]        # summed live charm, not the peak strike


def test_history_records_the_card_numbers(tmp_path):
    s = normalize_snapshot(make_raw(), DAY)
    path = tmp_path / "h.json"
    spx_run._record_history(s, DAY, path)
    rec = spx_run._load_history(path)[DAY]
    assert rec["vanna_at_spot"] == s["vanna_at_spot"]
    assert rec["charm_live_sum"] == s["charm_live_sum"]


def _render_cards(snapshot, history, prev_snapshot=None):
    import json
    root = snapshot["_root"]
    (root / "spx_gamma_gex_history.json").write_text(json.dumps(history), encoding="utf-8")
    ctx = render.build_context(snapshot, prev_snapshot=prev_snapshot, data_root=root)
    return {c["title"]: c for c in ctx["summary"]}


def test_vanna_delta_is_the_at_spot_change(tmp_path):
    s = _snapshot_with_scaled_vanna(2.0)
    s["_root"] = tmp_path
    hist = {PREV: {"is_friday_close": False, "vanna_at_spot": 1_000_000.0, "net_vanna": 0.0,
                   "charm_live_sum": 5.0, "live_charm": 0.0}}
    cards = _render_cards(s, hist)
    assert cards["Vanna"]["delta_1d"] == s["vanna_at_spot"] - 1_000_000.0
    assert cards["Charm"]["delta_1d"] == s["charm_live_sum"] - 5.0


def test_old_history_records_give_no_vanna_or_charm_delta(tmp_path):
    s = normalize_snapshot(make_raw(), DAY)
    s["_root"] = tmp_path
    hist = {PREV: {"is_friday_close": False, "net_vanna": 1.0, "live_charm": 1.0}}
    cards = _render_cards(s, hist)
    assert cards["Vanna"]["delta_1d"] is None and cards["Charm"]["delta_1d"] is None


def test_delta_pill_is_the_net_dex_change(tmp_path):
    s = normalize_snapshot(make_raw(), DAY)
    prev = copy.deepcopy(s)
    prev["current_level"] = (s["current_level"] or 0) - 50.0     # spot moved 50 points
    prev["net_dex"] = (s["net_dex"] or 0) - 2_000_000.0          # Net DEX moved 2M
    s["_root"] = tmp_path
    cards = _render_cards(s, {}, prev_snapshot=prev)
    assert cards["Delta"]["delta"] == 2_000_000.0


# ── DF26: no flow rows is not "Wall Holding" ───────────────────────────────
def test_no_flow_rows_read_no_flow_data():
    res = evaluate_flow_signal({"current_level": 7800.0, "levels": {}, "flow_strikes": []})
    assert res["label"] == "No flow data"
    assert res["signal"] is None


# ── Review S1: a no-data day makes no vanna claim in the footer ─────────────
def test_footer_has_no_vanna_claim_without_spot_exposures(tmp_path):
    s = _snapshot_without_spot_exposures()
    ctx = render.build_context(s, data_root=tmp_path)
    assert "Short vanna" not in ctx["footer"] and "Long vanna" not in ctx["footer"]


def test_footer_names_the_vanna_side_the_card_shows(tmp_path):
    s = normalize_snapshot(make_raw(), DAY)
    label = _cards(s)["Vanna"]["signal"]["label"]
    assert label in ("Long", "Short")
    ctx = render.build_context(s, data_root=tmp_path)
    assert f"{label} vanna:" in ctx["footer"]

"""Swing thresholds as editable board settings (#384 SW-S4).

The defaults stay ``module_swing_potential/config.py``'s and are read at call time; only a value a user saved
lives in ``board_settings``, and Reset deletes it. ``test_defaults_reproduce_the_pre_change_output`` pins the
board's output at defaults against a golden captured from the code **before** this slice (so the change cannot
move a single pick), and the tests after it are the ones that failed before the slice existed.

``settings`` is imported inside the tests that exercise it: that keeps the golden pin runnable against the tree
the golden came from (the module did not exist yet).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.module_swing_potential import config, freeze, pipeline, ranking, screeners

B = "module_swing_potential"
_GOLDEN = Path(__file__).with_name("golden_swing_defaults_s4.json")


def _settings():
    from RevenueChaser.module_swing_potential import settings
    return settings


# The keys the SPEC registers, in the order the Settings card shows them.
_KEYS = ("MIN_PRICE", "MIN_DOLLAR_VOLUME", "RS_MIN", "VOL_CONFIRM", "BREAKOUT_HIGH_PCT",
         "MOVE_MIN", "ADR_MIN", "W_PROX", "W_CONS", "STOP_ATR", "TARGET_R")


# ── the fixed fixture the golden and the override tests share ───────────────
def _ind(**over):
    ind = {
        "price": 100.0, "sma50": 95.0, "sma150": 90.0, "sma200": 80.0,
        "sma200_prev_1m": 78.0, "sma150_prev_1m": 88.0,
        "above_52w_low_pct": 1.5, "within_52w_high_pct": 0.99,
        "vol_ratio": 2.0, "rs_vs_spy_3m": 0.05, "rs_vs_spy_12m": 0.10,
        "ema10": 96.0, "ema20": 92.0, "ema10_prev": 90.0, "ema20_prev": 88.0,
        "move_max_1_3m": 0.35, "adr_pct": 0.04, "atr14": 2.0,
        "change_pct": 0.01, "ret_3m": 0.10, "mom_12_1": 0.30, "consistency_6m": 0.8,
    }
    ind.update(over)
    return ind


def _rows():
    """Four names: AAA passes everything, BBB sits just above three thresholds, CCC fails direction, DDD fails
    the Weinstein volume rule and the Qullamaggie ADR rule."""
    return [
        {"ticker": "AAA", "name": "Alpha", "ind": _ind(), "market_cap": 1e9, "sector": "Technology",
         "industry": "Software - Application", "themes": ["Software"], "earnings_date": 1790000000.0},
        {"ticker": "BBB", "name": "Beta",
         "ind": _ind(vol_ratio=1.6, adr_pct=0.032, move_max_1_3m=0.26, rs_vs_spy_12m=0.04,
                     mom_12_1=0.20, consistency_6m=0.6),
         "market_cap": 5e9, "sector": "Healthcare", "industry": "Biotechnology", "themes": []},
        {"ticker": "CCC", "name": "Gamma", "ind": _ind(ret_3m=-0.02), "market_cap": 2e9,
         "sector": "Industrials", "industry": "Machinery", "themes": ["Industrials"]},
        {"ticker": "DDD", "name": "Delta", "ind": _ind(vol_ratio=1.2, adr_pct=0.02), "market_cap": 3e9,
         "sector": "Energy", "industry": "Oil & Gas", "themes": []},
    ]


def _prefilter_quotes(monkeypatch):
    """Three D04 quotes around the default price and dollar-volume floors (``quotes.prefilter_quotes``)."""
    held = {"LOWP": {"price": 4.99, "avg_volume": 10_000.0},
            "THIN": {"price": 50.0, "avg_volume": 1_000.0},
            "OK": {"price": 100.0, "avg_volume": 5_000.0}}
    monkeypatch.setattr("RevenueChaser.core.quotes.prefilter_quotes", lambda tickers: dict(held))
    return [{"ticker": t, "quote": {}} for t in ("LOWP", "THIN", "OK")]


# ── defaults unchanged (golden) ─────────────────────────────────────────────
def test_defaults_reproduce_the_pre_change_output(rawstore_tmp_db, monkeypatch):
    """``golden_swing_defaults_s4.json`` was captured from the code before #384 SW-S4.

    The payload (picks, gates, quality, momentum, levels, counts) and the prefilter's kept/drops are identical
    at defaults: a setting nobody saved cannot change a result."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    payload = pipeline.build_payload(_rows(), top_n=10)
    payload.pop("generated_at", None)                      # the only clock-dependent field
    payload.pop("settings")                                # the run's thresholds (finding 2), not a result
    pf = pipeline.prefilter(_prefilter_quotes(monkeypatch))
    assert {"payload": payload,
            "prefilter": {"kept": [r["ticker"] for r in pf.kept], "drops": pf.drops,
                          "no_volume": pf.no_volume}} == golden


# ── the SPEC ────────────────────────────────────────────────────────────────
def test_the_spec_registers_exactly_the_editable_thresholds(rawstore_tmp_db):
    assert B in bs.boards()
    rows = {r["key"]: r for r in bs.describe(B)}
    assert set(rows) == set(_KEYS)
    assert bs.defaults(B) == {k: getattr(config, k) for k in _KEYS}   # a default is config's, not a copy
    assert rows["MIN_PRICE"]["lo"] == config.MIN_PRICE_LO
    assert rows["MIN_PRICE"]["hi"] == config.MIN_PRICE_HI
    assert rows["TARGET_R"]["label"]
    for r in rows.values():
        assert r["choices"] is None and r["lo"] is not None and r["hi"] is not None


def test_a_default_is_read_from_config_at_call_time(rawstore_tmp_db, monkeypatch):
    assert bs.get(B, "RS_MIN") == config.RS_MIN
    monkeypatch.setattr(config, "RS_MIN", 82)
    assert bs.get(B, "RS_MIN") == 82                       # not copied at import or on first read
    assert bs.values(B)["overridden"] == []


# ── effective(): the one resolver ───────────────────────────────────────────
def test_effective_at_defaults_is_the_config(rawstore_tmp_db):
    e = _settings().effective()
    assert (e.min_price, e.min_dollar_volume) == (config.MIN_PRICE, config.MIN_DOLLAR_VOLUME)
    assert (e.rs_min, e.vol_confirm, e.breakout_high_pct) == (
        config.RS_MIN, config.VOL_CONFIRM, config.BREAKOUT_HIGH_PCT)
    assert (e.move_min, e.adr_min) == (config.MOVE_MIN, config.ADR_MIN)
    assert (e.w_prox, e.w_cons) == (config.W_PROX, config.W_CONS)
    assert (e.stop_atr, e.target_r) == (config.STOP_ATR, config.TARGET_R)
    assert e.custom is False and e.overridden == ()


def test_a_saved_override_is_what_the_resolver_returns(rawstore_tmp_db):
    bs.save(B, {"MIN_PRICE": 12.0, "RS_MIN": 80, "STOP_ATR": 3.0})
    e = _settings().effective()
    assert (e.min_price, e.rs_min, e.stop_atr) == (12.0, 80.0, 3.0)
    assert e.vol_confirm == config.VOL_CONFIRM             # untouched keys stay config's
    assert e.custom is True and e.overridden == ("MIN_PRICE", "RS_MIN", "STOP_ATR")


def test_a_store_error_falls_back_to_the_defaults(rawstore_tmp_db, monkeypatch):
    def broken(board):
        raise RuntimeError("store locked")
    monkeypatch.setattr(bs, "values", broken)
    e = _settings().effective()
    assert (e.min_price, e.rs_min, e.stop_atr) == (config.MIN_PRICE, config.RS_MIN, config.STOP_ATR)
    assert e.custom is False and e.overridden == ()
    assert freeze.effective_config()["RS_MIN"] == config.RS_MIN      # the freeze reads defaults, never raises


# ── the pipeline reads the effective values ─────────────────────────────────
def test_a_saved_override_reaches_the_gates_and_the_levels(rawstore_tmp_db):
    before = pipeline.build_payload(_rows(), top_n=10)
    assert before["passing"]["weinstein"] == 3
    bs.save(B, {"VOL_CONFIRM": 2.5, "STOP_ATR": 3.0})
    after = pipeline.build_payload(_rows(), top_n=10)
    assert after["passing"]["weinstein"] == 0              # no name clears 2.5x any more
    aaa = next(c for c in after["candidates"] if c["ticker"] == "AAA")
    assert (aaa["stop"], aaa["target"], aaa["rr"]) == (94.0, 112.0, 2.0)   # stop 100 - 3x2, target 100 + 2x6


def test_the_ranking_weights_come_from_the_settings(rawstore_tmp_db):
    e = _settings().effective()
    assert ranking.composite(100, 0, 0, eff=e) == 50.0     # proximity and consistency still weigh in
    bs.save(B, {"W_PROX": 0.0, "W_CONS": 0.0})
    assert ranking.composite(100, 0, 0, eff=_settings().effective()) == 100.0


def test_a_saved_prefilter_override_drops_a_name(rawstore_tmp_db, monkeypatch):
    rows = _prefilter_quotes(monkeypatch)
    assert [r["ticker"] for r in pipeline.prefilter(rows).kept] == ["OK"]
    bs.save(B, {"MIN_DOLLAR_VOLUME": 600_000})
    pf = pipeline.prefilter(rows)
    assert pf.kept == [] and pf.drops == {"low_price": ["LOWP"], "low_dollar_volume": ["THIN", "OK"]}


def test_the_gate_text_names_the_threshold_that_is_used(rawstore_tmp_db):
    g = screeners.evaluate_minervini(_ind(), 75.0)
    assert next(r for r in g["rules"] if r["id"] == "rs_ge_70")["threshold"] == "≥ 70"
    bs.save(B, {"RS_MIN": 80, "VOL_CONFIRM": 2.5, "MOVE_MIN": 0.4, "ADR_MIN": 0.05})
    e = _settings().effective()
    rs = next(r for r in screeners.evaluate_minervini(_ind(), 75.0, eff=e)["rules"] if r["id"] == "rs_ge_70")
    assert rs["threshold"] == "≥ 80" and rs["label"] == "RS Rating at least 80"
    rules = {r["id"]: r for r in screeners.evaluate_weinstein(_ind(), eff=e)["rules"]}
    assert rules["volume_confirmation"]["threshold"] == "≥ 2.5× 20-day avg"
    q = {r["id"]: r for r in screeners.evaluate_qullamaggie(_ind(), eff=e)["rules"]}
    assert q["large_move"]["threshold"] == "≥ 40%"
    assert q["adr_ge_3"]["threshold"] == "≥ 5%"


# ── bounds and cross-rules ──────────────────────────────────────────────────
@pytest.mark.parametrize("change,needle", [
    ({"MIN_PRICE": 0.4}, "MIN_PRICE must be between 0.5 and 100"),
    ({"MIN_PRICE": 101}, "MIN_PRICE must be between 0.5 and 100"),
    ({"MIN_DOLLAR_VOLUME": -1}, "MIN_DOLLAR_VOLUME must be between 0 and 50000000"),
    ({"MIN_DOLLAR_VOLUME": 50_000_001}, "MIN_DOLLAR_VOLUME must be between 0 and 50000000"),
    ({"RS_MIN": 101}, "RS_MIN must be between 0 and 100"),
    ({"VOL_CONFIRM": 0.4}, "VOL_CONFIRM must be between 0.5 and 10"),
    ({"BREAKOUT_HIGH_PCT": 1.01}, "BREAKOUT_HIGH_PCT must be between 0.5 and 1"),
    ({"MOVE_MIN": 3.5}, "MOVE_MIN must be between 0 and 3"),
    ({"ADR_MIN": 0.3}, "ADR_MIN must be between 0 and 0.25"),
    ({"W_PROX": 1.1}, "W_PROX must be between 0 and 1"),
    ({"W_CONS": -0.1}, "W_CONS must be between 0 and 1"),
    ({"STOP_ATR": 0.4}, "STOP_ATR must be between 0.5 and 10"),
    ({"TARGET_R": 25}, "TARGET_R must be between 0.5 and 20"),
    ({"RS_MIN": "abc"}, "RS_MIN must be a number"),
    ({"NOPE": 1}, "unknown setting 'NOPE'"),
])
def test_validate_every_bound_names_its_rule(rawstore_tmp_db, change, needle):
    msgs = bs.validate(B, change)
    assert any(needle in m for m in msgs), msgs


def test_cross_rule_the_weights_leave_momentum_the_largest_tilt(rawstore_tmp_db):
    msgs = bs.validate(B, {"W_PROX": 0.8, "W_CONS": 0.4})
    assert any("W_PROX + W_CONS must not exceed 1" in m for m in msgs), msgs
    assert bs.validate(B, {"W_PROX": 0.5, "W_CONS": 0.5}) == []
    assert bs.validate(B, {"W_PROX": 0.0, "W_CONS": 0.0}) == []
    # TARGET_R < STOP_ATR is allowed: a sub-1R target is a choice, not an error
    assert bs.validate(B, {"STOP_ATR": 4.0, "TARGET_R": 1.0}) == []


def test_a_rejected_change_writes_nothing(rawstore_tmp_db):
    with pytest.raises(bs.SettingsError):
        bs.save(B, {"RS_MIN": 80, "VOL_CONFIRM": 0.1})
    assert rawstore.count("board_settings") == 0


# ── only an override is stored; Reset deletes it ────────────────────────────
def test_only_the_changed_key_is_stored(rawstore_tmp_db):
    bs.save(B, {"RS_MIN": 80})
    bs.save(B, {"MIN_DOLLAR_VOLUME": 500_000.0})
    assert sorted(r["key"] for r in rawstore.query(
        "SELECT key FROM board_settings WHERE board = ?", [B])) == ["MIN_DOLLAR_VOLUME", "RS_MIN"]
    bs.save(B, {"VOL_CONFIRM": config.VOL_CONFIRM})        # saving the default stores nothing for it
    assert bs.values(B)["overridden"] == ["MIN_DOLLAR_VOLUME", "RS_MIN"]


def test_reset_deletes_the_rows_and_the_defaults_are_configs_again(rawstore_tmp_db):
    bs.save(B, {"RS_MIN": 80, "STOP_ATR": 3.0})
    assert rawstore.count("board_settings") == 2
    bs.reset(B)
    assert rawstore.count("board_settings") == 0            # deleted, not rewritten as defaults
    assert not bs.is_custom(B)
    assert _settings().effective().rs_min == config.RS_MIN


# ── the model version ───────────────────────────────────────────────────────
def test_model_version_is_unchanged_at_defaults(rawstore_tmp_db):
    """The literal from before this slice: an unconfigured board versions exactly what it did."""
    assert freeze.model_version() == "4+ab881cb0"


def test_a_saved_override_moves_the_model_version_and_reset_restores_it(rawstore_tmp_db):
    base = freeze.model_version()
    bs.save(B, {"RS_MIN": 80})
    assert freeze.model_version() != base
    bs.reset(B)
    assert freeze.model_version() == base


def test_the_model_version_still_hashes_the_non_editable_thresholds(rawstore_tmp_db, monkeypatch):
    before = freeze.model_version()
    monkeypatch.setattr(config, "ABOVE_52W_LOW", 1.3)
    assert freeze.effective_config()["ABOVE_52W_LOW"] == 1.3
    assert freeze.model_version() != before


# ── the Settings board: card, page and the generic route ────────────────────
def test_the_settings_card_lists_the_swing_keys(rawstore_tmp_db):
    from RevenueChaser.module_settings import boards, report

    card = next(c for c in boards.board_cards() if c["key"] == B)
    s = card["settings"]
    assert s["board"] == B and s["custom"] is False
    assert {f["key"] for f in s["inputs"] + s["fine"]} == set(_KEYS)
    by = {f["key"]: f for f in s["inputs"] + s["fine"]}
    assert by["RS_MIN"]["value"] == config.RS_MIN and by["RS_MIN"]["overridden"] is False
    assert by["RS_MIN"]["range"] == "0–100" and by["MIN_PRICE"]["range"] == "0.5–100"
    html = report.render_settings_dashboard({"boards": [card]})
    assert 'data-board="module_swing_potential"' in html and 'data-setting="RS_MIN"' in html
    bs.save(B, {"RS_MIN": 80})
    card = next(c for c in boards.board_cards() if c["key"] == B)
    by = {f["key"]: f for f in card["settings"]["inputs"] + card["settings"]["fine"]}
    assert card["settings"]["custom"] is True
    assert by["RS_MIN"]["value"] == 80 and by["RS_MIN"]["overridden"] is True


def test_the_save_button_reads_only_its_own_board_fields():
    """Two boards share the page now, so a Save may not pick up the other card's inputs (#384 SW-S4)."""
    from RevenueChaser.module_settings import report
    assert "document.querySelectorAll('#settings-' + board + ' [data-setting]')" in report.JS


@pytest.fixture
def client(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.app import dashboard
    submitted = []
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params=None: submitted.append((module, params)) or "job123")
    dashboard.app.config["TESTING"] = True
    c = panel_test_client(dashboard.app)
    c.submitted = submitted
    return c


def test_route_save_stores_the_swing_value_and_queues_a_store_only_refresh(client):
    r = client.post("/api/settings/board/module_swing_potential", json={"changes": {"RS_MIN": 80}})
    assert r.status_code == 202
    body = r.get_json()
    assert body["ok"] is True and body["job_id"] == "job123"
    assert body["values"]["values"]["RS_MIN"] == 80
    assert body["values"]["overridden"] == ["RS_MIN"]
    assert client.submitted == [("settings_refresh", {})]   # not the Co-movement recompute
    assert bs.get(B, "RS_MIN") == 80


def test_route_rejects_an_out_of_range_swing_value_and_writes_nothing(client):
    r = client.post("/api/settings/board/module_swing_potential", json={"changes": {"VOL_CONFIRM": 0.1}})
    assert r.status_code == 400
    assert any("VOL_CONFIRM must be between 0.5 and 10" in m for m in r.get_json()["messages"])
    assert rawstore.count("board_settings") == 0 and client.submitted == []


def test_route_reset_deletes_the_swing_rows(client):
    bs.save(B, {"RS_MIN": 80, "STOP_ATR": 3.0})
    r = client.post("/api/settings/board/module_swing_potential", json={"reset": True})
    assert r.status_code == 202 and r.get_json()["values"]["overridden"] == []
    assert rawstore.count("board_settings") == 0
    assert client.submitted == [("settings_refresh", {})]


def test_zero_weights_and_no_momentum_percentile_give_no_composite_not_a_crash(rawstore_tmp_db):
    bs.save(B, {"W_PROX": 0.0, "W_CONS": 0.0})
    assert ranking.composite(None, 40.0, 60.0, eff=_settings().effective()) is None

"""Board-inputs cards: read each board's rendered payload, store-only."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_comovement import config as cfg
from RevenueChaser.module_settings import boards


def _steps(result=60):
    return [
        {"key": "scanned", "label": "Scanned", "count": 100, "kind": "start", "fix": None},
        {"key": "below_floor", "label": "Below floor", "count": 30, "kind": "drop",
         "by_design": True, "fix": None},
        {"key": "no_prices", "label": "No prices", "count": 10, "kind": "drop",
         "by_design": False, "fix": {"route": "/run/module_comovement",
                                     "label": "Fetch prices"}},
        {"key": "clustered", "label": "Clustered", "count": result, "kind": "result",
         "fix": None},
    ]


def _payload(**over):
    p = {"as_of": "2026-09-25T20:00:00+00:00", "generated_at": "2026-09-25T20:00:00+00:00",
         "window_start": "2025-09-25", "oos_start": "2026-06-25", "prices_through":
         "2026-09-25", "funnel_steps": _steps()}
    p.update(over)
    return p


@pytest.fixture
def payload_file(tmp_path, monkeypatch):
    path = tmp_path / "comovement_latest.json"
    monkeypatch.setattr(cfg, "COMOVEMENT_JSON", path)
    return path


def _card(key="module_comovement"):
    cards = boards.board_cards()
    return next(c for c in cards if c["key"] == key)


def test_ok_card(payload_file):
    payload_file.write_text(json.dumps(_payload()), encoding="utf-8")
    c = _card()
    assert c["state"] == "ok" and c["error"] is None
    assert c["key"] == "module_comovement" and c["label"] == "Co-movement"
    assert c["tables"] == ["equity_universe", "swing_ohlc"]
    assert "dividend-adjusted" in c["price_basis"]
    assert c["as_of"] == "2026-09-25T20:00:00+00:00"
    assert c["window"]["window_start"] == "2025-09-25"
    assert c["window"]["prices_through"] == "2026-09-25"
    assert [s["key"] for s in c["funnel_steps"]][0] == "scanned"


def test_missing_payload_is_not_rendered(payload_file):
    c = _card()
    assert c["state"] == "not_rendered"
    assert c["funnel_steps"] == []


def test_old_payload_without_steps_is_no_funnel(payload_file):
    p = _payload()
    del p["funnel_steps"]
    payload_file.write_text(json.dumps(p), encoding="utf-8")
    assert _card()["state"] == "no_funnel"
    payload_file.write_text(json.dumps(_payload(funnel_steps=[])), encoding="utf-8")
    assert _card()["state"] == "no_funnel"


def test_steps_that_do_not_add_up_are_invalid(payload_file):
    payload_file.write_text(json.dumps(_payload(funnel_steps=_steps(result=59))),
                            encoding="utf-8")
    c = _card()
    assert c["state"] == "invalid"
    assert "59" in c["error"] and "60" in c["error"]


def test_corrupt_json_is_an_error_not_a_raise(payload_file):
    payload_file.write_text("{not json", encoding="utf-8")
    c = _card()
    assert c["state"] == "error" and c["error"]
    payload_file.write_text("[1, 2]", encoding="utf-8")
    assert _card()["state"] == "error"


def test_params_are_read_from_the_config_at_call_time(monkeypatch):
    text = " | ".join(boards.BOARD_INPUTS[0]["params"]())
    assert str(cfg.WINDOW) in text and str(cfg.MIN_OBS) in text
    assert str(cfg.OOS_HOLDOUT) in text and str(cfg.WINSOR_K) in text
    assert str(cfg.DEFAULT_HORIZON) in text and str(cfg.OHLC_PERIOD) in text
    assert cfg.BENCHMARKS[cfg.DEFAULT_BENCHMARK] in text
    monkeypatch.setattr(cfg, "WINDOW", 999)
    assert "999" in " | ".join(boards.BOARD_INPUTS[0]["params"]())


def test_every_fix_route_is_a_registered_route():
    from RevenueChaser.app import dashboard

    rules = {r.rule for r in dashboard.app.url_map.iter_rules()}
    assert "/run/universe-stats" in rules and "/run/module_comovement" in rules
    from RevenueChaser.module_comovement import engine

    f = {"scanned_total": 9, "below_floor": 1, "no_market_cap": 1, "no_prices": 1,
         "short_history": 1, "flat": 1, "clustered": 4}
    for s in engine.funnel_steps(f):
        if s.get("fix"):
            assert s["fix"]["route"] in rules, s


# ── editable settings block (#40 Part B) ─────────────────────────────────────
def test_card_carries_the_editable_settings_block(payload_file, rawstore_tmp_db):
    from RevenueChaser.core import board_settings
    payload_file.write_text(json.dumps(_payload()), encoding="utf-8")
    s = _card()["settings"]
    assert s["board"] == "module_comovement" and s["custom"] is False
    assert [f["key"] for f in s["inputs"]] == ["WINDOW", "MIN_OBS", "OOS_HOLDOUT"]
    assert [f["key"] for f in s["fine"]] == ["WINSOR_K", "MIN_PERIODS",
                                             "BENCH_STALE_SESSIONS"]
    by = {f["key"]: f for f in s["inputs"] + s["fine"]}
    assert by["MIN_OBS"]["value"] == cfg.MIN_OBS and by["MIN_OBS"]["overridden"] is False
    assert "60" in by["MIN_OBS"]["range"] and "WINDOW" in by["MIN_OBS"]["range"]
    assert by["OOS_HOLDOUT"]["range"] == "42–126"
    assert by["WINSOR_K"]["range"] == "4–8"
    assert by["BENCH_STALE_SESSIONS"]["range"] == "0–5"
    assert by["WINDOW"]["choices"][0] == [21, "1m (21)"]
    assert s["derived"] == {"label": "Held-out observations needed (derived)",
                            "key": "OOS_MIN_OBS",
                            "value": round(cfg.OOS_MIN_SHARE * cfg.OOS_HOLDOUT)}
    board_settings.save("module_comovement", {"MIN_OBS": 100, "OOS_HOLDOUT": 84})
    s = _card()["settings"]
    assert s["custom"] is True
    by = {f["key"]: f for f in s["inputs"] + s["fine"]}
    assert by["MIN_OBS"]["value"] == 100 and by["MIN_OBS"]["overridden"] is True
    assert s["derived"]["value"] == round(cfg.OOS_MIN_SHARE * 84)


def test_the_settings_block_is_there_even_before_the_board_is_rendered(
        payload_file, rawstore_tmp_db):
    c = _card()
    assert c["state"] == "not_rendered" and c["settings"]["inputs"]


# ── the Swing card (#384 SW-S4) ─────────────────────────────────────────────
def test_the_swing_card_lists_its_editable_thresholds(rawstore_tmp_db, monkeypatch, tmp_path):
    from RevenueChaser.core import board_settings
    from RevenueChaser.module_swing_potential import config as sw_cfg

    # A Swing artifact a test earlier in the session left in the shared cache dir would make the state "no_funnel".
    monkeypatch.setattr(sw_cfg, "SWING_JSON", tmp_path / "swing_latest.json")
    c = _card("module_swing_potential")
    assert c["label"] == "Swing" and c["state"] == "not_rendered"
    assert c["tables"] == ["equity_universe", "price_bars_daily"] and "nominal" in c["price_basis"]
    s = c["settings"]
    assert s["board"] == "module_swing_potential" and s["custom"] is False
    by = {f["key"]: f for f in s["inputs"] + s["fine"]}
    assert [f["key"] for f in s["inputs"]] == ["MIN_PRICE", "MIN_DOLLAR_VOLUME", "RS_MIN"]
    assert set(by) == {"MIN_PRICE", "MIN_DOLLAR_VOLUME", "RS_MIN", "VOL_CONFIRM", "BREAKOUT_HIGH_PCT",
                       "MOVE_MIN", "ADR_MIN", "W_PROX", "W_CONS", "STOP_ATR", "TARGET_R"}
    assert by["MIN_PRICE"]["value"] == sw_cfg.MIN_PRICE and by["MIN_PRICE"]["overridden"] is False
    assert by["MIN_PRICE"]["range"] == f"{sw_cfg.MIN_PRICE_LO:g}–{sw_cfg.MIN_PRICE_HI:g}"
    board_settings.save("module_swing_potential", {"RS_MIN": 80})
    by = {f["key"]: f for f in _card("module_swing_potential")["settings"]["inputs"]}
    assert by["RS_MIN"]["value"] == 80 and by["RS_MIN"]["overridden"] is True


def test_the_swing_params_are_read_from_the_effective_settings_at_call_time(
        rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import board_settings
    from RevenueChaser.module_swing_potential import config as sw_cfg

    text = " | ".join(_card("module_swing_potential")["params"])
    assert f"{sw_cfg.MIN_PRICE:g}" in text and f"{sw_cfg.RS_MIN:g}" in text
    monkeypatch.setattr(sw_cfg, "RS_MIN", 82)
    assert "82" in " | ".join(_card("module_swing_potential")["params"])   # a default moves the card
    board_settings.save("module_swing_potential", {"VOL_CONFIRM": 2.5})
    assert "2.5× the 20-day average" in " | ".join(_card("module_swing_potential")["params"])


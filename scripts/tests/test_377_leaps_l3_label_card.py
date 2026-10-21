"""#377 L3 card script: the before/after label rows and the short-history count, offline (no store, no network)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "research" / "377_leaps_l3_label_card.py"
FAR = "2027-12-17"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("leaps_l3_card_377", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _oi(call, expiry=FAR):
    return [{"expiry": expiry, "call_oi": call, "put_oi": 50}]


def _snap(label="Accumulating", as_of="2026-09-15"):
    return {"ticker": "AAA", "label": label, "as_of": as_of, "far_call_oi": 1000, "far_put_oi": 50,
            "far_call_share": 0.9524, "far_oi_growth_1d_pct": None, "far_oi_growth_q_pct": None,
            "near_term_event": False}


def _rows(entry_list):
    return [{"as_of": as_of, "oi_per_expiry": oi, "far_call_oi": sum(r["call_oi"] for r in oi)} for as_of, oi in entry_list]


def test_a_short_history_reads_quiet_and_is_counted(mod):
    # One recorded session: no baseline yet, so no growth. The call share and the ask share are strong, so the
    # Quiet label is a data reason, and the card counts it.
    bases = {"AAA": {"newest": {"as_of": "2026-09-15", "oi_per_expiry": _oi(1000), "ask_share": 0.8},
                     "rows": _rows([("2026-09-15", _oi(1000))])}}
    rows = mod.card(["AAA"], bases, lambda t: _snap())
    assert rows[0]["before"] == "Accumulating" and rows[0]["after"] == "Quiet"
    assert rows[0]["quiet_only_for_short_history"] is True
    assert mod.summary(rows)["quiet_only_for_short_history"] == 1
    assert "Quiet only because history is short" in mod.render(rows, 1)


def test_a_quarter_baseline_gives_the_after_label(mod):
    bases = {"AAA": {"newest": {"as_of": "2026-09-15", "oi_per_expiry": _oi(1000), "ask_share": 0.8},
                     "rows": _rows([("2026-09-15", _oi(1000)), ("2026-06-30", _oi(400))])}}
    rows = mod.card(["AAA"], bases, lambda t: _snap(label="Quiet"))
    assert rows[0]["after"] == "Accumulating" and rows[0]["growth_1d_pct"] == 150.0
    assert rows[0]["quiet_only_for_short_history"] is False
    assert mod.summary(rows)["changed"] == 1


def test_an_all_bid_print_is_not_read_as_short_history(mod):
    bases = {"AAA": {"newest": {"as_of": "2026-09-15", "oi_per_expiry": _oi(1000), "ask_share": 0.1},
                     "rows": _rows([("2026-09-15", _oi(1000))])}}
    rows = mod.card(["AAA"], bases, lambda t: _snap())
    assert rows[0]["after"] == "Quiet" and rows[0]["quiet_only_for_short_history"] is False


def test_a_newer_fetch_that_is_not_recorded_keeps_the_stored_label(mod):
    bases = {"AAA": {"newest": {"as_of": "2026-09-15", "oi_per_expiry": _oi(1000), "ask_share": 0.8},
                     "rows": _rows([("2026-09-15", _oi(1000))])}}
    rows = mod.card(["AAA"], bases, lambda t: _snap(label="Distributing", as_of="2026-10-07"))
    assert rows[0]["after"] == "Distributing" and rows[0]["recorded"] is True
    assert mod.summary(rows)["changed"] == 0


def test_a_name_with_no_recorded_session_is_reported(mod):
    rows = mod.card(["AAA"], {}, lambda t: _snap())
    assert rows[0]["after"] == rows[0]["before"] == "Accumulating"
    assert rows[0]["recorded"] is False and mod.summary(rows)["unrecorded"] == 1


def test_the_latest_loader_unwraps_the_cache_payload(mod, monkeypatch):
    """``load_cache`` returns ``{latest, history}``; the card reads ``latest`` (a wrong shape reads N/A)."""
    monkeypatch.setattr(mod.engine, "load_cache",
                        lambda t: {"latest": {"ticker": t, "label": "Quiet", "as_of": "2026-09-15"}, "history": []})
    assert mod._load_latest("AAA")["label"] == "Quiet"
    monkeypatch.setattr(mod.engine, "load_cache", lambda t: None)
    assert mod._load_latest("AAA") == {}


def test_the_card_waits_for_two_sessions(mod):
    assert mod.MIN_SESSIONS == 2
    assert not mod.enough_history(1) and mod.enough_history(2)


def test_the_script_is_read_only(mod):
    assert os.environ.get("RC_STORE_AUTO_ENSURE") == "0"

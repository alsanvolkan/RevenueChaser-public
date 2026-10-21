"""#145 (27-1): a Refresh is store-only and honest about what it has.

Parametrised over Fundamentals and Swing, which share ``screen_common.load_bars`` (D01 since C12, #383 R3) and the
scan shape.

- U1: a ``no_fetch`` scan with no stored universe makes no screener call; it says so.
- U6: a missing SPY series is "no regime", never a fabricated RISK-ON.
- U7 / D5: a ``no_fetch`` scan persists nothing; only a Fetch writes ``*_scores`` / ``*_gates``.
- B10: the board's JSON is written atomically.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import rawstore, screen_common
from RevenueChaser.core.screen_common import spy_summary
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import config as fcfg
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_fundamentals import scores as fscores
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_swing_potential import config as scfg
from RevenueChaser.module_swing_potential import pipeline as sp
from RevenueChaser.module_swing_potential import scores as sscores
from RevenueChaser.module_swing_potential.report import render_swing_dashboard
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df

MODS = [(fp, render_fundamental_dashboard, fscores, fcfg), (sp, render_swing_dashboard, sscores, scfg)]
ANNUAL = [{"revenue": 100.0 * (i + 1), "gross_profit": 50.0, "operating_cash_flow": 20.0,
           "capex": 5.0, "total_debt": 10.0, "cash": 30.0, "total_assets": 200.0,
           "total_equity": 100.0, "net_income": 10.0} for i in range(3)]


@pytest.fixture
def yf_calls(monkeypatch):
    """Every yfinance entry point records and raises: a scan must not reach the network."""
    calls: list[str] = []

    def _boom(name):
        def f(*a, **k):
            calls.append(name)
            raise AssertionError(f"yfinance.{name} was called")
        return f

    for name in ("screen", "download"):
        monkeypatch.setattr(f"yfinance.{name}", _boom(name))
    monkeypatch.setattr("yfinance.Ticker", _boom("Ticker"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls


@pytest.fixture
def seeded(rawstore_tmp_db, monkeypatch):
    """A stored universe of two scoreable names with D01 bars (SPY included), the clock on their last session."""
    rows = [{"ticker": t, "market_cap": 1e9, "price": 10.0, "name": t, "sector": "Technology"}
            for t in ("AAA", "BBB")]
    rawstore.replace_all("equity_universe", rows)
    for mod in (fp, sp):
        monkeypatch.setattr(mod, "prefilter", lambda r: screen_common.Prefiltered(list(r)))
        monkeypatch.setattr(mod, "load_ticker_info", lambda *a, **k: {})
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals", lambda *a, **k: (ANNUAL, [], {}))
    monkeypatch.setattr(sp, "data_as_of", lambda: None)
    monkeypatch.setattr(cal, "_now", lambda: scn.NOW)
    for t in ("AAA", "BBB", "SPY"):
        scn.seed_d01(t, scn._bars("up", 0.001, 300))
    return rows


# ── U1: no screener call on Refresh ───────────────────────────────────────────

@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_refresh_with_no_stored_universe_makes_no_screener_call(rawstore_tmp_db, yf_calls, mod, render, scores, cfg):
    payload = mod.run_scan(no_fetch=True)
    assert yf_calls == []
    assert payload["no_data"] is True and payload["is_fixture"] is False
    assert payload["no_data_reason"] == "no_universe_stored"
    assert "No stored universe yet" in payload["no_data_message"]
    assert "Fetch Universe" in payload["no_data_message"]
    assert "No stored universe yet" in render(payload)


@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_fetch_with_no_stored_universe_still_loads_it(rawstore_tmp_db, monkeypatch, mod, render, scores, cfg):
    """The first load is the documented network exception (D1), and only a Fetch makes it."""
    import yfinance
    from RevenueChaser.core import universe_fetch
    screened: list[int] = []
    monkeypatch.setattr(yfinance, "screen", lambda *a, **k: screened.append(1) or {"quotes": [], "total": 0})
    monkeypatch.setattr("time.sleep", lambda s: None)
    # Membership comes from the SEC list first (#264): a healthy list of one name, the screener then empty.
    monkeypatch.setattr(universe_fetch, "_sec_member_list", lambda: ([{"ticker": "AAA", "name": "AAA Inc"}], None))
    monkeypatch.setattr(universe_fetch, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(universe_fetch, "backfill_universe_caps", lambda **kw: {})
    payload = mod.run_scan()
    assert screened, "a Fetch with no stored universe must try the screener"
    assert payload["no_data"] is True and payload.get("no_data_reason") != "no_universe_stored"


@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_refresh_passes_no_fetch_to_the_universe_load(monkeypatch, rawstore_tmp_db, mod, render, scores, cfg):
    captured = {}
    monkeypatch.setattr(mod, "fetch_universe_stats",
                        lambda **kw: captured.update(kw) or ([], {"empty_store": True}))
    mod.run_scan(no_fetch=True)
    assert captured["no_fetch"] is True
    captured.clear()
    mod.run_scan()
    assert captured["no_fetch"] is False


# ── U6: no fabricated regime ──────────────────────────────────────────────────

def test_spy_summary_has_no_regime_without_usable_spy():
    assert spy_summary(None)["regime"] is None
    assert spy_summary(pd.DataFrame(columns=["Close"]))["regime"] is None


def test_spy_summary_has_no_regime_with_a_short_series():
    """Fewer than 200 bars: the price is known, the 200-day average is not, so there is no regime."""
    s = spy_summary(_df(100))
    assert s["spy_price"] is not None
    assert s["spy_sma200"] is None
    assert s["regime"] is None


def test_spy_summary_regime_with_a_full_series():
    assert spy_summary(_df(300))["regime"] in ("RISK-ON", "RISK-OFF")


@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_a_missing_regime_stays_missing_in_the_payload_and_on_the_board(mod, render, scores, cfg):
    payload = mod.build_payload([], run_stats={})
    assert payload["regime"] is None
    assert payload["summary"]["regime"] is None
    html = render(payload)
    assert "Regime —" in html
    assert "RISK-ON" not in html and "RISK-OFF" not in html


@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_a_known_regime_still_renders(mod, render, scores, cfg):
    payload = mod.build_payload([], run_stats={"regime": "RISK-OFF"})
    assert payload["regime"] == "RISK-OFF"
    assert "Regime RISK-OFF" in render(payload)


def test_regime_parts():
    from RevenueChaser.core.scan_quality import regime_parts
    assert regime_parts("RISK-ON")[:2] == ("on", "RISK-ON")
    assert regime_parts("RISK-OFF")[:2] == ("off", "RISK-OFF")
    cls, label, momentum = regime_parts(None)
    assert label == "—" and cls not in ("on", "off") and "unknown" in momentum


@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_a_scan_with_no_spy_bars_reports_no_regime(seeded, monkeypatch, mod, render, scores, cfg):
    """A Fetch that cannot get SPY (no stored bars, a dead network) scores the names and shows no regime.

    This ran as a Refresh before K4 (#383 R1); a Refresh no longer scores, so it is a Fetch now."""
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM price_bars_daily WHERE ticker = 'SPY'")
    monkeypatch.setattr(scores, "persist", lambda r: None)
    for name in ("Ticker", "download"):
        monkeypatch.setattr(f"yfinance.{name}", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    payload = mod.run_scan()
    assert payload["no_data"] is False
    assert payload["regime"] is None


# ── U7 / D5: Refresh persists nothing ─────────────────────────────────────────

@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_refresh_does_not_persist_scores(seeded, yf_calls, monkeypatch, mod, render, scores, cfg):
    persisted = []
    monkeypatch.setattr(scores, "persist", lambda r: persisted.append(len(r)))
    payload = mod.run_scan(no_fetch=True)
    # K4 (#383 R1): a Refresh never re-scores; with no recorded Fetch it says there is no stored scan.
    assert payload["no_data"] is True and payload["no_data_reason"] == "no_scan_stored"
    assert persisted == []


@pytest.mark.parametrize("mod,render,scores,cfg", MODS)
def test_fetch_persists_scores(seeded, monkeypatch, mod, render, scores, cfg):
    persisted = []
    monkeypatch.setattr(scores, "persist", lambda r: persisted.append(len(r)))
    # A Fetch may reach yfinance for stale bars; the seeded bars are fresh, so give it a dead network.
    monkeypatch.setattr("yfinance.Ticker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    mod.run_scan()
    assert persisted == [2]


@pytest.mark.parametrize("mod,render,scores,cfg,table", [
    (fp, render_fundamental_dashboard, fscores, fcfg, "fundamentals_scores"),
    (sp, render_swing_dashboard, sscores, scfg, "swing_scores"),
])
def test_refresh_leaves_the_stored_score_table_alone(seeded, yf_calls, mod, render, scores, cfg, table):
    """Measured on main e83aa26: a Refresh replaced {OLD1, OLD2} with whatever the cache could score."""
    rawstore.replace_all(table, [{"ticker": "OLD1", "multiplier": 3}, {"ticker": "OLD2", "multiplier": 2}])
    mod.run_scan(no_fetch=True)
    after = sorted(r["ticker"] for r in rawstore.query(f"SELECT ticker FROM {table}"))
    assert after == ["OLD1", "OLD2"]


# ── B10: atomic JSON ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("mod,render,scores,cfg,attr", [
    (fp, render_fundamental_dashboard, fscores, fcfg, "FUNDAMENTALS_JSON"),
    (sp, render_swing_dashboard, sscores, scfg, "SWING_JSON"),
])
def test_save_payload_is_atomic(tmp_path, monkeypatch, mod, render, scores, cfg, attr):
    target = tmp_path / "board" / "latest.json"
    target.parent.mkdir()
    target.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(cfg, attr, target)

    def _boom(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        mod.save_payload({"new": True})
    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert [p.name for p in target.parent.iterdir()] == ["latest.json"], "a temp file was left behind"


@pytest.mark.parametrize("mod,render,scores,cfg,attr", [
    (fp, render_fundamental_dashboard, fscores, fcfg, "FUNDAMENTALS_JSON"),
    (sp, render_swing_dashboard, sscores, scfg, "SWING_JSON"),
])
def test_save_payload_round_trips_non_ascii(tmp_path, monkeypatch, mod, render, scores, cfg, attr):
    import json
    target = tmp_path / "latest.json"
    monkeypatch.setattr(cfg, attr, target)
    assert mod.save_payload({"name": "Zürich Ünïcode ñ"}) == str(target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "Zürich Ünïcode ñ"}
    assert "Zürich" in target.read_text(encoding="utf-8")  # ensure_ascii stays off

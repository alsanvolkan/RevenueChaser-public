"""Common K4 + AG S5 (C19, #383 R1): a screener Refresh renders what the last Fetch stored and never re-scores.

- Asymmetric Growth: the cards are the ``ticker_scores`` rows (``ticker_store``), the rest (universe, summary, funnel)
  the last Fetch's history header (K2). The card the board embeds is the stored shape (``ticker_store.card_view``),
  so a Fetch and a Refresh embed the same card.
- Swing and Fundamentals: the last Fetch's board payload, recorded on its history header, or the board's saved JSON
  when that is newer (a Fetch whose history write failed). A saved board that is empty or the demo fixture never
  counts. A header from before K4 (no payload) falls back to the saved JSON; with neither, no scan is stored.
- With ``no_fetch=True`` no ``compute_row`` / scorer runs and no pool opens; one that would raise changes nothing.
- Golden: on fixtures the Refresh render equals the Fetch render. The Recorded-sessions panel is read when the page
  renders (a Fetch shows its own session from the next render), so it is left out of the comparison.
Offline, temp store.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from RevenueChaser.app import runners
from RevenueChaser.core import history, rawstore, screen_common, ticker_store
from RevenueChaser.module_fundamentals import config as fcfg
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import config as scfg
from RevenueChaser.module_swing_potential import pipeline as sp
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df, _seed_ohlc

_PANEL = re.compile(r'<details class="(?:rules|rc-history)" id="history-panel">.*?</details>', re.S)
ANNUAL = [{"fiscal_year": 2022 + i, "revenue": 100.0 * (i + 1), "gross_profit": 60.0 * (i + 1),
           "operating_cash_flow": 30.0 * (i + 1), "capex": 5.0, "total_debt": 10.0, "cash": 30.0,
           "total_assets": 200.0, "total_equity": 100.0, "net_income": 20.0 * (i + 1)} for i in range(3)]


@pytest.fixture
def out(isolate_board_artifacts, monkeypatch):
    """Every artifact the runners write lands in this test's folder: the root conftest already points both copies
    of each board path there (#111), so only the runners' own OUTPUT_DIR is set here."""
    monkeypatch.setattr(runners, "OUTPUT_DIR", isolate_board_artifacts)
    return isolate_board_artifacts


def _html(path) -> str:
    return _PANEL.sub("", path.read_text(encoding="utf-8"))


def _no_scoring(monkeypatch):
    """Any scorer, pool or network call on a Refresh fails the test."""
    def boom(*a, **k):
        raise AssertionError("a Refresh must not score")
    from RevenueChaser.module_asymmetric_growth import engine
    for mod in (sp, fp):
        monkeypatch.setattr(mod, "_compute_row", boom)
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", boom)
    monkeypatch.setattr(engine, "enrich_and_score", boom)
    monkeypatch.setattr(screen_common, "ThreadPoolExecutor", boom)
    monkeypatch.setattr("yfinance.Ticker", boom)
    monkeypatch.setattr("yfinance.download", boom)


# ── Asymmetric Growth (S5) ───────────────────────────────────────────────────────────────────────────────────────
def _card(ticker, points, **kw):
    card = {"ticker": ticker, "fiscal_year": 2025, "name": f"{ticker} Inc", "sector": "Technology",
            "industry": "Software", "inst_ownership_pct": 0.61, "price": 12.5, "market_cap": 8.0e8,
            "ps_ratio": 4.2, "rev_growth": 0.45, "gross_margin": 0.7, "fcf": 5_000_000, "runway_years": None,
            "rd_intensity": 0.2, "dilution_yoy": 0.01, "earnings_date": 1.7e9, "total_points": points,
            "signals": [{"name": "Growth", "points": 2, "subs": [{"param": "rev_growth", "passed": True}]}],
            "themes": ["AI"], "tech_breakthrough": True, "dilution_warning": False, "us_listed": True,
            "source": "sec_edgar", "inst_holders": 120.0, "inst_new": 4, "inst_exited": 2, "inst_buy_flow": 1.5e7,
            "inst_sell_flow": 3.0e6, "inst_shares": 2.0e7, "inst_shares_delta_pct": 0.05}
    card.update(kw)
    return card


def _ag_result():
    from RevenueChaser.module_asymmetric_growth import engine, freeze
    mv = freeze.model_version()             # rows of this version: the board shows no "scored before" banner
    ends = {"revenue": ["2025-12-31", "2024-12-31", "2023-12-31"], "balance_sheet": "2025-12-31", "cash_flow": None}
    cards = [_card("AAA", 9, tier=3, flags=[], model_version=mv, anchor_fy=2025, prior_fy=2024, period_ends=ends),
             _card("BBB", 6, rev_growth=None, themes=[], tier=None, flags=["mixed_share_basis"], model_version=mv),
             _card("CCC", 6, rev_growth=0.9, tier=2, flags=["suspicious_growth"], model_version=mv)]
    engine._sort_cards(cards)                # tier, points, capped growth, ticker (#380 S2)
    return {"asof": "2026-10-06T22:05:00+00:00", "data_as_of": "2026-10-06", "universe_total": 4187,
            "universe_generated_at": "2026-10-06T21:00:00",
            "universe_refreshed_by": "Asymmetric Growth", "universe_partial": True,
            "funnel_steps": [{"step": "universe", "names": 4187}, {"step": "scored", "names": 3}],
            "summary": {"scanned": 3, "excluded_types": 12, "pts_9": 1, "pts_8_7": 0, "pts_6_5": 2, "pts_lt5": 0,
                        "score_errors": 1, "score_error_types": {"ValueError": 1}},
            "cards": cards, "thresholds": __import__(
                "RevenueChaser.module_asymmetric_growth.engine", fromlist=["THRESHOLDS"]).THRESHOLDS}


def _ag_payload():
    """The cards the AG page loads (S7, K10): the gzip payload file next to the page."""
    import gzip
    from RevenueChaser.core.reporting_common import payload_path
    return json.loads(gzip.decompress(payload_path(runners.ASYMMETRIC_GROWTH_LATEST).read_bytes()))


@pytest.fixture
def ag_fetched(rawstore_tmp_db, out, monkeypatch):
    from RevenueChaser.module_asymmetric_growth import engine, freeze
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "price": 10.0, "name": "A"}])
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: _ag_result())
    monkeypatch.setattr(freeze, "freeze_result", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "fetch-1")
    runners.run_asymmetric_growth(None)
    return _html(runners.ASYMMETRIC_GROWTH_LATEST), _ag_payload()


def test_ag_refresh_renders_the_fetch_render(ag_fetched, monkeypatch):
    _no_scoring(monkeypatch)
    runners.ASYMMETRIC_GROWTH_LATEST.unlink()
    runners.run_asymmetric_growth(None, no_fetch=True)
    html, payload = ag_fetched
    assert _html(runners.ASYMMETRIC_GROWTH_LATEST) == html
    assert _ag_payload() == payload                         # the cards, outside the page (S7)
    assert [c["ticker"] for c in payload["cards"]] == ["AAA", "CCC", "BBB"]
    assert [(c["tier"], c["flags"]) for c in payload["cards"]] == [
        (3, []), (2, ["suspicious_growth"]), (None, ["mixed_share_basis"])]       # #380 S2: tier and flags kept
    assert payload["cards"][0]["period_ends"]["revenue"][2] == "2023-12-31"         # #380 S3: the provenance kept
    assert payload["cards"][1]["period_ends"] == {} and payload["cards"][0]["anchor_fy"] == 2025


def test_ag_refresh_reads_the_stored_rows_and_the_header(ag_fetched, monkeypatch):
    from RevenueChaser.core import index_registry
    from RevenueChaser.module_asymmetric_growth import engine
    monkeypatch.setattr(index_registry, "load_registry", lambda: {"CCC": {"index": ["S&P 500"]}})
    res = engine.stored_result()
    assert [c["ticker"] for c in res["cards"]] == ["AAA", "CCC", "BBB"]
    assert [c["index"] for c in res["cards"]] == [[], ["S&P 500"], []]      # the index labels, read on the Refresh
    assert res["summary"]["score_error_types"] == {"ValueError": 1}
    assert res["funnel_steps"][-1] == {"step": "scored", "names": 3}
    assert res["universe_partial"] is True and res["asof"] == "2026-10-06T22:05:00+00:00"


def test_ag_refresh_header_says_the_fetch_s_prices_session(ag_fetched, monkeypatch):
    """#380 S4: the Fetch names the D01 session its momentum read, and a Refresh restores it from the header."""
    from RevenueChaser.module_asymmetric_growth import engine
    html, _ = ag_fetched
    assert "Prices: D01 session 2026-10-06" in html
    assert engine.stored_result()["data_as_of"] == "2026-10-06"
    _no_scoring(monkeypatch)
    runners.ASYMMETRIC_GROWTH_LATEST.unlink()
    runners.run_asymmetric_growth(None, no_fetch=True)
    refreshed = _html(runners.ASYMMETRIC_GROWTH_LATEST)
    assert "Prices: D01 session 2026-10-06" in refreshed
    assert refreshed == html                    # the whole header, not just the chip


def test_ag_stored_cards_are_what_ticker_scores_holds(ag_fetched):
    from RevenueChaser.module_asymmetric_growth import engine
    ticker_store.write_from_cards([{"ticker": t, "name": t, "sector": "Energy", "total_points": pts,
                                    "themes": [], "signals": [], "tech_breakthrough": False}
                                   for t, pts in (("YYY", 3), ("ZZZ", 5))],
                                  asof="2026-10-07T22:05:00+00:00")         # a later scan with no header
    res = engine.stored_result()
    assert [c["ticker"] for c in res["cards"]] == ["ZZZ", "YYY"]            # by points, as the Fetch sorts them
    # the header is not this scan's (its asof differs): the summary comes from the cards, no funnel
    assert res["summary"]["scanned"] == 2 and res["funnel_steps"] is None


def test_ag_a_fetch_embeds_the_stored_card_shape():
    from RevenueChaser.module_asymmetric_growth.report import _card_rows
    card = _card("AAA", 9, index=["S&P 500"])
    shown = _card_rows([card])[0]
    assert "fiscal_year" not in shown and shown["index"] == ["S&P 500"]
    assert shown["inst_holders"] == 120 and isinstance(shown["fcf"], float)
    # The page row adds the shared filter fields at render (#383 R2b C1c), from a store read: the stored card stays.
    assert isinstance(shown.pop("gics"), list) and isinstance(shown.pop("theme_ids"), list)
    assert shown == ticker_store.card_view(ticker_store._row_to_card(ticker_store.rows_from_cards([card])[0])
                                           | {"index": ["S&P 500"]})


def test_ag_refresh_with_a_universe_but_no_stored_scan_says_so(rawstore_tmp_db, out, monkeypatch):
    _no_scoring(monkeypatch)
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "price": 10.0, "name": "A"}])
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert "No stored scan yet" in runners.ASYMMETRIC_GROWTH_LATEST.read_text(encoding="utf-8")


# ── Swing and Fundamentals (K4) ──────────────────────────────────────────────────────────────────────────────────
BOARDS = [pytest.param(sp, "run_swing", "SWING_LATEST", scfg, "SWING_JSON", "module_swing_potential", id="swing"),
          pytest.param(fp, "run_fundamentals", "FUNDAMENTALS_LATEST", fcfg, "FUNDAMENTALS_JSON",
                       "module_fundamentals", id="fundamentals")]


@pytest.fixture
def seeded(rawstore_tmp_db, out, monkeypatch):
    from RevenueChaser.module_fundamentals import freeze as ffreeze
    from RevenueChaser.module_swing_potential import freeze as sfreeze
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1e9, "price": 10.0, "name": t, "sector": "Technology"} for t in ("AAA", "BBB")])
    for mod in (fp, sp):
        monkeypatch.setattr(mod, "prefilter", lambda r: screen_common.Prefiltered(list(r)))
        monkeypatch.setattr(mod, "load_ticker_info", lambda *a, **k: {})
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals", lambda *a, **k: (ANNUAL, [], {}))
    monkeypatch.setattr(sp, "data_as_of", lambda: None)
    monkeypatch.setattr(fp, "data_as_of", lambda: None)
    monkeypatch.setattr(sfreeze, "freeze_payload", lambda *a, **k: None)
    monkeypatch.setattr(ffreeze, "freeze_payload", lambda *a, **k: None)
    monkeypatch.setattr("yfinance.Ticker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(screen_common, "load_bars", lambda *a, **k: (_df(300), _df(300)))     # D01 (C12)
    _seed_ohlc("SPY", _df(300))                                    # SPY's fallback: no D01 SPY in this store
    # Swing: indicators that pass its gates (the cached bars alone pass none), so the board has cards to compare.
    from RevenueChaser.module_swing_potential.tests.test_swing_history_278 import _ind
    monkeypatch.setattr(sp, "_compute_row", lambda r, spy_df, no_fetch, spine, drops: {
        "ticker": r["ticker"], "name": r["name"], "market_cap": r["market_cap"], "sector": "Technology",
        "industry": "Software", "themes": [], "earnings_date": None, "ind": _ind(), "ohlc_stale": False})


@pytest.mark.parametrize("mod,runner,latest,cfg,saved,board", BOARDS)
def test_a_refresh_renders_the_fetch_render(seeded, monkeypatch, mod, runner, latest, cfg, saved, board):
    getattr(runners, runner)(None)
    fetched = _html(getattr(runners, latest))
    assert history.last_run(board, payload=True)["payload"]["candidates"]          # the fixture scores names
    _no_scoring(monkeypatch)
    getattr(runners, latest).unlink()
    getattr(runners, runner)(None, no_fetch=True)
    assert _html(getattr(runners, latest)) == fetched


@pytest.mark.parametrize("mod,runner,latest,cfg,saved,board", BOARDS)
def test_a_refresh_shows_the_last_fetch_not_a_rescore(seeded, monkeypatch, mod, runner, latest, cfg, saved, board):
    getattr(runners, runner)(None)
    payload = history.last_run(board, payload=True)["payload"]
    _no_scoring(monkeypatch)
    assert mod.run_scan(no_fetch=True) == payload
    # an older saved board (a render before this Fetch) loses to the recorded payload
    older = dict(mod.build_payload([], run_stats={"regime": "RISK-OFF"}), generated_at="2020-01-01T00:00:00+00:00")
    getattr(cfg, saved).write_text(json.dumps(older), encoding="utf-8")
    assert mod.run_scan(no_fetch=True) == payload


@pytest.mark.parametrize("mod,runner,latest,cfg,saved,board", BOARDS)
def test_a_fetch_whose_history_write_failed_is_still_what_a_refresh_shows(seeded, monkeypatch, mod, runner, latest,
                                                                         cfg, saved, board):
    """M-1: the newer of the recorded payload and the saved board wins, so an older header never replaces it."""
    getattr(runners, runner)(None)
    first = history.last_run(board, payload=True)["payload"]
    newer = dict(first, generated_at="2099-01-01T00:00:00+00:00", regime="RISK-OFF")   # Fetch 2, header lost
    getattr(cfg, saved).write_text(json.dumps(newer), encoding="utf-8")
    _no_scoring(monkeypatch)
    assert mod.run_scan(no_fetch=True) == newer
    getattr(runners, runner)(None, no_fetch=True)                  # the Refresh render keeps Fetch 2 on disk
    assert json.loads(getattr(cfg, saved).read_text(encoding="utf-8")) == newer


@pytest.mark.parametrize("mod,runner,latest,cfg,saved,board", BOARDS)
@pytest.mark.parametrize("flag", ["no_data", "is_fixture"])
def test_an_empty_or_demo_saved_board_never_wins(seeded, monkeypatch, mod, runner, latest, cfg, saved, board, flag):
    getattr(runners, runner)(None)
    payload = history.last_run(board, payload=True)["payload"]
    junk = dict(mod.build_payload([], run_stats={}), generated_at="2099-01-01T00:00:00+00:00", **{flag: True})
    getattr(cfg, saved).write_text(json.dumps(junk), encoding="utf-8")
    _no_scoring(monkeypatch)
    assert mod.run_scan(no_fetch=True) == payload                  # newer, but empty or demo: the Fetch wins
    with rawstore.connect(db="history") as conn:
        conn.execute("DELETE FROM result_runs")
    shown = mod.run_scan(no_fetch=True)                           # no header at all: still never the junk board
    assert shown["no_data_reason"] == "no_scan_stored" and not shown.get("is_fixture")


@pytest.mark.parametrize("mod,runner,latest,cfg,saved,board", BOARDS)
def test_a_header_without_payload_falls_back_to_the_saved_board(rawstore_tmp_db, out, monkeypatch, mod, runner,
                                                                latest, cfg, saved, board):
    _no_scoring(monkeypatch)
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "price": 10.0, "name": "A"}])
    when = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
    history.record_run(board, run_id="old", run_at=when, model_version="m")          # a header from before K4
    shown = mod.build_payload([], run_stats={"regime": "RISK-OFF"})
    getattr(cfg, saved).parent.mkdir(parents=True, exist_ok=True)
    getattr(cfg, saved).write_text(json.dumps(shown), encoding="utf-8")
    assert mod.run_scan(no_fetch=True) == shown
    getattr(cfg, saved).unlink()
    payload = mod.run_scan(no_fetch=True)
    assert payload["no_data"] is True and payload["no_data_reason"] == "no_scan_stored"
    assert "No stored scan yet" in payload["no_data_message"]


@pytest.mark.parametrize("mod,runner,latest,cfg,saved,board", BOARDS)
def test_an_empty_store_still_says_no_universe(rawstore_tmp_db, out, monkeypatch, mod, runner, latest, cfg, saved,
                                              board):
    _no_scoring(monkeypatch)
    payload = mod.run_scan(no_fetch=True)
    assert payload["no_data_reason"] == "no_universe_stored"

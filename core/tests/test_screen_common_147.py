"""#147 (27-3a): the shared screen skeleton (``core/screen_common.py``) with fake boards, no store, no network.

``run_screen`` is the body both Swing and Fundamentals ran as a copy. These tests pin its contract with plain
callables: what it loads, what it excludes and caps, what it hands each ``compute_row``, what it counts, and when it
persists. ``test_screen_parity_147.py`` pins the same body against the real boards.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from RevenueChaser.core import screen_common as sc
from RevenueChaser.module_fundamentals import config as fcfg
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import config as scfg
from RevenueChaser.module_swing_potential import pipeline as sp

CFG = SimpleNamespace(MIN_PRICE=5.0, MIN_DOLLAR_VOLUME=250_000.0)


def _q(price=50.0, vol=100_000.0):
    return {"price": price, "avg_volume": vol, "session_date": None, "source": "yfinance"}


# ── prefilter (7-A on D04 since #383 R3 C12; core/tests/test_screen_c12_383.py reads the store) ───────────────────

@pytest.mark.parametrize("name,quote,reason", [
    ("healthy", _q(), None),
    ("no quote at all", None, "no_quote"),
    ("no price", _q(price=None), "no_quote"),
    ("price under the floor", _q(price=4.99), "low_price"),
    ("price exactly at the floor", _q(price=5.0, vol=1e6), None),
    ("dollar volume under the floor", _q(price=10.0, vol=24_999.0), "low_dollar_volume"),
    ("dollar volume exactly at the floor", _q(price=10.0, vol=25_000.0), None),
    ("volume missing skips the volume rule", _q(price=10.0, vol=None), None),
    ("NaN price", _q(price=float("nan")), "no_quote"),
])
def test_prefilter_rules(name, quote, reason):
    row = {"ticker": "X", "quote": {}}
    pf = sc.prefilter([row], CFG, quotes={} if quote is None else {"X": quote})
    assert (pf.kept == [row]) is (reason is None), name
    assert pf.drops == ({} if reason is None else {reason: ["X"]}), name
    assert pf.dropped == (0 if reason is None else 1)


def test_prefilter_counts_and_keeps_order():
    rows = [{"ticker": t} for t in "ABCD"]
    pf = sc.prefilter(rows, CFG, quotes={"A": _q(), "B": _q(price=1.0), "C": _q(), "D": _q(price=None)})
    assert [r["ticker"] for r in pf.kept] == ["A", "C"] and pf.dropped == 2
    assert pf.drops == {"low_price": ["B"], "no_quote": ["D"]}


@pytest.mark.parametrize("mod,cfg", [(sp, scfg), (fp, fcfg)], ids=["swing", "fundamentals"])
def test_each_board_filters_with_its_own_config(monkeypatch, mod, cfg):
    row = {"ticker": "X", "quote": {}}
    monkeypatch.setattr("RevenueChaser.core.screen_common.prefilter_inputs", lambda tickers: {"X": _q()})
    assert mod.prefilter([row]).kept == [row]
    monkeypatch.setattr(cfg, "MIN_PRICE", 60.0)
    assert mod.prefilter([row]).drops == {"low_price": ["X"]}


def test_both_boards_hold_the_same_prefilter_thresholds_today():
    for name in ("MIN_PRICE", "MIN_DOLLAR_VOLUME"):
        assert getattr(scfg, name) == getattr(fcfg, name), name


# ── spy_summary ───────────────────────────────────────────────────────────────

def _spy(n, start=100.0, step=0.5):
    idx = pd.bdate_range("2024-01-02", periods=n)
    return pd.DataFrame({"Close": [start + step * i for i in range(n)]}, index=idx)


def test_spy_summary_regime_follows_price_against_the_200_day_average():
    up, down = sc.spy_summary(_spy(300)), sc.spy_summary(_spy(300, start=400.0, step=-0.5))
    assert up["regime"] == "RISK-ON" and down["regime"] == "RISK-OFF"


def test_spy_summary_needs_200_bars_for_a_regime_and_64_for_the_3_month_return():
    assert sc.spy_summary(_spy(199))["regime"] is None
    assert sc.spy_summary(_spy(200))["regime"] == "RISK-ON"
    assert sc.spy_summary(_spy(63))["spy_3m_return"] is None
    assert sc.spy_summary(_spy(64))["spy_3m_return"] is not None


def test_spy_summary_rounds_and_reports_the_values():
    s = sc.spy_summary(_spy(300))
    close = _spy(300)["Close"]
    assert s["spy_price"] == round(close.iloc[-1], 2)
    assert s["spy_sma200"] == round(close.rolling(200).mean().iloc[-1], 2)
    assert s["spy_3m_return"] == round(close.iloc[-1] / close.iloc[-64] - 1.0, 4)


def test_spy_summary_of_nothing_is_all_none():
    empty = {"regime": None, "spy_price": None, "spy_sma200": None, "spy_3m_return": None}
    assert sc.spy_summary(None) == empty
    assert sc.spy_summary(pd.DataFrame(columns=["Close"])) == empty


# ── run_screen ────────────────────────────────────────────────────────────────

from RevenueChaser.core.gics_map import GicsPath as _GP  # noqa: E402


class Board:
    """Fake board: records what ``run_screen`` calls and with what."""

    def __init__(self, monkeypatch, rows=None, u_stats=None, excluded=(), spine=None, spy=None, cfg=CFG):
        self.rows = rows if rows is not None else [{"ticker": t, "quote": {}} for t in "ABCDE"]
        self.u_stats = u_stats if u_stats is not None else {"out_of_universe": 7}
        self.spy = spy if spy is not None else _spy(300)
        self.cfg = cfg
        self.load_calls: list[dict] = []
        self.prefilter_in: list[list[dict]] = []
        self.compute_calls: list[tuple] = []
        self.persisted: list[list[dict]] = []
        self.spy_loads = 0
        self.payload_calls: list[dict] = []
        self.behaviour = {}            # ticker -> row dict | None | Exception
        self.reasons = {}              # ticker -> drop reason appended when its row is None
        monkeypatch.setattr(sc._universe, "excluded_tickers", lambda: set(excluded))
        monkeypatch.setattr(sc._universe, "spine_gics", lambda: spine if spine is not None else {"A": _GP("Technology", "Software"), "B": _GP("Technology", "Software")})

    def load_universe(self, **kw):
        self.load_calls.append(kw)
        return self.rows, self.u_stats

    def prefilter(self, rows):
        self.prefilter_in.append(list(rows))
        return sc.Prefiltered(list(rows), {})

    def load_spy(self):
        self.spy_loads += 1
        return self.spy

    def compute_row(self, row, spy_df, no_fetch, spine, drops):
        self.compute_calls.append((row["ticker"], spy_df, no_fetch, spine, drops))
        what = self.behaviour.get(row["ticker"], {"ticker": row["ticker"]})
        if isinstance(what, Exception):
            raise what
        if what is None:
            drops.append(self.reasons.get(row["ticker"], "no_ohlc"))
        return what

    def build_payload(self, results, top_n=None, run_stats=None):
        self.payload_calls.append({"results": results, "top_n": top_n, "run_stats": run_stats})
        return {"results": sorted(r["ticker"] for r in results), "run_stats": run_stats, "top_n": top_n,
                "no_data": False}

    def run(self, **kw):
        args = dict(board="test", refreshed_by="Test Board", cfg=self.cfg, load_universe=self.load_universe,
                    prefilter=self.prefilter, load_spy=self.load_spy, compute_row=self.compute_row,
                    build_payload=self.build_payload, persist=self.persisted.append)
        args.update(kw)
        return sc.run_screen(**args)

    @property
    def stats(self):
        return self.payload_calls[-1]["run_stats"]


@pytest.fixture
def board(monkeypatch):
    return Board(monkeypatch)


def test_the_universe_is_loaded_with_the_callers_flags(board):
    board.run(refresh_universe=True, no_fetch=False)
    board.run(refresh_universe=False, no_fetch=True)
    assert board.load_calls == [{"refreshed_by": "Test Board", "refresh": True, "no_fetch": False},
                                {"refreshed_by": "Test Board", "refresh": False, "no_fetch": True}]


@pytest.mark.parametrize("no_fetch,reason", [(False, "no_universe"), (True, "no_universe_stored")])
def test_an_empty_universe_is_an_explicit_empty_state_and_does_nothing_else(monkeypatch, no_fetch, reason):
    b = Board(monkeypatch, rows=[])
    payload = b.run(no_fetch=no_fetch)
    assert payload["no_data"] is True and "is_fixture" not in payload
    assert payload["no_data_reason"] == reason
    assert b.payload_calls == [{"results": [], "top_n": None, "run_stats": {}}]
    assert b.compute_calls == [] and b.persisted == [] and b.spy_loads == 0


def test_excluded_types_never_reach_the_prefilter_or_a_row(monkeypatch):
    b = Board(monkeypatch, excluded={"B", "D"})
    b.run()
    assert [r["ticker"] for r in b.prefilter_in[0]] == ["A", "C", "E"]
    assert {c[0] for c in b.compute_calls} == {"A", "C", "E"}
    assert b.stats["excluded_types"] == 2
    assert b.stats["raw_universe"] == 5, "the scanned universe counts every stored row"


def test_every_row_the_prefilter_keeps_is_scored_with_no_cap(monkeypatch):
    """C12 (#383 R3, card C): no ``MAX_PHASE2`` shortlist. The prefilter's drops are funnel steps (C16) and its quote
    session is in the payload's data_quality (U9)."""
    b = Board(monkeypatch)
    from datetime import date
    b.prefilter = lambda rows: sc.Prefiltered(rows[1:], {"low_price": ["A"]}, date(2026, 10, 14), 2)
    payload = b.run()
    assert {c[0] for c in b.compute_calls} == {"B", "C", "D", "E"}
    assert b.stats["filtered_universe"] == 4 and b.stats["hygiene"] == 1
    low = {s["key"]: s for s in payload["funnel_steps"]}["low_price"]
    assert low["count"] == 1 and low["tickers"] == ["A"]
    assert payload["data_quality"]["quote_session"] == "2026-10-14" and payload["data_quality"]["older_quotes"] == 2


def test_every_compute_row_gets_the_shared_inputs(monkeypatch):
    spy = _spy(300)
    spine = {"A": _GP("Technology", "Software")}
    b = Board(monkeypatch, spy=spy, spine=spine)
    b.run(no_fetch=False)
    assert len(b.compute_calls) == 5
    drops_lists = {id(c[4]) for c in b.compute_calls}
    assert len(drops_lists) == 5, "one drop list per row, so the funnel knows each reason's ticker (C16, #383 R3)"
    for _t, spy_df, no_fetch, sp_, _d in b.compute_calls:
        assert spy_df is spy and no_fetch is False and sp_ is spine
    assert b.spy_loads == 1


def test_the_spy_summary_and_staleness_go_into_the_run_stats(monkeypatch):
    spy = _spy(300)
    spy.attrs["stale"] = True
    b = Board(monkeypatch, spy=spy)
    b.run()
    want = sc.spy_summary(spy)
    for k in ("regime", "spy_price", "spy_sma200", "spy_3m_return"):
        assert b.stats[k] == want[k]
    assert b.stats["spy_stale"] is True


def test_missing_spy_gives_no_regime_and_the_scan_still_runs(monkeypatch):
    b = Board(monkeypatch, spy=pd.DataFrame())
    payload = b.run()
    assert b.stats["regime"] is None and b.stats["spy_stale"] is False
    assert payload["results"] == list("ABCDE")


def test_rows_that_come_back_none_are_counted_with_the_reason_they_gave(monkeypatch):
    b = Board(monkeypatch)
    b.behaviour = {"B": None, "C": None, "D": None}
    b.reasons = {"B": "no_ohlc", "C": "no_ohlc", "D": "short_bars"}
    payload = b.run()
    assert payload["results"] == ["A", "E"]
    assert b.stats["data_short"] == 3
    assert b.stats["dropped"] == {"no_ohlc": 2, "short_bars": 1}


def test_a_ticker_that_raises_is_counted_as_a_compute_error_and_the_scan_carries_on(monkeypatch):
    b = Board(monkeypatch)
    b.behaviour = {"C": RuntimeError("boom"), "D": KeyError("x")}
    payload = b.run()
    assert payload["results"] == ["A", "B", "E"]
    assert b.stats["dropped"] == {"compute_error": 2}
    assert b.stats["data_short"] == 2
    assert sorted(r["ticker"] for r in b.persisted[0]) == ["A", "B", "E"]


def test_every_ticker_raising_is_an_explicit_empty_state(monkeypatch):
    b = Board(monkeypatch)
    b.behaviour = {t: ValueError(t) for t in "ABCDE"}
    payload = b.run()
    assert payload["no_data"] is True and payload["no_data_reason"] == "no_scorable_tickers"
    assert b.stats["dropped"] == {"compute_error": 5}
    assert "unexpected error while scoring" in payload["no_data_message"]
    assert b.persisted == []


def test_the_empty_scorable_state_reports_the_prefiltered_count(monkeypatch):
    b = Board(monkeypatch)
    b.prefilter = lambda rows: sc.Prefiltered(rows[:2], {"low_price": ["C", "D", "E"]})
    b.behaviour = {t: None for t in "ABCDE"}
    payload = b.run()
    assert payload["no_data_reason"] == "no_scorable_tickers"
    assert "none of the 2 shortlisted tickers" in payload["no_data_message"]


def test_stale_bars_no_spine_and_the_universe_provenance_are_reported(monkeypatch):
    b = Board(monkeypatch, u_stats={"out_of_universe": 3, "universe_generated_at": "2026-10-01T00:00:00+00:00",
                                    "universe_refreshed_by": "Swing Potential", "partial": True},
              spine={"A": _GP("Technology", "Software"), "B": _GP("Technology", "Software"),
                     "C": _GP("Unclassified", "Oil & Gas")})
    b.behaviour = {"A": {"ticker": "A", "ohlc_stale": True}, "B": {"ticker": "B", "ohlc_stale": True},
                   "C": {"ticker": "C", "ohlc_stale": False}}
    b.run()
    s = b.stats
    assert s["stale_ohlc"] == 2
    assert s["no_spine_gics"] == 3, "C has an Unclassified sector (its industry does not matter); D and E have no spine row"
    assert s["out_of_universe"] == 3
    assert s["universe_generated_at"] == "2026-10-01T00:00:00+00:00"
    assert s["universe_refreshed_by"] == "Swing Potential"
    assert s["universe_partial"] is True


def test_the_partial_flag_defaults_to_false(board):
    board.run()
    assert board.stats["universe_partial"] is False


def test_extra_stats_are_added_after_the_scan_and_top_n_is_passed_on(board):
    seen = []
    board.run(extra_stats=lambda: seen.append(len(board.compute_calls)) or {"data_as_of": "2026-10-02"}, top_n=7)
    assert seen == [5], "extra stats are read after every row was scored"
    assert board.stats["data_as_of"] == "2026-10-02"
    assert board.payload_calls[-1]["top_n"] == 7


def test_a_fetch_persists_the_results_once_and_a_refresh_persists_nothing(board):
    board.run(no_fetch=False)
    assert len(board.persisted) == 1
    assert sorted(r["ticker"] for r in board.persisted[0]) == list("ABCDE")
    board.persisted.clear()
    board.run(no_fetch=True)
    assert board.persisted == []


def test_a_refresh_scores_nothing_and_says_there_is_no_stored_scan(board):
    """K4 (#383 R1): with no recorded Fetch, a Refresh loads only the stored universe and shows the no-scan message."""
    payload = board.run(no_fetch=True)
    assert board.compute_calls == [] and board.prefilter_in == [] and board.spy_loads == 0
    assert board.persisted == []
    assert payload["no_data"] is True and payload["no_data_reason"] == "no_scan_stored"


def test_the_payload_is_what_the_board_built(board):
    payload = board.run()
    assert payload["results"] == list("ABCDE") and payload["no_data"] is False


# ── the two boards' thin ``run_scan`` wrappers ────────────────────────────────

@pytest.mark.parametrize("mod,name", [(sp, "Swing Potential"), (fp, "Fundamentals")], ids=["swing", "fundamentals"])
@pytest.mark.parametrize("refresh,no_fetch", [(False, False), (True, False), (False, True)])
def test_each_board_loads_the_universe_under_its_own_name_with_the_callers_flags(monkeypatch, mod, name, refresh,
                                                                                  no_fetch):
    seen = {}
    monkeypatch.setattr(mod, "fetch_universe_stats", lambda **kw: seen.update(kw) or ([], {}))
    mod.run_scan(refresh_universe=refresh, no_fetch=no_fetch)
    assert seen == {"refreshed_by": name, "refresh": refresh, "no_fetch": no_fetch}


@pytest.mark.parametrize("mod", [sp, fp], ids=["swing", "fundamentals"])
def test_top_n_reaches_the_payload(rawstore_tmp_db, monkeypatch, mod):
    from RevenueChaser.core.tests import screen_parity as scn
    scn.seed()
    scn.patch(monkeypatch)
    everything = mod.run_scan()["candidates"]
    assert len(everything) >= 2
    assert len(mod.run_scan(top_n=1)["candidates"]) == 1

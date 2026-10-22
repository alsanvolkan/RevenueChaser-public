"""D28/D29 (#34): SPX Gamma reads the vol indices and the COT the nightly sweep stored, and asks live only when the
store has nothing.

The point of the lane is the two provider calls a night that no longer leave the process: with both tables filled the
injected live fetchers are never called, the manifest's ``input_sources`` says ``store``, and the payload reaches
``normalize_vix`` / ``normalize_cftc`` in the very shape their live fetchers serve — one expected dict, two sources.
"""
from __future__ import annotations

import json
from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.normalize import normalize_cftc, normalize_vix
from RevenueChaser.module_spx_gamma.vix import VIX_TICKERS

from .conftest import FakeUW
from .test_s2_fetch import CANDLES, DAY, _handler, _no_network

DAY_DATE = date.fromisoformat(DAY)
CLOSES = {"VIX": 15.5, "VIX9D": 14.25, "VIX3M": 17.0, "VIX6M": 18.0, "VIX1Y": 19.0, "VVIX": 90.5}
COT = {"open_interest": 2_000_000.0,
       "asset_mgr_long": 500_000.0, "asset_mgr_short": 120_000.0, "asset_mgr_change_long": 1_500.0,
       "lev_money_long": 300_000.0, "lev_money_short": 260_000.0, "lev_money_change_long": -2_200.0,
       "dealer_long": 90_000.0, "dealer_short": 420_000.0, "dealer_change_long": 300.0}


def _seed_vol(session: date = DAY_DATE) -> None:
    rawstore.write_rows("vol_index_daily", [
        {"symbol": symbol, "session_date": session, "source": "cboe", "close": close,
         "open": close, "high": close, "low": close} for symbol, close in CLOSES.items()])


def _seed_cot(report: date = DAY_DATE, session: date | None = None) -> None:
    rawstore.write_rows("cftc_cot", [{
        "report_type": "TFF", "market": "E-MINI S&P 500", "report_date": report,
        "session_date": session or report, "source": "cftc", **COT}])


def _fetch(tmp_path, *, vix_fetcher, cftc_fetcher, candles=True):
    return snap.fetch_snapshot(FakeUW(_handler, prefix=""), DAY, data_root=tmp_path,
                               vix_fetcher=vix_fetcher, cftc_fetcher=cftc_fetcher,
                               candle_fetcher=lambda *a, **k: list(CANDLES) if candles else [])


def _spies(calls):
    def vix_live():
        calls.append("vix")
        return {"tickers": {t: 1.0 for t in VIX_TICKERS}}

    def cftc_live():
        calls.append("cftc")
        return {"report_date": "2026-08-11", "open_interest": 1.0}

    return vix_live, cftc_live


def _manifest(tmp_path):
    return json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))


# ── 6. one normalizer, two sources, one dict ─────────────────────────────────────────────────────────────────────

def test_normalize_vix_cannot_tell_the_stored_payload_from_the_live_one(rawstore_tmp_db):
    _seed_vol()
    stored, session = stored_inputs.vol_index_rows(DAY_DATE)
    live = {"tickers": {f"^{s}": c for s, c in CLOSES.items()}, "term_mode": "contango"}
    expected = {"vix": 15.5, "vix9d": 14.25, "vix3m": 17.0, "vix6m": 18.0, "vix1y": 19.0, "vvix": 90.5,
                "term_mode": "contango"}

    assert session == DAY_DATE
    assert normalize_vix(stored) == expected
    assert normalize_vix(live) == expected


def test_normalize_cftc_cannot_tell_the_stored_payload_from_the_live_one(rawstore_tmp_db):
    _seed_cot()
    stored, session = stored_inputs.cot_row(DAY_DATE)
    live = {"report_date": DAY, "open_interest": 2_000_000.0,
            "asset_manager": {"long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
            "leveraged_funds": {"long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
            "dealer": {"long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0}}
    expected = {"report_date": DAY, "open_interest": 2_000_000.0, "rows": [
        {"group": "Asset Manager", "long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
        {"group": "Leveraged Funds", "long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
        {"group": "Dealer", "long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0},
    ]}

    assert session == DAY_DATE
    assert normalize_cftc(stored) == expected
    assert normalize_cftc(live) == expected


# ── 7. stored first, live only when the store has nothing ───────────────────────────────────────────────────────

def test_stored_rows_mean_no_vix_and_no_cot_provider_call(rawstore_tmp_db, tmp_path):
    _seed_vol()
    _seed_cot()
    calls = []

    raw = _fetch(tmp_path, vix_fetcher=_spies(calls)[0], cftc_fetcher=_spies(calls)[1])

    assert calls == []
    assert raw["vix"]["tickers"]["^VIX"] == 15.5
    assert raw["vix"]["term_mode"] == "contango"
    assert raw["cftc"]["report_date"] == DAY
    sources = _manifest(tmp_path)["input_sources"]
    assert sources["vix"] == {"source": "store", "session": DAY}
    assert sources["cftc"] == {"source": "store", "session": DAY}


def test_empty_tables_make_the_live_fetchers_run(rawstore_tmp_db, tmp_path):
    calls = []

    raw = _fetch(tmp_path, vix_fetcher=_spies(calls)[0], cftc_fetcher=_spies(calls)[1])

    assert calls == ["vix", "cftc"]
    assert raw["vix"]["tickers"]["^VIX"] == 1.0                    # the injected live payload
    assert raw["cftc"]["report_date"] == "2026-08-11"
    sources = _manifest(tmp_path)["input_sources"]
    assert sources == {"vix": {"source": "live"}, "cftc": {"source": "live"}}


def test_a_stale_series_falls_back_to_live(rawstore_tmp_db, tmp_path):
    """A stopped nightly must not make the board show an old VIX as current."""
    _seed_vol(date(2026, 5, 1))
    _seed_cot(date(2026, 5, 1))
    calls = []

    _fetch(tmp_path, vix_fetcher=_spies(calls)[0], cftc_fetcher=_spies(calls)[1])

    assert calls == ["vix", "cftc"]


def test_the_stored_reads_add_no_request_to_the_manifest(rawstore_tmp_db, tmp_path):
    _seed_vol()
    _seed_cot()
    calls = []

    _fetch(tmp_path, vix_fetcher=_spies(calls)[0], cftc_fetcher=_spies(calls)[1])
    stored_requests = _manifest(tmp_path)["requests"]

    (tmp_path / "live").mkdir()
    _fetch(tmp_path / "live", vix_fetcher=_spies(calls)[0], cftc_fetcher=_spies(calls)[1])
    live_requests = _manifest(tmp_path / "live")["requests"]

    assert stored_requests == live_requests


# ── 8. data safety: the stored path leaves the process untouched ─────────────────────────────────────────────────

def test_the_stored_path_makes_no_network_call_at_all(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed_vol()
    _seed_cot()
    _no_network(monkeypatch)                                       # httpx, the live fetchers and UW all raise

    raw = _fetch(tmp_path, vix_fetcher=snap.fetch_vix_family, cftc_fetcher=snap.fetch_cftc_tff)

    assert raw["vix"]["tickers"]["^VIX"] == 15.5
    assert raw["cftc"]["open_interest"] == 2_000_000.0
    assert not any(w.startswith(("vix_family", "cftc_tff")) for w in _manifest(tmp_path)["warnings"])


def test_a_broken_store_still_degrades_to_live(rawstore_tmp_db, tmp_path, monkeypatch):
    """The readers swallow a store error; the snapshot must not notice it."""
    def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rawstore, "query", locked)
    calls = []

    raw = _fetch(tmp_path, vix_fetcher=_spies(calls)[0], cftc_fetcher=_spies(calls)[1])

    assert calls == ["vix", "cftc"]
    assert raw["vix"]["tickers"]["^VIX"] == 1.0


# ── review #103 S-1: no session to anchor on means live, never "the newest stored" ───────────────────────────────

def test_an_unanchored_run_goes_live_instead_of_reading_the_newest_session(rawstore_tmp_db, tmp_path,
                                                                          monkeypatch):
    """An ``asof`` that is not a date used to fall through to the readers' own default, which is the newest stored
    session — i.e. today's closes stamped onto a day that is not today. It must ask live instead."""
    _seed_vol()
    _seed_cot()
    reads = []
    monkeypatch.setattr(stored_inputs, "vol_index_rows",
                        lambda *a, **k: reads.append("vix") or ({"tickers": {"^VIX": 99.0}}, DAY_DATE))
    monkeypatch.setattr(stored_inputs, "cot_row",
                        lambda *a, **k: reads.append("cftc") or ({"report_date": "1999-01-01"}, DAY_DATE))
    calls = []
    vix_live, cftc_live = _spies(calls)

    raw = snap.fetch_snapshot(FakeUW(_handler, prefix=""), "not-a-date", data_root=tmp_path,
                              vix_fetcher=vix_live, cftc_fetcher=cftc_live,
                              candle_fetcher=lambda *a, **k: list(CANDLES))

    assert reads == []                                   # neither reader was consulted
    assert calls == ["vix", "cftc"]
    assert raw["vix"]["tickers"]["^VIX"] == 1.0          # the live payload, not the stored 99.0


# ── review #103 check 2: the panel renders report_date raw, so both sources must spell it the same ───────────────

def test_the_cot_report_date_reads_the_same_whichever_source_served_it():
    """Socrata serves ``2026-10-06T00:00:00.000`` and the stored row ``2026-10-06``; the panel prints this string
    as it is (``section_position_map.html.j2``), so the normalizer settles on the plain day."""
    live = normalize_cftc({"report_date": "2026-10-06T00:00:00.000", "open_interest": 1.0})
    stored = normalize_cftc({"report_date": "2026-10-06", "open_interest": 1.0})
    assert live["report_date"] == stored["report_date"] == "2026-10-06"
    assert normalize_cftc({"report_date": None, "open_interest": 1.0})["report_date"] is None

"""``fetch_snapshot(history_only=True)``: the dated-only fetch the 2-year history pull runs (D1, 25 Oct 2026).

Every test drives the snapshot against a fake UW client and asserts on the requests it saw, so a call that slips
into history mode (an undated endpoint, the option chain, the previous-day strike call, an all-expiry ladder) fails
here with its path in the message. No network: the client is a fake and the candles are a fake store read.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_spx_gamma import run as spx_run
from RevenueChaser.module_spx_gamma import snapshot as snap
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
from RevenueChaser.module_spx_gamma.raw_day import load_raw_day
from RevenueChaser.module_spx_gamma.snapshot import UWApiError

from .conftest import FakeUW

DAY = "2025-03-14"          # a Friday session inside the 2-year window
CLOSE = 5638.94


def _rows(asof=None, ticker=None):
    """A stored ^GSPC window ending on DAY (the close of DAY is the history spot)."""
    return [{"date": "2025-03-12", "open": 1.0, "high": 2.0, "low": 0.5, "close": 5599.3, "volume": 1.0},
            {"date": "2025-03-13", "open": 1.0, "high": 2.0, "low": 0.5, "close": 5521.52, "volume": 1.0},
            {"date": DAY, "open": 1.0, "high": 2.0, "low": 0.5, "close": CLOSE, "volume": 1.0}]


def _handler(failures=None):
    failures = failures or {}

    def handle(path, params):
        for suffix, status in failures.items():
            if path.endswith(suffix):
                raise UWApiError(path, status, f"HTTP {status}")
        if path.endswith("/gex-levels"):
            return {"data": {"call_wall": "5700", "gamma_flip": "5600", "gamma_magnet": "5650", "put_wall": "5500"}}
        if path.endswith("/greek-exposure/expiry"):
            return {"data": [{"expiry": DAY, "dte": 0, "call_gex": "1", "put_gex": "-1"},
                             {"expiry": "2025-03-21", "dte": 7, "call_gex": "1", "put_gex": "-1"}]}
        if path.endswith("/spot-exposures/strike"):
            return {"data": [{"strike": str(params["min_strike"]), "call_gamma_oi": "1"}]}
        if path.endswith("/flow-per-strike"):
            return []
        return {"data": [{"date": params.get("date"), "value": "1"}]}
    return handle


def _fetch(tmp_path, uw, **kw):
    return snap.fetch_snapshot(uw, DAY, data_root=tmp_path, history_only=True, candle_fetcher=_rows,
                               vix_fetcher=_no_live("vix"), cftc_fetcher=_no_live("cftc"), **kw)


def _no_live(what):
    def boom(*a, **k):
        raise AssertionError(f"history mode asked the live {what} source")
    return boom


@pytest.fixture(autouse=True)
def _no_stored_inputs(monkeypatch):
    """No VIX / COT stored for the day: history mode must then leave them out, never go live."""
    monkeypatch.setattr(snap.stored_inputs, "vol_index_rows", lambda day=None: ({}, None))
    monkeypatch.setattr(snap.stored_inputs, "cot_row", lambda day=None: ({}, None))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def uw(calls):
    fake = FakeUW(_handler())
    real = fake.get_result

    def get_result(ep, params=None):
        calls.append((ep, dict(params or {})))
        return real(ep, params)
    fake.get_result = get_result
    return fake


EXPECTED = [
    ("/stock/SPX/gex-levels", {"date": DAY, "source": "oi"}),
    ("/stock/SPX/gex-levels", {"date": DAY, "source": "vol"}),
    ("/stock/SPX/greek-exposure/expiry", {"date": DAY}),
    ("/stock/SPX/greek-exposure/strike", {"date": DAY}),
    ("/stock/SPX/interpolated-iv", {"date": DAY}),
    ("/stock/SPX/max-pain", {"date": DAY}),
    ("/stock/SPX/volatility/term-structure", {"date": DAY}),
    ("/stock/SPX/volatility/variance-risk-premium", {"date": DAY}),
    ("/stock/SPX/volatility/realized", {"date": DAY}),
    ("/stock/SPX/flow-per-strike", {"date": DAY}),
    ("/stock/SPX/spot-exposures/strike", {"date": DAY, "min_strike": int(CLOSE * 0.75), "max_strike": int(CLOSE),
                                          "limit": snap.SPOT_EXPOSURE_LIMIT}),
    ("/stock/SPX/spot-exposures/strike", {"date": DAY, "min_strike": int(CLOSE) + 1,
                                          "max_strike": int(CLOSE * 1.25), "limit": snap.SPOT_EXPOSURE_LIMIT}),
    ("/stock/SPX/greek-exposure/strike-expiry", {"date": DAY, "expiry": DAY}),
]


def test_history_mode_makes_only_the_dated_calls(tmp_path, uw, calls):
    _fetch(tmp_path, uw)
    assert calls == EXPECTED
    assert len(calls) == snap.HISTORY_CALLS_PER_SESSION == 13
    # every call carries the day asked for: nothing undated, nothing for another session
    assert all(p.get("date") == DAY for _ep, p in calls)


def test_history_mode_spot_is_the_stored_close_and_live_spot_is_never_called(tmp_path, uw, calls, monkeypatch):
    from RevenueChaser.core import calendar as nyse_calendar
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: True)   # even mid-session
    monkeypatch.setattr(snap, "live_spot", _no_live("spot"))
    raw = _fetch(tmp_path, uw)
    assert raw["spot"]["value"] == CLOSE
    assert "index_levels_daily" in raw["spot"]["source"]
    manifest = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["spot"]["value"] == CLOSE
    assert manifest["spot_exposure_search_bounds"] == [int(CLOSE * 0.75), int(CLOSE * 1.25)]


def test_history_mode_writes_no_live_file_and_marks_its_origin(tmp_path, uw):
    _fetch(tmp_path, uw)
    day_dir = tmp_path / DAY
    manifest = json.loads((day_dir / "manifest.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in day_dir.glob("*.json")} - {"manifest.json"}
    # The Fetch's LIVE_FILES less the five volatility files, which history mode asks WITH the day's date.
    dated_here = {snap._FILENAMES[n] for n in snap.HISTORY_ENDPOINTS}
    undated = snap.LIVE_FILES - dated_here
    assert undated == {"option_chain.json", "iv_rank.json", "options_volume.json", "vix_family.json",
                       "cftc_tff.json"}
    assert not on_disk & undated, f"live files written into a past day: {sorted(on_disk & undated)}"
    assert not any(n.startswith("strike_expiry_") for n in on_disk), "history mode fetched the all-expiry ladder"
    assert "greek_exposure_strike_prev.json" not in on_disk
    assert all(meta["as_of"] for meta in manifest["files"].values())
    assert manifest["origin"] == snap.HISTORY_ORIGIN == "history_pull"
    # the board's "n/a (historical)" marking reads the manifest: nothing in a history day is live
    assert spx_run._historical_live_files(day_dir) == []


def test_history_day_reads_back_and_normalizes(tmp_path, uw):
    _fetch(tmp_path, uw)
    raw = load_raw_day(tmp_path / DAY)
    assert raw["spot"]["value"] == CLOSE
    snapshot = normalize_snapshot(raw, DAY)
    assert snapshot["current_level"] == CLOSE


def test_no_stored_close_for_the_day_costs_no_request(tmp_path, uw, calls):
    def window_without_the_day(asof=None, ticker=None):
        return _rows()[:-1]
    with pytest.raises(snap.NoStoredClose):
        snap.fetch_snapshot(uw, DAY, data_root=tmp_path, history_only=True, candle_fetcher=window_without_the_day)
    assert calls == []
    assert not (tmp_path / DAY).exists()


def test_history_mode_does_not_fall_back_to_the_previous_session(tmp_path, calls):
    fake = FakeUW(_handler({"/gex-levels": 403}))
    real = fake.get_result
    fake.get_result = lambda ep, params=None: (calls.append((ep, dict(params or {}))), real(ep, params))[1]
    with pytest.raises(snap.SnapshotError):
        _fetch(tmp_path, fake)
    assert calls == [EXPECTED[0]], "a rejected past day must cost one call and never move to another session"
    assert not (tmp_path / DAY).exists()


@pytest.mark.parametrize("status", [429, 500, 0])
def test_a_transient_failure_leaves_no_day_behind(tmp_path, status):
    """A day with a 429/5xx/no-response hole would read as complete and never be pulled again."""
    with pytest.raises(snap.SnapshotError, match="max_pain"):
        _fetch(tmp_path, FakeUW(_handler({"/max-pain": status})))
    assert not (tmp_path / DAY).exists()
    assert not list(tmp_path.glob(".partial-*"))


def test_a_permanent_rejection_of_an_optional_endpoint_keeps_the_day(tmp_path):
    """A 422/404 is the same answer tomorrow: keep the day with its warning rather than pay for it again."""
    _fetch(tmp_path, FakeUW(_handler({"/variance-risk-premium": 422})))
    manifest = json.loads((tmp_path / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert any("variance_risk_premium" in w for w in manifest["warnings"])


def test_history_mode_honours_overwrite_false(tmp_path, uw, calls):
    day_dir = tmp_path / DAY
    day_dir.mkdir()
    (day_dir / "manifest.json").write_text('{"origin": "fetch"}', encoding="utf-8")
    assert _fetch(tmp_path, uw, overwrite=False) is None
    assert calls == []
    assert json.loads((day_dir / "manifest.json").read_text(encoding="utf-8")) == {"origin": "fetch"}


def test_history_mode_never_replaces_a_complete_day_that_appeared_meanwhile(tmp_path, monkeypatch):
    """The promote re-checks: a day a Fetch completed while this one was staging is kept as it is."""
    day_dir = tmp_path / DAY

    def handler(path, params):
        if path.endswith("/greek-exposure/strike-expiry") and not day_dir.exists():
            day_dir.mkdir()
            (day_dir / "manifest.json").write_text('{"origin": "fetch"}', encoding="utf-8")
        return _handler()(path, params)
    assert _fetch(tmp_path, FakeUW(handler)) is None
    assert json.loads((day_dir / "manifest.json").read_text(encoding="utf-8")) == {"origin": "fetch"}
    assert not list(tmp_path.glob(".partial-*"))


def test_history_mode_refuses_the_all_expiry_ladder(tmp_path, uw, calls):
    with pytest.raises(ValueError):
        _fetch(tmp_path, uw, fetch_all_expiries=True)
    assert calls == []


def test_the_fetch_path_keeps_its_endpoints_and_live_files():
    """History mode has its own dated table; the board's Fetch requests and LIVE_FILES are unchanged."""
    assert "date" not in snap.BASE_ENDPOINTS["interpolated_iv"][1]
    assert {"interpolated_iv.json", "max_pain.json", "iv_rank.json", "options_volume.json", "option_chain.json",
            "vol_term_structure.json", "variance_risk_premium.json", "realized_vol.json"} <= snap.LIVE_FILES


def test_two_threads_staging_the_same_day_never_mix_their_files(tmp_path):
    """A board Fetch's backfill and the pull can stage one day at once in one process (review MUST, #194).

    Both threads stop at a barrier inside their first request, so both staging folders exist at the same time.
    Whichever promotes, the day on disk must hold exactly the files its own manifest lists.
    """
    import threading

    barrier = threading.Barrier(2, timeout=5)
    seen_first: set[str] = set()
    lock = threading.Lock()

    def handler(path, params):
        name = threading.current_thread().name
        with lock:
            first = name not in seen_first
            seen_first.add(name)
        if first:
            barrier.wait()
        if path.endswith("/option-contracts"):
            return {"data": [{"option_symbol": "SPXW", "nbbo_bid": "1"}]}
        return _handler()(path, params)

    errors: list[BaseException] = []

    def run(**kw):
        try:
            snap.fetch_snapshot(FakeUW(handler), DAY, data_root=tmp_path, candle_fetcher=_rows,
                                vix_fetcher=lambda: {"tickers": {}}, cftc_fetcher=lambda: {}, **kw)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    fetch = threading.Thread(target=run, name="fetch", kwargs={"fetch_all_expiries": True, "overwrite": False})
    pull = threading.Thread(target=run, name="pull", kwargs={"history_only": True, "overwrite": False})
    fetch.start(); pull.start(); fetch.join(10); pull.join(10)
    assert errors == []
    day_dir = tmp_path / DAY
    manifest = json.loads((day_dir / "manifest.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in day_dir.glob("*.json")} - {"manifest.json"}
    assert on_disk == set(manifest["files"]), f"files outside the manifest: {sorted(on_disk - set(manifest['files']))}"
    assert not list(tmp_path.glob(".partial-*"))

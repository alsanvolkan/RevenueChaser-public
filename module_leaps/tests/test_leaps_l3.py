"""L3 (#377): the LEAPS label is classified from ``leaps_footprint_history``, not the cache list.

Red-first for the four L3 behaviours (O8, O9, O10, Q5):

1. the quarter baseline is the last recorded session at or before quarter end (O8);
2. growth is matched-expiry far-call OI growth over the expiries present in both snapshots, far being decided at
   the BASELINE date, so an expiry crossing the 270-day line or a new listing is not growth (O9);
3. an all-bid flow (ask share below ``ASK_SHARE_MIN``) is not "Accumulating" (O10);
4. the board says the signal is exploratory (Q5).

``leaps_cache.latest`` stays (Earnings reads it); its ``history[]`` list is frozen: carried unchanged, never
appended, never deleted (D1).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_leaps import engine
from RevenueChaser.module_leaps import history as leaps_history
from RevenueChaser.module_leaps.scan import scan_leaps, scan_leaps_cached
from RevenueChaser.module_leaps.tests.options_store import pin, print_row, store_footprint

BOARD, TABLE = "module_leaps", "leaps_footprint_history"
TODAY = date(2026, 10, 5)                       # a Monday session
FAR = "2027-12-17"                              # > 270 days after TODAY
# The nine keys Earnings reads off ``leaps_cache.latest`` (pipeline._score, C26).
EARNINGS_KEYS = ("label", "as_of", "score", "far_call_oi", "far_put_oi", "far_call_share",
                 "far_oi_growth_1d_pct", "far_oi_growth_q_pct", "iv_rank")


# ── the seed helper: a real recorded session, as a Fetch writes one ───────────────────────────────────────────────

def run_at_of(day: date) -> float:
    """18:00 ET on ``day`` — a complete New York session (rule B)."""
    return datetime(day.year, day.month, day.day, 22, 0, tzinfo=timezone.utc).timestamp()


def _per(value, ticker):
    return value.get(ticker) if isinstance(value, dict) else value


def seed_session(as_of: date, oi_by_ticker: dict, *, label="Quiet", ask_share=None, run_at=None):
    """Record one complete LEAPS session (a real ``leaps_footprint_history`` run) for every ticker.

    ``oi_by_ticker`` maps ticker -> the OI-per-expiry rows the footprint was built from. The row carries them in
    ``inputs``, which is where the classifier reads its baselines from (O9).
    """
    rows = []
    for ticker, oi in oi_by_ticker.items():
        totals = engine.far_oi_totals(oi, as_of)
        calls, puts = totals["far_call_oi"], totals["far_put_oi"]
        snap = {
            "ticker": ticker, "name": f"{ticker} Inc", "as_of": as_of.isoformat(),
            "label": _per(label, ticker), "score": 10,
            "far_call_oi": calls, "far_put_oi": puts,
            "far_call_share": round(calls / (calls + puts), 4) if calls + puts else None,
            "near_term_event": False, "ask_share": _per(ask_share, ticker),
        }
        rows.append({"ticker": ticker, "name": snap["name"], "snapshot": snap,
                     "inputs": {"oi_per_expiry": oi, "flow_per_expiry": [], "iv_rank": []},
                     "fetch_status": "ok"})
    return leaps_history.record(rows, run_id=f"s{as_of.isoformat()}", run_at=run_at or run_at_of(as_of))


class _UW:
    """A UW double: far-dated OI per ticker; every read answers ``ok``."""
    auth_error = None

    def __init__(self, oi: dict):
        self.oi = oi
        self.calls = self.failed = 0

    def health(self):
        return {"calls": self.calls, "failed": self.failed, "auth_error": None, "failures": [],
                "breaker_open": False, "skipped_calls": 0}

    def option_trades(self, **kw):
        return []

    def screener_batch(self, tickers):
        return {t: {"full_name": f"{t} Inc"} for t in tickers}

    def oi_per_expiry(self, ticker, date=None):
        self.calls += 1
        call, put = self.oi.get(ticker, (0, 0))
        return [{"expiry": FAR, "call_oi": call, "put_oi": put}]

    def flow_per_expiry(self, ticker):
        self.calls += 1
        return []

    def iv_rank(self, ticker):
        self.calls += 1
        return [{"iv_rank_1y": "55.0"}]

    def oi_per_expiry_result(self, ticker, date=None):
        return ProviderResult("ok", self.oi_per_expiry(ticker, date))

    def flow_per_expiry_result(self, ticker):
        return ProviderResult("ok", self.flow_per_expiry(ticker))

    def iv_rank_result(self, ticker):
        return ProviderResult("ok", self.iv_rank(ticker))


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.text, self.headers = status, payload or {"data": []}, "", {}

    def json(self):
        return self._payload


def _uw_404(ticker, call=1000):
    """The real client, offline: ``ticker``'s oi-per-expiry answers 404, every other read answers."""
    from RevenueChaser.core.providers.unusual_whales import UWClient
    c = UWClient(key="k", sleep=lambda s: None)

    def _get(url, params=None):
        if f"/stock/{ticker}/oi-per-expiry" in url:
            return _Resp(404, {"message": "not found"})
        if "/oi-per-expiry" in url:
            return _Resp(200, {"data": [{"expiry": FAR, "call_oi": call, "put_oi": 100}]})
        return _Resp(200)

    c._client.get = _get  # type: ignore[method-assign]
    return c


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


def _oi(call, put=50, expiry=FAR):
    return [{"expiry": expiry, "call_oi": call, "put_oi": put}]


# ── 1. an expiry crossing the far line is not growth ──────────────────────────────────────────────────────────────

def test_an_expiry_crossing_the_far_line_is_not_growth():
    # 2027-03-27 is exactly 270 days after the 2026-06-30 baseline and 269 days before the 2027-03-28 snapshot.
    base_oi = _oi(500, expiry="2027-03-27")
    cur_oi = _oi(500, expiry="2027-03-27")
    assert engine.matched_far_growth(cur_oi, base_oi, "2026-06-30") == 0.0
    snap = {"far_call_oi": 500, "far_put_oi": 50, "far_call_share": 0.9091, "near_term_event": False}
    base = {"as_of": "2026-06-30", "oi_per_expiry": base_oi, "far_call_oi": 500}
    assert engine.classify(snap, base, None, cur_oi=cur_oi)["label"] == "Quiet"


def test_an_expiry_just_inside_the_far_line_is_left_out():
    # 2027-03-26 is 269 days from the baseline: near-dated there, so its OI doubling is not far growth.
    base_oi = [{"expiry": "2027-03-27", "call_oi": 500, "put_oi": 50},
               {"expiry": "2027-03-26", "call_oi": 100, "put_oi": 10}]
    cur_oi = [{"expiry": "2027-03-27", "call_oi": 500, "put_oi": 50},
              {"expiry": "2027-03-26", "call_oi": 200, "put_oi": 10}]
    assert engine.matched_far_growth(cur_oi, base_oi, "2026-06-30") == 0.0


# ── 2. matched expiries ──────────────────────────────────────────────────────────────────────────────────────────

def test_a_new_listing_is_not_growth():
    base_oi = _oi(1000, expiry="2027-06-18")
    cur_oi = _oi(1000, expiry="2027-06-18") + _oi(900, expiry="2028-01-21")   # a new far expiry appears
    assert engine.matched_far_growth(cur_oi, base_oi, "2026-06-30") == 0.0
    snap = {"far_call_oi": 1900, "far_put_oi": 100, "far_call_share": 0.95, "near_term_event": False}
    base = {"as_of": "2026-06-30", "oi_per_expiry": base_oi, "far_call_oi": 1000}
    assert engine.classify(snap, base, None, cur_oi=cur_oi)["label"] == "Quiet"


def test_no_matched_expiry_gives_none():
    base_oi = _oi(500, expiry="2027-06-18")
    cur_oi = _oi(900, expiry="2027-12-17")          # a different expiry on each side: nothing to compare
    assert engine.matched_far_growth(cur_oi, base_oi, "2026-06-30") is None


def test_an_empty_history_gives_none_never_zero():
    assert engine.matched_far_growth(_oi(900), [], "2026-06-30") is None
    assert engine.matched_far_growth(_oi(900), _oi(0), "2026-06-30") is None      # a 0 baseline is no growth


def test_the_fetch_reads_history_not_the_frozen_cache_list(store):
    # A frozen cache list holding a far-call OI of 400 (the old baseline source) is not read: no recorded
    # session, no baseline.
    pin("AAA")
    store_footprint("AAA", TODAY, {date.fromisoformat(FAR): (1000, 200)})
    frozen = [{"ticker": "AAA", "far_call_oi": 400, "as_of": "2026-10-02"}]
    rawstore.write_rows("leaps_cache", [{"ticker": "AAA", "payload": {
        "latest": {"ticker": "AAA", "far_call_oi": 400, "as_of": "2026-10-02"}, "history": frozen}}])
    watched, _ = scan_leaps(None, today=TODAY)
    (aaa,) = watched
    assert aaa["far_oi_growth_1d_pct"] is None and aaa["label"] == "Quiet"
    # With a recorded session the same Fetch grows from it, not from the cache list.
    seed_session(date(2026, 10, 2), {"AAA": _oi(400)})
    footprints: list = []
    watched, _ = scan_leaps(None, today=TODAY, footprints=footprints)
    assert footprints[0]["inputs"]["baseline_1d"]["as_of"] == "2026-10-02"
    assert watched[0]["far_oi_growth_1d_pct"] == 150.0


# ── 3. the quarter baseline ──────────────────────────────────────────────────────────────────────────────────────

def test_quarter_baseline_is_the_latest_row_at_or_before_quarter_end(store):
    seed_session(date(2026, 6, 29), {"AAA": _oi(400)})
    seed_session(date(2026, 7, 2), {"AAA": _oi(900)})
    base = leaps_history.baselines(["AAA"], date(2026, 8, 10))["AAA"]
    assert base["q"]["as_of"] == "2026-06-29"          # 07-02 is after the 06-30 quarter end
    assert base["1d"]["as_of"] == "2026-07-02"


def test_a_same_session_row_is_not_its_own_baseline(store):
    seed_session(date(2026, 10, 2), {"AAA": _oi(400)})
    seed_session(TODAY, {"AAA": _oi(1000)})
    base = leaps_history.baselines(["AAA"], TODAY)["AAA"]
    assert base["newest"]["as_of"] == "2026-10-05"
    assert base["1d"]["as_of"] == "2026-10-02"
    # Today's row is the only one recorded: there is no baseline yet, not itself.
    assert leaps_history.baselines(["AAA"], date(2026, 10, 2))["AAA"]["1d"] is None


def test_history_dates_compare_as_iso_strings(store):
    # ``as_of`` is a DATE column: DuckDB hands back a ``date``, and the quarter end is an ISO string.
    seed_session(date(2026, 6, 29), {"AAA": _oi(300)})
    seed_session(date(2026, 6, 30), {"AAA": _oi(400)})
    base = leaps_history.baselines(["AAA"], date(2026, 8, 10))["AAA"]
    assert base["q"]["as_of"] == "2026-06-30"          # the quarter-end row itself counts
    assert base["1d"]["as_of"] == "2026-06-30"
    assert all(isinstance(r["as_of"], str) for r in base["rows"])


# ── 4. the ask-share gate ────────────────────────────────────────────────────────────────────────────────────────

def _snap(share=0.9):
    return {"far_call_oi": 1000, "far_put_oi": 50, "far_call_share": share, "near_term_event": False}


def _cand(share=0.8, ask_share=0.2):
    return {"ticker": "ABC", "final_score": 80.0, "frequency": 4, "notional": 4e5, "net_notional": 1e5,
            "concentration": 0.6, "itm_conc": 0.6, "otm_conc": 0.1, "concentration_mult": 1.2,
            "call_share": share, "ask_share": ask_share, "near_event": False}


def test_an_all_bid_flow_is_not_accumulating():
    out = engine.classify(_snap(), {"far_call_oi": 700}, None, ask_share=0.2)
    assert out["far_oi_growth_1d_pct"] > 0            # the OI did grow
    assert out["label"] == "Quiet"                    # but the prints were all bid
    assert engine.radar_snapshot(_cand(ask_share=0.2))["label"] == "Quiet"


def test_no_prints_is_not_gated():
    assert engine.classify(_snap(), {"far_call_oi": 700}, None, ask_share=None)["label"] == "Accumulating"
    assert engine.radar_snapshot(_cand(ask_share=None))["label"] == "Accumulating"


def test_high_ask_share_stays_accumulating():
    assert engine.classify(_snap(), {"far_call_oi": 700}, None, ask_share=0.8)["label"] == "Accumulating"
    assert engine.radar_snapshot(_cand(ask_share=0.8))["label"] == "Accumulating"


def test_the_scan_passes_the_flows_ask_share_to_the_classifier(store):
    # A seeded baseline says the footprint grew; the scan's only print is all bid, so it is not Accumulating.
    pin("AAA")
    seed_session(date(2026, 10, 2), {"AAA": _oi(400)})
    store_footprint("AAA", TODAY, {date.fromisoformat(FAR): (1000, 100)},
                    prints=[print_row("AAA", "b1", TODAY, date(2027, 6, 18), premium=200_000.0, ask_vol=0,
                                      bid_vol=10)])
    watched, _ = scan_leaps(None, today=TODAY)
    (aaa,) = watched
    assert aaa["far_oi_growth_1d_pct"] == 150.0        # the footprint grew from the recorded session
    assert aaa["ask_share"] == 0.0 and aaa["label"] == "Quiet"


# ── 5. a failed read leaves the recorded baseline alone ──────────────────────────────────────────────────────────

def test_a_failed_read_leaves_the_baseline_alone(store):
    rawstore.save_watchlist({"AAA"})
    seed_session(date(2026, 10, 2), {"AAA": _oi(400)})
    failed = engine.fetch_and_classify("AAA", _uw_404("AAA"), today=TODAY, inputs={})
    assert failed["label"] == "N/A"
    inputs: dict = {}
    snap = engine.fetch_and_classify("AAA", _UW({"AAA": (1000, 100)}), today=TODAY, inputs=inputs)
    assert inputs["baseline_1d"]["as_of"] == "2026-10-02"
    assert inputs["baseline_1d"]["far_call_oi"] == 400
    assert snap["far_oi_growth_1d_pct"] == 150.0 and snap["label"] == "Accumulating"


# ── 6. an unreadable history is a warning, never a failed Fetch ──────────────────────────────────────────────────

def test_unreadable_history_gives_growth_none_and_a_warning(store, monkeypatch):
    def boom(*a, **k):
        raise rawstore.StoreLockedError("history.duckdb", 4242, "duckdb.exe", "C:/duckdb.exe", "held")

    monkeypatch.setattr(leaps_history, "baselines", boom)
    pin("AAA")
    store_footprint("AAA", TODAY, {date.fromisoformat(FAR): (1000, 200)})
    notes: list = []
    footprints: list = []
    watched, _ = scan_leaps(None, today=TODAY, warnings=notes, footprints=footprints)
    # One warning for the baseline read, never one per ticker (the scan's calendar warning may sit beside it, DF94).
    assert sum("baseline" in n.lower() for n in notes) == 1
    (aaa,) = watched
    (fp,) = footprints
    assert aaa["far_oi_growth_1d_pct"] is None and aaa["far_oi_growth_q_pct"] is None
    assert aaa["label"] == "Quiet"                    # not a footprint: no baseline, not growth
    assert fp["fetch_status"] == "ok"


def test_baseline_read_is_batched(store, monkeypatch):
    tickers = [f"T{i:03d}" for i in range(100)]
    calls = []
    real = rawstore.query

    def counted(sql, params=None, **kw):
        if kw.get("db") == "history":
            calls.append(sql)
        return real(sql, params, **kw)

    monkeypatch.setattr(rawstore, "query", counted)
    leaps_history.baselines(tickers, TODAY)
    assert len(calls) <= 3                            # the sessions, their run count and one rows read


# ── 7. the cache payload: latest kept, history frozen ────────────────────────────────────────────────────────────

def test_latest_keeps_label_and_as_of(store):
    old = [{"ticker": "AAA", "label": "Quiet", "as_of": "2026-09-30"},
           {"ticker": "AAA", "label": "Accumulating", "as_of": "2026-10-01"}]
    rawstore.save_watchlist({"AAA"})
    rawstore.write_rows("leaps_cache", [{"ticker": "AAA", "payload": {
        "latest": {"ticker": "AAA", "label": "Quiet", "as_of": "2026-10-01"}, "history": old}}])
    engine.fetch_and_classify("AAA", _UW({"AAA": (1000, 200)}), today=TODAY, inputs={})
    engine.fetch_and_classify("AAA", _UW({"AAA": (1200, 200)}), today=TODAY, inputs={})
    data = engine.load_cache("AAA")
    latest = data["latest"]
    assert latest["label"] in engine.LABELS and latest["as_of"] == TODAY.isoformat()
    for key in EARNINGS_KEYS:
        assert key in latest, f"Earnings reads {key}"
    assert "inputs" not in latest and "oi_per_expiry" not in latest
    assert data["history"] == old                     # carried unchanged: never appended, never deleted (D1)


# ── 8. Refresh: the far line is decided at the stored session's date, not the Refresh date (D4) ─────────────────

def test_refresh_rederives_with_the_rows_own_date(store):
    # The store's newest session is 2026-09-15: at that date 2027-06-18 is 276 DTE (far), at the 2026-11-20
    # Refresh date it is 210 (near). Deriving at the session's own date finds the growth; any other date does not.
    expiry = date(2027, 6, 18)
    seed_session(date(2026, 6, 30), {"AAA": _oi(500, expiry=expiry.isoformat())})
    pin("AAA")
    store_footprint("AAA", date(2026, 9, 15), {expiry: (1000, 50)})
    watched, _ = scan_leaps_cached(today=date(2026, 11, 20))
    (aaa,) = watched
    assert aaa["as_of"] == "2026-09-15"
    assert aaa["far_oi_growth_1d_pct"] == 100.0 and aaa["label"] == "Accumulating"


# ── 9. the board says the signal is exploratory (Q5) ─────────────────────────────────────────────────────────────

def test_the_board_says_exploratory(tmp_path, monkeypatch):
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    snap = {"ticker": "ABC", "label": "Quiet", "score": 5, "frequency": 0, "net_notional": 0.0}
    html = ld.render_leaps_dashboard([snap], []).read_text(encoding="utf-8")
    header = html[html.index('class="header"'):html.index('class="controls"')]
    assert "exploratory" in header.lower()
    help_box = html[html.index('<details class="help">'):html.index('id="tier-watchlist"')]
    assert "exploratory" in help_box.lower()
    legend = html[html.index('<div class="legend">'):html.index("<footer>")]
    assert "exploratory" in legend.lower()
    assert "ask" in legend.lower() and "bid" in legend.lower()

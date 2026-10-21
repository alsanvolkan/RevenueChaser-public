"""L2 (#377): every pinned name is visible, the 7-day event guard fires, the unwatched cap is stated.

Red-first for the three L2 behaviours:
1. a pinned (watchlist) name labelled Quiet is shown on the watchlist table, on the Refresh (cached) and the
   Fetch (live) path, and the board carries the label filter chips (default all on);
2. a far-dated print whose ticker reports earnings within 7 days makes that footprint ``near_term_event`` and Quiet;
3. the board states the 80-name unwatched cap in one sentence.
"""
from datetime import date, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_leaps import engine
from RevenueChaser.module_leaps.scan import scan_leaps, scan_leaps_cached
from RevenueChaser.module_leaps.tests.test_leaps_l3 import seed_session

TODAY = date(2026, 10, 6)
FAR = "2027-12-17"                       # > 270 days after TODAY


class _UW:
    """A UW double: far-dated OI per ticker, one tape page, names from the screener."""
    auth_error = None

    def __init__(self, oi, tape=()):
        self.oi, self.tape = oi, list(tape)
        self.calls = self.failed = 0

    def health(self):
        return {"calls": self.calls, "failed": self.failed, "auth_error": None, "failures": [],
                "breaker_open": False, "skipped_calls": 0}

    def option_trades(self, **kw):
        return list(self.tape)

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


def _print(pid, ticker, ned=None):
    return {"id": pid, "underlying_symbol": ticker, "strike": "200", "expiry": "2027-06-18",
            "premium": "200000", "size": 10, "option_type": "call", "delta": "0.8",
            "next_earnings_date": ned, "executed_at": f"{TODAY.isoformat()}T14:00:00Z",
            "ask_vol": 10, "bid_vol": 2, "tags": []}


def _render(tmp_path, monkeypatch, watched, unwatched, watchlist):
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    return ld.render_leaps_dashboard(watched, unwatched, watchlist=watchlist).read_text(encoding="utf-8")


def _watchlist_block(html: str) -> str:
    return html[html.index('id="tier-watchlist"'):html.index('id="tier-high"')]


# ── 1. every pinned name is shown ──

def test_a_quiet_pinned_name_is_shown_on_the_refresh_path(rawstore_tmp_db, tmp_path, monkeypatch):
    rawstore.save_watchlist({"MSFT"})
    seed_session(date(2026, 10, 5), {"MSFT": [{"expiry": FAR, "call_oi": 300, "put_oi": 300}]})
    engine.save_cache("MSFT", {"ticker": "MSFT", "label": "Quiet", "score": 5, "as_of": "2026-10-05"})
    watched, unwatched = scan_leaps_cached(today=TODAY)
    assert "MSFT" in [w["ticker"] for w in watched]
    html = _render(tmp_path, monkeypatch, watched, unwatched, {"MSFT"})
    assert '<div class="tkr">MSFT</div>' in _watchlist_block(html)


def test_a_quiet_pinned_name_is_shown_on_the_fetch_path(rawstore_tmp_db, tmp_path, monkeypatch):
    rawstore.save_watchlist({"BBB"})
    watched, unwatched = scan_leaps(_UW({"BBB": (300, 300)}), today=TODAY)
    assert [w["ticker"] for w in watched] == ["BBB"] and watched[0]["label"] == "Quiet"
    html = _render(tmp_path, monkeypatch, watched, unwatched, {"BBB"})
    assert '<div class="tkr">BBB</div>' in _watchlist_block(html)


def test_the_board_carries_a_label_filter_chip_per_label_default_on(tmp_path, monkeypatch):
    snap = {"ticker": "ABC", "label": "Quiet", "score": 5}
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    html = ld.render_leaps_dashboard([snap], []).read_text(encoding="utf-8")
    for lbl in ("Accumulating", "Distributing", "Quiet", "N/A"):
        assert f'<input type="checkbox" class="label-chip" data-label="{lbl}"' in html
    assert html.count('class="label-chip"') == 4
    assert html.count("checked") >= 4          # every chip is on by default


# ── 2. the event guard ──

def test_a_print_with_earnings_within_seven_days_is_quiet_and_tagged(rawstore_tmp_db):
    rawstore.save_watchlist({"AAA"})
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    ned = (TODAY + timedelta(days=3)).isoformat()
    watched, _ = scan_leaps(_UW({"AAA": (1000, 200)}, tape=[_print("q1", "AAA", ned)]), today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is True and aaa["label"] == "Quiet"


def test_a_print_with_earnings_beyond_seven_days_stays_accumulating(rawstore_tmp_db):
    rawstore.save_watchlist({"AAA"})
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    ned = (TODAY + timedelta(days=30)).isoformat()
    watched, _ = scan_leaps(_UW({"AAA": (1000, 200)}, tape=[_print("q1", "AAA", ned)]), today=TODAY)
    (aaa,) = watched
    assert aaa["near_term_event"] is False and aaa["label"] == "Accumulating"


# ── 3. the cap sentence ──

def test_the_board_states_the_unwatched_cap(tmp_path, monkeypatch):
    snap = {"ticker": "ABC", "label": "Quiet", "score": 5}
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    html = ld.render_leaps_dashboard([snap], []).read_text(encoding="utf-8")
    assert "at most 80 unwatched names" in html

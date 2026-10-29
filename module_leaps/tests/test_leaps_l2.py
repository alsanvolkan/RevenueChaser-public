"""L2 (#377): every pinned name is visible, the 7-day event guard fires, the unwatched cap is stated.

Red-first for the three L2 behaviours:
1. a pinned (watchlist) name labelled Quiet is shown on the watchlist table, on the Refresh (cached) and the
   Fetch (live) path, and the board carries the label filter chips (default all on);
2. a far-dated print whose ticker reports earnings within 7 days makes that footprint ``near_term_event`` and Quiet;
3. the board states the 80-name unwatched cap in one sentence.
"""
from datetime import date, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.module_leaps import engine
from RevenueChaser.module_leaps.scan import scan_leaps, scan_leaps_cached
from RevenueChaser.module_leaps.tests.options_store import pin, print_row, store_footprint
from RevenueChaser.module_leaps.tests.test_leaps_l3 import seed_session
from RevenueChaser.module_oipd.tests.options_store import write_options

TODAY = date(2026, 10, 6)
FAR = "2027-12-17"                       # > 270 days after TODAY


def _calendar(dates: dict):
    """The options store's screener set, the calendar the near-event flag reads (G9)."""
    write_options(option_screener_daily=[{"session_date": TODAY, "ticker": t, "report_date": d, "passes_gates": True,
                                          "source": "uw/option_screener"} for t, d in dates.items()])


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
    pin("BBB")
    store_footprint("BBB", TODAY, {date.fromisoformat(FAR): (300, 300)})
    watched, unwatched = scan_leaps(None, today=TODAY)
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

def _event_scan(days_to_report: int):
    """AAA pinned and on the option list, its far-dated call OI doubled since yesterday, one far-dated large print
    today, reporting ``days_to_report`` days out."""
    pin("AAA")
    seed_session(date(2026, 10, 5), {"AAA": [{"expiry": FAR, "call_oi": 500, "put_oi": 0}]})
    store_footprint("AAA", TODAY, {date.fromisoformat(FAR): (1000, 200)},
                    prints=[print_row("AAA", "q1", TODAY, date(2027, 6, 18))])
    _calendar({"AAA": TODAY + timedelta(days=days_to_report)})
    (aaa,), _ = scan_leaps(None, today=TODAY)
    assert aaa["frequency"] == 1
    return aaa


def test_a_print_with_earnings_within_seven_days_is_quiet_and_tagged(rawstore_tmp_db):
    aaa = _event_scan(3)
    assert aaa["near_term_event"] is True and aaa["label"] == "Quiet"


def test_a_print_with_earnings_beyond_seven_days_stays_accumulating(rawstore_tmp_db):
    aaa = _event_scan(30)
    assert aaa["near_term_event"] is False and aaa["label"] == "Accumulating"


# ── 3. the cap sentence ──

def test_the_board_states_the_unwatched_cap(tmp_path, monkeypatch):
    snap = {"ticker": "ABC", "label": "Quiet", "score": 5}
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    html = ld.render_leaps_dashboard([snap], []).read_text(encoding="utf-8")
    assert "at most 80 unwatched names" in html

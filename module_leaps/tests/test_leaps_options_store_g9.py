"""G9 (build plan PR 19): LEAPS reads the options store, for SPX and the option watch list only.

* a pinned name on the option watch list gets its footprint from the store's newest session: the far-dated OI per
  expiry from ``option_eod``, the ask-side premium per expiry from ``option_expiry``, the IV rank from
  ``option_daily``; the session dates it;
* a pinned name off the option watch list shows "not on the option watch list" and gets no footprint score, even
  when the store holds rows for it;
* the large-print scan runs over the list names' stored prints (``option_print``, set ``list``) only: far-dated
  (``MIN_DTE``) and large (``MIN_PREMIUM``), not cancelled; the earnings set's prints are not read;
* a Fetch and a Refresh draw the same board and call no provider; the Fetch records the run and queues
  ``uw/option_list`` once it has had its first run; nothing is written to the old store.

Fixture serving files in the test's own ``MARKET_DIR``; offline.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_leaps import engine, scan
from RevenueChaser.module_leaps.tests.test_leaps_l3 import seed_session
from RevenueChaser.module_oipd.tests.options_store import daily_row, write_options
from RevenueChaser.rcdata import ops

SESSION = date(2026, 10, 6)                     # a Tuesday
TODAY = SESSION + timedelta(days=1)
FAR, FAR2, NEAR = (SESSION + timedelta(days=n) for n in (300, 400, 30))
PROV = {"source": "uw/option_list", "source_as_of": SESSION, "fetched_at": 1.0, "run_id": "fixture",
        "observed": True}


def _eod(ticker, session, expiry, kind, oi):
    sym = f"{ticker}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}00100000"
    return {"session_date": session, "ticker": ticker, "option_symbol": sym, "expiry": expiry, "strike": 100.0,
            "option_type": kind, "days_to_expiry": (expiry - session).days, "open_interest": oi, **PROV}


def _chain(ticker, session=SESSION, far_calls=800, far_puts=200, near_calls=5000):
    return [_eod(ticker, session, FAR, "call", far_calls), _eod(ticker, session, FAR, "put", far_puts),
            _eod(ticker, session, FAR2, "call", far_calls), _eod(ticker, session, NEAR, "call", near_calls),
            _eod(ticker, session, NEAR, "put", near_calls)]


def _print(ticker, pid, session=SESSION, expiry=FAR, premium=250_000.0, set_name="list", **kw):
    return {"session_date": session, "ticker": ticker, "set_name": set_name, "print_id": pid,
            "executed_at": f"{session}T15:00:00Z", "option_symbol": f"{ticker}{pid}", "expiry": expiry,
            "strike": 100.0, "option_type": "call", "premium": premium, "delta": 0.7, "size": 10, "ask_vol": 9,
            "bid_vol": 1, "canceled": False, **PROV, **kw}


def _store(ticker, session=SESSION, *, rank=(40.0, 42.0, 45.0, 50.0, 55.0), prints=(), **chain):
    days = [session - timedelta(days=len(rank) - 1 - i) for i in range(len(rank))]
    write_options(option_eod=_chain(ticker, session, **chain),
                  option_expiry=[{"session_date": session, "ticker": ticker, "set_name": "list", "expiry": FAR,
                                  "call_premium_ask_side": 900.0, "put_premium_ask_side": 100.0, **PROV}],
                  option_daily=[daily_row(ticker, d, 100.0, iv_rank_1y=r) for d, r in zip(days, rank)],
                  option_print=list(prints))


def _pin(*tickers, option=()):
    for t in tickers:
        ops.watch_add("stock", t, by="test")
    for t in option:
        ops.watch_add("option", t, by="test")


class _NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"LEAPS must not call a provider ({name})")


def _by_ticker(rows):
    return {r["ticker"]: r for r in rows}


def test_a_pinned_list_name_gets_its_footprint_from_the_store(rawstore_tmp_db):
    _pin("AAA", option=["AAA"])
    _store("AAA")
    (snap,), unwatched = scan.scan_leaps(_NoProvider(), today=TODAY)
    assert snap["ticker"] == "AAA" and snap["as_of"] == SESSION.isoformat()
    assert snap["far_call_oi"] == 1600 and snap["far_put_oi"] == 200        # the near expiry is not far
    assert snap["far_call_share"] == round(1600 / 1800, 4)
    assert snap["far_call_sweep_pct"] == 0.9 and snap["iv_rank"] == 55.0 and snap["iv_rank_slope"] == 3.75
    assert snap["label"] in engine.LABELS and snap.get("note") is None and unwatched == []


def test_a_pinned_name_off_the_list_gets_the_label_and_no_footprint_even_with_rows(rawstore_tmp_db):
    _pin("AAA", "BBB", option=["AAA"])
    _store("AAA")
    _store("BBB")
    for path in (scan.scan_leaps, lambda *a, **k: scan.scan_leaps_cached(**k)):
        watched = _by_ticker(path(None, today=TODAY)[0])
        bbb = watched["BBB"]
        assert bbb["note"] == "not on the option watch list" and bbb["label"] == "N/A"
        assert bbb["far_call_oi"] == 0 and bbb["footprint_score"] is None
        assert watched["AAA"].get("note") is None


def test_a_pinned_list_name_with_no_stored_chain_says_when_it_fills(rawstore_tmp_db):
    _pin("AAA", option=["AAA"])
    footprints: list = []
    (snap,), _ = scan.scan_leaps(None, today=TODAY, footprints=footprints)
    assert snap["label"] == "N/A" and "no stored option chain yet" in snap["note"]
    assert footprints[0]["fetch_status"] == "missing"


def test_the_print_scan_covers_the_list_names_far_dated_large_prints_only(rawstore_tmp_db):
    """CCC is on the list and not pinned: its far, large prints make it a market-scan row. A near print, a small
    one, a cancelled one and an earnings-set print are not read; DDD is not on the list, so its prints are not."""
    _pin(option=["CCC"])
    _store("CCC", prints=[_print("CCC", "p1"), _print("CCC", "p2", premium=400_000.0),
                          _print("CCC", "near", expiry=NEAR), _print("CCC", "small", premium=10_000.0),
                          _print("CCC", "gone", canceled=True), _print("CCC", "er", set_name="earnings")])
    _store("DDD", prints=[_print("DDD", "d1")])
    watched, unwatched = scan.scan_leaps(None, today=TODAY)
    assert watched == [] and [u["ticker"] for u in unwatched] == ["CCC"]
    ccc = unwatched[0]
    assert ccc["frequency"] == 2 and ccc["notional"] == 650_000.0 and ccc["ask_share"] == 0.9


def test_a_pinned_list_name_with_prints_carries_its_flow_score(rawstore_tmp_db):
    _pin("AAA", option=["AAA"])
    _store("AAA", prints=[_print("AAA", "p1")])
    (snap,), unwatched = scan.scan_leaps(None, today=TODAY)
    assert snap["frequency"] == 1 and snap["flow_score"] is not None and unwatched == []


def test_spx_is_on_the_list_but_an_index_is_never_a_print_row(rawstore_tmp_db):
    _store("SPX", prints=[_print("SPX", "x1")])
    assert scan.scan_leaps(None, today=TODAY) == ([], [])


def test_a_stored_report_date_within_the_window_makes_the_footprint_quiet(rawstore_tmp_db):
    """The near-event dates come from the options store's screener set (``option_screener_daily.report_date``)."""
    _pin("AAA", option=["AAA"])
    _store("AAA")
    write_options(option_screener_daily=[{"session_date": SESSION, "ticker": "AAA",
                                          "report_date": SESSION + timedelta(days=3), "passes_gates": True,
                                          "source": "uw/option_screener"}])
    (snap,), _ = scan.scan_leaps(None, today=TODAY)
    assert snap["near_term_event"] is True and snap["label"] == "Quiet"


def test_growth_is_against_the_recorded_session_before_the_stored_one(rawstore_tmp_db):
    """The baseline is picked at the stored session, so a Refresh the day after a Fetch is not compared with the
    session it shows (which would read 0% growth)."""
    _pin("AAA", option=["AAA"])
    seed_session(SESSION - timedelta(days=1), {"AAA": [{"expiry": FAR.isoformat(), "call_oi": 400, "put_oi": 100},
                                                       {"expiry": FAR2.isoformat(), "call_oi": 400, "put_oi": 0}]})
    _store("AAA")
    seed_session(SESSION, {"AAA": [{"expiry": FAR.isoformat(), "call_oi": 800, "put_oi": 200},
                                   {"expiry": FAR2.isoformat(), "call_oi": 800, "put_oi": 0}]})
    for path in (scan.scan_leaps, lambda *a, **k: scan.scan_leaps_cached(**k)):
        (snap,), _ = path(None, today=TODAY)
        assert snap["far_oi_growth_1d_pct"] == 100.0 and snap["label"] == "Accumulating"


def test_a_fetch_and_a_refresh_draw_the_same_board(rawstore_tmp_db):
    _pin("AAA", "BBB", option=["AAA", "CCC"])
    _store("AAA", prints=[_print("AAA", "p1")])
    _store("CCC", prints=[_print("CCC", "c1")])
    fetched = scan.scan_leaps(_NoProvider(), today=TODAY, footprints=[])
    assert scan.scan_leaps_cached(today=TODAY) == fetched


def test_nothing_is_written_to_the_old_store(rawstore_tmp_db):
    _pin("AAA", option=["AAA"])
    _store("AAA", prints=[_print("AAA", "p1")])
    scan.scan_leaps(None, today=TODAY, footprints=[])
    assert rawstore.count("leaps_cache") == 0 and rawstore.count("leaps_flow_prints") == 0


# ── the runner and the history ──────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def board(rawstore_tmp_db, tmp_path, monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.core import ledger
    import RevenueChaser.module_leaps.report as ld
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(runners, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(runners, "LEAPS_LATEST", tmp_path / "leaps_latest.html")
    ledger.take_notes()
    _pin("AAA", "BBB", option=["AAA"])
    _store("AAA")
    return runners


def test_the_fetch_queues_uw_option_list_only_after_its_first_run(board, monkeypatch):
    board.run_leaps_latest(None)
    assert ops.queue_view()["active"] == []
    assert "uw/option_list not queued: it has not had its first run" in board.LEAPS_LATEST.read_text("utf-8")
    monkeypatch.setattr(ops, "ever_built", lambda name: name == "uw/option_list")
    board.run_leaps_latest(None)
    board.run_leaps_latest(None)
    assert [(r["dataset"], r["requested_by"]) for r in ops.queue_view()["active"]] == [("uw/option_list", "leaps")]


def test_a_name_off_the_list_does_not_make_the_run_partial(board):
    from RevenueChaser.core import history
    board.run_leaps_latest(None)
    head = history.last_run("module_leaps")
    assert head["quality"] in ("complete", "intraday") and not head.get("quality_note")
    rows = {r["ticker"]: r for r in rawstore.query("SELECT ticker, fetch_status FROM leaps_footprint_history",
                                                    db="history")}
    assert rows["BBB"]["fetch_status"] == "not_on_list" and rows["AAA"]["fetch_status"] == "ok"


def test_the_board_shows_the_label_for_a_name_off_the_list(board):
    board.run_leaps_latest(None)
    html = board.LEAPS_LATEST.read_text("utf-8")
    block = html[html.index('data-ticker="BBB"'):]
    assert "not on the option watch list" in block[:block.index("</tr>")]


def test_the_board_job_needs_no_unusual_whales_client():
    from RevenueChaser.app import dashboard, jobs  # noqa: F401 - registers the jobs
    assert jobs._REQUIRES_UW["module_leaps"] is False

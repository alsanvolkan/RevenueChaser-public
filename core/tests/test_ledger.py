"""core.ledger — the forecast-ledger writer (#128). Offline; the store is a temp DuckDB."""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.tests.d01_seed import seed_d01

D = date(2026, 10, 2)          # a Friday and a normal session


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _pick(subject, rank, **kw):
    row = {"subject": subject, "rank": rank, "score": 80.0 - rank, "bucket": "3",
           "market_cap": 2.0e9, "sector": "Information Technology", "shown_price": 10.0 + rank,
           "stop": 9.0, "target": 14.0}
    row.update(kw)
    return row


def _write(rows, *, source="swing", forecast_date=D, run_id="run1", **kw):
    return ledger.write_picks(source, "Swing", rows, model_version="v1+abcd1234",
                              forecast_date=forecast_date, run_id=run_id, **kw)


def _stored(source="swing"):
    return rawstore.query("SELECT * FROM forecast_ledger WHERE source = ? ORDER BY rank", [source])


# ── write_picks ──────────────────────────────────────────────────────────────

def test_write_picks_stores_one_row_per_pick_with_the_exact_columns(rawstore_tmp_db):
    out = _write([_pick("AAA", 1), _pick("BBB", 2, stop=None, target=None)], frozen_at=1_790_000_000.0)
    assert (out.frozen, out.reason, out.forecast_date) == (2, None, D)
    rows = _stored()
    assert [r["subject"] for r in rows] == ["AAA", "BBB"]
    first = rows[0]
    assert first["source"] == "swing"
    assert first["forecast_date"] == D and first["target_date"] == D
    assert first["target_type"] == "pick"
    assert first["frozen_at"] == 1_790_000_000.0
    assert first["run_id"] == "run1" and first["model_version"] == "v1+abcd1234"
    assert (first["rank"], first["score"], first["bucket"]) == (1, 79.0, "3")
    assert (first["market_cap"], first["sector"], first["shown_price"]) == (2.0e9, "Information Technology", 11.0)
    assert (first["stop"], first["target"]) == (9.0, 14.0)
    assert first["probs"] is None
    assert rows[1]["stop"] is None and rows[1]["target"] is None


def test_rank_is_one_based(rawstore_tmp_db):
    with pytest.raises(ValueError):
        _write([_pick("AAA", 0)])
    assert rawstore.count("forecast_ledger") == 0


def test_a_second_write_the_same_day_adds_nothing(rawstore_tmp_db):
    _write([_pick("AAA", 1), _pick("BBB", 2)], run_id="first")
    out = _write([_pick("AAA", 1), _pick("BBB", 2)], run_id="second")
    assert out.frozen == 0 and "already frozen" in out.reason and str(D) in out.reason
    assert {r["run_id"] for r in _stored()} == {"first"}


def test_a_second_write_the_same_day_with_a_different_candidate_set_adds_nothing(rawstore_tmp_db):
    """The key includes ``subject``: without a day-level check the second run would add its
    new tickers and the day would be the union of two runs."""
    _write([_pick("AAA", 1), _pick("BBB", 2)], run_id="first")
    out = _write([_pick("CCC", 1), _pick("DDD", 2), _pick("AAA", 3)], run_id="second")
    assert out.frozen == 0
    assert sorted(r["subject"] for r in _stored()) == ["AAA", "BBB"]
    assert rawstore.count("forecast_ledger") == 2


def test_the_day_check_is_per_source_and_per_day(rawstore_tmp_db):
    _write([_pick("AAA", 1)], source="swing")
    assert _write([_pick("AAA", 1)], source="fundamentals").frozen == 1
    assert _write([_pick("AAA", 1)], source="swing", forecast_date=date(2026, 10, 5)).frozen == 1
    assert rawstore.count("forecast_ledger") == 3


def test_a_duplicate_subject_inside_one_batch_keeps_the_first(rawstore_tmp_db):
    out = _write([_pick("AAA", 1, score=90.0), _pick("AAA", 2, score=10.0), _pick("BBB", 3)])
    assert out.frozen == 2 and out.dropped == 1
    rows = {r["subject"]: r for r in _stored()}
    assert rows["AAA"]["score"] == 90.0 and rows["AAA"]["rank"] == 1


def test_subjects_are_upper_cased_and_stripped(rawstore_tmp_db):
    _write([_pick(" aapl ", 1)])
    assert [r["subject"] for r in _stored()] == ["AAPL"]


def test_nan_and_missing_numbers_become_null(rawstore_tmp_db):
    _write([_pick("AAA", 1, score=float("nan"), market_cap=None, stop=float("inf"))])
    row = _stored()[0]
    assert row["score"] is None and row["market_cap"] is None and row["stop"] is None


def test_every_date_handed_to_the_store_is_a_plain_date(rawstore_tmp_db, monkeypatch):
    """``rawstore._dedupe`` keys on the Python value: text, ``datetime`` and ``date`` are three
    different keys, so a mixed batch would not collapse and a retry would not conflict."""
    seen = []
    real = rawstore.write_rows

    def spy(name, rows):
        seen.extend(rows)
        return real(name, rows)

    monkeypatch.setattr(rawstore, "write_rows", spy)
    _write([_pick("AAA", 1), _pick("BBB", 2)])
    assert seen
    for r in seen:
        assert type(r["forecast_date"]) is date
        assert type(r["target_date"]) is date


@pytest.mark.parametrize("bad", ["2026-10-02", datetime(2026, 10, 2, 22, 0), None])
def test_a_forecast_date_that_is_not_a_date_is_refused(rawstore_tmp_db, bad):
    with pytest.raises(TypeError):
        _write([_pick("AAA", 1)], forecast_date=bad)
    assert rawstore.count("forecast_ledger") == 0


def test_an_empty_batch_writes_nothing_and_says_so(rawstore_tmp_db):
    out = _write([])
    assert out.frozen == 0 and out.reason


def test_the_writer_never_uses_the_destructive_rawstore_calls(rawstore_tmp_db, monkeypatch):
    """rawstore does not refuse ``update_columns``/``replace_all``/``delete``/``clear`` for this
    table, so the guarantee that the ledger is append-only is this writer's discipline."""
    def boom(*a, **k):
        raise AssertionError("the ledger writer must never call this")

    for name in ("update_columns", "replace_all", "delete", "clear"):
        monkeypatch.setattr(rawstore, name, boom)
    assert _write([_pick("AAA", 1)]).frozen == 1
    assert _write([_pick("BBB", 1)]).frozen == 0          # the day-already-frozen path too


def test_the_writer_source_names_no_destructive_call():
    import ast
    import inspect
    used = set()
    for node in ast.walk(ast.parse(inspect.getsource(ledger))):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
    assert not used & {"update_columns", "replace_all", "delete", "clear"}


def test_concurrent_writers_of_one_day_cannot_make_a_union(rawstore_tmp_db, monkeypatch):
    """A Swing job (short lane) and the universe job (long lane) can run at once. The day check
    and the write sit under one lock: the loser sees the winner's rows and writes nothing."""
    real = ledger._day_frozen

    def slow_check(source, forecast_date):
        found = real(source, forecast_date)
        time.sleep(0.3)                 # both threads would pass the check before either writes
        return found

    monkeypatch.setattr(ledger, "_day_frozen", slow_check)
    results = {}

    def run(tag, rows):
        results[tag] = _write(rows, run_id=tag)

    t1 = threading.Thread(target=run, args=("a", [_pick("AAA", 1), _pick("BBB", 2)]))
    t2 = threading.Thread(target=run, args=("b", [_pick("CCC", 1), _pick("DDD", 2)]))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()
    assert sorted(r["frozen"] for r in map(vars, results.values())) == [0, 2]
    assert len({r["run_id"] for r in _stored()}) == 1
    assert rawstore.count("forecast_ledger") == 2


# ── notes ─────────────────────────────────────────────────────────────────────

def test_take_notes_returns_every_note_and_clears():
    ledger.take_notes()
    ledger.add_note("one")
    ledger.add_note("two")
    assert ledger.take_notes() == ["one", "two"]
    assert ledger.take_notes() == []


def test_notes_are_per_thread():
    ledger.take_notes()
    ledger.add_note("main thread")
    seen = []
    t = threading.Thread(target=lambda: seen.append(ledger.take_notes()))
    t.start()
    t.join()
    assert seen == [[]]
    assert ledger.take_notes() == ["main thread"]


def test_outcome_messages():
    froze = ledger.Outcome("asymmetric_growth", "AG", frozen=4251, forecast_date=D)
    assert froze.message == "Froze 4,251 AG rows for 2026-10-02."
    left_out = ledger.Outcome("swing", "Swing", frozen=190, forecast_date=D, dropped=10,
                              dropped_why="duplicate ticker")
    assert left_out.message == "Froze 190 Swing rows for 2026-10-02 (10 left out: duplicate ticker)."
    skipped = ledger.Outcome("swing", "Swing", reason="inside the US session window")
    assert skipped.message == "Swing not frozen: inside the US session window."


# ── session gate ──────────────────────────────────────────────────────────────

def test_the_gate_returns_the_latest_complete_session_after_the_close():
    assert ledger.session_gate(_utc(2026, 10, 2, 21, 0)) == (D, None)       # 17:00 ET, EDT


def test_the_gate_uses_the_new_york_date_after_midnight_utc():
    # 00:30Z on Tue 2026-10-27 is Monday 20:30 ET: the session is 10-26, not the UTC date 10-27.
    assert ledger.session_gate(_utc(2026, 10, 27, 0, 30)) == (date(2026, 10, 26), None)


def test_the_gate_refuses_inside_the_session_window():
    d, why = ledger.session_gate(_utc(2026, 10, 2, 16, 0))                  # 12:00 ET
    assert d is None and "session window" in why
    d, why = ledger.session_gate(_utc(2026, 10, 2, 20, 15))                 # 16:15 ET: still in the 16:30 margin
    assert d is None and "session window" in why


def test_the_gate_allows_a_weekend_run_and_names_friday():
    assert ledger.session_gate(_utc(2026, 10, 3, 15, 0)) == (D, None)       # Saturday midday


def test_the_gate_skips_with_a_message_past_the_holiday_table():
    d, why = ledger.session_gate(_utc(2029, 3, 1, 22, 0))
    assert d is None and "2029" in why


def test_the_gate_reads_the_clock_seam_by_default(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: _utc(2026, 10, 2, 21, 0))
    assert ledger.session_gate() == (D, None)


# ── payload guards ────────────────────────────────────────────────────────────

def _payload(**kw):
    p = {"is_fixture": False, "no_data": False, "data_quality": {"spy_stale": False},
         "candidates": [{"ticker": "AAA"}]}
    p.update(kw)
    return p


def test_a_real_payload_has_no_problem():
    assert ledger.payload_problem(_payload(), "candidates") is None


@pytest.mark.parametrize("payload, needle", [
    (_payload(is_fixture=True), "fixture"),
    (_payload(no_data=True), "no data"),
    (_payload(candidates=[]), "no candidates"),
    (_payload(data_quality={"spy_stale": True}), "SPY"),
])
def test_a_bad_payload_is_named(payload, needle):
    assert needle in ledger.payload_problem(payload, "candidates")


def test_the_spy_stale_check_can_be_left_to_a_family_without_the_flag():
    p = _payload(data_quality={"spy_stale": True})
    assert ledger.payload_problem(p, "candidates", check_spy=False) is None


# ── OHLC anchor ───────────────────────────────────────────────────────────────

def _seed_bars(ticker, last_day, cached_at, ts_suffix="T00:00:00"):
    """The bars in ``swing_ohlc`` (no longer read since PR 10) and in the daily bars (``ohlc_gaps`` since K8b, #383 R3;
    the prices store since PR 10, which ``seed_d01`` also writes); ``ts_suffix`` shapes the cache's text timestamp
    only."""
    rawstore.write_rows("swing_ohlc", [
        {"ticker": ticker, "ts": f"{d}{ts_suffix}", "open": 1.0, "high": 2.0, "low": 0.5,
         "close": 1.5, "volume": 100.0, "cached_at": cached_at}
        for d in ("2026-09-30", "2026-10-01", last_day)])
    seed_d01(ticker, ("2026-09-30", "2026-10-01", last_day), cached_at)


def _close_plus(minutes):
    return cal.session_close(D).timestamp() + minutes * 60


def test_ohlc_complete_needs_the_session_as_last_bar_and_a_post_close_write(rawstore_tmp_db):
    _seed_bars("OK", "2026-10-02", _close_plus(45))
    _seed_bars("INTRADAY", "2026-10-02", _close_plus(-270))        # written 11:30 ET on D
    _seed_bars("OLD", "2026-10-01", _close_plus(45))               # last bar is the day before
    _seed_bars("TEXTDATE", "2026-10-02", _close_plus(45), ts_suffix="")
    ok = ledger.ohlc_complete(D, ["OK", "INTRADAY", "OLD", "TEXTDATE", "MISSING"])
    assert ok == {"OK", "TEXTDATE"}


def test_ohlc_complete_with_no_tickers_is_empty(rawstore_tmp_db):
    assert ledger.ohlc_complete(D, []) == set()


def test_ohlc_gaps_name_why_each_incomplete_ticker_is_incomplete(rawstore_tmp_db):
    _seed_bars("OK", "2026-10-02", _close_plus(45))
    _seed_bars("TEXTDATE", "2026-10-02", _close_plus(45), ts_suffix="")
    _seed_bars("INTRADAY", "2026-10-02", _close_plus(-270))        # the D bar was cached at 11:30 ET on D
    _seed_bars("STALE", "2026-10-01", _close_plus(-270))           # ends D-1, written before D's close + 30
    _seed_bars("HALTED", "2026-10-01", _close_plus(45))            # ends D-1, written after: nothing newer exists
    gaps = ledger.ohlc_gaps(D, ["OK", "TEXTDATE", "INTRADAY", "STALE", "HALTED", "MISSING"])
    # Option A: a D bar cached before close + 30 is a refetch away, exactly like an older last bar.
    assert gaps == {"INTRADAY": ledger.NEEDS_REFETCH, "STALE": ledger.NEEDS_REFETCH,
                    "HALTED": ledger.NO_SESSION_BAR, "MISSING": ledger.NO_SESSION_BAR}


def test_the_gap_causes_are_two_different_things_and_the_old_third_one_is_gone():
    assert ledger.NEEDS_REFETCH != ledger.NO_SESSION_BAR
    assert not hasattr(ledger, "CACHED_INTRADAY")           # option B's "a later Fetch reuses it" is untrue now


def test_ohlc_gaps_and_ohlc_complete_agree_on_every_ticker(rawstore_tmp_db):
    _seed_bars("OK", "2026-10-02", _close_plus(45))
    _seed_bars("INTRADAY", "2026-10-02", _close_plus(-270))
    _seed_bars("STALE", "2026-10-01", _close_plus(-270))
    _seed_bars("HALTED", "2026-10-01", _close_plus(45))
    tickers = ["OK", "INTRADAY", "STALE", "HALTED", "MISSING"]
    assert ledger.ohlc_complete(D, tickers) | set(ledger.ohlc_gaps(D, tickers)) == set(tickers)
    assert ledger.ohlc_complete(D, tickers) & set(ledger.ohlc_gaps(D, tickers)) == set()


def test_ohlc_gaps_with_no_tickers_is_empty(rawstore_tmp_db):
    assert ledger.ohlc_gaps(D, []) == {}


def test_a_stored_bar_later_than_d_is_a_gap_whatever_its_write_time(rawstore_tmp_db):
    """The cache can only hold a bar later than D through the clock seam (or bad data). It is not D's bar,
    so the pick is refused, and a refetch for D would not help: NO_SESSION_BAR, never "complete"."""
    _seed_bars("LATER_POST", "2026-10-05", _close_plus(45))        # later than D, written after D's close + 30
    _seed_bars("LATER_PRE", "2026-10-05", _close_plus(-270))       # later than D, written before it
    assert ledger.ohlc_gaps(D, ["LATER_POST", "LATER_PRE"]) == {
        "LATER_POST": ledger.NO_SESSION_BAR, "LATER_PRE": ledger.NO_SESSION_BAR}
    assert ledger.ohlc_complete(D, ["LATER_POST", "LATER_PRE"]) == set()


# ── F1: when does a cached series predate the latest complete session? ───────

@pytest.mark.parametrize("last_day, written_min, expected", [
    ("2026-10-01", -270, True),     # ends before D, written long before D's close
    ("2026-10-01", 29, True),       # written at 16:29 ET + ... one minute short of close + 30
    ("2026-10-01", 30, False),      # exactly close + 30: complete (the same >= as bars_complete)
    ("2026-10-01", 45, False),      # a halted name: written after the close, nothing newer exists
    ("2026-10-02", -270, True),     # option A: the D bar was cached mid-session (a partial bar)
    ("2026-10-02", 29, True),
    ("2026-10-02", 30, False),      # the D bar written exactly at close + 30: complete
    ("2026-10-02", 45, False),
    ("2026-10-05", -270, False),    # a bar later than D (clock seam only): nothing to refetch for D
])
def test_needs_session_refetch(last_day, written_min, expected):
    assert ledger.needs_session_refetch(last_day, _close_plus(written_min), session=D) is expected


def test_needs_session_refetch_reads_a_full_timestamp_and_a_missing_write_time():
    assert ledger.needs_session_refetch("2026-10-01T00:00:00", _close_plus(-60), session=D) is True
    assert ledger.needs_session_refetch("2026-10-01 00:00:00-04:00", _close_plus(45), session=D) is False
    assert ledger.needs_session_refetch("2026-10-01", None, session=D) is True      # never written: stale


def test_needs_session_refetch_takes_d_from_the_clock_seam_by_default(monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: _utc(2026, 10, 2, 21, 0))              # 17:00 ET, D = 10-02
    assert ledger.needs_session_refetch("2026-10-01", _close_plus(-60)) is True
    monkeypatch.setattr(cal, "_now", lambda: _utc(2026, 10, 2, 16, 0))              # 12:00 ET: D = 10-01
    assert ledger.needs_session_refetch("2026-10-01", _close_plus(-60)) is False


def test_needs_session_refetch_is_false_past_the_holiday_table(monkeypatch):
    """The calendar raises past 2028; the rule steps aside and the TTL alone decides."""
    monkeypatch.setattr(cal, "_now", lambda: _utc(2029, 3, 1, 22, 0))
    assert ledger.needs_session_refetch("2029-02-27", 0.0) is False


def test_needs_session_refetch_on_an_est_day_uses_the_est_close(monkeypatch):
    """D = Monday 2026-11-02 (EST): close 16:00 EST = 21:00Z, so close + 30 is 21:30Z, not the EDT 20:30Z."""
    monkeypatch.setattr(cal, "_now", lambda: _utc(2026, 11, 2, 22, 0))
    assert ledger.needs_session_refetch("2026-10-30", _utc(2026, 11, 2, 20, 30).timestamp()) is True
    assert ledger.needs_session_refetch("2026-10-30", _utc(2026, 11, 2, 21, 45).timestamp()) is False


def test_spy_anchor_problem_names_the_reason(rawstore_tmp_db):
    assert "no SPY" in ledger.spy_anchor_problem(D)
    _seed_bars("SPY", "2026-10-01", _close_plus(45))
    assert "2026-10-01" in ledger.spy_anchor_problem(D)
    _seed_bars("SPY", "2026-10-02", _close_plus(-270))
    why = ledger.spy_anchor_problem(D)
    assert why == "SPY's 2026-10-02 bar was read before the close + 30 min"
    _seed_bars("SPY", "2026-10-02", _close_plus(31))
    assert ledger.spy_anchor_problem(D) is None


# ── model version ─────────────────────────────────────────────────────────────

def test_model_version_is_the_selection_version_plus_an_order_free_config_hash():
    a = ledger.model_version("2", {"x": 1, "y": 2.5})
    b = ledger.model_version("2", {"y": 2.5, "x": 1})
    assert a == b
    version, digest = a.split("+")
    assert version == "2" and len(digest) == 8
    assert ledger.model_version("2", {"x": 1, "y": 2.6}) != a
    assert ledger.model_version("3", {"x": 1, "y": 2.5}).split("+")[1] == digest


def test_new_run_id_is_short_and_unique():
    ids = {ledger.new_run_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 12 for i in ids)


# ── freeze_board: the whole rule for one board ───────────────────────────────

POST_CLOSE = _utc(2026, 10, 2, 21, 0)       # 17:00 ET on the session D
INTRADAY = _utc(2026, 10, 2, 16, 0)         # 12:00 ET


def _board(n=3, **kw):
    p = _payload(candidates=[{"ticker": f"T{i}", "q": 50.0 + i} for i in range(1, n + 1)])
    p.update(kw)
    return p


def _to_rows(items):
    return [_pick(c["ticker"], i, score=c["q"]) for i, c in enumerate(items, start=1)]


def _freeze(payload, *, now=POST_CLOSE, **kw):
    ledger.take_notes()
    args = dict(rows_key="candidates", to_rows=_to_rows, model_version="v1+abcd1234", run_id="r1", now=now)
    args.update(kw)
    return ledger.freeze_board("swing", "Swing", payload, **args)


def test_freeze_board_writes_the_shown_picks_and_notes_it(rawstore_tmp_db):
    out = _freeze(_board(3))
    assert (out.frozen, out.forecast_date) == (3, D)
    assert [r["rank"] for r in _stored()] == [1, 2, 3]
    assert ledger.take_notes() == ["Froze 3 Swing rows for 2026-10-02."]


def test_freeze_board_inside_the_session_window_freezes_nothing_and_says_why(rawstore_tmp_db):
    out = _freeze(_board(3), now=INTRADAY)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert note.startswith("Swing not frozen: inside the US session window")


@pytest.mark.parametrize("payload", [_payload(is_fixture=True), _payload(no_data=True), _payload(candidates=[])])
def test_freeze_board_refuses_a_fixture_no_data_or_empty_payload(rawstore_tmp_db, payload):
    out = _freeze(payload)
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    assert len(ledger.take_notes()) == 1


def test_freeze_board_refuses_a_stale_spy_unless_the_board_has_no_such_flag(rawstore_tmp_db):
    stale = _board(2, data_quality={"spy_stale": True})
    assert _freeze(stale).frozen == 0
    assert _freeze(stale, check_spy=False).frozen == 2


def test_freeze_board_runs_the_anchor_and_reports_its_reason(rawstore_tmp_db):
    out = _freeze(_board(2), anchor=lambda session: f"no anchor for {session}")
    assert out.frozen == 0 and "no anchor for 2026-10-02" in out.reason
    assert rawstore.count("forecast_ledger") == 0


def _gaps(mapping):
    """A ``bar_gaps`` callback: ``mapping`` is {ticker: cause}; only the asked-for tickers are reported."""
    return lambda session, subjects: {t: c for t, c in mapping.items() if t in set(subjects)}


def test_freeze_board_freezes_nothing_when_one_shown_pick_lacks_a_complete_bar(rawstore_tmp_db):
    """All-or-nothing: a board with 1 of 3 picks missing its D bar is not frozen at all."""
    out = _freeze(_board(3), bar_gaps=_gaps({"T2": ledger.NO_SESSION_BAR}))
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    (note,) = ledger.take_notes()
    assert note.startswith("Swing not frozen:") and "1 of 3" in note and "T2" in note
    assert "T1" not in note and "T3" not in note


def test_freeze_board_freezes_every_pick_when_every_bar_is_complete(rawstore_tmp_db):
    out = _freeze(_board(4), bar_gaps=_gaps({}))
    assert out.frozen == 4 and out.dropped == 0
    assert [(r["subject"], r["rank"]) for r in _stored()] == [("T1", 1), ("T2", 2), ("T3", 3), ("T4", 4)]
    assert ledger.take_notes() == ["Froze 4 Swing rows for 2026-10-02."]


def test_freeze_board_with_no_complete_pick_freezes_nothing(rawstore_tmp_db):
    out = _freeze(_board(2), bar_gaps=_gaps({"T1": ledger.NEEDS_REFETCH, "T2": ledger.NEEDS_REFETCH}))
    assert out.frozen == 0 and "2 of 2" in out.reason
    assert rawstore.count("forecast_ledger") == 0


def test_a_refused_day_can_still_be_frozen_by_a_later_complete_run(rawstore_tmp_db):
    assert _freeze(_board(3), bar_gaps=_gaps({"T3": ledger.NEEDS_REFETCH})).frozen == 0
    assert _freeze(_board(3), bar_gaps=_gaps({})).frozen == 3
    assert rawstore.count("forecast_ledger") == 3


def test_all_or_nothing_still_keeps_first_write_wins(rawstore_tmp_db):
    assert _freeze(_board(3), bar_gaps=_gaps({})).frozen == 3
    out = _freeze(_board(5), bar_gaps=_gaps({}))
    assert out.frozen == 0 and "already frozen" in out.reason
    assert rawstore.count("forecast_ledger") == 3


def test_the_gap_check_runs_after_the_other_refusals(rawstore_tmp_db):
    """An intraday run is refused by the window before any bar is looked at."""
    def boom(session, subjects):
        raise AssertionError("the bar check must not run for a run that is already refused")
    assert _freeze(_board(2), now=INTRADAY, bar_gaps=boom).frozen == 0


def test_the_refusal_note_splits_the_incomplete_picks_by_cause_in_rank_order(rawstore_tmp_db):
    gaps = {"T6": ledger.NO_SESSION_BAR, "T4": ledger.NEEDS_REFETCH, "T5": ledger.NO_SESSION_BAR,
            "T2": ledger.NEEDS_REFETCH, "T3": ledger.NO_SESSION_BAR}
    out = _freeze(_board(7), bar_gaps=_gaps(gaps))
    assert out.frozen == 0 and rawstore.count("forecast_ledger") == 0
    why = out.reason
    assert "5 of 7" in why
    refetch = re.search(r"need their 2026-10-02 bar \(([^)]*)\): ([A-Z0-9, ]+?)(?:;|$)", why)
    no_bar = re.search(r"no 2026-10-02 bar after the close \(([^)]*)\): ([A-Z0-9, ]+?)(?:;|$)", why)
    assert refetch and no_bar
    assert refetch.group(2) == "T2, T4" and no_bar.group(2) == "T3, T5, T6"
    assert refetch.group(1) == ("none in the prices store read past the close + 30 min yet; "
                                "a later Fetch can complete the day once the builder's daily bars have run")
    assert no_bar.group(1) == ("a halt or a gap in the daily bars; "
                               "a later Fetch will not change it unless the daily bars are read again")
    assert why.count("later Fetch can") == 1                      # only the refetch cause may promise a fix
    assert "cached intraday" not in why and "reuses" not in why


def test_a_refusal_without_a_refetch_cause_never_says_a_later_fetch_can_help(rawstore_tmp_db):
    out = _freeze(_board(3), bar_gaps=_gaps({"T1": ledger.NO_SESSION_BAR, "T2": ledger.NO_SESSION_BAR}))
    assert "later Fetch can" not in out.reason and "can complete" not in out.reason
    assert "need their" not in out.reason


def test_a_refusal_with_only_one_cause_names_only_that_cause(rawstore_tmp_db):
    out = _freeze(_board(3), bar_gaps=_gaps({"T2": ledger.NEEDS_REFETCH}))
    assert "need their 2026-10-02 bar" in out.reason
    assert "after the close" not in out.reason


def test_a_long_ticker_list_is_cut_after_ten_names(rawstore_tmp_db):
    gaps = {f"T{i}": ledger.NO_SESSION_BAR for i in range(1, 13)}
    out = _freeze(_board(12), bar_gaps=_gaps(gaps))
    assert "12 of 12" in out.reason
    assert "T10" in out.reason and "T11" not in out.reason and "+2 more" in out.reason


def test_a_duplicate_ticker_does_not_count_as_a_second_incomplete_pick(rawstore_tmp_db):
    def twice(items):
        return [_pick("T1", 1), _pick("T2", 2), _pick("T1", 3)]
    out = _freeze(_board(2), to_rows=twice, bar_gaps=_gaps({"T1": ledger.NEEDS_REFETCH}))
    assert out.frozen == 0 and "1 of 2" in out.reason
    out = _freeze(_board(2), to_rows=twice, bar_gaps=_gaps({}))
    assert out.frozen == 2 and out.dropped == 1
    assert ledger.take_notes()[-1] == "Froze 2 Swing rows for 2026-10-02 (1 left out: duplicate ticker)."


def test_the_gap_check_is_case_and_space_blind_on_tickers(rawstore_tmp_db):
    def sloppy(items):
        return [_pick(" t1 ", 1), _pick("T2", 2)]
    out = _freeze(_board(2), to_rows=sloppy, bar_gaps=_gaps({"T1": ledger.NO_SESSION_BAR}))
    assert out.frozen == 0 and "T1" in out.reason


def test_freeze_board_is_first_write_wins_for_the_day(rawstore_tmp_db):
    _freeze(_board(3))
    out = _freeze(_board(5))
    assert out.frozen == 0 and "already frozen" in out.reason
    assert rawstore.count("forecast_ledger") == 3


def test_freeze_board_names_a_run_id_when_none_is_given(rawstore_tmp_db):
    ledger.take_notes()
    ledger.freeze_board("swing", "Swing", _board(1), rows_key="candidates", to_rows=_to_rows,
                        model_version="v1+abcd1234", now=POST_CLOSE)
    assert len(_stored()[0]["run_id"]) == 12


def test_freeze_board_uses_the_new_york_session_after_midnight_utc(rawstore_tmp_db):
    out = _freeze(_board(2), now=_utc(2026, 10, 27, 0, 30))
    assert out.forecast_date == date(2026, 10, 26)
    assert {r["forecast_date"] for r in _stored()} == {date(2026, 10, 26)}


def test_freeze_board_past_the_holiday_table_skips_with_a_message(rawstore_tmp_db):
    out = _freeze(_board(2), now=_utc(2029, 3, 1, 22, 0))
    assert out.frozen == 0 and "2029" in ledger.take_notes()[0]


def test_freeze_board_never_raises_job_message(rawstore_tmp_db):
    """A skipped freeze inside ``run_universe_refresh`` must not stop the next family."""
    from RevenueChaser.app import jobs
    for payload, now in ((_board(2), INTRADAY), (_payload(is_fixture=True), POST_CLOSE)):
        try:
            _freeze(payload, now=now)
        except jobs.JobMessage:                      # pragma: no cover - the failure being tested
            pytest.fail("freeze_board raised JobMessage")


def test_a_full_size_batch_goes_through_the_bulk_insert_path(rawstore_tmp_db):
    """AG freezes ~4,000 rows a day; ``rawstore`` switches to a DataFrame ``INSERT ... SELECT`` from 64 rows.
    The dates must still land as DATE, nullable numbers as NULL, and the whole day as one batch."""
    rows = [_pick(f"T{i:05d}", i, score=float(i), stop=None, target=None,
                  market_cap=None if i % 7 == 0 else 1.0e9) for i in range(1, 4252)]
    out = _write(rows, source="asymmetric_growth", frozen_at=1_790_000_000.0)
    assert out.frozen == 4251
    got = rawstore.query("SELECT count(*) AS n, count(DISTINCT subject) AS s, min(rank) AS lo, max(rank) AS hi, "
                         "min(forecast_date) AS fd, max(target_date) AS td, count(stop) AS stops, "
                         "count(market_cap) AS caps, any_value(typeof(forecast_date)) AS ty "
                         "FROM forecast_ledger")[0]
    assert (got["n"], got["s"], got["lo"], got["hi"]) == (4251, 4251, 1, 4251)
    assert got["fd"] == D and got["td"] == D and got["ty"] == "DATE"
    assert got["stops"] == 0 and got["caps"] == 4251 - len([i for i in range(1, 4252) if i % 7 == 0])
    assert _write([_pick("OTHER", 1)], source="asymmetric_growth").frozen == 0

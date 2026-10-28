"""``uw/option_list`` through the builder: SPX and the option watch list, with UW mocked at the HTTP boundary."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import rows as option_rows, tables, uw_list

from .options_fixture import SESSION, UWOptions

AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)       # 17:00 New York, Monday 26 Oct 2026
IN_SESSION = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)        # 11:00 New York, Tuesday


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)


@pytest.fixture
def uw_opts(http_mock):
    mock = UWOptions()
    http_mock.add("uw.test/api", mock)
    return mock


def _daily(**kw):
    return base.run_request({"dataset": uw_list.NAME, "params": {}}, **kw)


def _query(sql: str, params=()):
    with serving.open_read("options") as con:
        return con.execute(sql, list(params)).fetchall()


def test_the_list_set_is_spx_and_the_option_watch_list_read_at_each_run(uw_opts):
    """A name added to the list is fetched by the next run; the names already done are not asked again."""
    ops.watch_add("option", "aapl", by="test")
    row = _daily()
    assert row["result"] == "changed" and row["coverage"] == 1.0
    assert row["calls"] == 2 * uw_opts.per_name()
    assert {p.split("/")[2] for p, _ in uw_opts.calls("/stock/")} == {"SPX", "AAPL"}
    ops.watch_add("option", "MSFT", by="test")
    uw_opts.requests.clear()
    row = _daily()
    assert {p.split("/")[2] for p, _ in uw_opts.calls("/stock/")} == {"MSFT"}
    assert row["calls"] == uw_opts.per_name() and row["coverage"] == 1.0
    assert _query("SELECT ticker FROM option_daily ORDER BY ticker") == [("AAPL",), ("MSFT",), ("SPX",)]


def test_chain_rows_are_one_per_contract_and_date_with_their_greeks(uw_opts):
    _daily()
    rows = _query("SELECT session_date, option_symbol, expiry, strike, option_type, days_to_expiry, open_interest, "
                  "oi_change, charm, greeks_iv, observed FROM option_eod ORDER BY option_symbol")
    assert len(rows) == len(uw_opts.expiries) * len(uw_opts.strikes) * 2
    assert len({(r[0], r[1]) for r in rows}) == len(rows)
    first = rows[0]
    assert first[1].startswith("SPXW") and first[2] == uw_opts.expiries[0] and first[3] == 100.0
    assert first[4] == "call" and first[5] == (uw_opts.expiries[0] - SESSION).days
    assert first[6] == 120 and first[7] == 20, "oi_change is open_interest - prev_oi of the same answer"
    assert first[8] == 0.7 and first[9] == 0.32 and first[10] is True
    puts = [r for r in rows if r[4] == "put"]
    assert puts and all(r[8] == -0.7 and r[9] == 0.33 for r in puts), "a put takes the put side of /greeks"
    # The next session adds its own rows: still one per contract and date.
    nxt = calendar.next_trading_day(SESSION)
    uw_opts.session = nxt
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc))
        _daily()
    days = dict(_query("SELECT session_date, count(*) FROM option_eod GROUP BY ALL"))
    assert days == {SESSION: len(rows), nxt: len(rows)}


def test_a_repeated_contract_in_one_answer_is_kept_once_with_an_issue(uw_opts):
    real = uw_opts._contracts
    uw_opts._contracts = lambda ticker, expiry: (lambda r: r + r[:1])(real(ticker, expiry))
    _daily()
    assert _query("SELECT count(*) FROM option_eod")[0][0] == len(uw_opts.expiries) * len(uw_opts.strikes) * 2
    _, issues = ops.open_issues(uw_list.NAME)
    assert any(i["kind"] == "duplicate_key" for i in issues)


def test_the_stats_land_per_strike_expiry_and_day(uw_opts):
    _daily()
    strike = _query("SELECT strike, set_name, price, call_gamma_oi, call_gex FROM option_strike ORDER BY strike")
    assert strike[0] == (100.0, "list", 101.5, 1000.5, 5000.0) and len(strike) == len(uw_opts.strikes)
    expiry = _query("SELECT expiry, chains, open_interest, call_gex, call_premium, call_trades, put_volume, "
                    "volatility, implied_move_perc FROM option_expiry ORDER BY expiry")
    assert expiry[0] == (uw_opts.expiries[0], 6, 1000, 5000.0, 12000.0, 30, 70, 0.3, 0.04)
    daily = _query("SELECT expiries, contracts, gamma_flip, call_volume, put_volume, iv_rank_1y, close "
                   "FROM option_daily")
    assert daily == [(2, 12, 102.5, 600, 400, 0.5, 101.0)], "the options-volume and iv-rank rows of the session"


def test_iv_daily_holds_uw_s_tenors_observed(uw_opts):
    _daily()
    rows = _query("SELECT days, volatility, source, observed FROM iv_daily ORDER BY days")
    assert [r[0] for r in rows] == [1, 5, 7, 14, 30]
    assert all(r[2] == "uw" and r[3] is True for r in rows)


def test_prints_keep_the_newest_50_of_the_session_with_their_side_and_tags(uw_opts):
    _daily()
    rows = _query("SELECT print_id, executed_at, tags, nbbo_bid, ask_vol, multi_vol, upstream_condition_detail, "
                  "side, package FROM option_print ORDER BY executed_at DESC")
    assert len(rows) == 50 and rows[0][0] == "SPX-p59" and rows[-1][0] == "SPX-p10"
    assert rows[0][2] == ["ask_side", "bullish"] and rows[0][3] == 1.10 and rows[0][4] == 10
    assert rows[0][7:] == ("ask", False)
    assert all(not r[0].endswith("-old") for r in rows), "a print of another session is left out"


def test_a_print_s_side_is_uw_s_tag_else_the_nearer_side_of_its_quote():
    """Earnings Radar review S4: a lean is read from the print's side (ask-call, bid-put), so each print carries
    one. UW's tag wins; without one, the side of the quote's midpoint the price is on. The first real sample (300
    tagged prints) found that nearer-side rule agreeing with the tag on 235 of the 241 with a quote, and the strict
    rule (inside the spread is mid) on only 123 of 300. No tag and no usable quote is no side, never a guess."""
    side = option_rows.print_side
    quote = {"nbbo_bid": "1.10", "nbbo_ask": "1.20"}
    assert side({"tags": ["bid_side", "bearish"], "price": "1.20", **quote}) == "bid", "UW's own tag wins"
    assert side({"tags": "ASK_SIDE"}) == "ask" and side({"tags": ["mid_side"], "price": "1.20", **quote}) == "mid"
    assert side({"tags": ["bullish"], "price": "1.18", **quote}) == "ask", "inside the spread, nearer the ask"
    assert side({"price": "1.12", **quote}) == "bid" and side({"price": 1.15, **quote}) == "mid"
    assert side({"price": "1.25", **quote}) == "ask" and side({"price": "1.05", **quote}) == "bid"
    assert side({"price": "1.15"}) is None and side({}) is None
    assert side({"price": "1.15", "nbbo_bid": "1.20", "nbbo_ask": "1.20"}) is None, "a locked quote has no side"


def test_a_multi_leg_or_stock_tied_print_is_marked_a_package():
    """Earnings Radar review S4: a package leg is not a directional print. The OPRA trade condition says so."""
    package = option_rows.is_package
    assert package({"upstream_condition_detail": "mlft"}) is True
    assert package({"upstream_condition_detail": "TLET"}) is True
    assert package({"upstream_condition_detail": "auto"}) is False
    assert package({"upstream_condition_detail": ""}) is None and package({}) is None


def test_outside_the_observed_window_no_call_is_sent(uw_opts, monkeypatch):
    base.parquet_root().mkdir(parents=True)             # the raw disk is mounted; nothing typed yet
    monkeypatch.setattr(calendar, "_now", lambda: IN_SESSION)
    row = base.run_request({"dataset": uw_list.NAME, "params": {"as_of": "2026-10-26"}})
    assert row["calls"] == 0 and uw_opts.requests == []
    assert "outside the observed window" in row["note"]
    assert row["next_check_due"] == "2026-10-27", "the names left wait for the next session"


def test_a_name_whose_call_fails_is_asked_again_by_the_next_run(uw_opts):
    ops.watch_add("option", "AAPL", by="test")
    uw_opts.fail["/stock/AAPL/greeks"] = 500
    row = _daily()
    assert row["result"] == "changed" and row["coverage"] == 0.5
    assert row["next_check_due"] == SESSION.isoformat(), "the same session again while the window is open"
    assert "1 left" in row["note"] and not uw_list.raw_path(SESSION, "AAPL").exists()
    uw_opts.fail.clear()
    row = _daily()
    assert row["coverage"] == 1.0 and row["next_check_due"] == "2026-10-27"
    assert _query("SELECT count(DISTINCT ticker) FROM option_eod")[0][0] == 2


def test_a_part_uw_refuses_is_stored_empty_and_named_in_an_issue(uw_opts):
    uw_opts.fail["/stock/SPX/gex-levels"] = 404
    row = _daily()
    assert row["coverage"] == 1.0
    assert _query("SELECT gamma_flip, call_volume FROM option_daily") == [(None, 600)]
    _, issues = ops.open_issues(uw_list.NAME)
    assert any(i["kind"] == "part_empty" and "gex_levels HTTP 404" in i["detail"] for i in issues)


def test_a_refused_key_fails_the_run(uw_opts):
    uw_opts.fail["/stock/SPX/expiry-breakdown"] = 401
    row = _daily()
    assert row["result"] == "failed" and "key was refused" in row["note"]


def test_the_run_budget_stops_starting_names_and_the_next_run_resumes(uw_opts):
    for t in ("AAPL", "MSFT"):
        ops.watch_add("option", t, by="test")
    reads = iter([0, 0])                    # the run's start, then SPX's turn; every later read is past the budget
    row = _daily(clock=lambda: next(reads, uw_list.RUN_BUDGET_S))
    assert row["coverage"] == round(1 / 3, 4) and "budget" in row["note"]
    assert row["next_check_due"] == SESSION.isoformat()
    row = _daily()
    assert row["coverage"] == 1.0


def test_a_raw_file_a_killed_run_left_untyped_is_typed_by_the_next_run(uw_opts, monkeypatch):
    monkeypatch.setattr(uw_list.UWOptionList, "to_parquet_all", lambda self, ctx, raws: [])
    _daily()
    assert uw_list.raw_path(SESSION, "SPX").exists() and tables.names_done(uw_list.SOURCE, SESSION) == {}
    monkeypatch.undo()
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    uw_opts.requests.clear()
    row = _daily()
    assert row["calls"] == 0 and uw_opts.requests == [] and row["coverage"] == 1.0
    assert _query("SELECT count(*) FROM option_daily")[0][0] == 1


def test_a_full_page_of_contracts_is_paged_and_a_moved_answer_fails_the_name(uw_opts):
    uw_opts.contracts["SPX"] = 501
    row = _daily()
    pages = [q.get("page") for p, q in uw_opts.calls("/option-contracts")]
    assert pages.count(None) == 2 and pages.count(["1"]) == 2, "page 0 without page=, then page=1, per expiry"
    assert row["calls"] == uw_opts.per_name() + 2
    assert _query("SELECT count(*) FROM option_eod")[0][0] == 2 * 501
    # A second page that repeats a key of the first: the answer moved, the name is not stored.
    uw_opts.session = calendar.next_trading_day(SESSION)
    real = uw_opts.body

    def moved(path, q):
        body = real(path, q)
        if path.endswith("option-contracts") and q.get("page") == ["1"]:
            body["data"] = real(path, {**q, "page": ["0"]})["data"][:1]
        return body
    uw_opts.body = moved
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc))
        row = _daily()
    assert row["coverage"] == 0.0 and not uw_list.raw_path(uw_opts.session, "SPX").exists()


def test_the_raw_size_per_name_is_in_the_check_note(uw_opts):
    row = _daily()
    assert "KB a name (largest SPX" in row["note"]
    done = tables.names_done(uw_list.SOURCE, SESSION)
    assert done["SPX"]["raw_bytes"] == uw_list.raw_path(SESSION, "SPX").stat().st_size
    assert done["SPX"]["calls"] == uw_opts.per_name()


def test_the_dataset_starts_by_hand_and_has_no_backfill(uw_opts):
    from RevenueChaser.rcdata.build import DATASETS, due
    assert DATASETS[uw_list.NAME] is uw_list.DATASET
    assert due.is_due(uw_list.DATASET) == (False, due.BY_HAND_WHY)
    row = base.run_request({"dataset": uw_list.NAME, "params": {"mode": "backfill", "from": "2026-10-01"}})
    assert row["calls"] == 0 and "no backfill" in row["note"]

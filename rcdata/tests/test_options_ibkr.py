"""``ibkr/iv_history``: the IBKR year of 30-day IV per list name, through the minimal TWS client, against a fake
gateway (``ibkr_fixture``). Market data only: the client never asks for an account, a position or an order."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import ibkr_iv, uw_list
from RevenueChaser.core.providers import ibkr_tws as ibkr

from .ibkr_fixture import ACCOUNT_PLACEHOLDER, FakeGateway
from .options_fixture import SESSION, UWOptions

AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)
# Messages a market-data client may send: startApi and reqHistoricalData. Never reqPositions (61), reqAccountUpdates
# (6), reqAccountSummary (62), reqManagedAccts (17 outgoing), placeOrder (3), reqOpenOrders (5), reqExecutions (7).
ALLOWED = {"71", "20"}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)


@pytest.fixture
def gateway(monkeypatch):
    gw = FakeGateway(last_bar=SESSION)
    monkeypatch.setattr(ibkr, "CONNECT", gw.connect)
    return gw


def _run(**kw):
    return base.run_request({"dataset": ibkr_iv.NAME, "params": {}}, sleep=lambda s: None, **kw)


def _query(sql: str, params=()):
    with serving.open_read("options") as con:
        return con.execute(sql, list(params)).fetchall()


# ── the client ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_client_sends_the_handshake_startapi_and_the_history_request_only(gateway, caplog):
    caplog.set_level(logging.DEBUG)
    with ibkr.TWSClient("127.0.0.1", 4001) as client:
        bars = client.history(1, ibkr.contract_for("AAPL"))
        assert ACCOUNT_PLACEHOLDER not in repr(vars(client)), "the account list is dropped unread"
    assert gateway.handshake.startswith(b"API\0") and b"v157..178" in gateway.handshake
    assert set(gateway.message_ids()) <= ALLOWED
    assert gateway.sent[0] == ["71", "2", str(ibkr.CLIENT_ID), ""]
    assert len(bars) == 252 and bars[-1]["date"] == SESSION and bars[-1]["close"] == pytest.approx(0.2251)
    assert ACCOUNT_PLACEHOLDER not in caplog.text


def test_the_history_request_asks_one_year_of_daily_option_implied_volatility(gateway):
    with ibkr.TWSClient("127.0.0.1", 4001) as client:
        client.history(7, ibkr.contract_for("SPX"))
        client.history(8, ibkr.contract_for("BRK.B"))
    spx, brk = gateway.history_requests()
    assert spx == ["20", "7", "0", "SPX", "IND", "", "0.0", "", "", "CBOE", "", "USD", "", "", "0", "", "1 day",
                   "1 Y", "1", "OPTION_IMPLIED_VOLATILITY", "1", "0", ""]
    assert brk[3:5] == ["BRK B", "STK"] and brk[9] == "SMART"


def test_an_error_for_the_request_is_raised_and_info_messages_are_skipped(gateway):
    gateway.errors["ZZZZ"] = (200, "No security definition has been found for the request")
    with ibkr.TWSClient("127.0.0.1", 4001) as client:
        with pytest.raises(ibkr.HistoryError) as e:
            client.history(3, ibkr.contract_for("ZZZZ"))
    assert e.value.code == 200


def test_a_gateway_that_is_not_listening_is_a_tws_error(gateway):
    gateway.down = True
    with pytest.raises(ibkr.TWSError):
        ibkr.TWSClient("127.0.0.1", 4001).open()


# ── the dataset ────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_name_added_to_the_list_gets_its_year_once(gateway):
    ops.watch_add("option", "AAPL", by="test")
    row = _run()
    assert [r[3] for r in gateway.history_requests()] == ["SPX", "AAPL"] and row["calls"] == 2
    assert row["coverage"] == 1.0
    ops.watch_add("option", "MSFT", by="test")
    gateway.sent.clear()
    row = _run()
    assert [r[3] for r in gateway.history_requests()] == ["MSFT"] and row["calls"] == 1
    connects = gateway.connects
    row = _run()
    assert gateway.connects == connects and row["calls"] == 0, "nothing missing: no connection at all"
    assert dict(_query("SELECT ticker, count(*) FROM iv_daily WHERE source = 'ibkr' GROUP BY ALL")) == {
        "AAPL": 252, "MSFT": 252, "SPX": 252}


def test_iv_daily_flags_the_ibkr_year_backfilled_and_uw_s_daily_observed(gateway, http_mock, monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    http_mock.add("uw.test/api", UWOptions())
    base.run_request({"dataset": uw_list.NAME, "params": {}})
    _run()
    got = dict(_query("SELECT source || ':' || observed, count(*) FROM iv_daily WHERE ticker = 'SPX' GROUP BY ALL"))
    assert got == {"uw:true": 5, "ibkr:false": 252}
    assert _query("SELECT days FROM iv_daily WHERE source = 'ibkr' GROUP BY ALL") == [(30,)]


def test_a_bar_of_a_session_not_complete_yet_is_left_out(gateway):
    gateway.extra_bars["SPX"] = [(calendar.next_trading_day(SESSION), 0.9)]
    _run()
    assert _query("SELECT max(session_date) FROM iv_daily WHERE source = 'ibkr'") == [(SESSION,)]


def test_the_gateway_down_while_names_are_missing_fails_the_check(gateway):
    gateway.down = True
    row = _run()
    assert row["result"] == "failed" and "IBKR gateway" in row["note"]


def test_requests_are_paced_at_six_a_minute(gateway):
    for i in range(7):
        ops.watch_add("option", f"N{i}", by="test")
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
    row = base.run_request({"dataset": ibkr_iv.NAME, "params": {}}, sleep=sleep, clock=lambda: now[0])
    assert row["calls"] == 8 and ibkr_iv.PER_MINUTE == 6
    assert sum(slept) >= 60.0, "the seventh request waits for the first one's minute to pass"


def test_no_data_for_a_name_is_kept_once_with_an_issue(gateway):
    ops.watch_add("option", "NODATA", by="test")
    gateway.errors["NODATA"] = (162, "Historical Market Data Service error message:HMDS query returned no data")
    row = _run()
    assert row["coverage"] == 1.0 and ibkr_iv.raw_path("NODATA").exists()
    _, issues = ops.open_issues(ibkr_iv.NAME)
    assert any(i["kind"] == "no_history" and i["key"] == "NODATA" for i in issues)
    gateway.sent.clear()
    _run()
    assert gateway.history_requests() == []


def test_the_dataset_starts_by_hand_and_needs_no_first_day():
    from RevenueChaser.rcdata.build import DATASETS, due
    assert DATASETS[ibkr_iv.NAME] is ibkr_iv.DATASET and ibkr_iv.DATASET.providers == ("ibkr",)
    assert due.is_due(ibkr_iv.DATASET) == (False, due.BY_HAND_WHY)
    assert ibkr_iv.DATASET.backfill_needs_from is False

"""``uw/far_contracts`` (#746): the sweep of a session's far-dated contracts and, the next morning, each contract's
next-session open interest, with the rule written on the issue before any data was looked at. UW is mocked at the
HTTP boundary."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import read, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import uw_far_contracts as far

from .options_fixture import SESSION, UWOptions, occ

# New York is UTC-4 until 1 Nov 2026. SESSION is Monday 26 Oct 2026.
AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)        # 17:00 New York, the session's evening
EARLY = datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc)              # 07:00 New York, before UW's update margin
MORNING = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)            # 08:00 New York, before the next open
NEXT_EVENING = datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc)       # 17:00 New York on the next session
AFTER_WINDOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)       # 08:00 New York, two sessions on
EXPIRY = date(2028, 1, 21)


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    at(monkeypatch, AFTER_CLOSE)


def at(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(calendar, "_now", lambda: when)


@pytest.fixture
def uw_opts(http_mock):
    mock = UWOptions()
    http_mock.add("uw.test/api", mock)
    return mock


def contract(ticker: str, cp: str, strike: float, *, volume=1000, oi=100, prev_oi=90, premium="75000.00",
             expiry=EXPIRY, **extra) -> dict:
    """One screener row as UW sends it (numbers as strings)."""
    return {"option_symbol": occ(ticker, expiry, cp, strike), "ticker_symbol": ticker, "volume": str(volume),
            "open_interest": str(oi), "prev_oi": str(prev_oi), "premium": premium, "ask_side_volume": str(volume // 2),
            "bid_side_volume": str(volume // 4), "multileg_volume": str(volume // 10), "trades": "12",
            "issue_type": "Common Stock", "sector": "Technology", "last_fill": "2026-10-26T19:58:00Z",
            "next_earnings_date": "2027-01-28", **extra}


def chain_row(sweep_row: dict, *, oi: int, prev_oi: int | None = None) -> dict:
    """The far chain's row of a swept contract the next morning: ``prev_oi`` is the sweep's ``open_interest``."""
    return {"option_symbol": sweep_row["option_symbol"], "open_interest": str(oi),
            "prev_oi": sweep_row["open_interest"] if prev_oi is None else str(prev_oi)}


def run():
    return base.run_request({"dataset": far.NAME, "params": {}})


def query(sql: str, params=()):
    with serving.open_read("options") as con:
        return con.execute(sql, list(params)).fetchall()


# ── The sweep ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_sweep_pages_each_side_to_a_short_page_with_the_filters_of_the_rule(uw_opts):
    uw_opts.contract_screener["call"] = [contract(f"C{i:03d}", "C", 100) for i in range(260)]
    uw_opts.contract_screener["put"] = [contract("PUTS", "P", 50 + i) for i in range(10)]
    row = run()
    assert row["result"] != "failed", row["note"]
    asked = uw_opts.calls("/screener/option-contracts")
    assert [(q["type"][0], (q.get("page") or ["0"])[0]) for _, q in asked] == [
        ("call", "0"), ("call", "1"), ("put", "0")], "0-based pages until a short page, then the other side"
    assert all(q["min_dte"] == ["183"] and q["min_premium"] == ["50000"] and q["limit"] == ["250"] for _, q in asked)
    assert query("SELECT count(*), count(DISTINCT ticker) FROM option_far_contract") == [(270, 261)]
    assert query("SELECT DISTINCT session_date, oi_status FROM option_far_contract") == [(SESSION, "pending")]
    assert row["next_check_due"] == SESSION.isoformat(), "asked again: the open interest is still to read"


def test_a_sweep_row_carries_the_screener_fields_and_the_shares(uw_opts):
    uw_opts.contract_screener["call"] = [contract("NVDA", "C", 110, volume=3000, multileg_volume="0")]
    uw_opts.contract_screener["put"] = [contract("NVDA", "P", 50)]
    run()
    assert query("SELECT option_symbol, ticker, option_type, expiry, strike, days_to_expiry, volume, "
                 "ask_side_volume, ask_share, multileg_share, premium, open_interest, prev_oi, issue_type, "
                 "next_earnings_date FROM option_far_contract WHERE option_type = 'call'") == [
        ("NVDA280121C00110000", "NVDA", "call", EXPIRY, 110.0, (EXPIRY - SESSION).days, 3000, 1500, 0.5, 0.0,
         75000.0, 100, 90, "Common Stock", date(2027, 1, 28))]


@pytest.mark.parametrize("bad, why", [
    ({"expiry": date(2027, 1, 15)}, "days to expiry"),
    ({"premium": "49999"}, "premium"),
    ({"cp": "P"}, "a put on the call side"),
])
def test_one_row_that_breaks_a_filter_fails_the_run_and_keeps_nothing(uw_opts, bad, why):
    """UW ignores a parameter it does not honour without an error, so a row outside the filter means the filter was
    not applied: nothing of the sweep is kept."""
    bad = dict(bad)
    odd = contract("ODD", bad.pop("cp", "C"), 100, **bad)
    uw_opts.contract_screener["call"] = [contract("GOOD", "C", 100), odd]
    uw_opts.contract_screener["put"] = [contract("GOOD", "P", 100)]
    row = run()
    assert row["result"] == "failed" and why in row["note"]
    assert not far.sweep_path(SESSION).exists()


def test_a_symbol_on_two_pages_fails_the_sweep(uw_opts):
    rows = [contract(f"C{i:03d}", "C", 100) for i in range(250)]
    uw_opts.contract_screener["call"] = rows + [rows[3]]
    uw_opts.contract_screener["put"] = [contract("GOOD", "P", 100)]
    row = run()
    assert row["result"] == "failed" and "repeated across pages" in row["note"]
    assert not far.sweep_path(SESSION).exists()


def test_outside_the_observed_window_no_sweep_is_asked(uw_opts, monkeypatch):
    at(monkeypatch, datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc))     # 11:00 New York, in the next session
    base.parquet_root().mkdir(parents=True)
    row = base.run_request({"dataset": far.NAME, "params": {"as_of": SESSION.isoformat()}})
    assert row["calls"] == 0 and uw_opts.requests == [] and "no sweep" in row["note"]


# ── The next session's open interest ───────────────────────────────────────────────────────────────────────────

def _swept(uw_opts, rows):
    """Sweep ``rows`` on the session's evening (each on its side, by the OCC symbol's right)."""
    uw_opts.contract_screener = {"call": [r for r in rows if r["option_symbol"][-9] == "C"],
                                 "put": [r for r in rows if r["option_symbol"][-9] == "P"]}
    assert run()["result"] != "failed"
    uw_opts.requests.clear()


def test_no_chain_is_asked_before_the_morning_margin(uw_opts, monkeypatch):
    _swept(uw_opts, [contract("AAA", "C", 100)])
    uw_opts.requests.clear()
    at(monkeypatch, EARLY)
    run()
    assert uw_opts.calls("/option-contracts") == []


def test_the_morning_reads_each_ticker_s_chain_and_computes_the_opened_share(uw_opts, monkeypatch):
    a = contract("AAA", "C", 100, volume=1000, oi=100)        # 600 of 1,000 opened
    b = contract("AAA", "P", 90, volume=400, oi=500)          # 100 closed
    c = contract("BBB", "C", 50, volume=200, oi=10)           # prev_oi does not match: stale
    d = contract("BBB", "C", 55, volume=300, oi=20)           # not in the chain: missing
    e = contract("CCC", "C", 70, volume=100, oi=1000)         # 300 off on 100 traded: out of range
    _swept(uw_opts, [a, b, c, d, e])
    uw_opts.far_chain = {"AAA": [chain_row(a, oi=700), chain_row(b, oi=400)],
                         "BBB": [chain_row(c, oi=60, prev_oi=11)],
                         "CCC": [chain_row(e, oi=700)]}
    at(monkeypatch, MORNING)
    row = run()
    assert row["result"] != "failed", row["note"]
    asked = uw_opts.calls("/option-contracts")
    assert sorted(p.split("/")[2] for p, _ in asked) == ["AAA", "BBB", "CCC"]
    assert all(q["min_dte"] == ["175"] and q["limit"] == ["500"] and "expiry" not in q for _, q in asked)
    got = {s: rest for s, *rest in query(
        "SELECT option_symbol, oi_status, oi_next, oi_change, opened_share, out_of_range FROM option_far_contract")}
    assert got == {a["option_symbol"]: ["matched", 700, 600, 0.6, False],
                   b["option_symbol"]: ["matched", 400, -100, -0.25, False],
                   c["option_symbol"]: ["stale", None, None, None, None],
                   d["option_symbol"]: ["missing", None, None, None, None],
                   e["option_symbol"]: ["matched", 700, -300, -3.0, True]}, "raw, not clipped"
    assert "open interest of the newest read session: 3 matched, 1 stale, 1 missing" in row["note"]
    assert row["next_check_due"] == calendar.next_trading_day(SESSION).isoformat(), "nothing left: not due again"
    uw_opts.requests.clear()
    run()
    assert uw_opts.calls("/option-contracts") == [], "a ticker's chain is read once a session"


def test_a_ticker_uw_has_not_updated_is_asked_again_by_the_next_run(uw_opts, monkeypatch):
    rows = [contract("SLOW", "C", 100 + i, oi=100 + i) for i in range(3)]
    _swept(uw_opts, rows)
    uw_opts.far_chain = {"SLOW": [chain_row(r, oi=int(r["open_interest"]), prev_oi=90) for r in rows]}
    at(monkeypatch, MORNING)
    row = run()
    assert "UW not updated yet: SLOW" in row["note"] and not far.oi_path(SESSION, "SLOW").exists()
    assert query("SELECT DISTINCT oi_status FROM option_far_contract") == [("pending",)]
    uw_opts.far_chain = {"SLOW": [chain_row(r, oi=int(r["open_interest"]) + 50) for r in rows]}
    run()
    assert query("SELECT DISTINCT oi_status, oi_change FROM option_far_contract") == [("matched", 50)]


def test_a_chain_row_outside_its_filter_fails_that_ticker_not_the_run(uw_opts, monkeypatch):
    a, b = contract("AAA", "C", 100), contract("BBB", "C", 100)
    _swept(uw_opts, [a, b])
    near = {"option_symbol": occ("AAA", date(2027, 1, 15), "C", 100), "open_interest": "5", "prev_oi": "5"}
    uw_opts.far_chain = {"AAA": [chain_row(a, oi=200), near], "BBB": [chain_row(b, oi=300)]}
    at(monkeypatch, MORNING)
    row = run()
    assert row["result"] != "failed" and "failed: AAA" in row["note"]
    assert not far.oi_path(SESSION, "AAA").exists() and far.oi_path(SESSION, "BBB").exists()
    assert dict(query("SELECT ticker, oi_status FROM option_far_contract")) == {"AAA": "pending", "BBB": "matched"}


def test_a_refused_chain_is_not_stored_and_ends_stale_not_missing(uw_opts, monkeypatch):
    _swept(uw_opts, [contract("AAA", "C", 100)])
    uw_opts.fail["/stock/AAA/option-contracts"] = 404
    at(monkeypatch, MORNING)
    row = run()
    assert "failed: AAA" in row["note"] and not far.oi_path(SESSION, "AAA").exists()
    at(monkeypatch, AFTER_WINDOW)
    run()
    assert query("SELECT oi_status FROM option_far_contract") == [("stale",)]


def test_a_name_with_one_contract_uw_has_provably_not_updated_waits(uw_opts, monkeypatch):
    """Its chain row still has the sweep's open interest and previous open interest, which differ: not updated."""
    a = contract("ONE", "C", 100, oi=100, prev_oi=90)
    _swept(uw_opts, [a])
    uw_opts.far_chain = {"ONE": [chain_row(a, oi=100, prev_oi=90)]}
    at(monkeypatch, MORNING)
    assert "UW not updated yet: ONE" in run()["note"] and not far.oi_path(SESSION, "ONE").exists()
    uw_opts.far_chain = {"ONE": [chain_row(a, oi=400)]}
    run()
    assert query("SELECT oi_status, oi_change FROM option_far_contract") == [("matched", 300)]


def test_a_name_with_no_proof_either_way_waits_until_noon_then_is_taken_as_it_is(uw_opts, monkeypatch):
    """Open interest unchanged the day before and again on the session: the chain looks the same before and after
    UW's update, so the name waits until ``ACCEPT_FROM`` and is then matched with a change of 0. A chain holding
    none of the swept contracts is the same case, and ends ``missing``."""
    a = contract("FLAT", "C", 100, oi=100, prev_oi=100)
    b = contract("GONE", "C", 100)
    _swept(uw_opts, [a, b])
    uw_opts.far_chain = {"FLAT": [chain_row(a, oi=100)], "GONE": []}
    at(monkeypatch, MORNING)
    row = run()
    assert "UW not updated yet: FLAT, GONE" in row["note"]
    assert query("SELECT DISTINCT oi_status FROM option_far_contract") == [("pending",)]
    at(monkeypatch, datetime(2026, 10, 27, 16, 5, tzinfo=timezone.utc))     # 12:05 New York
    run()
    assert dict(query("SELECT ticker, oi_status FROM option_far_contract")) == {"FLAT": "matched", "GONE": "missing"}
    assert query("SELECT oi_change, opened_share FROM option_far_contract WHERE ticker = 'FLAT'") == [(0, 0.0)]


def test_a_failed_sweep_still_reads_the_chains_left_over(uw_opts, monkeypatch):
    a = contract("AAA", "C", 100, volume=1000, oi=100)
    _swept(uw_opts, [a])
    uw_opts.far_chain = {"AAA": [chain_row(a, oi=600)]}
    uw_opts.contract_screener = {"call": [], "put": []}                     # the next evening's sweep comes back empty
    at(monkeypatch, NEXT_EVENING)
    row = run()
    assert row["result"] == "failed" and "no far-dated contract" in row["note"]
    assert far.oi_path(SESSION, "AAA").exists(), "the chain was read before the run failed"


def test_no_sweep_after_uw_s_morning_update(uw_opts, monkeypatch):
    """After about 06:45 New York the screener's open interest is the next session's: the evening's sweep is over."""
    uw_opts.contract_screener["call"] = [contract("AAA", "C", 100)]
    base.parquet_root().mkdir(parents=True)
    at(monkeypatch, MORNING)
    row = run()
    assert uw_opts.requests == [] and "outside the sweep window" in row["note"]
    at(monkeypatch, datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc))     # 05:30 New York: still the evening's
    assert run()["result"] != "failed" and far.sweep_path(SESSION).exists()


def test_an_empty_sweep_fails_and_is_asked_again(uw_opts):
    row = run()
    assert row["result"] == "failed" and "no far-dated contract" in row["note"]
    assert not far.sweep_path(SESSION).exists()


def test_the_next_evening_reads_what_the_morning_missed_and_sweeps_its_own_session(uw_opts, monkeypatch):
    a = contract("AAA", "C", 100, volume=1000, oi=100)
    _swept(uw_opts, [a])
    uw_opts.far_chain = {"AAA": [chain_row(a, oi=900)]}
    at(monkeypatch, NEXT_EVENING)
    nxt = calendar.next_trading_day(SESSION)
    run()
    assert query("SELECT session_date, oi_status, opened_share FROM option_far_contract ORDER BY session_date") == [
        (SESSION, "matched", 0.8), (nxt, "pending", None)]


def test_after_the_window_an_unread_ticker_is_stale_and_the_session_is_closed(uw_opts, monkeypatch):
    _swept(uw_opts, [contract("AAA", "C", 100)])
    uw_opts.fail["/stock/AAA/option-contracts"] = 503
    at(monkeypatch, MORNING)
    row = run()
    assert "failed: AAA" in row["note"] and row["next_check_due"] == SESSION.isoformat()
    at(monkeypatch, AFTER_WINDOW)
    uw_opts.contract_screener = {"call": [], "put": []}
    run()
    assert far.closed_path(SESSION).exists()
    assert query("SELECT oi_status FROM option_far_contract WHERE session_date = ?", [SESSION]) == [("stale",)]


# ── The roll flag ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_roll_is_one_contract_closing_while_another_opens_on_the_same_name_side_and_day(uw_opts, monkeypatch):
    """Shaped on the NVDA case of the issue: about 3,000 sold in one far call while 3,000 were bought in another."""
    out = contract("NVDA", "C", 100, volume=3000, oi=15725, expiry=date(2027, 12, 17))   # 15,725 -> 14,567
    into = contract("NVDA", "C", 110, volume=3100, oi=130)                                 # 130 -> 3,000
    lone = contract("NVDA", "C", 150, volume=9000, oi=10)      # opens, but its volume is far from the others'
    put = contract("NVDA", "P", 80, volume=3000, oi=5000)       # closes, but on the other side
    _swept(uw_opts, [out, into, lone, put])
    uw_opts.far_chain = {"NVDA": [chain_row(out, oi=14567), chain_row(into, oi=3000), chain_row(lone, oi=9000),
                                  chain_row(put, oi=2500)]}
    at(monkeypatch, MORNING)
    run()
    got = {s: (roll, partner) for s, roll, partner in query(
        "SELECT option_symbol, roll, roll_partner FROM option_far_contract")}
    assert got == {out["option_symbol"]: (True, into["option_symbol"]),
                   into["option_symbol"]: (True, out["option_symbol"]),
                   lone["option_symbol"]: (False, None), put["option_symbol"]: (False, None)}


def test_the_roll_rule_on_its_own():
    assert (far.ROLL_MIN_SHARE, far.ROLL_VOLUME_RATIO) == (0.25, 1.25)

    def r(sym, share, volume, status="matched"):
        return {"option_symbol": sym, "ticker": "X", "option_type": "call", "oi_status": status,
                "opened_share": share, "volume": volume, "roll": None, "roll_partner": None}
    rows = [r("A", 0.9, 1000), r("B", -0.3, 1250), r("C", -0.6, 1251), r("D", -0.2, 1000),
            r("E", 0.9, 1000, status="pending")]
    far._rolls(rows)
    assert [(x["option_symbol"], x["roll"], x["roll_partner"]) for x in rows] == [
        ("A", True, "B"), ("B", True, "A"), ("C", False, None), ("D", False, None), ("E", None, None)]


# ── Reading it ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_read_api_serves_the_newest_session_and_the_collecting_counts(uw_opts, monkeypatch):
    assert read.far_contracts() == [] and read.far_contract_sessions() == []
    a, b = contract("AAA", "C", 100), contract("BBB", "C", 100)
    _swept(uw_opts, [a, b])
    uw_opts.far_chain = {"AAA": [chain_row(a, oi=500)], "BBB": []}
    at(monkeypatch, MORNING)
    run()
    rows = read.far_contracts(tickers=["aaa"])
    assert [(r["ticker"], r["oi_status"], r["opened_share"]) for r in rows] == [("AAA", "matched", 0.4)]
    sessions = read.far_contract_sessions()
    assert [s.pop("status") for s in sessions] == ["changed"], "the dataset's newest check comes with the rows"
    assert sessions == [{"session_date": SESSION, "contracts": 2, "tickers": 2, "matched": 1,
                         "pending": 1, "stale": 0, "missing": 0}], \
        "a chain holding none of the swept contracts waits for the morning's cut-off"

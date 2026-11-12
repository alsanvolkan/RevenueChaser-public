"""#375 F2 (13F-B, 13F-7): the 13F board's snapshot inputs come from the shared stores, not ``sec13f_enrichment``.

* shares outstanding (and so % Out) from ``ownership_snapshot``, in the readers' order: UW ``info``, then SEC dei,
  then yfinance; the newest session of the first source that has a value;
* Insider % from ``ownership_snapshot.insider_pct`` (a stored fraction, in %), read but not shown since #732;
* Price and Price Δ from D01 (``price_bars_daily``) through the K8a windowed read: the last close on or before
  each quarter end;
* a name with no row shows "—", never 0; Short% is neither read nor shown since #732 (owner, 12 Nov 2026);
* ``START_QUARTER`` is the engine's constant and the empty-state colspan is the header's column count.

Offline; temp stores.
"""
from __future__ import annotations

import re
from datetime import date

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import engine, enrich, report
from RevenueChaser.core.tests.d01_seed import write_d01
from RevenueChaser.rcdata.tests import prices_store

Q1, Q2 = "2025-03-31", "2025-06-30"


def _cell(shares):
    return {"shares": shares, "value": shares * 10.0, "holders": 2, "new_holders": 1, "exited_holders": 0,
            "top10_pct": 90.0, "hhi": 4000.0, "buy_flow": 1.0, "sell_flow": 0.0}


TOTALS = {"AAA": {Q1: _cell(100.0), Q2: _cell(200.0)},
          "BBB": {Q1: _cell(50.0), Q2: _cell(60.0)}}
META = {"AAA": {"name": "Alpha", "sector": "Technology"}, "BBB": {"name": "Bravo", "sector": "Energy"}}


def _own(ticker, source, session, shares=None, insider=None):
    return {"ticker": ticker, "session_date": date.fromisoformat(session), "source": source,
            "shares_outstanding": shares, "insider_pct": insider, "institutional_pct": None,
            "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def _bar(ticker, d, close):
    return {"ticker": ticker, "session_date": date.fromisoformat(d), "open": close, "high": close, "low": close,
            "close": close, "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None,
            "source": "uw", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def _short(ticker, session, si_float, source="uw_short_screener"):
    return {"ticker": ticker, "session_date": date.fromisoformat(session), "market_date": None,
            "short_interest": None, "si_float": si_float, "total_float": None, "days_to_cover": None,
            "fee_rate": None, "rebate_rate": None, "short_shares_available": None, "payload": {},
            "source": source, "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0}


def _cells(payload, ticker):
    for row in payload["rows"]:
        if row[0] == ticker:
            return dict(zip(payload["cols"], row[3]))
    raise AssertionError(f"{ticker} not in the payload")


# ── the ownership reader ────────────────────────────────────────────────────

_SHARES_SESSION = date(2026, 10, 1)


def _served_shares(ticker: str, *, outstanding: float | None = None, close: float = 10.0,
                   cap: float | None = None) -> None:
    """The served numbers the "% Out" denominator reads since #646 PR 1: one bar, and a cover-page count or a cap."""
    prices_store.put_bars([{"ticker": ticker, "session_date": _SHARES_SESSION, "close": close}], replace=False)
    con = prices_store._connect()
    try:
        sid = f"sym:{ticker}"
        if cap is not None:
            con.execute("INSERT INTO market_cap_daily (security_id, session_date, close, market_cap, "
                        "issuer_market_cap) VALUES (?, ?, ?, ?, ?)", [sid, _SHARES_SESSION, close, cap, cap])
        if outstanding is not None:
            con.execute("INSERT INTO shares_class (security_id, measure, as_of, value, symbol, source) "
                        "VALUES (?, 'outstanding', ?, ?, ?, 'sec/cover_shares')",
                        [sid, _SHARES_SESSION, outstanding, ticker])
        con.execute("CHECKPOINT")
    finally:
        con.close()


def test_pct_out_comes_from_the_cover_pages_count(rawstore_tmp_db):
    """#646 PR 1: the denominator is the served count, not ``ownership_snapshot``'s."""
    _served_shares("AAA", outstanding=1000.0)
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["shares_outstanding"] == 1000.0
    assert inputs["AAA"]["shares_basis"] == "stored"
    cells = _cells(report.board_payload(TOTALS, META, inputs), "AAA")
    assert cells["pct_out"][:3] == ["20.0%", pytest.approx(20.0), "zero"]       # 200 of 1,000 shares
    assert "cover page" in cells["pct_out"][3]


def test_without_a_count_the_cell_says_the_number_is_implied(rawstore_tmp_db):
    _served_shares("AAA", close=10.0, cap=10_000.0)               # 1,000 shares implied
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["shares_outstanding"] == pytest.approx(1000.0)
    assert inputs["AAA"]["shares_basis"] == "market_cap/price"
    cells = _cells(report.board_payload(TOTALS, META, inputs), "AAA")
    assert "implied" in cells["pct_out"][3]


def test_the_old_snapshot_no_longer_feeds_the_denominator(rawstore_tmp_db):
    """It still carries insider %, but a shares row there is not read any more (#646 PR 1)."""
    rawstore.write_rows("ownership_snapshot", [_own("AAA", "uw_info", "2026-10-08", shares=1000.0)])
    assert "shares_outstanding" not in enrich.load_inputs(TOTALS).get("AAA", {})


def test_insider_pct_is_the_stored_fraction_in_percent(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [
        _own("AAA", "uw_info", "2026-10-08", shares=1000.0),
        _own("AAA", "yfinance", "2026-10-09", shares=4000.0, insider=0.014)])
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["insider_pct"] == pytest.approx(1.4)
    assert "insider" not in _cells(report.board_payload(TOTALS, META, inputs), "AAA")     # not shown (#732)


def test_a_ticker_outside_the_totals_is_not_read_into_the_inputs(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [_own("ZZZ", "uw_info", "2026-10-08", shares=5.0)])
    assert "ZZZ" not in enrich.load_inputs(TOTALS)


# ── the D01 price reader ────────────────────────────────────────────────────

def test_price_is_the_last_d01_close_on_or_before_each_quarter_end(rawstore_tmp_db):
    write_d01([
        _bar("AAA", "2025-03-27", 9.0), _bar("AAA", "2025-03-28", 10.0),     # Fri before Q1 (Mon 03-31 missing)
        _bar("AAA", "2025-06-27", 11.0), _bar("AAA", "2025-06-30", 12.0),
        _bar("AAA", "2025-07-01", 99.0)])                                    # after the quarter: never read
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["price"] == {Q1: 10.0, Q2: 12.0}
    cells = _cells(report.board_payload(TOTALS, META, inputs), "AAA")
    assert cells["price_delta"] == ["+20.0%", pytest.approx(20.0), "pos"]


def test_a_close_older_than_the_lookback_is_no_quarter_end_price(rawstore_tmp_db):
    write_d01([_bar("AAA", "2025-03-28", 10.0), _bar("AAA", "2025-05-01", 11.0)])
    assert enrich.load_inputs(TOTALS)["AAA"]["price"] == {Q1: 10.0}


def test_a_split_after_the_quarter_end_divides_a_close_fetched_before_it(rawstore_tmp_db):
    """The nominal series is ``prices.daily_bars``': a 2:1 split on 2025-05-01 halves the Q1 close fetched in March."""
    from RevenueChaser.core import prices
    write_d01([_bar("AAA", "2025-03-31", 20.0), _bar("AAA", "2025-06-30", 12.0)])
    write_d01([], [{"ticker": "AAA", "ex_date": date(2025, 5, 1), "action": "split",
                                           "value": 2.0, "source": "yfinance", "fetched_at": 0.0,
                                           "run_id": "seed", "run_at": 0.0}])
    closes = enrich.load_inputs(TOTALS)["AAA"]["price"]
    assert closes == {Q1: 10.0, Q2: 12.0}
    full = prices.daily_bars(["AAA"], "nominal")
    assert closes[Q1] == float(full.loc[full["session_date"] == "2025-03-31", "close"].iloc[0])


def test_the_price_read_is_bounded_to_the_quarter_end_windows(rawstore_tmp_db, monkeypatch):
    """Only the bars inside the quarter-end windows are read, never the whole D01 history of the 13F universe."""
    from RevenueChaser.core import prices
    write_d01([_bar("AAA", d, 10.0) for d in
                                             ("2025-03-28", "2025-04-15", "2025-06-30", "2025-08-01", "2026-10-01")])
    seen = []
    real = prices.apply_events
    def spy(bars, actions, basis):
        seen.append((sorted(str(b["session_date"]) for b in bars), basis))
        return real(bars, actions, basis)

    monkeypatch.setattr(prices, "apply_events", spy)
    enrich.load_inputs(TOTALS)
    assert seen == [(["2025-03-28", "2025-06-30"], "nominal")]


# ── the board ───────────────────────────────────────────────────────────────

def test_a_name_with_no_row_shows_a_dash_never_zero(rawstore_tmp_db):
    inputs = enrich.load_inputs(TOTALS)
    cells = _cells(report.board_payload(TOTALS, META, inputs), "BBB")
    for col in ("pct_out", "price_delta"):
        assert cells[col][0] == "—" and cells[col][1] is None, col


# ── Short % and Insider % are off the board (#732, owner 12 Nov 2026: both read old tables) ──────────────────

def test_the_board_reads_no_short_interest_and_shows_neither_column(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("short_interest", [_short("AAA", "2026-10-15", 0.052)])

    def refuse(tickers):
        raise AssertionError("the 13F board read short_interest")

    monkeypatch.setattr(enrich, "load_short_interest", refuse)
    inputs = enrich.load_inputs(TOTALS)
    assert "short_pct" not in inputs.get("AAA", {})
    payload = report.board_payload(TOTALS, META, inputs)
    assert "short" not in payload["cols"] and "insider" not in payload["cols"]
    html = report.render_sec13f_dashboard(TOTALS, META, inputs)
    assert ">Short%<" not in html and ">Insider%<" not in html and "short interest as % of float" not in html


def test_the_board_quarters_start_at_the_engines_start_quarter(monkeypatch):
    monkeypatch.setattr(engine, "START_QUARTER", Q2)
    payload = report.board_payload(TOTALS, META, {})
    assert [c for c in payload["cols"] if c.startswith("sh_")] == [f"sh_{Q2}"]


def _header_columns(html: str) -> int:
    second = html.split("</tr><tr>", 1)[1].split("</tr>", 1)[0]
    return 2 + second.count("<th ")                 # Ticker and Sector span both header rows


@pytest.mark.parametrize("totals", [TOTALS, {}], ids=["rows", "empty"])
def test_the_placeholder_row_spans_every_header_column(totals):
    html = report.render_sec13f_dashboard(totals, META, {})
    (span,) = re.findall(r'<td colspan="(\d+)" class="empty">', html)
    assert int(span) == _header_columns(html)
    assert 'colspan="40"' not in html and "'40'" not in html

"""#375 F2 (13F-B, 13F-7): the 13F board's snapshot inputs come from the shared stores, not ``sec13f_enrichment``.

* shares outstanding (and so % Out) from ``ownership_snapshot``, in the readers' order: UW ``info``, then SEC dei,
  then yfinance; the newest session of the first source that has a value;
* Insider % from ``ownership_snapshot.insider_pct`` (a stored fraction, shown in %);
* Price and Price Δ from D01 (``price_bars_daily``) through the K8a windowed read: the last close on or before
  each quarter end;
* a name with no row shows "—", never 0; the Short% column is hidden until D10 (F6);
* ``START_QUARTER`` is the engine's constant and the empty-state colspan is the header's column count.

Offline; temp stores.
"""
from __future__ import annotations

import re
from datetime import date

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import engine, enrich, report

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

def test_pct_out_comes_from_a_uw_info_row(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [_own("AAA", "uw_info", "2026-10-08", shares=1000.0)])
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["shares_outstanding"] == 1000.0
    cells = _cells(report.board_payload(TOTALS, META, inputs), "AAA")
    assert cells["pct_out"] == ["20.0%", pytest.approx(20.0), "zero"]      # 200 of 1,000 shares


def test_uw_wins_over_yfinance_and_yfinance_fills_a_name_uw_lacks(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [
        _own("AAA", "yfinance", "2026-10-09", shares=4000.0, insider=0.014),
        _own("AAA", "uw_info", "2026-10-08", shares=1000.0),
        _own("BBB", "yfinance", "2026-10-09", shares=600.0, insider=0.5)])
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["shares_outstanding"] == 1000.0           # UW first, even with a newer yfinance row
    assert inputs["BBB"]["shares_outstanding"] == 600.0            # no UW row: yfinance
    assert enrich.SHARES_ORDER == ("uw_info", enrich.SEC_DEI, "yfinance")


def test_the_newest_session_of_a_source_wins(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [
        _own("AAA", "uw_info", "2026-10-07", shares=900.0),
        _own("AAA", "uw_info", "2026-10-08", shares=1000.0),
        _own("AAA", "uw_info", "2026-10-09")])                     # no shares that session: not a value
    assert enrich.load_inputs(TOTALS)["AAA"]["shares_outstanding"] == 1000.0


def test_insider_pct_is_the_stored_fraction_in_percent(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [
        _own("AAA", "uw_info", "2026-10-08", shares=1000.0),
        _own("AAA", "yfinance", "2026-10-09", shares=4000.0, insider=0.014)])
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["insider_pct"] == pytest.approx(1.4)
    assert _cells(report.board_payload(TOTALS, META, inputs), "AAA")["insider"][:2] == ["1.4%", pytest.approx(1.4)]


def test_a_ticker_outside_the_totals_is_not_read_into_the_inputs(rawstore_tmp_db):
    rawstore.write_rows("ownership_snapshot", [_own("ZZZ", "uw_info", "2026-10-08", shares=5.0)])
    assert "ZZZ" not in enrich.load_inputs(TOTALS)


# ── the D01 price reader ────────────────────────────────────────────────────

def test_price_is_the_last_d01_close_on_or_before_each_quarter_end(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [
        _bar("AAA", "2025-03-27", 9.0), _bar("AAA", "2025-03-28", 10.0),     # Fri before Q1 (Mon 03-31 missing)
        _bar("AAA", "2025-06-27", 11.0), _bar("AAA", "2025-06-30", 12.0),
        _bar("AAA", "2025-07-01", 99.0)])                                    # after the quarter: never read
    inputs = enrich.load_inputs(TOTALS)
    assert inputs["AAA"]["price"] == {Q1: 10.0, Q2: 12.0}
    cells = _cells(report.board_payload(TOTALS, META, inputs), "AAA")
    assert cells["price_delta"] == ["+20.0%", pytest.approx(20.0), "pos"]


def test_a_close_older_than_the_lookback_is_no_quarter_end_price(rawstore_tmp_db):
    rawstore.write_rows("price_bars_daily", [_bar("AAA", "2025-03-28", 10.0), _bar("AAA", "2025-05-01", 11.0)])
    assert enrich.load_inputs(TOTALS)["AAA"]["price"] == {Q1: 10.0}


def test_a_split_after_the_quarter_end_divides_a_close_fetched_before_it(rawstore_tmp_db):
    """The nominal series is ``prices.daily_bars``': a 2:1 split on 2025-05-01 halves the Q1 close fetched in March."""
    from RevenueChaser.core import prices
    rawstore.write_rows("price_bars_daily", [_bar("AAA", "2025-03-31", 20.0), _bar("AAA", "2025-06-30", 12.0)])
    rawstore.write_rows("price_actions", [{"ticker": "AAA", "ex_date": date(2025, 5, 1), "action": "split",
                                           "value": 2.0, "source": "yfinance", "fetched_at": 0.0,
                                           "run_id": "seed", "run_at": 0.0}])
    closes = enrich.load_inputs(TOTALS)["AAA"]["price"]
    assert closes == {Q1: 10.0, Q2: 12.0}
    full = prices.daily_bars(["AAA"], "nominal")
    assert closes[Q1] == float(full.loc[full["session_date"] == "2025-03-31", "close"].iloc[0])


def test_the_price_read_is_bounded_to_the_quarter_end_windows(rawstore_tmp_db, monkeypatch):
    """Only the bars inside the quarter-end windows are read, never the whole D01 history of the 13F universe."""
    from RevenueChaser.core import prices
    rawstore.write_rows("price_bars_daily", [_bar("AAA", d, 10.0) for d in
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
    for col in ("pct_out", "insider", "short", "price_delta"):
        assert cells[col][0] == "—" and cells[col][1] is None, col


# ── the old D10 short-interest table is not read (G9) ─────────────────────

def test_the_old_short_interest_table_is_not_read(rawstore_tmp_db):
    """Since G9 the Short column is FINRA's short interest from the ownership store (``store_reads``); a row in the
    old ``short_interest`` table (D10) neither fills it nor reaches the inputs."""
    rawstore.write_rows("short_interest", [_short("AAA", "2026-10-15", 0.052)])
    inputs = enrich.load_inputs(TOTALS)
    assert "short_pct" not in inputs.get("AAA", {})
    cells = _cells(report.board_payload(TOTALS, META, inputs), "AAA")
    assert cells["short"][0] == "—" and cells["short"][1] is None
    html = report.render_sec13f_dashboard(TOTALS, META, inputs)
    assert ">Short int<" in html and ">Short%<" not in html


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

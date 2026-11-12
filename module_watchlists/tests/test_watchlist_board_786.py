"""The Watch lists board: the payload the page is drawn from, and the search behind the add box (#786).

The numbers come from the rcdata layer, so every test here stubs ``rcdata.read`` rather than building a serving
file: what is under test is which number lands in which key, and that a symbol with no source keeps its row.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.module_watchlists import board, store

pytestmark = pytest.mark.rawstore_schema_from_scratch


def _bars(rows: list[tuple]):
    """A ``ticker_bars``-shaped frame: ``(ticker, session_date, close, volume)`` tuples."""
    import pandas as pd
    return pd.DataFrame([{"ticker": t, "session_date": pd.Timestamp(d), "close": c, "volume": v}
                         for t, d, c, v in rows])


@pytest.fixture
def no_data(monkeypatch):
    """Every rcdata read answers empty, so a test opts in to the one it is about."""
    import pandas as pd
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: pd.DataFrame(
        columns=["ticker", "session_date", "close", "volume"]))
    monkeypatch.setattr(board.read, "latest_market", lambda *a, **k: {})
    monkeypatch.setattr(board.read, "market_series", lambda *a, **k: [])
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [])


# ── the payload ──────────────────────────────────────────────────────────────

def test_no_list_at_all_is_no_payload_so_the_tab_can_show_its_empty_page(no_data):
    assert board.payload() is None


def test_a_us_listing_carries_its_close_change_volume_and_cap(monkeypatch, no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    today = date.today()
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: _bars([
        ("NVDA", today - timedelta(days=2), 100.0, 1_000_000.0),
        ("NVDA", today - timedelta(days=1), 110.0, 3_000_000.0),
    ]))
    monkeypatch.setattr(board.read, "latest_market",
                        lambda *a, **k: {"NVDA": {"price": 110.0, "market_cap": 4.19e12}})
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [{"ticker": "NVDA", "name": "NVIDIA Corporation"}])
    row = board.payload(list_id)["sections"][0]["rows"][0]
    assert row["t"] == "NVDA" and row["nm"] == "NVIDIA Corporation"
    assert row["last"] == 110.0
    assert row["chg"] == pytest.approx(10.0)
    assert row["chgp"] == pytest.approx(10.0)
    assert row["vol"] == 3_000_000.0
    assert row["avol"] == pytest.approx(2_000_000.0)       # the window's mean, not the last session
    assert row["cap"] == 4.19e12


def test_one_bar_is_a_price_with_no_change_rather_than_a_change_of_zero(monkeypatch, no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _bars([("NVDA", date.today(), 110.0, 5.0)]))
    row = board.payload(list_id)["sections"][0]["rows"][0]
    assert row["last"] == 110.0
    assert row["chg"] is None and row["chgp"] is None


def test_a_symbol_we_have_no_source_for_keeps_its_row_and_shows_no_number(no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "BIST:THYAO")
    data = board.payload(list_id)
    row = data["sections"][0]["rows"][0]
    assert row["cov"] == "unsupported"
    assert (row["last"], row["chg"], row["vol"], row["cap"]) == (None, None, None, None)
    assert data["counts"]["unsupported"] == 1


def test_a_market_series_shows_its_level_and_change_and_a_yield_says_percent(monkeypatch, no_data):
    list_id = store.create_list("Macro")
    store.add_symbol(list_id, "TVC:US10Y")
    monkeypatch.setattr(board.read, "market_series", lambda *a, **k: [
        {"series_id": "DGS10", "obs_date": date(2026, 11, 10), "value": 4.13},
        {"series_id": "DGS10", "obs_date": date(2026, 11, 11), "value": 4.11},
    ])
    row = board.payload(list_id)["sections"][0]["rows"][0]
    assert row["last"] == 4.11
    assert row["chg"] == pytest.approx(-0.02)
    assert row["unit"] == "%"
    assert row["vol"] is None and row["cap"] is None       # a series has neither


def test_the_payload_walks_sections_in_avos_order_not_the_symbols_flat(no_data):
    list_id = store.create_list("Growth")
    chips = store.add_section(list_id, "Chips")
    cloud = store.add_section(list_id, "Cloud")
    store.add_symbol(list_id, "NASDAQ:MSFT", section_id=cloud)
    store.add_symbol(list_id, "NASDAQ:NVDA", section_id=chips)
    store.reorder_sections(list_id, [cloud, chips])
    named = [s for s in board.payload(list_id)["sections"] if s["name"]]
    assert [s["name"] for s in named] == ["Cloud", "Chips"]
    assert [row["t"] for s in named for row in s["rows"]] == ["MSFT", "NVDA"]


def test_an_unknown_list_id_opens_the_first_list_instead_of_failing(no_data):
    first = store.create_list("Growth")
    store.create_list("Dividends")
    assert board.payload("no-such-list")["list"]["id"] == first


# ── the add box's search ─────────────────────────────────────────────────────

def test_a_ticker_that_starts_with_the_query_is_offered_before_one_that_only_holds_it(monkeypatch):
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [
        {"ticker": "ACRD", "name": "Accord Inc", "exchange": "NYSE"},
        {"ticker": "CRDO", "name": "Credo Technology", "exchange": "NASDAQ"},
        {"ticker": "XYZ", "name": "CRD Holdings", "exchange": "NYSE"},
    ])
    assert [r["ticker"] for r in board.search("CRD")] == ["CRDO", "ACRD", "XYZ"]


def test_a_suggestion_is_written_the_way_a_watch_list_spells_it(monkeypatch):
    monkeypatch.setattr(board.read, "identity",
                        lambda *a, **k: [{"ticker": "NVDA", "name": "NVIDIA", "exchange": "NASDAQ"}])
    assert board.search("NVDA")[0]["symbol"] == "NASDAQ:NVDA"


def test_an_empty_query_reads_the_spine_for_nothing(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("an empty query must not read the spine")
    monkeypatch.setattr(board.read, "identity", refuse)
    assert board.search("  ") == []


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_page_says_the_unit_once_in_the_header_and_never_on_a_row(monkeypatch, no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _bars([("NVDA", date.today(), 110.0, 5.0)]))
    html = board.render_html(board.payload(list_id))
    # The header follows the open tab, so the browser draws it from this column set rather than the server.
    assert [c[0] for c in board.COLUMNS["price"] if c[2] == "USD"] == ["last", "chg", "cap"]
    assert '"unit": "USD"' in html or '"unit":"USD"' in html, "the unit reaches the page in the column set"
    assert '<span class="wl-u">USD</span>' not in html, "a row never repeats the unit"


def test_the_remove_button_is_the_rows_last_cell_and_not_part_of_the_symbol(no_data):
    """avo, 12 Nov 2026: "Listeden cikarma dugmesini en saga koy" — the × is a column of its own at the far
    right, not a mark crowding the ticker, and the header carries a cell above it so the grid stays square."""
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    js = board.render_html(board.payload(list_id))
    assert "</span></div></td>' + cells +" in js, "the symbol cell closes before the numbers, with no button in it"
    assert "'<td class=\"wl-xc\"><button class=\"wl-x\"" in js, "the × is the cell after the numbers"
    assert "+ '<th class=\"wl-xc\">" in js, "the header ends with its own cell"
    assert "(cols().length + 1)" in js, "a section heading spans the × column too"


def test_the_phone_rule_fits_every_column_rather_than_scrolling_sideways():
    phone = board._CSS.split("@media (max-width:720px)", 1)[1]
    assert "overflow-x" not in phone, "the phone shows every column; nothing scrolls sideways"
    assert ".wl-grip{display:none}" in phone, "the drag handle is for the mouse"
    assert ".wl-nm{display:block;max-width:70px" in phone, "the company name stays, as a capped second line"
    assert ".wl-badge{min-width:0;max-width:70px;overflow:hidden;text-overflow:ellipsis" in phone, \
        "a long symbol (FRED:BAMLH0A0HYM2) is cut at the name's width, not left to widen the table"
    assert ".wl td.wl-xc{padding:0 2px}" in phone, "the × column takes no more than its glyph and two pixels a side"


def test_the_hidden_remove_label_is_pinned_inside_its_own_header_cell():
    """Live on 12 Nov 2026 the 1 px "Remove" label sat at 392 px on a 390 px phone and made the page scroll
    sideways. Pinned to its cell (``position:relative`` on the cell, ``left:0`` on the label) it cannot."""
    assert ".wl th.wl-xc{padding:0;position:relative}" in board._CSS
    assert ".wl .sr{position:absolute;left:0;top:0;" in board._CSS


# ── the derived tabs (#786 step 4b) ──────────────────────────────────────────

def _series(ticker: str, closes: list[float], start: date):
    """One bar a day from ``start`` for one ticker."""
    return [(ticker, start + timedelta(days=i), c, 1_000_000.0) for i, c in enumerate(closes)]


def _frame_with_ranges(rows: list[tuple]):
    """A ``ticker_bars`` frame that also carries ``high`` and ``low`` — what the risk tab reads."""
    import pandas as pd
    return pd.DataFrame([{"ticker": t, "session_date": pd.Timestamp(d), "close": c,
                          "high": c * 1.02, "low": c * 0.98, "volume": v} for t, d, c, v in rows])


@pytest.fixture
def a_long_list():
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    store.add_symbol(list_id, "TVC:VIX")
    store.add_symbol(list_id, "BIST:THYAO")
    return list_id


def test_an_unknown_list_or_a_tab_that_is_not_derived_is_no_answer(no_data, a_long_list):
    assert board.columns(a_long_list, "price") is None, "Price rides the first payload, not this route"
    assert board.columns("no-such-list", "performance") is None


def test_performance_gives_every_window_and_none_where_the_history_is_too_short(monkeypatch, no_data, a_long_list):
    start = date(2026, 1, 2)
    closes = [100.0 + i for i in range(300)]                       # 300 sessions: a 1Y window, not a 5Y one
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _frame_with_ranges(_series("NVDA", closes, start)))
    values = board.columns(a_long_list, "performance")["values"]
    row = values["NASDAQ:NVDA"]
    assert row["p1W"] == pytest.approx((closes[-1] - closes[-6]) / closes[-6] * 100)
    assert row["p1Y"] is not None
    assert row["p5Y"] is None, "a name with 300 sessions has no 5-year number"
    assert "pYTD" in row


def test_only_a_us_listing_gets_derived_numbers(monkeypatch, no_data, a_long_list):
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _frame_with_ranges(_series("NVDA", [100.0 + i for i in range(60)],
                                                                   date(2026, 1, 2))))
    values = board.columns(a_long_list, "technicals")["values"]
    assert set(values) == {"NASDAQ:NVDA"}, "a market series is not a bar series, and #785 has no source at all"


def test_the_technicals_tab_carries_the_three_ratings_with_their_words(monkeypatch, no_data, a_long_list):
    rising = [100.0 * (1.004 ** i) for i in range(300)]
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _frame_with_ranges(_series("NVDA", rising, date(2026, 1, 2))))
    row = board.columns(a_long_list, "technicals")["values"]["NASDAQ:NVDA"]
    assert row["tech_label"] in ("strong_buy", "buy"), "a series that only rises is not a sell"
    assert row["ma_label"] and row["os_label"]
    assert row["rsi"] is not None and row["mom"] is not None


def test_beta_is_measured_over_the_sessions_the_two_series_share(monkeypatch, no_data, a_long_list):
    """The name trades every second day of the index's calendar.

    Paired by position its returns would sit against the index's returns of other days, which is not a beta.
    Aligned, the two move together on every shared session, so the beta is 1.
    """
    start = date(2026, 1, 2)
    spy_closes, nvda_closes = [100.0], [50.0]
    for i in range(1, 700):
        step = 1.01 if i % 3 else 0.99
        spy_closes.append(spy_closes[-1] * step)
        nvda_closes.append(nvda_closes[-1] * step)                 # same move, so an aligned beta is 1
    spy = _series("SPY", spy_closes, start)
    nvda = [row for i, row in enumerate(_series("NVDA", nvda_closes, start)) if i % 2 == 0]
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: _frame_with_ranges(spy + nvda))
    row = board.columns(a_long_list, "risk")["values"]["NASDAQ:NVDA"]
    assert row["b1Y"] == pytest.approx(1.0, abs=1e-6)
    assert row["v1D"] == pytest.approx((1.02 - 0.98) / 0.98 * 100, rel=1e-6)

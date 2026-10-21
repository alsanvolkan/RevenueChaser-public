"""S&P 1500 GICS snapshot (Wikipedia) and the diff gate."""
from __future__ import annotations

import httpx

from RevenueChaser.core.identity.sources import sp1500
from RevenueChaser.core.identity.tests.helpers import fixture_text


def route(http):
    http.add("List_of_S%26P_500_companies", fixture_text("sp500.html"))
    http.add("List_of_S%26P_400_companies", fixture_text("sp400.html"))
    http.add("List_of_S%26P_600_companies", fixture_text("sp600.html"))


def test_snapshot_happy_path(http):
    route(http)
    r = sp1500.snapshot()
    assert r.source == "sp1500" and r.status == "ok"
    by = {x["ticker"]: x for x in r.rows}
    assert by["AAPL"]["sector"] == "Information Technology" and by["AAPL"]["index"] == "sp500"
    assert by["AAPL"]["sub_industry"] == "Technology Hardware, Storage & Peripherals"
    assert by["OZK"]["index"] == "sp400" and by["OZK"]["sector"] == "Financials"
    assert by["AAMI"]["index"] == "sp600"
    assert "BRK-B" in by and "BRK.B" not in by
    assert by["AAPL"]["name"] == "Apple Inc."
    assert len(r.rows) == 7


def test_snapshot_reads_the_real_table_shape_with_links_and_footnotes(http):
    page = ('<table><tr><th><a>GICS</a> Sector</th><th>Symbol</th><th>GICS Sub-Industry</th></tr>'
            '<tr><td>Financials[1]</td><td><a href="x">BRK.B</a></td>'
            '<td><a>Multi-Sector Holdings</a></td></tr></table>')
    http.add("wiki/List_of_S%26P_500", page)
    http.add("wiki/List_of_S%26P_400", page.replace("BRK.B", "OZK"))
    http.add("wiki/List_of_S%26P_600", page.replace("BRK.B", "TOWN"))
    r = sp1500.snapshot()
    assert r.status == "ok"
    row = next(x for x in r.rows if x["ticker"] == "BRK-B")
    assert row["sector"] == "Financials" and row["sub_industry"] == "Multi-Sector Holdings"


def test_default_base_is_wikipedia_and_env_override_is_honoured(http, monkeypatch):
    route(http)
    sp1500.snapshot()
    assert http.calls[0]["url"] == "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    monkeypatch.setenv("RC_WIKIPEDIA_BASE", "http://127.0.0.1:9/")
    http.calls.clear()
    sp1500.snapshot()
    assert http.calls[0]["url"] == "http://127.0.0.1:9/wiki/List_of_S%26P_500_companies"


def test_a_page_without_the_gics_table_is_an_error(http):
    route(http)
    http.routes.insert(0, ("List_of_S%26P_400", "<html><table><tr><th>Other</th></tr></table></html>", 200, {}))
    r = sp1500.snapshot()
    assert r.status == "error" and "sp400" in r.error


def test_5xx_and_timeout_are_errors(http):
    http.add("wikipedia", {}, status=503)
    r = sp1500.snapshot()
    assert r.status == "error" and "503" in r.error
    http.routes.clear()
    http.add("wikipedia", httpx.ReadTimeout("slow"))
    assert sp1500.snapshot().status == "error"


# -- diff gate ----------------------------------------------------------------
def _book(n, changed=0):
    prev = [{"ticker": f"T{i}", "sector": "Financials"} for i in range(n)]
    new = [{"ticker": f"T{i}", "sector": "Industrials" if i < changed else "Financials"} for i in range(n)]
    return prev, new


def test_diff_gate_accepts_two_percent():
    prev, new = _book(100, 2)
    accept, share, changed = sp1500.diff_gate(prev, new)
    assert accept is True and share == 0.02
    assert [c["ticker"] for c in changed] == ["T0", "T1"]
    assert changed[0] == {"ticker": "T0", "old_sector": "Financials", "new_sector": "Industrials"}


def test_diff_gate_rejects_four_percent():
    prev, new = _book(100, 4)
    accept, share, changed = sp1500.diff_gate(prev, new)
    assert accept is False and share == 0.04 and len(changed) == 4


def test_diff_gate_boundary_three_percent_is_accepted():
    prev, new = _book(100, 3)
    assert sp1500.diff_gate(prev, new)[0] is True


def test_diff_gate_threshold_is_a_parameter():
    prev, new = _book(100, 4)
    assert sp1500.diff_gate(prev, new, threshold=0.05)[0] is True


def test_diff_gate_only_counts_tickers_present_in_both():
    prev, new = _book(100, 2)
    new += [{"ticker": f"N{i}", "sector": "Energy"} for i in range(50)]     # additions
    prev = prev[:90]                                                        # removals
    accept, share, _ = sp1500.diff_gate(prev, new)
    assert accept is True and share == round(2 / 90, 6)


def test_diff_gate_first_run_has_nothing_to_compare():
    accept, share, changed = sp1500.diff_gate([], [{"ticker": "A", "sector": "Energy"}])
    assert accept is True and share == 0.0 and changed == []


def test_diff_gate_rejects_an_empty_new_snapshot_over_a_real_one():
    prev, _ = _book(100)
    accept, _, _ = sp1500.diff_gate(prev, [])
    assert accept is False

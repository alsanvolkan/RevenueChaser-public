"""#760: the 13F board's "Top holdings by investor type": the stacked-share figure, its route and its section.

The rows are ``sec13f_type_holdings``' (``rcdata/tests/test_ownership_serve.py`` builds them from filings); here
they are given, because what is tested is what the board draws from them.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import ownership
from RevenueChaser.core.reporting_common import THEME_SCALE
from RevenueChaser.module_sec_13f import report

Q2, Q3 = "2026-06-30", "2026-09-30"


def _row(period: str, ticker: str | None, share: float, rank: int | None = None, **kw) -> dict:
    return {"tag": "value_investor", "period": period, "ticker": ticker, "rank": rank, "share": share,
            "value": share * 1e7, "holders": 1 if ticker else None, "type_value": 1e9, "type_filers": 42,
            "snapshot_date": "2026-11-12", **kw}


def _rows() -> list[dict]:
    """Eight names over two quarters: H is the largest only in Q2 and gone from Q3's top, G and F are the smallest."""
    q2 = [_row(Q2, t, s, i + 1) for i, (t, s) in enumerate(
        [("H", 30.0), ("A", 20.0), ("B", 10.0), ("C", 8.0), ("D", 6.0), ("E", 5.0), ("G", 1.0)])]
    q3 = [_row(Q3, t, s, i + 1) for i, (t, s) in enumerate(
        [("A", 40.0), ("B", 12.0), ("C", 9.0), ("D", 7.0), ("E", 6.0), ("F", 2.0)])]
    return q2 + [_row(Q2, None, 20.0)] + q3 + [_row(Q3, None, 24.0)]


def test_the_figure_stacks_the_newest_quarters_six_largest_names_and_the_rest_as_other():
    fig, note = report.build_type_fig("Value", _rows())
    traces = fig["data"]
    # Ranked by the newest quarter's share: H, the largest in Q2 and out of Q3's top, is in Other, not a series.
    assert [t["name"] for t in traces] == ["A", "B", "C", "D", "E", "F", "Other"]
    assert all(t["stackgroup"] == "one" for t in traces)
    by = {t["name"]: list(t["y"]) for t in traces}
    assert by["F"] == [0.0, 2.0] and by["A"] == [20.0, 40.0]
    # Other is what the six leave of 100 in each quarter (H, G and the stored rest in Q2; the stored rest in Q3).
    assert by["Other"] == pytest.approx([51.0, 24.0])
    assert [sum(v[i] for v in by.values()) for i in range(2)] == pytest.approx([100.0, 100.0])
    # The colour follows the name in the ramp's fixed order, never cycled; Other is neutral.
    assert [t["line"]["color"] for t in traces[:6]] == [THEME_SCALE[f"c{i}"] for i in range(1, 7)]
    assert list(fig["layout"]["yaxis"]["range"]) == [0, 100]
    assert "42 filers" in note and "2026-11-12" in note and "Q3" in note


def test_a_quarter_still_arriving_is_marked_on_the_axis_and_named_in_the_note():
    fig, note = report.build_type_fig("Value", _rows(), still_arriving={Q3})
    assert list(fig["data"][0]["x"]) == [report._qlabel(Q2), report._qlabel(Q3) + report.ARRIVING_MARK]
    assert "still arriving" in note


def test_a_type_with_no_rows_draws_nothing_and_says_so():
    fig, note = report.build_type_fig("Credit", [])
    assert fig is None and "No 13F filer of type Credit" in note


def test_the_route_answers_a_type_the_board_offers_and_refuses_another(monkeypatch):
    from RevenueChaser.app import dashboard
    asked = []
    monkeypatch.setattr(ownership, "sec13f_type_holdings", lambda tag: asked.append(tag) or _rows())
    monkeypatch.setattr(ownership, "sec13f_open_periods", lambda: set())
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    r = client.get("/api/sec_13f/types/Value_Investor")
    assert r.status_code == 200 and asked == ["value_investor"]
    body = r.get_json()
    assert body["ok"] and body["label"] == "Value" and body["fig"]["data"][0]["name"] == "A"
    assert client.get("/api/sec_13f/types/no_such_type").status_code == 404
    assert asked == ["value_investor"]


def test_the_reader_gives_no_rows_when_the_store_cannot_be_read(monkeypatch):
    from RevenueChaser.rcdata import read as rcread

    def broken(*a, **kw):
        raise OSError("no ownership store")
    monkeypatch.setattr(rcread, "type_holdings_13f", broken)
    assert ownership.sec13f_type_holdings("energy") == []


def test_the_board_carries_the_collapsed_section_with_every_type_and_value_picked():
    page = report.render_sec13f_dashboard({}, {})
    assert '<details class="types" id="types">' in page
    for tag, label in ownership.SEC13F_TYPES:
        assert f'value="{tag}"' in page and f">{label}</option>" in page
    assert '<option value="value_investor" selected>Value</option>' in page
    assert "/api/sec_13f/types/" in page

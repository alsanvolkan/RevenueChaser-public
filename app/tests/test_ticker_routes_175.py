"""The three routes that take a ticker from the URL validate it (ADR-X1, #175).

``/api/ticker/<t>``, ``/api/sec_13f/<t>`` and the Rotation holdings fragment accepted any
string. They now normalise first (``strip().upper()``, as ``api_settings_ticker`` does) and
then apply ``_valid_ticker``, so ``aapl`` still works and a payload is a 400 that never echoes it.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

HOSTILE = ["<img src=x onerror=alert(1)>", "\"'><b>x</b>", "AAPL/../../x", "A B", ".", "-",
           "ABCDEFGHIJK", "a;b", "AAPL%00"]

# (url template, True when the ticker is a path segment rather than a query value)
ROUTES = [
    ("/api/ticker/{t}", True),
    ("/api/sec_13f/{t}", True),
    ("/view/rotation/holdings?ticker={t}", False),
]


@pytest.fixture
def client(rawstore_tmp_db):
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def _get(client, template: str, ticker: str):
    return client.get(template.format(t=quote(ticker, safe="")))


@pytest.mark.parametrize("ticker", HOSTILE)
@pytest.mark.parametrize("template,in_path", ROUTES)
def test_a_hostile_ticker_is_a_400_that_is_not_echoed(client, template, in_path, ticker):
    r = _get(client, template, ticker)
    # a ``/`` in a path segment never reaches the view (the router answers 404); either way no payload comes back
    expected = 404 if in_path and "/" in ticker else 400
    assert r.status_code == expected
    assert ticker.encode() not in r.data and b"<img" not in r.data and b"<b>" not in r.data


@pytest.mark.parametrize("ticker", ["aapl", " aapl", "AAPL", "BRK.B", "brk.b", "BF-B"])
@pytest.mark.parametrize("template,_in_path", ROUTES)
def test_a_normal_or_lower_case_ticker_is_not_a_400(client, template, _in_path, ticker):
    """Normalise first, then validate: ``aapl`` must not become a 400."""
    assert _get(client, template, ticker).status_code != 400


def test_the_api_ticker_404_names_the_normalised_symbol(client):
    r = client.get("/api/ticker/zzzz")
    assert r.status_code == 404 and r.get_json()["ticker"] == "ZZZZ"


@pytest.mark.parametrize("ticker", sorted(t for t, i in ROTATION_ETFS.items()
                                          if i["group"] in ("sector", "theme")))
def test_every_expandable_rotation_row_can_load_its_holdings(client, ticker):
    """The sector and theme rows are the only ones with a caret; none may be refused."""
    assert client.get("/view/rotation/holdings", query_string={"ticker": ticker}).status_code != 400


def test_the_symbols_the_validator_refuses_never_expand():
    """``US10Y-US02Y`` and ``CL1!`` fail the ticker shape; they must stay in groups without a caret."""
    refused = {t: i["group"] for t, i in ROTATION_ETFS.items() if not dashboard._valid_ticker(t)}
    assert refused == {"US10Y-US02Y": "yield", "CL1!": "commodity"}
    assert not set(refused.values()) & {"sector", "theme"}


def test_the_rotation_page_gives_those_rows_no_caret(tmp_path, monkeypatch, rawstore_tmp_db):
    import RevenueChaser.module_sector_rotation.report as rd
    from datetime import date
    from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
    monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(rd, "coarse_theme_catalog", lambda: {})
    snap = lambda t, g: ETFRotationSnapshot(
        as_of_date=date(2026, 8, 14), ticker=t, group_name=g, close_price=1.0, rs_1w=1.0, rs_1m=1.0,
        rs_3m=1.0, rs_ratio=1.0, rs_momentum=1.0, rs_ratio_z=0.5, rs_momentum_z=0.5, rrg_quadrant="LEADING")
    out = rd.export_rotation_dashboard(
        [snap("XLK", "sector"), snap("US10Y-US02Y", "yield"), snap("CL1!", "commodity")],
        filename="caret.html", uw=None)
    html = out.read_text(encoding="utf-8")
    assert 'id="caret-XLK"' in html
    assert "caret-US10Y-US02Y" not in html and "caret-CL1!" not in html

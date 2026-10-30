"""``GET /api/data`` and ``GET /api/data/<need>`` (step D): the internal data API over HTTP.

Read-only. The routes call ``rcdata.api.get``, which calls no provider and writes nothing, so a page may ask them on
every render. Filling (``POST .../fill``) arrives with step C, where filling itself is built.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.rcdata import api


@pytest.fixture
def client():
    return dashboard.app.test_client()


@pytest.fixture
def answer(monkeypatch):
    """Pin what ``api.get`` returns, so the route is tested and not the store."""
    seen = {}

    def set_answer(**over):
        base = dict(need="identity.universe", rows=[{"ticker": "MSFT", "source_as_of": date(2026, 10, 27)}],
                    state="fresh", freshness={"as_of_session": date(2026, 10, 27), "lag_sessions": 0, "max_lag": 5,
                               "unit": "sessions"},
                    coverage={"asked": 1, "served": 1, "missing": [], "from_fallback": 0},
                    provenance=[{"key": "MSFT", "dataset": "spine/security", "source": "sec",
                                 "source_as_of": date(2026, 10, 27), "fetched_at": 1.0, "run_id": "r1"}],
                    status="changed", fill=None)
        ans = api.Answer(**{**base, **over})

        def fake_get(need, *, fill="never", **params):
            seen["need"], seen["fill"], seen["params"] = need, fill, params
            return ans
        monkeypatch.setattr(api, "get", fake_get)
        return seen
    return set_answer


def test_the_catalog_is_listed_with_its_freshness_rule(client):
    body = client.get("/api/data").get_json()
    names = {n["need"]: n for n in body["needs"]}
    assert "identity.universe" in names and "earnings.next" in names
    assert names["earnings.next"]["max_lag"] == 1 and names["earnings.next"]["max_lag_unit"] == "sessions"
    assert names["fundamentals.estimates"]["max_lag_unit"] == "days"
    assert names["earnings.next"]["dataset"] == "uw/option_screener"
    assert names["earnings.next"]["kind"] == "options"


def test_an_answer_comes_back_as_json_with_dates_as_iso_strings(client, answer):
    answer()
    body = client.get("/api/data/identity.universe").get_json()
    assert body["need"] == "identity.universe" and body["state"] == "fresh"
    assert body["freshness"] == {"as_of_session": "2026-10-27", "lag_sessions": 0, "max_lag": 5,
                                 "unit": "sessions"}
    assert body["coverage"]["served"] == 1 and body["fill"] is None
    assert body["rows"][0]["source_as_of"] == "2026-10-27"
    assert body["provenance"][0]["key"] == "MSFT"


def test_the_list_param_of_the_need_is_split_on_commas(client, answer):
    seen = answer()
    client.get("/api/data/identity.security?tickers=msft,aapl")
    assert seen["params"] == {"tickers": ["msft", "aapl"]}


def test_a_scalar_param_is_passed_through(client, answer):
    seen = answer()
    client.get("/api/data/prices.bars?security_ids=sec:1&basis=split")
    assert seen["params"] == {"security_ids": ["sec:1"], "basis": "split"}


def test_an_unknown_need_is_404(client):
    r = client.get("/api/data/prices.nope")
    assert r.status_code == 404 and r.get_json()["ok"] is False


def test_an_unknown_param_is_400_never_a_500(client):
    r = client.get("/api/data/identity.universe?nonsense=1")
    assert r.status_code == 400 and "nonsense" in r.get_json()["error"]


def test_the_route_never_fills(client, answer):
    seen = answer()
    client.get("/api/data/identity.universe?fill=if_stale")
    assert seen["fill"] == "never", "a GET must never trigger a fill (avo, 29 Oct 2026)"
    assert seen["params"] == {}, "a fill in the query string must be dropped, not smuggled into the reader"


def test_a_get_needs_no_panel_header(client, answer):
    answer()
    assert client.get("/api/data/identity.universe").status_code == 200


# ── summary=1: the Settings / Data screen's read (no ticker, never a fill) ───────────────────────────────────────

@pytest.fixture
def summary(monkeypatch):
    seen = {}

    def set_summary(**over):
        base = {"need": "identity.universe", "state": "fresh",
                "freshness": {"as_of_session": date(2026, 10, 27), "lag_sessions": 0, "max_lag": 5,
                              "unit": "sessions"},
                "coverage": {"asked": None, "served": 14, "missing": [], "from_fallback": 0},
                "status": "changed", "row_count": 14, "key_count": 14, "dataset": "spine/security",
                "legacy": False, "fill": None,
                "sample_rows": [{"ticker": "MSFT", "source_as_of": date(2026, 10, 27)}],
                "provenance": [{"key": "MSFT", "dataset": "spine/security", "source": "sec",
                                "source_as_of": date(2026, 10, 27), "fetched_at": 1.0, "run_id": "r1"}]}

        def fake_summary(need, *, sample=0):
            seen["need"], seen["sample"] = need, sample
            return {**base, **over}
        monkeypatch.setattr(api, "summary", fake_summary)
        return seen
    return set_summary


def test_summary_answers_without_a_ticker_and_dates_are_iso(client, summary):
    summary()
    body = client.get("/api/data/identity.universe?summary=1").get_json()
    assert body["row_count"] == 14 and body["coverage"]["asked"] is None and body["fill"] is None
    assert body["freshness"]["as_of_session"] == "2026-10-27" and body["freshness"]["unit"] == "sessions"
    assert body["sample_rows"][0]["source_as_of"] == "2026-10-27"


def test_summary_asks_for_five_sample_rows_by_default(client, summary):
    seen = summary()
    client.get("/api/data/identity.universe?summary=1")
    assert seen["sample"] == 5
    client.get("/api/data/identity.universe?summary=1&sample=2")
    assert seen["sample"] == 2


def test_the_sample_size_is_bounded_and_a_bad_one_falls_back(client, summary):
    seen = summary()
    client.get("/api/data/identity.universe?summary=1&sample=5000")
    assert seen["sample"] == 50
    client.get("/api/data/identity.universe?summary=1&sample=lots")
    assert seen["sample"] == 5


def test_summary_0_is_the_ordinary_read(client, answer):
    seen = answer()
    client.get("/api/data/identity.universe?summary=0")
    assert seen["fill"] == "never" and "rows" in client.get("/api/data/identity.universe").get_json()


def test_an_unknown_need_is_still_404_with_summary(client):
    assert client.get("/api/data/prices.nope?summary=1").status_code == 404


# ── The review's I2: the ordinary read path has a row cap ──

def test_a_needs_answer_is_capped_and_says_so(client, answer, monkeypatch):
    """Fourteen needs take no argument, so without a cap their URL serves the whole table as JSON - with one
    provenance entry per row - from the process that holds the DuckDB writer. The summary path has had a cap of 50
    all along; this is the same bound on the ordinary one, and the body says when it bit."""
    monkeypatch.setattr(dashboard, "DATA_ROWS_MAX", 3)
    answer(rows=[{"ticker": f"T{i}", "source_as_of": date(2026, 10, 27)} for i in range(10)],
           provenance=[{"key": f"T{i}"} for i in range(10)])
    body = client.get("/api/data/identity.universe").get_json()
    assert len(body["rows"]) == 3 and len(body["provenance"]) == 3
    assert body["truncated"] is True and body["row_cap"] == 3
    assert body["coverage"]["served"] == 1, "the coverage counts are the store's, not the page's"


def test_an_answer_inside_the_cap_is_not_marked_truncated(client, answer, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA_ROWS_MAX", 3)
    answer(rows=[{"ticker": "MSFT"}], provenance=[{"key": "MSFT"}])
    body = client.get("/api/data/identity.universe").get_json()
    assert body["truncated"] is False and len(body["rows"]) == 1


def test_the_caller_may_ask_for_fewer_rows_but_not_for_more(client, answer, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA_ROWS_MAX", 3)
    answer(rows=[{"ticker": f"T{i}"} for i in range(10)], provenance=[{"key": f"T{i}"} for i in range(10)])
    assert len(client.get("/api/data/identity.universe?limit=2").get_json()["rows"]) == 2
    assert len(client.get("/api/data/identity.universe?limit=99").get_json()["rows"]) == 3

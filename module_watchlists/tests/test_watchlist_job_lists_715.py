"""The two lists the jobs read, on the Watch lists board (#715; avo, 13 Nov 2026: "Watchlist panosuna bunlari
ekle. Ne amacla kullandiklari da listenin isminde olsun").

They are ``ops.sqlite``'s ``option`` and ``stock`` lists, edited on Settings until then. The board shows them
after avo's own lists and writes the same ops rows a board's button pair writes, so the jobs read what he sees.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.module_watchlists import board, store
from RevenueChaser.rcdata import ops, read

pytestmark = pytest.mark.rawstore_schema_from_scratch


@pytest.fixture
def no_data(monkeypatch):
    import pandas as pd
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: pd.DataFrame(
        columns=["ticker", "session_date", "close", "volume"]))
    monkeypatch.setattr(board.read, "latest_market", lambda *a, **k: {})
    monkeypatch.setattr(board.read, "market_series", lambda *a, **k: [])
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [])


@pytest.fixture
def client(no_data):
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


def test_each_job_list_says_in_its_name_what_it_is_fetched_for():
    names = {spec["kind"]: spec["name"] for spec in board.JOB_LISTS.values()}
    assert set(names) == set(ops.WATCH_KINDS)
    assert "Option chains" in names["option"] and "fetched" in names["option"]
    assert "prices" in names["stock"] and "LEAPS" in names["stock"] and "Earnings" in names["stock"]


def test_a_job_list_id_is_one_avos_own_list_can_never_take():
    for list_id in board.JOB_LISTS:
        assert store.derive_id(list_id) != list_id


def test_the_job_lists_come_after_avos_own_lists_with_their_counts(no_data):
    own = store.create_list("Growth")
    ops.watch_add("option", "NVDA", by="test")
    ops.watch_add("option", "AMD", by="test")
    lists = board.payload()["lists"]
    assert [r["id"] for r in lists] == [own, "jobs.option", "jobs.stock"]
    assert [r["job"] for r in lists] == [False, True, True]
    assert [r["symbols"] for r in lists] == [0, 2, 0]


def test_a_job_list_opens_with_its_tickers_as_us_listings_in_one_unnamed_section(monkeypatch, no_data):
    ops.watch_add("stock", "NVDA", by="test")
    monkeypatch.setattr(board.read, "latest_market",
                        lambda *a, **k: {"NVDA": {"price": 110.0, "market_cap": 4.19e12}})
    data = board.payload("jobs.stock")
    assert data["list"]["id"] == "jobs.stock" and data["list"]["job"] == "stock"
    assert data["list"]["name"] == board.JOB_LISTS["jobs.stock"]["name"]
    assert [s["name"] for s in data["sections"]] == [""]
    row = data["sections"][0]["rows"][0]
    assert (row["sym"], row["t"], row["cov"], row["cap"]) == ("NVDA", "NVDA", "us_equity", 4.19e12)
    assert data["counts"]["unsupported"] == 0


def test_with_no_list_of_avos_a_filled_job_list_still_opens_the_board(no_data):
    assert board.payload() is None
    ops.watch_add("option", "NVDA", by="test")
    assert board.payload()["list"]["id"] == "jobs.option"


def test_one_of_avos_lists_is_not_a_job_list(no_data):
    own = store.create_list("Growth")
    assert board.payload(own)["list"]["job"] == ""
    assert board.job_kind(own) is None and board.job_kind("jobs.option") == "option"


def test_the_page_draws_no_rename_section_or_export_control_on_a_job_list(no_data):
    ops.watch_add("option", "NVDA", by="test")
    own = store.create_list("Growth")
    job_page, own_page = board.render_html(board.payload("jobs.option")), board.render_html(board.payload(own))
    assert '<main class="wl wl-job">' in job_page and '<main class="wl">' in own_page
    assert ".wl-job .wl-own{display:none !important}" in job_page
    for control in ('id="wl-export"', 'data-do="rename"', 'data-do="delete"'):
        tag = job_page[job_page.rindex("<", 0, job_page.index(control)):job_page.index(control)]
        assert "wl-own" in tag, control
    assert "Read by the jobs" in own_page, "the picker names the group the two lists stand under"


def test_adding_on_a_job_list_writes_the_ops_list_the_jobs_read(client):
    ops.watch_add("option", "AMD", by="test")
    answer = client.post("/api/watchlists/jobs.option/symbol/add", json={"symbol": "NASDAQ:NVDA", "section_id": "_"})
    assert answer.status_code == 200, answer.get_data(as_text=True)
    assert read.watched("option") == {"AMD", "NVDA"}
    rows = answer.get_json()["payload"]["sections"][0]["rows"]
    assert [r["t"] for r in rows] == ["AMD", "NVDA"]
    assert read.watched("stock") == set(), "the other list is not touched"


def test_removing_on_a_job_list_takes_the_ticker_off_the_ops_list(client):
    ops.watch_add("stock", "NVDA", by="test")
    ops.watch_add("stock", "AMD", by="test")
    answer = client.post("/api/watchlists/jobs.stock/symbol/remove", json={"symbol": "NVDA"})
    assert answer.status_code == 200
    assert read.watched("stock") == {"AMD"}


def test_a_spread_or_a_series_is_refused_on_a_job_list_and_nothing_is_written(client):
    ops.watch_add("option", "AMD", by="test")
    answer = client.post("/api/watchlists/jobs.option/symbol/add", json={"symbol": "TVC:US10Y-TVC:US02Y"})
    assert answer.status_code == 400
    assert read.watched("option") == {"AMD"}


def test_a_job_list_cannot_be_renamed_or_deleted(client):
    ops.watch_add("option", "AMD", by="test")
    assert client.post("/api/watchlists/jobs.option", json={"name": "Mine"}).status_code in (400, 404)
    assert client.post("/api/watchlists/jobs.option/delete", json={}).status_code in (400, 404)
    assert read.watched("option") == {"AMD"}


def test_a_derived_tab_answers_for_a_job_list(monkeypatch, no_data):
    ops.watch_add("stock", "NVDA", by="test")
    answer = board.columns("jobs.stock", "performance")
    assert answer is not None and answer["tab"] == "performance"

"""The News tab in the dashboard (plan decision D): first in the Research group, Refresh is store-only, Fetch builds."""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard, runners
from RevenueChaser.app.jobs import GAPS, JobMessage
from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client


def _client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


def test_news_is_the_first_tab_of_research():
    keys = [t.get("key") or t["label"] for t in dashboard.TABS]
    assert keys[keys.index("Research") + 1] == "module_news"


def test_refresh_renders_from_the_board_file_without_building(monkeypatch, tmp_path):
    monkeypatch.setattr(runners, "NEWS_LATEST", tmp_path / "news_latest.html")
    built = []
    from RevenueChaser.module_news import calendar
    monkeypatch.setattr(calendar, "build", lambda **kw: built.append(kw))
    r = _client().get("/view/module_news", query_string={"refresh": "1"})
    assert r.status_code == 200 and "ABD · Ekonomik takvim" in r.get_data(as_text=True)
    assert built == []                                   # a Refresh never writes the store


def test_the_fetch_queues_the_news_job(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params: captured.update(module=module, params=params) or "stubjob")
    assert _client().post("/run/module_news").status_code == 202
    assert captured == {"module": "module_news", "params": {}}


def test_the_job_builds_then_renders(monkeypatch, tmp_path):
    from RevenueChaser.module_news import headlines, report
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    asked = []
    monkeypatch.setattr(headlines, "fetch", lambda names, **kw: asked.append(names) or
                        {"tickers": len(names), "stored": 0, "skipped": [], "error": "no API session"})
    rawstore.write_rows("economic_calendar", [{
        "session_date": "2026-11-09", "event_ts": 1794317400.0, "event": "CPI, M/M%", "type": "report",
        "forecast": None, "prev": None, "reported_period": None, "raw_row": "{}", "source": "uw",
        "fetched_at": 100.0, "run_id": "s", "run_at": 100.0}])
    with pytest.raises(JobMessage):                   # a down gateway ends the job "done" with gaps, never failed
        runners.run_news()
    assert (tmp_path / "news_latest.html").exists()
    assert rawstore.query("SELECT event FROM news_events", db="news") == [{"event": "CPI, M/M%"}]
    assert asked and asked[0][0] == "AAPL"


def test_a_down_gateway_shows_the_data_incomplete_banner(monkeypatch, tmp_path):
    from RevenueChaser.module_news import headlines, report
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(headlines, "fetch", lambda names, **kw:
                        {"tickers": len(names), "stored": 0, "skipped": [], "error": "no API session"})
    with pytest.raises(JobMessage) as exc:
        runners.run_news()
    # The job says so too, so the Welcome freshness card does not tick it (Joker-1's shot, 9 Nov).
    assert exc.value.message.startswith(f"{GAPS}: ") and "no API session" in exc.value.message
    page = (tmp_path / "news_latest.html").read_text(encoding="utf-8")
    assert page.count('class="rc-provider-health"') == 1
    assert "Interactive Brokers" in page and "no API session" in page


def test_a_name_ibkr_does_not_know_is_no_banner(monkeypatch, tmp_path):
    from RevenueChaser.module_news import headlines, report
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(headlines, "fetch", lambda names, **kw:
                        {"tickers": len(names), "stored": 3, "skipped": ["BRK.B"], "error": None})
    assert 'class="rc-provider-health"' not in runners.run_news().read_text(encoding="utf-8")


def test_the_listener_route_queues_the_headlines_job_once(monkeypatch):
    captured = []
    monkeypatch.setattr(dashboard.jobs, "submit", lambda module, params: captured.append(module) or "stubjob")
    monkeypatch.setattr(dashboard.jobs, "active_id", lambda modules: None)
    assert _client().post("/run/news_headlines").status_code == 202 and captured == ["news_headlines"]
    monkeypatch.setattr(dashboard.jobs, "active_id", lambda modules: "busyjob")      # a pass still queued
    r = _client().post("/run/news_headlines")
    assert r.status_code == 409 and r.get_json()["job_id"] == "busyjob" and captured == ["news_headlines"]


def test_the_headlines_job_loads_the_tape_then_renders_without_the_calendar(monkeypatch, tmp_path):
    from RevenueChaser.module_news import calendar, headlines, report
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(calendar, "build", lambda **kw: pytest.fail("the 5-minute pass builds no calendar"))
    order = []
    monkeypatch.setattr(headlines, "load_tape", lambda **kw: order.append("tape") or {"read": 2, "stored": 1})
    monkeypatch.setattr(headlines, "fetch", lambda names, **kw: order.append("fetch") or
                        {"tickers": len(names), "stored": 0, "skipped": [], "error": None})
    assert runners.run_news_headlines().name == "news_latest.html" and order == ["tape", "fetch"]


def test_the_api_returns_headlines_stored_after_since_with_their_lane():
    import json
    import time
    now = time.time()
    rawstore.write_rows("news_headlines", [
        {"headline_id": h, "provider": "ibkr", "published_at": now - 60, "headline": h, "source": "Dow Jones",
         "tickers": json.dumps(t), "url": None, "fetched_at": now, "run_id": "r", "run_at": at}
        for h, t, at in (("old", ["AAPL"], now - 600), ("market", [], now - 10), ("stock", ["NVDA"], now - 5))])
    r = _client().get("/api/news/headlines", query_string={"since": now - 100})
    rows = {x["headline"]: x for x in r.get_json()["rows"]}
    assert set(rows) == {"market", "stock"}
    assert rows["market"]["lane"] == "market" and rows["stock"]["lane"] == "stocks" and rows["stock"]["tickers"] == ["NVDA"]
    assert _client().get("/api/news/headlines", query_string={"since": "x"}).status_code == 400

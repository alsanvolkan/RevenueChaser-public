"""The News tab in the dashboard (plan decision D): first in the Research group, Refresh is store-only, Fetch builds."""
from __future__ import annotations

from RevenueChaser.app import dashboard, runners
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
    from RevenueChaser.module_news import report
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    rawstore.write_rows("economic_calendar", [{
        "session_date": "2026-11-09", "event_ts": 1794317400.0, "event": "CPI, M/M%", "type": "report",
        "forecast": None, "prev": None, "reported_period": None, "raw_row": "{}", "source": "uw",
        "fetched_at": 100.0, "run_id": "s", "run_at": 100.0}])
    path = runners.run_news()
    assert path == tmp_path / "news_latest.html"
    assert rawstore.query("SELECT event FROM news_events", db="news") == [{"event": "CPI, M/M%"}]

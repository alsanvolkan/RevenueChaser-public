"""Which board each job feeds (app/activity.py), for the Grafana boards; the Activity page itself closed on 11 Nov 2026."""
from __future__ import annotations

import pytest

from RevenueChaser.app import activity
from RevenueChaser.app.dashboard import app
from RevenueChaser.core.tests.panel_client import panel_test_client


@pytest.fixture()
def client():
    return panel_test_client(app)


def test_every_scheduled_job_has_a_place_and_the_known_ones_are_where_they_belong():
    from RevenueChaser.core import automations
    got = activity.job_boards()
    listed = {j for b in got["boards"] for j in b["jobs"]}
    assert listed == {e.name for e in automations.REGISTRY}, "every job under at least one board, or System"
    by = {b["key"]: b["jobs"] for b in got["boards"]}
    assert "RC-Earnings-Calendar" in by["module_earnings_radar"]
    assert "RC-SPX-Gamma-Fetch" in by["module_spx_gamma"]
    assert {"RC-Build", "RC-Backup-Weekly"} <= set(by[activity.SYSTEM])
    assert "module_settings" not in by, "Settings reads every table: it would hold every job"
    assert got["boards"][-1]["key"] == activity.SYSTEM


def test_a_board_feeds_on_a_table_only_by_its_whole_name(monkeypatch):
    from RevenueChaser.core import automations
    entry = automations.AutomationEntry(name="RC-Test", label="t", args=(), time="01:00", description="",
                                        tables_written=("bars",))
    monkeypatch.setattr(automations, "REGISTRY", (entry,))
    monkeypatch.setattr(activity, "_board_code", lambda: {"module_a": 'q("price_bars")', "module_b": 'read("bars")'})
    got = activity.job_boards()
    assert [b["key"] for b in got["boards"] if b["jobs"]] == ["module_b"]


def test_pressed_runs_count_per_board_and_others_are_system():
    rows = [{"module": "module_news", "status": "done", "started_at": 10.0},
            {"module": "module_news", "status": "failed", "started_at": 20.0},
            {"module": "settings_refresh", "status": "lost", "started_at": 5.0}]
    got = activity.pressed_by_board(rows)
    assert got["module_news"] == {"runs": 2, "last": 20.0, "bad": 1}
    assert got[activity.SYSTEM]["bad"] == 1


def test_the_boards_route_answers_the_grouping_with_the_pressed_counts(client):
    body = client.get("/api/activity/boards").get_json()
    assert body["ok"] and body["boards"][-1]["key"] == activity.SYSTEM
    assert all({"runs", "last", "bad"} <= set(b["pressed"]) for b in body["boards"])


def test_add_never_removes_and_remove_never_adds(client):
    from RevenueChaser.rcdata import ops
    r = client.post("/api/watchlist/option/add/nvda").get_json()
    assert r["ok"] and r["added"] is True and r["ticker"] == "NVDA"
    assert client.post("/api/watchlist/option/add/NVDA").get_json()["added"] is False
    assert [w["ticker"] for w in ops.watch_rows("option")] == ["NVDA"], "a second Add keeps the name"
    assert client.post("/api/watchlist/nope/add/NVDA").status_code == 404
    assert client.post("/api/watchlist/option/add/bad%20t").status_code == 400


def test_the_activity_page_is_closed_and_its_address_leads_to_settings(client):
    from RevenueChaser.app import dashboard
    assert "activity" not in {t.get("key") for t in dashboard.TABS}
    r = client.get("/view/activity")
    assert r.status_code == 302 and r.headers["Location"].endswith("/view/module_settings")
    assert not hasattr(activity, "render_page")

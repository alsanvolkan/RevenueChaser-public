"""The Activity board (app/activity.py; avo, 10 Nov 2026): runs and logs by the board they feed, and the watch lists."""
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


def test_the_page_has_three_tabs_frames_grafana_only_when_opened_and_escapes_the_lists(client):
    html = activity.render_page({"option": ([{"ticker": "<b>X", "added_at": 0, "added_by": "avo", "note": "<i>"}], None),
                                 "stock": (None, "OSError: boom")})
    assert html.count('role="tab"') == 3
    assert 'data-board="rc-activity"' in html and 'data-board="rc-logs"' in html
    assert " src=" not in html.split('<iframe')[1].split(">")[0], "Grafana loads only when its tab opens"
    assert "<b>X" not in html and "&lt;b&gt;X" in html
    assert "could not be read: OSError: boom" in html
    assert client.get("/view/activity").status_code == 200

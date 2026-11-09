"""Welcome is the dashboard's opening screen (decision A, avo 9 Nov 17:26)."""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.app import dashboard, runners
from RevenueChaser.core.tests.panel_client import panel_test_client

SHELL = Path(dashboard.__file__).resolve().parents[1] / "templates" / "dashboard.html"


def _client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


def test_welcome_is_the_first_tab_and_the_default_screen():
    tabs = [t for t in dashboard.TABS if t["type"] == "tab"]
    assert tabs[0]["key"] == "module_welcome"
    assert "showTab(document.querySelector('.nav-item[data-tab=\"' + _h + '\"]') ? _h : 'module_welcome');" \
        in SHELL.read_text(encoding="utf-8")


def test_the_shell_opens_a_tab_a_board_asks_for():
    text = SHELL.read_text(encoding="utf-8")
    assert "e.data.type === 'rc-open-tab'" in text


def test_refresh_renders_without_building(monkeypatch, tmp_path):
    monkeypatch.setattr(runners, "WELCOME_LATEST", tmp_path / "welcome_latest.html")
    from RevenueChaser.module_welcome import build
    called = []
    monkeypatch.setattr(build, "build", lambda **kw: called.append(kw))
    r = _client().get("/view/module_welcome", query_string={"refresh": "1"})
    assert r.status_code == 200 and "RevenueChaser · Bugün" in r.get_data(as_text=True) and called == []


def test_the_fetch_queues_the_welcome_job(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params: captured.update(module=module, params=params) or "stubjob")
    assert _client().post("/run/module_welcome").status_code == 202
    assert captured == {"module": "module_welcome", "params": {}}


def test_job_labels_name_the_board_a_job_feeds():
    labels = runners.welcome_job_labels()
    assert labels["spxgamma"] == "SPX Gamma" and labels["module_news"] == "News"


def test_the_job_builds_then_renders(monkeypatch, tmp_path):
    from RevenueChaser.module_welcome import report
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    assert runners.run_welcome() == tmp_path / "welcome_latest.html"


def test_welcome_and_news_fill_the_pane():
    text = SHELL.read_text(encoding="utf-8")
    fill = text.split("var FILL_WIDTH = {", 1)[1].split("};", 1)[0]
    assert "'module_welcome': true" in fill and "'module_news': true" in fill

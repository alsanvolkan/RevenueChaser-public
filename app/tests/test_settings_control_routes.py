"""The Settings / Data control board's two writes and one read (design v28, 30 Oct 2026).

**Apply** saves the universe the board's presses ask for: a press on a tier set only drafts, and Apply posts the
draft to the boards' settings route (Karar 86 A), which stores it and re-renders the Settings card store-only. It
saves a choice and fetches nothing. **Refresh the example rows** (Karar 90 A) reads five rows of one serving table
again, read-only, and calls no provider.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import board_settings as bs
from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client


@pytest.fixture
def client(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.app import dashboard
    submitted = []
    monkeypatch.setattr(dashboard.jobs, "submit",
                        lambda module, params=None: submitted.append((module, params)) or "job7")
    dashboard.app.config["TESTING"] = True
    c = panel_test_client(dashboard.app)
    c.submitted = submitted
    return c


def test_apply_stores_the_universe_and_re_renders_the_settings_card_store_only(client):
    r = client.post("/api/settings/board/module_settings", json={"changes": {"UNIVERSE_SETS": 3,
                                                                             "UNIVERSE_INCLUDE": 5}})
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["values"]["values"] == {"UNIVERSE_SETS": 3, "UNIVERSE_INCLUDE": 5}
    assert client.submitted == [("settings_refresh", {})]
    assert bs.get("module_settings", "UNIVERSE_SETS") == 3


def test_an_empty_universe_is_refused_and_nothing_is_written(client):
    r = client.post("/api/settings/board/module_settings", json={"changes": {"UNIVERSE_SETS": 0}})
    assert r.status_code == 400
    assert rawstore.count("board_settings") == 0 and client.submitted == []

"""#370 The writer comparison job: up to 100 names with one provider + model, no board table written.

Offline, temp store and cache: ``complete`` of the chosen writer is a fake.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import evren
from RevenueChaser.module_relatedness import compare, engine
from RevenueChaser.module_relatedness.tests.test_relatedness import _seed_universe

TICKERS = ["AAA", "BBB", "CCC"]


def _seed_filings(texts):
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "", "form": "10-K", "filed": "2026-01-01", "accession": "a-" + t,
         "primary_doc": "u", "item_text": text, "quality": "ok", "fetched_at": 1.0} for t, text in texts.items()])


@pytest.fixture
def world(rawstore_tmp_db, monkeypatch, tmp_path):
    _seed_universe(TICKERS + ["DDD"])
    _seed_filings({"AAA": "AAA makes gpus", "BBB": "BBB makes gpus", "CCC": "CCC bakes bread"})
    monkeypatch.setattr(compare, "OUT_DIR", tmp_path / "research" / "370")
    seen = []

    def fake(system, user, **k):
        seen.append(k.get("model"))
        food = "bread" in user
        return json.dumps({"products": ["bread" if food else "gpu"], "end_markets": ["grocery" if food else "dc"]})

    monkeypatch.setattr(evren, "complete", fake)
    return seen


def test_the_job_writes_a_step0_scores_file_and_no_board_table(world):
    out = compare.run(["AAA", "BBB", "CCC", "DDD"], provider="evren", model="glm-5.3")
    data = json.loads(compare.out_path("evren", "glm-5.3").read_text(encoding="utf-8"))
    assert data["tickers"] == ["AAA", "BBB", "CCC", "DDD"] and len(data["matrix"]) == 4
    assert data["matrix"][0][1] > data["matrix"][0][2]                 # the two gpu makers are closer
    assert data["matrix"][0][0] is None and all(v is None for v in data["matrix"][3])   # DDD has no text
    assert data["provider"] == "evren" and data["model"] == "glm-5.3" and data["no_text"] == ["DDD"]
    assert data["usable"] == 3 and out["usable"] == 3 and set(world) == {"glm-5.3"}
    for table in ("relatedness_profiles", "relatedness_attributes", "relatedness_scores", "theme_runs"):
        assert rawstore.count(table) == 0


def test_more_than_100_names_is_refused(world):
    with pytest.raises(ValueError, match="at most 100"):
        compare.run([f"T{i}" for i in range(101)], provider="evren", model="glm-5.3")


def test_a_model_outside_the_providers_list_is_refused(world):
    with pytest.raises(ValueError, match="evren"):
        compare.run(TICKERS, provider="evren", model="grok-4.6")


def test_an_auth_failure_stops_and_still_writes_what_it_has(world, monkeypatch):
    def refuse(system, user, **k):
        raise evren.EvrenAuthError("EVREN rejected the API key (HTTP 401)")

    monkeypatch.setattr(evren, "complete", refuse)
    out = compare.run(TICKERS, provider="evren", model="glm-5.3")
    assert out["stopped_reason"] == "auth" and out["usable"] == 0
    assert compare.out_path("evren", "glm-5.3").exists()


def test_the_route_validates_before_queueing(world, monkeypatch):
    from RevenueChaser.app import dashboard
    from RevenueChaser.core.tests.panel_client import panel_test_client
    queued = []
    monkeypatch.setattr(dashboard.jobs, "submit", lambda job, params: queued.append((job, params)) or "j1")
    monkeypatch.setattr(dashboard.jobs, "credential_problem", lambda job: None)
    c = panel_test_client(dashboard.app)
    monkeypatch.delenv("EVREN_API_KEY", raising=False)
    r = c.post("/run/relatedness-compare", json={"provider": "evren", "model": "glm-5.3", "tickers": TICKERS})
    assert r.status_code == 400 and "EVREN_API_KEY" in r.get_json()["error"]
    monkeypatch.setenv("EVREN_API_KEY", "sentinel-370")
    assert c.post("/run/relatedness-compare", json={"provider": "evren", "model": "x", "tickers": TICKERS}).status_code == 400
    assert c.post("/run/relatedness-compare", json={"provider": "evren", "model": "glm-5.3",
                                                    "tickers": []}).status_code == 400
    r = c.post("/run/relatedness-compare", json={"provider": "evren", "model": "glm-5.3", "tickers": ["aaa", " bbb"]})
    assert r.status_code == 202
    assert queued == [("relatedness_compare", {"provider": "evren", "model": "glm-5.3", "tickers": ["AAA", "BBB"]})]
    assert "sentinel-370" not in r.get_data(as_text=True)

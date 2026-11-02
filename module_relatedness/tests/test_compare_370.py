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
    assert compare.out_path("evren", "glm-5.3", partial=True).exists()


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


# ── review round 1 (PR 371) ─────────────────────────────────────────────────────────────────────────

def _counts():
    out = {}
    for db in ("raw", "edgar", "history"):
        try:
            names = [r["table_name"] for r in rawstore.query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'", db=db)]
        except Exception:  # noqa: BLE001 - a store file this test never opened
            continue
        for t in names:
            if t != "llm_cache":
                out[(db, t)] = rawstore.query(f'SELECT count(*) AS n FROM "{t}"', db=db)[0]["n"]
    return out


def test_s4_no_table_but_llm_cache_changes(world):
    before = _counts()
    compare.run(TICKERS, provider="evren", model="glm-5.3")
    after = _counts()
    assert {k: v for k, v in after.items() if before.get(k) != v} == {}


def test_s1_the_deepseek_usd_cap_holds(world, monkeypatch):
    from RevenueChaser.core.providers import deepseek
    from RevenueChaser.module_relatedness import config
    monkeypatch.setattr(config, "USD_CAP", 0.0)
    monkeypatch.setattr(deepseek, "complete", lambda *a, **k: pytest.fail("a paid call past the cap"))
    out = compare.run(TICKERS, provider="deepseek", model="deepseek-flash")
    assert out["stopped_reason"] == "cap" and out["attempts"] == 0


def test_s2_a_request_error_stops_the_run(world, monkeypatch):
    from RevenueChaser.core.providers import grok
    calls = []

    def bad(*a, **k):
        calls.append(1)
        raise grok.GrokRequestError("Grok HTTP 404 (model grok-4.6)")

    monkeypatch.setattr(grok, "complete", bad)
    out = compare.run(TICKERS, provider="grok", model="grok-4.6")
    assert out["stopped_reason"] == "request" and len(calls) == 1


def test_s2_repeated_transient_failures_stop_the_run(world, monkeypatch):
    names = [f"T{i}" for i in range(8)]
    _seed_universe(names)
    _seed_filings({t: f"{t} makes gpus" for t in names})
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        raise evren.EvrenTransientError("EVREN HTTP 503")

    monkeypatch.setattr(evren, "complete", flaky)
    out = compare.run(names, provider="evren", model="glm-5.3")
    assert out["stopped_reason"] == "transient" and len(calls) == engine.MAX_TRANSIENT_FAILURES


def test_s2_unusable_replies_from_the_start_stop_the_run(world, monkeypatch):
    from RevenueChaser.module_relatedness import config
    names = [f"U{i}" for i in range(8)]
    _seed_universe(names)
    _seed_filings({t: f"{t} makes gpus" for t in names})
    monkeypatch.setattr(evren, "complete", lambda *a, **k: "I cannot help with that.")
    out = compare.run(names, provider="evren", model="glm-5.3")
    assert out["stopped_reason"] == "unusable" and len(out["empty"]) == config.GROK_UNUSABLE_STOP


def test_s3_a_held_relatedness_lock_is_a_job_message(world):
    from RevenueChaser.app import runners
    from RevenueChaser.core import job_lock
    with job_lock.acquire("relatedness", wait=0) as got:
        assert got
        with pytest.raises(runners.JobMessage, match="already in progress"):
            runners.run_relatedness_compare(None, provider="evren", model="glm-5.3", tickers=TICKERS)


def test_s5_a_peak_stop_writes_a_partial_result(world, monkeypatch):
    from RevenueChaser.core import offpeak
    from RevenueChaser.core.providers import deepseek

    def at_peak(*a, **k):
        raise deepseek.PeakHoursError(offpeak.next_off_peak())

    monkeypatch.setattr(deepseek, "complete", at_peak)
    out = compare.run(TICKERS, provider="deepseek", model="deepseek-flash")
    assert out["stopped_reason"] == "peak"
    assert compare.out_path("deepseek", "deepseek-flash", partial=True).exists()
    assert not compare.out_path("deepseek", "deepseek-flash").exists()   # a complete result is never overwritten


def test_s5_the_route_refuses_101_names_and_bad_shapes(world, monkeypatch):
    from RevenueChaser.app import dashboard
    from RevenueChaser.core.tests.panel_client import panel_test_client
    monkeypatch.setattr(dashboard.jobs, "submit", lambda *a: pytest.fail("must not queue"))
    monkeypatch.setenv("EVREN_API_KEY", "sentinel-370")
    c = panel_test_client(dashboard.app)
    for body in ({"provider": "evren", "model": "glm-5.3", "tickers": [f"T{i}" for i in range(101)]},
                 {"provider": ["evren"], "model": "glm-5.3", "tickers": TICKERS},
                 {"provider": "evren", "model": "glm-5.3", "tickers": ["../x"]},
                 {"provider": "evren", "model": "glm-5.3", "tickers": [{"a": 1}]}):
        assert c.post("/run/relatedness-compare", json=body).status_code == 400


def test_nit_duplicate_names_are_queued_once(world, monkeypatch):
    from RevenueChaser.app import dashboard
    from RevenueChaser.core.tests.panel_client import panel_test_client
    queued = []
    monkeypatch.setattr(dashboard.jobs, "submit", lambda job, params: queued.append(params) or "j1")
    monkeypatch.setattr(dashboard.jobs, "credential_problem", lambda job: None)
    monkeypatch.setenv("EVREN_API_KEY", "sentinel-370")
    c = panel_test_client(dashboard.app)
    assert c.post("/run/relatedness-compare",
                  json={"provider": "evren", "model": "glm-5.3", "tickers": ["AAA", "aaa", "BRK.B"]}).status_code == 202
    assert queued[0]["tickers"] == ["AAA", "BRK.B"]

"""#360 The profile writer is chosen on the board (framework D9, avo 1-2 Nov 2026): EVREN by default.

- ``WRITER`` / ``WRITER_MODEL`` are board settings (``value_text``); a model outside its provider's list is refused.
- An option whose key or login is absent is greyed with the variable NAME, never a value.
- The CLI and the dashboard run the board's writer when no ``--provider`` is given; the off-peak gate and the USD cap
  are DeepSeek's only; EVREN and Grok have no call cap (avo, 2 Nov).
- One writer per board: a profile of another writer is stale and stays out of the peer matrix.
- Every profile and every run names its provider and model.

Offline, temp store: the EVREN HTTP client and ``deepseek.complete`` are fakes.
"""
from __future__ import annotations

import json
import re

import pytest

from RevenueChaser.core import board_settings, rawstore
from RevenueChaser.core.providers import deepseek, evren
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import engine, settings, store, writers
from RevenueChaser.module_relatedness.tests.test_relatedness import _seed_universe

TICKERS = ["AAA", "BBB", "CCC"]
EVREN_KEY = "sentinel-evren-key-360"
DS_KEY = "sentinel-deepseek-key-360"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)
        self.headers: dict = {}

    def json(self):
        return self._payload


class _EvrenHttp:
    def __init__(self):
        self.posts: list[dict] = []

    def post(self, url, **k):
        body = k.get("json")
        self.posts.append({"url": url, "json": body})
        name = re.match(r"Company: (.+)\n", body["messages"][-1]["content"]).group(1).lower()
        content = json.dumps({"products": [f"{name} gpu"], "end_markets": ["data center"]})
        return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 5}})

    def close(self):
        pass


def _seed_filings(texts: dict[str, str]) -> None:
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "", "form": "10-K", "filed": "2026-01-01", "accession": "a-" + t,
         "primary_doc": "u", "item_text": text, "quality": "ok", "fetched_at": 1.0}
        for t, text in texts.items()])


@pytest.fixture
def world(rawstore_tmp_db, monkeypatch, tmp_path):
    _seed_universe(TICKERS)
    _seed_filings({t: f"{t} makes gpus for data centers" for t in TICKERS})
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {})
    monkeypatch.setenv("EVREN_API_KEY", EVREN_KEY)
    monkeypatch.delenv("RC_EVREN_BASE", raising=False)
    http = _EvrenHttp()
    monkeypatch.setattr(evren, "_new_client", lambda: http)
    return http


@pytest.fixture
def deepseek_calls(monkeypatch):
    seen: list[str] = []

    def fake(system, user, **k):
        seen.append(user)
        return json.dumps({"products": ["deepseek gpu"], "end_markets": ["data center"]})

    monkeypatch.setattr(deepseek, "complete", fake)
    return seen


# ── the setting ─────────────────────────────────────────────────────────────

def test_the_default_writer_is_evren(rawstore_tmp_db):
    assert settings.writer() == ("evren", "deepseek-v4.1-flash")
    assert writers.DEFAULT == "evren" and set(writers.WRITERS) == {"evren", "deepseek", "grok"}


def test_a_saved_writer_is_read_back(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", DS_KEY)
    board_settings.save(settings.BOARD, {"WRITER": "deepseek", "WRITER_MODEL": "deepseek-flash"})
    assert settings.writer() == ("deepseek", "deepseek-flash")


def test_a_model_outside_its_providers_list_is_refused(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("EVREN_API_KEY", EVREN_KEY)
    with pytest.raises(board_settings.SettingsError) as exc:
        board_settings.save(settings.BOARD, {"WRITER": "evren", "WRITER_MODEL": "grok-4.6"})
    assert "WRITER_MODEL" in exc.value.messages[0] and "evren" in exc.value.messages[0]


def test_an_unavailable_writer_cannot_be_saved(rawstore_tmp_db, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(board_settings.SettingsError) as exc:
        board_settings.save(settings.BOARD, {"WRITER": "deepseek", "WRITER_MODEL": "deepseek-flash"})
    assert "DEEPSEEK_API_KEY" in " ".join(exc.value.messages)


def test_options_grey_a_missing_key_by_name_and_never_show_a_value(rawstore_tmp_db, monkeypatch, tmp_path):
    monkeypatch.delenv("EVREN_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "no-such-auth.json"))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    opts = {o["provider"]: o for o in writers.options()}
    assert opts["evren"]["available"] is False and opts["evren"]["missing"] == "EVREN_API_KEY"
    assert opts["deepseek"]["available"] is True and opts["deepseek"]["missing"] is None
    assert opts["grok"]["available"] is False and opts["grok"]["missing"] == "RC_XAI_AUTH_FILE"
    assert DS_KEY not in json.dumps(opts)


# ── the run ─────────────────────────────────────────────────────────────────

def test_the_cli_runs_the_boards_writer_without_a_call_cap(rawstore_tmp_db, monkeypatch, capsys):
    monkeypatch.setenv("EVREN_API_KEY", EVREN_KEY)
    got = {}
    monkeypatch.setattr(cli.engine, "run", lambda **k: got.update(k) or {"n": 1, "pending": 0, "companies": []})
    assert cli.main(["--run", "--cap", "10"]) == 0          # what RC-Relatedness-OffPeak runs
    assert got == {"tickers": None, "provider": "evren", "max_calls": None}
    assert evren.MODEL == "deepseek-v4.1-flash"


def test_the_cli_keeps_the_usd_cap_for_a_deepseek_board(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", DS_KEY)
    board_settings.save(settings.BOARD, {"WRITER": "deepseek", "WRITER_MODEL": "deepseek-flash"})
    got = {}
    monkeypatch.setattr(cli.engine, "run", lambda **k: got.update(k) or {"n": 1, "pending": 0})
    assert cli.main(["--run", "--cap", "10"]) == 0
    assert got == {"tickers": None, "limit": None, "usd_cap": 10.0}


def test_a_writer_without_its_key_stops_before_any_call(rawstore_tmp_db, monkeypatch, capsys):
    monkeypatch.delenv("EVREN_API_KEY", raising=False)
    monkeypatch.setattr(cli.engine, "run", lambda **k: pytest.fail("must not run"))
    assert cli.main(["--run", "--cap", "10"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["provider"] == "evren" and out["calls"] == 0 and out["stopped_reason"] == "missing"
    assert "EVREN_API_KEY" in out["error"]


def test_an_evren_run_profiles_and_names_its_writer(world):
    payload = engine.run(provider="evren")
    assert len(world.posts) == 3 and payload["n"] == 3 and payload["pending"] == 0
    assert payload["model"] == evren.MODEL
    assert payload["writer"] == {"provider": "evren", "model": evren.MODEL}
    row = rawstore.get("relatedness_profiles", "AAA")
    assert row["provider"] == "evren" and row["model"] == evren.MODEL


def test_an_evren_run_is_not_off_peak_gated(world, monkeypatch):
    monkeypatch.setattr(deepseek, "peak_blocked", lambda now=None: True)
    assert engine.run(provider="evren")["n"] == 3


def test_a_deepseek_run_names_its_writer_too(world, deepseek_calls):
    payload = engine.run(usd_cap=10.0)
    assert payload["writer"] == {"provider": "deepseek", "model": deepseek.MODEL}
    assert rawstore.get("relatedness_profiles", "AAA")["provider"] == "deepseek"


def test_another_writers_profiles_are_stale_and_out_of_the_matrix(world, deepseek_calls):
    engine.build_profiles(TICKERS)                           # DeepSeek profiles
    assert len(deepseek_calls) == 3
    assert store.stale_tickers(TICKERS, writer=("evren", evren.MODEL)) == set(TICKERS)
    refresh = engine.run(no_fetch=True, provider="evren")    # the board switched to EVREN, no run yet
    assert refresh["n"] == 0 and refresh["other_writer"] == 3 and refresh["peers"] == {}
    payload = engine.run(provider="evren")                   # the next run re-profiles every name
    assert len(world.posts) == 3 and payload["n"] == 3 and payload["other_writer"] == 0


def test_a_model_change_inside_one_provider_is_a_writer_change(world):
    engine.run(provider="evren")
    assert store.stale_tickers(TICKERS, writer=("evren", "glm-5.3")) == set(TICKERS)
    assert store.stale_tickers(TICKERS, writer=("evren", evren.MODEL)) == set()


def test_the_scheduled_job_carries_the_10_usd_deepseek_cap():
    from RevenueChaser.core import automations
    e = automations.entry("RC-Relatedness-OffPeak")
    assert e.args == ("-m", "RevenueChaser.module_relatedness", "--run", "--cap", "10") and e.time == "12:30"


def test_the_same_model_name_under_another_provider_is_another_writer(world):
    """The provider decides on its own: one model string served by two providers is still two writers."""
    engine.run(provider="evren")
    assert store.stale_tickers(TICKERS, writer=("grok", evren.MODEL)) == set(TICKERS)
    assert store.other_writer_tickers(TICKERS, ("grok", evren.MODEL)) == set(TICKERS)

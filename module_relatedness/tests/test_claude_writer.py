"""The Claude subscription writer and mixed writers (avo, 11 Nov 2026: "Bu isi deepseek yerine Claude den bir model
yapsa? ... claude subscription ile kullaniliyor ... sadece ilk tur claude ile sonra deepseek e donebiliriz").

- ``core/providers/claude_code.py`` runs headless Claude Code (``claude -p``) on the subscription login: no Anthropic
  key ever reaches the CLI, ``RC_CLAUDE_CONFIG_DIR`` picks the login, the gate stops at 80% of the weekly window.
- A profile stays current whoever wrote it; the board names each profile's writer.
- On the layer a Fetch downloads no ``chain_filings`` (plan F3).
- ``compare.run`` records the Claude windows before and after.

Offline: ``subprocess.run`` and ``fetch_claude`` are fakes; nothing reaches Anthropic, DeepSeek or SEC.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from RevenueChaser.core import fundamentals_layer, rawstore, subscription_usage
from RevenueChaser.core.providers import claude_code
from RevenueChaser.core.subscription_usage import Window
from RevenueChaser.module_relatedness import compare, engine, store, writers
from RevenueChaser.module_relatedness.tests.test_grok_writer_388 import (  # noqa: F401
    TICKERS, deepseek_calls, world)
from RevenueChaser.module_relatedness.tests.test_relatedness import v3_reply


class _Cli:
    """``claude -p``: answers a profile naming the company of the prompt, or a scripted JSON result."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result = None            # a dict to print instead of a profile

    def __call__(self, cmd, input=None, **kw):
        self.calls.append({"cmd": cmd, "input": input, "env": kw.get("env") or {}, "cwd": kw.get("cwd")})
        if self.result is not None:
            out = self.result
        else:
            m = re.match(r"Company: (.+)\n", input or "")
            name = (m.group(1) if m else "x").lower()
            out = {"type": "result", "is_error": False, "result": v3_reply([f"{name} gpu"], ["data center"]),
                   "usage": {"output_tokens": 5}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


@pytest.fixture
def cli(monkeypatch, tmp_path):
    fake = _Cli()
    monkeypatch.setattr(claude_code, "_run", fake)
    monkeypatch.setattr(subscription_usage, "fetch_claude", lambda now=None: [Window("seven_day", 10.0),
                                                                              Window("five_hour", 5.0)])
    monkeypatch.setenv("RC_CLAUDE_CONFIG_DIR", str(tmp_path / "claude-b"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-api-key")
    monkeypatch.delenv("RC_CLAUDE_CAP", raising=False)
    claude_code.reset_gate()
    return fake


def test_a_call_uses_the_subscription_login_and_never_an_api_key(rawstore_tmp_db, cli, tmp_path):
    out = claude_code.complete("sys", "Company: AAA Inc\nbody", model="claude-sonnet-5-5", max_tokens=900)
    assert "aaa inc gpu" in out
    call = cli.calls[0]
    assert call["cmd"][:2] == ["claude", "-p"] and call["cmd"][call["cmd"].index("--model") + 1] == "claude-sonnet-5-5"
    assert call["cmd"][call["cmd"].index("--tools") + 1] == "" and "--no-session-persistence" in call["cmd"]
    assert "ANTHROPIC_API_KEY" not in call["env"] and "sentinel-api-key" not in json.dumps(call["env"])
    assert call["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-b")
    assert call["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "900"
    assert claude_code.complete("sys", "Company: AAA Inc\nbody", model="claude-sonnet-5-5") == out
    assert len(cli.calls) == 1                                       # the second answer came from llm_cache


def test_the_gate_reads_the_same_login_the_cli_spends(monkeypatch, tmp_path):
    monkeypatch.delenv("RC_CLAUDE_OAUTH_FILE", raising=False)
    monkeypatch.setenv("RC_CLAUDE_CONFIG_DIR", str(tmp_path / "claude-b"))
    assert subscription_usage.claude_credentials_path() == tmp_path / "claude-b" / ".credentials.json"


def test_the_weekly_window_at_80_percent_stops_before_any_call(rawstore_tmp_db, cli, monkeypatch):
    monkeypatch.setattr(subscription_usage, "fetch_claude", lambda now=None: [Window("seven_day", 80.0)])
    with pytest.raises(claude_code.ClaudeAllowanceError):
        claude_code.complete("sys", "Company: AAA Inc\nbody")
    assert cli.calls == []
    monkeypatch.setenv("RC_CLAUDE_CAP", "90")
    claude_code.reset_gate()
    claude_code.complete("sys", "Company: AAA Inc\nbody")
    assert len(cli.calls) == 1


def test_an_unreadable_usage_stops_rather_than_spend_blind(rawstore_tmp_db, cli, monkeypatch):
    import urllib.error

    def down(now=None):
        try:
            raise urllib.error.URLError("down")
        except urllib.error.URLError as exc:
            raise subscription_usage.SubscriptionUsageError("Claude subscription usage request failed") from exc

    monkeypatch.setattr(subscription_usage, "fetch_claude", down)
    with pytest.raises(claude_code.ClaudeAllowanceError):
        claude_code.complete("sys", "Company: AAA Inc\nbody")
    monkeypatch.setattr(subscription_usage, "fetch_claude", lambda now=None: (_ for _ in ()).throw(
        subscription_usage.SubscriptionUsageError("Claude Code login expired")))
    claude_code.reset_gate()
    with pytest.raises(claude_code.ClaudeAuthError):
        claude_code.complete("sys", "Company: AAA Inc\nbody")
    assert cli.calls == []


def test_cli_failures_map_to_the_run_stops(rawstore_tmp_db, cli, monkeypatch):
    cli.result = {"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1762900000"}
    with pytest.raises(claude_code.ClaudeAllowanceError):
        claude_code.complete("sys", "u1")
    cli.result = {"type": "result", "is_error": True, "result": "Invalid API key · Please run /login"}
    with pytest.raises(claude_code.ClaudeAuthError):
        claude_code.complete("sys", "u2")

    def absent(cmd, **kw):
        raise FileNotFoundError(2, "No such file", "claude")

    monkeypatch.setattr(claude_code, "_run", absent)
    with pytest.raises(claude_code.ClaudeRequestError):
        claude_code.complete("sys", "u3")


def test_the_errors_are_the_ones_the_engine_already_stops_on():
    from RevenueChaser.core.providers import grok
    assert issubclass(claude_code.ClaudeAllowanceError, grok.GrokAllowanceError)
    assert issubclass(claude_code.ClaudeAuthError, grok.GrokAuthError)
    assert issubclass(claude_code.ClaudeRequestError, grok.GrokRequestError)
    assert issubclass(claude_code.ClaudeTransientError, grok.GrokTransientError)


def test_the_writer_is_registered_with_haiku_first(rawstore_tmp_db, monkeypatch, tmp_path):
    w = writers.get("claude")
    assert w.models == ("claude-haiku-5-5", "claude-sonnet-5-5") and not w.paid
    monkeypatch.setenv("RC_CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("RC_CLAUDE_OAUTH_FILE", raising=False)
    assert writers.missing("claude") == "RC_CLAUDE_CONFIG_DIR"
    (tmp_path / ".credentials.json").write_text("{}", encoding="utf-8")
    assert writers.missing("claude") is None


# ── mixed writers: first round Claude, later DeepSeek ───────────────────────

def test_a_claude_first_round_stays_current_when_the_board_goes_back_to_deepseek(world, cli, deepseek_calls):
    payload = engine.run(provider="claude", model="claude-haiku-5-5")
    assert len(cli.calls) == 3 and payload["n"] == 3
    assert payload["other_writer"] == 0 and payload["writer"] == {"provider": "claude", "model": "claude-haiku-5-5"}
    assert rawstore.get("relatedness_profiles", "AAA")["provider"] == "claude"
    back = engine.run(usd_cap=10.0)                                # the writer is DeepSeek again
    assert deepseek_calls == [] and back["n"] == 3 and back["other_writer"] == 3
    assert not any(c["stale"] for c in back["companies"])
    assert {c["writer"] for c in back["companies"]} == {"claude/claude-haiku-5-5"}
    assert len(back["peers"]) == 3                                 # in the peer matrix


def test_the_board_badge_names_another_writers_model():
    from RevenueChaser.module_relatedness import report
    payload = {"as_of": "2026-11-11T00:00:00+00:00", "model": "deepseek-flash", "n": 1,
               "writer": {"provider": "deepseek", "model": "deepseek-flash"},
               "companies": [{"ticker": "AAA", "name": "A", "sector": "Technology", "market_cap": 1e9,
                              "weights": {}, "profile": {}, "stale": False, "writer": "claude/claude-haiku-5-5"}],
               "peers": {}, "scores": [], "thinking_compare": {}, "fields": []}
    html = report.render_relatedness_dashboard(payload)
    assert 'var WRITER = "deepseek/deepseek-flash"' in html and "function writerBadge" in html
    assert '"writer": "claude/claude-haiku-5-5"' in html


# ── the layer: no SEC re-download (plan F3) ─────────────────────────────────

def test_a_fetch_on_the_layer_downloads_no_chain_filings(world, monkeypatch):
    seen = []
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda ts, **k: seen.append(k) or {})
    monkeypatch.setattr(fundamentals_layer, "reads_layer", lambda: True)

    class Stop(engine.RunStopped):
        def __init__(self):
            super().__init__("auth", message="stop here")

    def stop(*a, **k):
        raise Stop()

    monkeypatch.setattr(engine, "build_profiles", stop)
    monkeypatch.setattr(engine, "_payload", lambda *a, **k: {"companies": []})
    try:
        engine.run(usd_cap=1.0, tickers=TICKERS)
    except Exception:  # noqa: BLE001 - only the corpus call is under test
        pass
    assert seen and seen[0]["no_fetch"] is True


# ── the comparison ──────────────────────────────────────────────────────────

def test_a_claude_comparison_records_the_window_before_and_after(world, cli, monkeypatch, tmp_path):
    monkeypatch.setattr(compare, "OUT_DIR", tmp_path / "research")
    readings = iter([[Window("seven_day", 10.0)], [Window("seven_day", 10.0)], [Window("seven_day", 12.5)]])
    monkeypatch.setattr(subscription_usage, "fetch_claude", lambda now=None: next(readings))
    out = compare.run(TICKERS, provider="claude", model="claude-sonnet-5-5")
    assert out["usable"] == 3 and len(cli.calls) == 3
    assert out["usage"] == {"before": {"seven_day": 10.0}, "after": {"seven_day": 12.5}}
    assert out["method"] in ("embedding", "tags")
    for table in ("relatedness_profiles", "relatedness_attributes", "relatedness_scores"):
        assert rawstore.count(table) == 0


def test_the_board_says_which_scorer_made_the_peers():
    from RevenueChaser.module_relatedness import report
    base = {"as_of": "2026-11-11T00:00:00+00:00", "model": "m", "n": 0, "companies": [], "peers": {},
            "scores": [], "thinking_compare": {}, "fields": []}
    assert "similarity v2 (embeddings)" in report.render_relatedness_dashboard({**base, "similarity": "embedding"})
    assert "tag cosine (v1)" in report.render_relatedness_dashboard({**base, "similarity": "tags"})

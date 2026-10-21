"""#388 Grok option for the Relatedness profile writer (avo 2026-10-16: "grok kullan direk").

- ``--run --provider grok --limit N`` writes v2 profiles through SuperGrok (``core/providers/grok.py``); ``--limit``
  caps the calls (never the universe), the allowance gate stops the run cleanly, there is no off-peak gate.
- The stored row names its writer: ``relatedness_profiles.model`` is the Grok model and the cache key carries
  ``"provider": "grok"``; the LLM cache keeps the two providers apart.
- **The trap:** a Grok profile at the current prompt version is CURRENT for the default DeepSeek nightly
  (RC-Relatedness-OffPeak ``--run --cap 8``), and a DeepSeek v2 profile is current for a Grok run; a v1 profile stays
  stale for both. The scheduled entry is byte-identical.
- Peer scores do not depend on which model wrote a profile.

Offline, temp store: the Grok HTTP client and ``deepseek.complete`` are fakes; nothing reaches xAI, DeepSeek or SEC.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import offpeak, rawstore, subscription_usage
from RevenueChaser.core.providers import deepseek, grok
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import config, engine, similarity, store
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_universe

TICKERS = ["AAA", "BBB", "CCC"]
TOKEN = "sentinel-grok-token-388"


def _seed_filings(texts: dict[str, str]) -> None:
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "", "form": "10-K", "filed": "2026-01-01", "accession": "a-" + t,
         "primary_doc": "u", "item_text": text, "quality": "ok", "fetched_at": 1.0}
        for t, text in texts.items()])


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.headers: dict = {}

    def json(self):
        return self._payload


class _GrokHttp:
    """The SuperGrok endpoint: answers a profile naming the company of the prompt (or a scripted reply)."""

    def __init__(self):
        self.posts: list[dict] = []
        self.reply = None             # a fixed content string, or None for a profile per company
        self.bad: set[str] = set()    # tickers whose reply is unusable
        self.status = 200

    def post(self, url, **k):
        body = k.get("json")
        self.posts.append({"url": url, "json": body, "headers": dict(k.get("headers") or {})})
        if self.status != 200:
            return _Resp(self.status, text="model grok-x not found")
        user = body["messages"][-1]["content"]
        m = re.match(r"Company: (.+)\n", user)
        name = (m.group(1) if m else "x").lower()
        if self.reply is not None:
            content = self.reply
        elif name.split()[0].upper() in self.bad:
            content = "I cannot help with that."
        else:
            content = json.dumps({"products": [f"{name} gpu"], "end_markets": ["data center"]})
        return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 5}})

    def close(self):
        pass


def _window(percent):
    return subscription_usage.Window(key="seven_day", percent=percent,
                                     resets_at=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def world(rawstore_tmp_db, monkeypatch, tmp_path):
    """Three names with filing text, a SuperGrok login with room, the Grok endpoint faked."""
    _seed_universe(TICKERS)
    _seed_filings({t: f"{t} makes gpus for data centers" for t in TICKERS})
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {})
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": TOKEN}}}}), encoding="utf-8")
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(auth))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("RC_GROK_BASE", raising=False)
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(10)])
    http = _GrokHttp()
    monkeypatch.setattr(grok, "_new_client", lambda: http)
    monkeypatch.setattr(grok, "_sleep", lambda s: None)
    grok.reset_gate()
    return http


@pytest.fixture
def deepseek_calls(monkeypatch):
    """The DeepSeek side (the nightly): every request it would bill, answered with a profile."""
    seen: list[str] = []

    def fake(system, user, **k):
        seen.append(user)
        return json.dumps({"products": ["deepseek gpu"], "end_markets": ["data center"]})

    monkeypatch.setattr(deepseek, "complete", fake)
    return seen


def _ck(ticker: str) -> dict:
    v = rawstore.get("relatedness_profiles", ticker)["cache_key"]
    return v if isinstance(v, dict) else json.loads(v)


# ── the trap: staleness across providers ────────────────────────────────────

def test_a_grok_v2_profile_is_current_for_the_default_deepseek_run(world, deepseek_calls):
    engine.build_profiles(TICKERS, provider="grok", max_calls=10)
    assert len(world.posts) == 3
    payload = engine.run(usd_cap=8.0)                    # what RC-Relatedness-OffPeak runs: --run --cap 8
    assert deepseek_calls == []
    assert payload["pending"] == 0 and payload["n"] == 3
    assert not any(c["stale"] for c in payload["companies"])
    assert store.stale_tickers(TICKERS) == set()


def test_a_deepseek_v2_profile_is_current_for_a_grok_run(world, deepseek_calls):
    engine.build_profiles(TICKERS)
    assert len(deepseek_calls) == 3
    engine.build_profiles(TICKERS, provider="grok", max_calls=10)
    assert world.posts == []


def test_a_v1_profile_stays_stale_for_both_providers(world, deepseek_calls):
    v1 = store.cache_key(deepseek.MODEL, config.THINKING, config.REASONING_EFFORT, "v1", "AAA", "h")
    store.save_profile("AAA", "", _prof(products=["old"]), v1, deepseek.MODEL, prompt_version="v1")
    grok_v1 = store.cache_key(grok.MODEL, False, "", "v1", "BBB", "h", provider="grok")
    store.save_profile("BBB", "", _prof(products=["old"]), grok_v1, grok.MODEL, prompt_version="v1")
    assert store.stale_tickers(TICKERS) == {"AAA", "BBB"}
    engine.build_profiles(["AAA"], provider="grok", max_calls=10)       # Grok redoes a v1 DeepSeek profile
    assert len(world.posts) == 1 and store.stale_tickers(["AAA"]) == set()
    engine.build_profiles(["BBB"])                                       # DeepSeek redoes a v1 Grok profile
    assert len(deepseek_calls) == 1 and store.stale_tickers(["BBB"]) == set()


def test_a_grok_profile_from_an_old_input_is_redone(world, deepseek_calls):
    """The input check (#388 D5) holds for a Grok row too: same prompt hash, so a new filing text redoes it."""
    engine.build_profiles(TICKERS, provider="grok", max_calls=10)
    _seed_filings({"BBB": "BBB now sells banking software"})
    engine.build_profiles(TICKERS)
    assert len(deepseek_calls) == 1 and "BBB now sells banking software" in deepseek_calls[0]


# ── the stored writer ───────────────────────────────────────────────────────

def test_the_stored_row_names_the_grok_model(world):
    engine.build_profiles(["AAA"], provider="grok", max_calls=10)
    row = rawstore.get("relatedness_profiles", "AAA")
    assert row["model"] == grok.MODEL and row["prompt_version"] == config.PROMPT_VERSION and row["status"] == "ok"
    ck = _ck("AAA")
    assert ck["provider"] == "grok" and ck["model"] == grok.MODEL and ck["prompt_version"] == config.PROMPT_VERSION
    assert store.load_profiles(["AAA"])["AAA"]["products"] == ["aaa inc gpu"]
    cached = rawstore.get_all("llm_cache")
    assert [r["model"] for r in cached] == [grok.MODEL] and cached[0]["response"]["provider"] == "grok"


def test_a_deepseek_cache_key_is_unchanged():
    old = '{"effort":"high","input_hash":"h","model":"m","prompt_version":"v2","thinking":1,"ticker":"AAA"}'
    assert store.cache_key("m", True, "high", "v2", "aaa", "h") == old
    assert store.cache_key("m", True, "high", "v2", "aaa", "h", provider="deepseek") == old
    assert json.loads(store.cache_key("g", False, "", "v2", "aaa", "h", provider="grok"))["provider"] == "grok"


def test_the_mode_stamp_is_unchanged():
    assert store.mode_stamp() == {"model": deepseek.MODEL, "thinking": bool(config.THINKING),
                                  "effort": config.REASONING_EFFORT, "prompt_version": config.PROMPT_VERSION}


# ── the Grok run: --limit caps calls, the allowance gate, no off-peak gate ───

def test_with_grok_limit_caps_the_calls_not_the_universe(world, monkeypatch):
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _seed_universe(names)
    _seed_filings({t: f"{t} makes gpus" for t in names})
    payload = engine.run(provider="grok", max_calls=2)
    assert len(world.posts) == 2
    assert payload["n"] == 2 and payload["pending"] == 3          # the other three wait; the board still covers all
    assert rawstore.get("run_registry", "relatedness_run")["value"]["pending"] == 3


def test_an_exhausted_allowance_stops_the_run_cleanly(world, monkeypatch, capsys):
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(95)])
    payload = engine.run(provider="grok", max_calls=10)
    assert world.posts == []
    assert payload["stopped_reason"] == "allowance" and payload["pending"] == 3
    assert payload["next_reset"] == "2026-10-20 12:00"
    assert cli.main(["--run", "--provider", "grok", "--limit", "10"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["provider"] == "grok" and out["calls"] == 0 and "pending" not in out    # stopped before the engine
    assert out["stopped_reason"] == "allowance" and out["cost_usd"] == 0.0 and out["next_reset"] == "2026-10-20 12:00"


def test_the_grok_run_has_no_off_peak_gate(world, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "1")
    monkeypatch.setattr(offpeak, "is_off_peak", lambda now=None: False)
    monkeypatch.setattr(deepseek, "peak_override_active", lambda now=None: False)
    payload = engine.run(provider="grok", max_calls=10)
    assert len(world.posts) == 3 and payload["n"] == 3 and "stopped_reason" not in payload


def test_the_grok_summary_reports_provider_calls_done_and_pending(world, capsys):
    assert cli.main(["--run", "--provider", "grok", "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == {"provider": "grok", "model": grok.MODEL, "calls": 2, "cost_usd": 0.0,
                   "done": 2, "pending": 1, "stale": 0}


def test_a_rejected_request_stops_the_run_and_writes_nothing(world, capsys):
    world.status = 404
    with pytest.raises(engine.RunStopped) as stop:
        engine.build_profiles(TICKERS, provider="grok", max_calls=10, workers=1)
    assert stop.value.reason == "request" and len(world.posts) == 1
    assert rawstore.get_all("relatedness_profiles") == []
    assert cli.main(["--run", "--provider", "grok", "--limit", "10"]) == 1


def test_a_missing_login_stops_as_auth(world, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "absent.json"))
    assert cli.main(["--run", "--provider", "grok", "--limit", "10"]) == 1
    out = capsys.readouterr().out
    assert '"stopped_reason": "auth"' in out and TOKEN not in out and world.posts == []


def test_unusable_replies_before_a_usable_one_stop_the_run_without_burning_attempts(world):
    """A wrong endpoint shape answers 200 with no profile: the run stops before it counts attempts on 3,324 groups."""
    names = [f"N{i}" for i in range(12)]
    _seed_universe(names)
    _seed_filings({t: f"{t} makes things" for t in names})
    world.reply = "I cannot help with that."
    with pytest.raises(engine.RunStopped) as stop:
        engine.build_profiles(names, provider="grok", max_calls=100, workers=1)
    assert stop.value.reason == "unusable"
    assert len(world.posts) == 2 * config.GROK_UNUSABLE_STOP          # profile_for retries each once, uncached
    assert rawstore.get_all("relatedness_profiles") == []


def test_after_a_usable_reply_an_unusable_one_is_counted_as_for_deepseek(world):
    world.bad = {"BBB"}
    engine.build_profiles(["AAA", "BBB"], provider="grok", max_calls=10, workers=1)
    row = rawstore.get("relatedness_profiles", "BBB")
    assert row["status"] == "empty" and row["attempts"] == 1 and row["model"] == grok.MODEL


def test_a_run_whose_unusable_replies_stay_below_the_stop_saves_none_of_them(world):
    world.bad = {"BBB"}
    engine.build_profiles(["BBB"], provider="grok", max_calls=10, workers=1)
    assert rawstore.get("relatedness_profiles", "BBB") is None       # nothing usable seen in this run: not counted


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_the_default_run_calls_the_engine_exactly_as_before(monkeypatch, capsys):
    got = {}
    monkeypatch.setattr(cli.engine, "run", lambda **k: got.update(k) or {"n": 1, "pending": 0})
    assert cli.main(["--run", "--cap", "8"]) == 0
    assert got == {"tickers": None, "limit": None, "usd_cap": 8.0}
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"n": 1, "pending": 0,
                                                                           "next_off_peak": None}


def test_the_grok_run_passes_the_limit_as_a_call_cap(monkeypatch, capsys):
    monkeypatch.setattr(grok, "preflight", lambda now=None: None)
    got = {}
    monkeypatch.setattr(cli.engine, "run", lambda **k: got.update(k) or {"n": 1, "pending": 0, "companies": []})
    assert cli.main(["--run", "--provider", "grok", "--limit", "40"]) == 0
    assert got == {"tickers": None, "provider": "grok", "max_calls": 40}


@pytest.mark.parametrize("argv", [
    ["--run", "--provider", "grok"],                               # no call cap
    ["--run", "--provider", "grok", "--limit", "5", "--cap", "8"],  # --cap is USD, DeepSeek only
    ["--run", "--provider", "grok", "--limit", "0"],
    ["--rebuild-themes", "--provider", "grok"],                    # Grok names themes only with --name
    ["--status", "--provider", "grok"],
    ["--cutover", "--run-id", "x", "--provider", "grok"],
    ["--provider", "chatgpt", "--run"],
])
def test_bad_grok_combinations_are_refused(argv, monkeypatch):
    monkeypatch.setattr(cli.engine, "run", lambda **k: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == 2


def test_the_scheduled_job_is_unchanged():
    from RevenueChaser.core import automations
    e = automations.entry("RC-Relatedness-OffPeak")
    assert e.args == ("-m", "RevenueChaser.module_relatedness", "--run", "--cap", "8") and e.time == "12:30"


def test_the_grok_ping_makes_one_call_and_writes_nothing(world, capsys):
    assert cli.main(["--grok-ping"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["calls"] == 1 and out["model"] == grok.MODEL
    assert out["endpoint"] == "https://cli-chat-proxy.grok.com/v1/chat/completions"
    assert len(world.posts) == 1 and world.posts[0]["json"]["max_tokens"] == 8000
    assert world.posts[0]["json"]["response_format"] == {"type": "json_object"}
    assert rawstore.get_all("llm_cache") == [] and rawstore.get_all("relatedness_profiles") == []


def test_a_failed_grok_ping_exits_1_and_says_why(world, capsys):
    world.status = 404
    assert cli.main(["--grok-ping"]) == 1
    text = capsys.readouterr().out
    out = json.loads(text.strip().splitlines()[-1])
    assert out["ok"] is False and "404" in out["error"] and TOKEN not in text


# ── theme naming with Grok (#108 S1) ────────────────────────────────────────

def test_name_themes_takes_the_grok_client(monkeypatch, capsys):
    from RevenueChaser.module_relatedness import naming
    monkeypatch.setattr(grok, "preflight", lambda now=None: None)
    calls = []
    monkeypatch.setattr(naming, "name_themes",
                        lambda run_id=None, **k: calls.append(k) or {"run_id": "r", "named": 1, "pending": 0})
    assert cli.main(["--name-themes", "--provider", "grok"]) == 0
    assert cli.main(["--name-themes"]) == 0
    assert calls[0]["complete"] is grok.complete
    assert calls[1].get("complete") in (None, deepseek.complete)


def test_grok_naming_stops_at_the_allowance_and_keeps_names(world, monkeypatch):
    from RevenueChaser.module_relatedness import naming
    from RevenueChaser.module_relatedness.tests.test_naming import _seed_run
    run_id = _seed_run()
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(100)])
    out = naming.name_themes(run_id, complete=grok.complete)
    assert out["named"] == 0 and out["pending"] > 0 and out["next_reset"] == "2026-10-20 12:00"
    assert world.posts == []


def test_grok_naming_names_themes(world):
    from RevenueChaser.module_relatedness import naming
    from RevenueChaser.module_relatedness.tests.test_naming import _seed_run
    run_id = _seed_run()
    world.reply = json.dumps({"name": "Semiconductors"})
    out = naming.name_themes(run_id, complete=grok.complete)
    assert out["named"] > 0 and out["pending"] == 0
    assert all("thinking" not in p["json"] and "reasoning_effort" not in p["json"] for p in world.posts)


# ── scores do not depend on the writer ──────────────────────────────────────

def test_peer_scores_do_not_depend_on_which_model_wrote_a_profile(rawstore_tmp_db):
    profs = {"AAA": _prof(products=["gpu", "cpu"], end_markets=["data center"]),
             "BBB": _prof(products=["gpu"], end_markets=["data center", "gaming"]),
             "CCC": _prof(products=["bread"], end_markets=["grocery"])}

    def peers(provider, model):
        for t, p in profs.items():
            ck = store.cache_key(model, False, "", config.PROMPT_VERSION, t, "h", provider=provider)
            store.save_profile(t, "", p, ck, model)
        return similarity.peer_lists(store.load_profiles(list(profs)), config.TOP_PEERS)

    assert peers("deepseek", deepseek.MODEL) == peers("grok", grok.MODEL)


# ── result history ──────────────────────────────────────────────────────────

def test_an_allowance_stop_is_recorded_partial_and_names_the_writer(world, monkeypatch):
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(95)])
    engine.run(provider="grok", max_calls=10, run_id="g1")
    row = rawstore.query("SELECT quality, quality_note, context FROM result_runs WHERE run_id = 'g1'", db="history")[0]
    ctx = row["context"] if isinstance(row["context"], dict) else json.loads(row["context"])
    assert row["quality"] == "partial" and "allowance" in row["quality_note"]
    assert ctx["writer"] == {"provider": "grok", "model": grok.MODEL}


def test_a_deepseek_run_records_no_writer_key(world, deepseek_calls):
    engine.run(usd_cap=8.0, run_id="d1")
    row = rawstore.query("SELECT context FROM result_runs WHERE run_id = 'd1'", db="history")[0]
    ctx = row["context"] if isinstance(row["context"], dict) else json.loads(row["context"])
    assert "writer" not in ctx

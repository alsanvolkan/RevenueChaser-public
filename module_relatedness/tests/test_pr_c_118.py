"""#118 PR C — Relatedness job and math.

C12 similarity percentile, C14/N4 cost cap, P6 stop event, P2 cached reply,
C13/P5 job messages, C22/C23 cache key and hygiene. Offline; fake LLM clients only.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek
from RevenueChaser.module_relatedness import config, engine, profiles, similarity, store


def _prof(**kw):
    out = {f: [] for f in config.PROFILE_FIELDS}
    out.update(kw)
    return out


def _seed_universe(tickers):
    rawstore.replace_all(engine._UNIVERSE, [{
        "ticker": t, "cik": "", "name": t + " Inc", "market_cap": 1.0e10,
        "price": 10.0, "sector": "Technology", "industry": "Semiconductors",
        "quote": {"symbol": t},
    } for t in tickers])


# ── C12: similarity percentile ───────────────────────────────────────────────
def test_percentile_matches_the_bruteforce_oracle(rawstore_tmp_db):
    """The payload percentile uses the upper triangle, zeros counted analytically,
    and the unrounded score — so it equals ``all_scores`` + ``percentile``."""
    profs = {
        "AAA": _prof(products=["alpha", "beta", "gamma"]),
        "BBB": _prof(products=["alpha", "beta"]),
        "CCC": _prof(products=["alpha"]),
        "DDD": _prof(products=["delta"]),
    }
    _seed_universe(list(profs))
    for t, p in profs.items():
        store.save_profile(t, "cik-" + t, p, "k", "m")

    payload = engine._payload(profs, {})
    n = len(profs)
    assert payload["n_pairs"] == n * (n - 1) // 2

    oracle = similarity.all_scores(profs)
    dist = list(oracle.values())
    assert len(dist) == n * (n - 1) // 2

    for focal, entries in payload["peers"].items():
        for p in entries:
            pair = (focal, p["peer"]) if (focal, p["peer"]) in oracle else (p["peer"], focal)
            want = similarity.percentile(oracle[pair], dist)
            assert p["percentile"] == pytest.approx(want, abs=0.1), (focal, p)


def test_peer_scores_never_ship_a_raw_column(rawstore_tmp_db):
    profs = {"AAA": _prof(products=["alpha", "beta"]),
             "BBB": _prof(products=["alpha"])}
    _seed_universe(list(profs))
    for t, p in profs.items():
        store.save_profile(t, "cik-" + t, p, "k", "m")
    payload = engine._payload(profs, {})
    for entries in payload["peers"].values():
        for p in entries:
            assert "score_raw" not in p


# ── C14 + N4: cost cap ───────────────────────────────────────────────────────
def _count_complete(monkeypatch, content):
    calls = {"n": 0}

    def fake(system, user, **kw):
        calls["n"] += 1
        return content

    monkeypatch.setattr(profiles.deepseek, "complete", fake)
    return calls


def _one_group_per_ticker(monkeypatch):
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: t for t in ts})
    monkeypatch.setattr(engine, "_filing_text", lambda t: (t, "we make things"))


def test_cap_zero_makes_no_llm_calls(rawstore_tmp_db, monkeypatch):
    _one_group_per_ticker(monkeypatch)
    calls = _count_complete(monkeypatch, "{}")
    engine.build_profiles([f"T{i}" for i in range(5)], use_llm=True, usd_cap=0.0)
    assert calls["n"] == 0


def test_cap_counts_calls_including_the_thinking_retry(rawstore_tmp_db, monkeypatch):
    _one_group_per_ticker(monkeypatch)
    monkeypatch.setattr(config, "EST_USD_PER_CALL", 0.001)
    monkeypatch.setattr(config, "THINKING", True)
    # An unusable reply makes ``profile_for`` retry once per group: without the
    # call budget the cap of 3 would still make 6 calls.
    calls = _count_complete(monkeypatch, "{}")
    engine.build_profiles([f"T{i}" for i in range(10)], use_llm=True,
                          usd_cap=0.003, workers=1)
    assert calls["n"] == 3


def test_empty_content_reply_counts_an_attempt_and_stops_after_three_runs(
        rawstore_tmp_db, monkeypatch):
    _one_group_per_ticker(monkeypatch)

    def empty(*a, **k):
        raise deepseek.DeepSeekError("DeepSeek returned no content")

    monkeypatch.setattr(engine.profiles_mod, "profile_for", empty)
    for _ in range(4):
        engine.build_profiles(["A"], use_llm=True)
    row = rawstore.get("relatedness_profiles", "A")
    assert row["status"] == "empty" and row["attempts"] == 3


# ── P6: one stop event ───────────────────────────────────────────────────────
def test_an_auth_failure_stops_the_pool_after_the_first_wave(rawstore_tmp_db, monkeypatch):
    _one_group_per_ticker(monkeypatch)
    calls = {"n": 0}

    def auth(*a, **k):
        calls["n"] += 1
        raise deepseek.DeepSeekAuthError("DeepSeek rejected the API key (HTTP 401)")

    monkeypatch.setattr(engine.profiles_mod, "profile_for", auth)
    with pytest.raises(engine.RunStopped) as exc:
        engine.build_profiles([f"T{i}" for i in range(40)], use_llm=True, workers=4)
    assert exc.value.reason == "auth"
    assert 0 < calls["n"] <= 4          # at most one wave, never one call per group


def test_repeated_transient_failures_stop_the_run(rawstore_tmp_db, monkeypatch):
    _one_group_per_ticker(monkeypatch)
    calls = {"n": 0}

    def transient(*a, **k):
        calls["n"] += 1
        raise deepseek.DeepSeekTransientError("DeepSeek HTTP 503")

    monkeypatch.setattr(engine.profiles_mod, "profile_for", transient)
    with pytest.raises(engine.RunStopped) as exc:
        engine.build_profiles([f"T{i}" for i in range(40)], use_llm=True, workers=1)
    assert exc.value.reason == "transient"
    assert calls["n"] == engine.MAX_TRANSIENT_FAILURES


# ── P2: an unparseable cached reply is not re-used ───────────────────────────
class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1

        class _Resp:
            status_code = 200

            def json(self_inner):
                return {"choices": [{"message": {"content": self._content}}],
                        "usage": {}}

        return _Resp()


def _seed_cached_reply(ticker, name, text, content, thinking):
    from RevenueChaser.module_relatedness import profiles as prof_mod
    effort = (config.REASONING_EFFORT or "").lower() or None
    key = deepseek._cache_key(prof_mod.system_prompt(), prof_mod.build_user(name, text),
                              thinking, deepseek.MODEL, effort)
    rawstore.write_rows("llm_cache", [{
        "cache_key": key, "ticker": ticker, "cik": "", "model": deepseek.MODEL,
        "thinking": int(thinking), "effort": config.REASONING_EFFORT,
        "prompt_version": config.PROMPT_VERSION, "input_hash": key,
        "response": {"content": content, "usage": {}}, "fetched_at": 1.0,
        "status": "ok"}])
    return key


@pytest.mark.parametrize("thinking", [False, True])
def test_an_unparseable_cached_reply_is_ignored_and_the_live_one_wins(
        rawstore_tmp_db, monkeypatch, thinking):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(deepseek, "key_for", lambda slug: "k")
    _seed_cached_reply("AAA", "A Corp", "text", "not json", thinking)
    client = _FakeClient('{"products": ["gpu"]}')
    prof = profiles.profile_for("AAA", "A Corp", "text", thinking=thinking,
                                effort=config.REASONING_EFFORT, client=client)
    assert prof["products"] == ["gpu"]      # the live reply, not the cached junk
    assert client.calls == 1


# ── C13 + P5 + C-F1: job messages and exit codes ─────────────────────────────
def test_cli_run_exits_zero_and_reports_a_peak_stop(rawstore_tmp_db, monkeypatch, capsys):
    import json as _json
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 4, "pending": 6, "next_off_peak": "2026-09-21 01:00",
        "stopped_reason": "peak"})
    assert cli.main(["--run"]) == 0          # a peak stop is the normal state: green (C-F1)
    out = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["done"] == 4 and out["pending"] == 6
    assert out["next_off_peak"] == "2026-09-21 01:00"
    assert out["stopped_reason"] == "peak"


def test_cli_run_exits_zero_at_the_cost_cap_and_when_work_is_pending(rawstore_tmp_db, monkeypatch, capsys):
    """The per-run cost cap and names with no text leave pending work, but not a failure (C-F1)."""
    import json as _json
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 4, "pending": 6, "next_off_peak": None})   # cap stop: no stopped_reason
    assert cli.main(["--run"]) == 0
    out = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["done"] == 4 and out["pending"] == 6


def test_cli_run_exits_one_for_an_auth_stop(rawstore_tmp_db, monkeypatch, capsys):
    import json as _json
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 0, "pending": 8, "next_off_peak": None,
        "stopped_reason": "auth", "error": "DeepSeek rejected the API key (HTTP 401)"})
    assert cli.main(["--run"]) == 1          # a bad credential the next run cannot fix (C-F1)
    out = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["stopped_reason"] == "auth" and "401" in out["error"]


def test_cli_run_exits_one_for_a_transient_stop(rawstore_tmp_db, monkeypatch, capsys):
    import json as _json
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 3, "pending": 5, "next_off_peak": None,
        "stopped_reason": "transient", "error": "DeepSeek HTTP 503"})
    assert cli.main(["--run"]) == 1          # repeated transient failures stop the run (P6, C-F1)
    out = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["stopped_reason"] == "transient"


def test_cli_run_clean_exits_zero(rawstore_tmp_db, monkeypatch, capsys):
    import json as _json
    from RevenueChaser.module_relatedness import __main__ as cli

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 5, "pending": 0, "next_off_peak": None})
    assert cli.main(["--run"]) == 0
    out = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["n"] == 5 and out["pending"] == 0


def test_dashboard_runner_raises_jobmessage_for_stopped_or_pending_work(monkeypatch):
    from RevenueChaser.app import jobs, runners

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 4, "pending": 6, "next_off_peak": "2026-09-21 01:00",
        "stopped_reason": "peak", "companies": [], "peers": {}})
    with pytest.raises(jobs.JobMessage) as exc:
        runners.run_relatedness(None, no_fetch=False)
    msg = str(exc.value)
    assert "4" in msg and "6" in msg and "2026-09-21 01:00" in msg


def test_dashboard_runner_refresh_with_pending_work_does_not_raise(monkeypatch):
    """C-F6: the JobMessage is for a Fetch; a Refresh with pending work returns the board silently."""
    from RevenueChaser.app import jobs, runners

    monkeypatch.setattr(engine, "run", lambda **k: {
        "n": 4, "pending": 6, "next_off_peak": None,
        "stopped_reason": "peak", "companies": [], "peers": {}})
    path = runners.run_relatedness(None, no_fetch=True)   # must not raise jobs.JobMessage
    assert path == runners.RELATEDNESS_LATEST


# ── C24: one current-theme-run reader ────────────────────────────────────────
def _seed_theme_run(run_id, is_current, coarse_label, medium_label, ticker):
    rawstore.write_rows("theme_runs", [{
        "id": run_id, "created_at": "2026-01-01", "k_fine": 1, "k_medium": 1,
        "k_coarse": 1, "jaccard": 1.0, "is_current": is_current}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{run_id}:c0", "level": "coarse", "parent_id": "",
         "label": coarse_label, "size": 5},
        {"theme_id": f"{run_id}:m0", "level": "medium", "parent_id": f"{run_id}:c0",
         "label": medium_label, "size": 20},
    ])
    rawstore.write_rows("ticker_themes", [
        {"ticker": ticker, "theme_id": f"{run_id}:c0", "theme": coarse_label,
         "level": "coarse", "theme_run_id": run_id},
        {"ticker": ticker, "theme_id": f"{run_id}:m0", "theme": medium_label,
         "level": "medium", "theme_run_id": run_id},
    ])


def test_one_current_theme_run_reader_and_one_coarse_label(rawstore_tmp_db):
    from RevenueChaser.core import universe
    from RevenueChaser.module_relatedness import themes
    from RevenueChaser.module_sector_rotation import theme_focus

    _seed_theme_run("old", 0, "Old Coarse", "Old Child", "OLD")
    _seed_theme_run("cur", 1, "Financials (2)", "Regional Banks", "JPM")

    assert universe.current_theme_run() == "cur"     # the one public reader
    assert themes.current_run_id() == "cur"

    cat = universe.theme_catalog()
    assert cat["cur:c0"]["label"] == "Regional Banks"    # sector-named -> largest child
    assert "Old Coarse" not in {t["label"] for t in cat.values()}

    levels = engine._theme_levels()
    assert levels["JPM"]["coarse"] == "Regional Banks"   # label from theme_catalog
    assert "OLD" not in levels                            # only the current run

    catalog = theme_focus.coarse_theme_catalog()
    assert "Regional Banks" in catalog
    assert "Financials (2)" not in catalog


# ── C22: a prompt_version bump misses the LLM cache ──────────────────────────
def test_a_prompt_version_bump_misses_the_llm_cache(rawstore_tmp_db, monkeypatch):
    monkeypatch.setenv("RC_DEEPSEEK_OFF_PEAK_ONLY", "0")
    monkeypatch.setattr(deepseek, "key_for", lambda slug: "k")
    monkeypatch.setattr(config, "PROMPT_VERSION", "v1")
    client = _FakeClient('{"products": ["gpu"]}')
    profiles.profile_for("AAA", "A", "text", thinking=False, client=client)
    assert client.calls == 1
    profiles.profile_for("AAA", "A", "text", thinking=False, client=client)
    assert client.calls == 1                       # same version -> cache hit
    monkeypatch.setattr(config, "PROMPT_VERSION", "v2")
    profiles.profile_for("AAA", "A", "text", thinking=False, client=client)
    assert client.calls == 2                       # version bump -> cache miss







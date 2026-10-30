"""Relatedness off-peak boundary, $ cap, and CLI (no HTTP)."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek
from RevenueChaser.module_relatedness import config
from RevenueChaser.module_relatedness import engine
from RevenueChaser.module_relatedness.tests.test_relatedness import long_body


def _seed_filings(n):
    rawstore.write_rows("chain_filings", [
        {"ticker": f"T{i}", "cik": str(i), "form": "10-K", "filed": "2026-01-01",
         "accession": "a", "primary_doc": "u", "item_text": long_body("we make things"),
         "quality": "ok"} for i in range(n)])


def _no_cik(monkeypatch):
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})


def test_usd_cap_limits_calls(rawstore_tmp_db, monkeypatch):
    _no_cik(monkeypatch)
    monkeypatch.setattr(engine.config, "EST_USD_PER_CALL", 0.001)
    _seed_filings(5)
    calls = {"n": 0}

    def fake_profile(ticker, name, text, **k):
        calls["n"] += 1
        return {f: ["x"] for f in config.PROFILE_FIELDS}

    monkeypatch.setattr(engine.profiles_mod, "profile_for", fake_profile)
    engine.build_profiles([f"T{i}" for i in range(5)], usd_cap=0.003)  # -> 3 calls
    assert calls["n"] == 3


def test_peak_boundary_returns_pending(rawstore_tmp_db, monkeypatch, tmp_path):
    _no_cik(monkeypatch)
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    _seed_filings(2)
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {"no_fetch": True})

    def boom(*a, **k):
        raise deepseek.PeakHoursError(datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(engine.profiles_mod, "profile_for", boom)
    payload = engine.run(no_fetch=False, tickers=["T0", "T1"])
    assert payload["pending"] == 2
    assert payload["next_off_peak"] == "2026-09-21 01:00"
    # the run marker is recorded
    assert rawstore.get("run_registry", "relatedness_run")["value"]["pending"] == 2


def test_cli_status(rawstore_tmp_db, capsys, monkeypatch):
    from RevenueChaser.core import subscription_usage
    from RevenueChaser.module_relatedness import __main__ as cli
    monkeypatch.setattr(subscription_usage, "check", lambda: [])
    assert cli.main(["--status"]) == 0
    assert "DeepSeek" in capsys.readouterr().out


def test_cli_status_prints_subscription_lines(rawstore_tmp_db, capsys, monkeypatch):
    from RevenueChaser.core import subscription_usage
    from RevenueChaser.module_relatedness import __main__ as cli
    monkeypatch.setattr(
        subscription_usage, "check", lambda: [("Grok", "Grok subscription: weekly 100% used (cap 90%) — paused.")]
    )
    assert cli.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "Grok subscription: weekly 100% used" in out
    assert "paused" in out


def test_cli_status_announces_peak_override(rawstore_tmp_db, capsys, monkeypatch):
    from RevenueChaser.core import offpeak, subscription_usage
    from RevenueChaser.module_relatedness import __main__ as cli
    monkeypatch.setattr(subscription_usage, "check", lambda: [])
    monkeypatch.setattr(offpeak, "is_off_peak", lambda now=None: False)
    monkeypatch.setattr(deepseek, "peak_override_active", lambda now=None: True)
    cli.main(["--status"])
    assert "Peak override ON" in capsys.readouterr().out


def test_cli_status_is_quiet_without_override(rawstore_tmp_db, capsys, monkeypatch):
    from RevenueChaser.core import offpeak, subscription_usage
    from RevenueChaser.module_relatedness import __main__ as cli
    monkeypatch.setattr(subscription_usage, "check", lambda: [])
    monkeypatch.setattr(offpeak, "is_off_peak", lambda now=None: False)
    cli.main(["--status"])  # the autouse fixture points the gate at an empty temp file
    assert "Peak override" not in capsys.readouterr().out


def test_cli_lock_skip_is_non_zero_but_a_peak_stop_is_green(monkeypatch, capsys):
    """#130/#118 C13 + C-F1: a skipped (locked) run exits 1; a peak/pending run exits 0."""
    from RevenueChaser.module_relatedness import __main__ as cli

    def locked(**k):
        raise engine.AlreadyRunning("Another Relatedness run is already in progress.")

    monkeypatch.setattr(cli.engine, "run", locked)
    assert cli.main(["--run"]) == 1
    assert "skipped" in capsys.readouterr().out

    monkeypatch.setattr(cli.engine, "run", lambda **k: {
        "n": 4, "pending": 6, "next_off_peak": "2026-09-21 01:00",
        "stopped_reason": "peak"})
    assert cli.main(["--run"]) == 0
    out = capsys.readouterr().out
    assert '"pending": 6' in out and "next_off_peak" in out

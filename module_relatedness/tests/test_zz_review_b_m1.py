"""Review B M1 and the R8a delta review DR-6: a Grok run writes nothing and asks SEC nothing before its login and
allowance are read, on every Grok command; a Grok --run never fetches filings (zero SEC), a DeepSeek one still does."""
import json

import pytest

from RevenueChaser.core import rawstore, subscription_usage
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness import engine, themes
from RevenueChaser.module_relatedness.tests.test_grok_writer_388 import (  # noqa: F401
    RESETS_AT_TEXT, TICKERS, TOKEN, _window, deepseek_calls, world)
from RevenueChaser.module_relatedness.tests.test_naming import _seed_run
from RevenueChaser.module_relatedness.tests.test_relatedness import _seed_universe


@pytest.fixture
def corpus(world, monkeypatch):
    calls = []
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda ts, **k: calls.append(k) or {})
    return calls


def _nothing_written(tmp_path):
    assert rawstore.get("run_registry", "relatedness_run") is None
    assert rawstore.get_all("relatedness_profiles") == []
    runs = rawstore.query("SELECT count(*) AS n FROM result_runs", db="history")
    assert runs == [] or runs[0]["n"] == 0             # [] = the history file was never created
    assert not (tmp_path / "r.json").exists()


def test_a_missing_login_writes_nothing_and_asks_sec_nothing(world, corpus, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "absent.json"))
    assert cli.main(["--run", "--provider", "grok", "--limit", "10"]) == 1
    out = capsys.readouterr().out
    assert '"stopped_reason": "auth"' in out and TOKEN not in out
    assert corpus == [] and world.posts == []
    _nothing_written(tmp_path)


def test_a_spent_allowance_writes_nothing_and_asks_sec_nothing(world, corpus, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(subscription_usage, "fetch_grok", lambda now=None: [_window(95)])
    assert cli.main(["--run", "--provider", "grok", "--limit", "10"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["stopped_reason"] == "allowance" and out["next_reset"] == RESETS_AT_TEXT
    assert corpus == [] and world.posts == []
    _nothing_written(tmp_path)


def test_rebuild_and_name_with_a_missing_login_switches_nothing(world, monkeypatch, tmp_path):
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(themes, "build_shadow", lambda **k: pytest.fail("rebuilt before the login was read"))
    assert cli.main(["--rebuild-themes", "--name", "--provider", "grok", "--run-id", "new"]) == 1


def test_a_grok_run_fetches_no_filing(world, corpus):
    engine.run(provider="grok", max_calls=10)
    assert corpus == [{"no_fetch": True}]


def test_a_deepseek_run_still_fetches_filings(world, corpus, deepseek_calls):
    engine.run(provider="deepseek", max_calls=10)
    assert corpus == [{"no_fetch": False}]                # the nightly downloads the filings it profiles


def _tables() -> dict:
    """Every raw and history table's rows (an absent history file reads as no tables)."""
    out = {}
    for db in ("raw", "history"):
        try:
            names = [r["table_name"] for r in rawstore.query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'", db=db)]
        except Exception:  # noqa: BLE001 - the history store was never created
            names = []
        for n in names:
            out[f"{db}.{n}"] = sorted(json.dumps(r, sort_keys=True, default=str)
                                      for r in rawstore.query(f'SELECT * FROM "{n}"', db=db))
    return out


def _unreadable(now=None):
    raise subscription_usage.SubscriptionUsageError("billing down")


_STATES = {
    "login missing": (1, lambda mp, tmp: mp.setenv("RC_XAI_AUTH_FILE", str(tmp / "absent.json"))),
    "allowance spent": (0, lambda mp, tmp: mp.setattr(subscription_usage, "fetch_grok",
                                                       lambda now=None: [_window(95)])),
    "allowance unreadable": (0, lambda mp, tmp: mp.setattr(subscription_usage, "fetch_grok", _unreadable)),
}
_COMMANDS = {
    "run": ["--run", "--provider", "grok", "--limit", "10"],
    "name themes": ["--name-themes", "--provider", "grok"],
    "rebuild and name": ["--rebuild-themes", "--name", "--provider", "grok", "--run-id", "new"],
}


@pytest.mark.parametrize("state", list(_STATES))
@pytest.mark.parametrize("command", list(_COMMANDS))
def test_every_grok_command_stops_before_any_write_or_request(world, corpus, monkeypatch, tmp_path, capsys,
                                                               command, state):
    _seed_run("old")                                      # unnamed themes, so naming has something to name
    _seed_universe(TICKERS)
    rc, apply = _STATES[state]
    apply(monkeypatch, tmp_path)
    built = []
    monkeypatch.setattr(themes, "build_shadow", lambda **k: built.append(k) or {"run_id": None})
    before = _tables()
    assert cli.main(_COMMANDS[command]) == rc
    out = capsys.readouterr().out
    assert _tables() == before and world.posts == [] and corpus == [] and built == []
    assert not (tmp_path / "r.json").exists() and TOKEN not in out

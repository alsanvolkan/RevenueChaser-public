"""Review B S1, S3 and the --limit refusal (proposed tests, not committed)."""
import json

import pytest

from RevenueChaser.core import rawstore, subscription_usage
from RevenueChaser.core.providers import grok
from RevenueChaser.module_relatedness import __main__ as cli
from RevenueChaser.module_relatedness.tests.test_grok_writer_388 import world  # noqa: F401

_FETCH_GROK = subscription_usage.fetch_grok          # the real reader, before the fixture replaces it


@pytest.mark.parametrize("body", [{}, {"error": "unauthorized"}, {"config": None}, []])
def test_a_billing_reply_without_config_is_no_reading(body):
    with pytest.raises(subscription_usage.SubscriptionUsageError):
        subscription_usage.parse_grok_billing(body)


def test_a_billing_reply_without_config_stops_the_grok_gate(world, monkeypatch):
    monkeypatch.setattr(subscription_usage, "_get_json", lambda *a, **k: {"error": "changed shape"})
    monkeypatch.setattr(subscription_usage, "fetch_grok", _FETCH_GROK)
    grok.reset_gate()
    with pytest.raises(grok.GrokAllowanceError):
        grok.complete("s", "u", use_cache=False)
    assert world.posts == []


def test_a_grok_name_that_names_nothing_is_not_cached(world):
    from RevenueChaser.module_relatedness import naming
    from RevenueChaser.module_relatedness.tests.test_naming import _seed_run
    run_id = _seed_run()
    world.reply = "I cannot help with that."
    first = naming.name_themes(run_id, complete=grok.complete)
    assert first["named"] == 0
    assert rawstore.get_all("llm_cache") == []
    n = len(world.posts)
    world.reply = json.dumps({"name": "Semiconductors"})
    second = naming.name_themes(run_id, complete=grok.complete)
    assert len(world.posts) > n and second["named"] > 0      # asked again, not replayed from the cache


def test_limit_is_refused_for_grok_naming(monkeypatch):
    monkeypatch.setattr(cli.engine, "run", lambda **k: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--name-themes", "--provider", "grok", "--limit", "20"])
    assert exit_.value.code == 2

"""R2 stack review probes (#118 PR C, S1). Copy into module_relatedness/tests/. Red on d4028bb; green with
s1_history_partial.diff (module_relatedness/history.py)."""
from __future__ import annotations

from RevenueChaser.module_relatedness import engine
from RevenueChaser.module_relatedness.tests.test_relatedness_history_388 import _headers, seeded  # noqa: F401


def test_an_auth_stop_is_partial_in_the_result_history(seeded, monkeypatch):  # noqa: F811
    """A run that stopped on a rejected key is not whole: its header should say so, like a peak stop."""
    def auth(tickers, **k):
        raise engine.RunStopped("auth", None, "DeepSeek rejected the API key (HTTP 401)")

    monkeypatch.setattr(engine, "build_profiles", auth)
    payload = engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    assert payload["stopped_reason"] == "auth"
    (head,) = _headers()
    assert head["quality"] == "partial", head


def test_a_transient_stop_is_partial_in_the_result_history(seeded, monkeypatch):  # noqa: F811
    def transient(tickers, **k):
        raise engine.RunStopped("transient", None, "DeepSeek HTTP 503")

    monkeypatch.setattr(engine, "build_profiles", transient)
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial", head

"""The e2e environment must keep every LLM and provider call off the real internet (#167 / P22).

``e2e_env`` is what the dashboard subprocess starts with. DeepSeek was missing
from it, so a board that called DeepSeek would have used the developer's real
key from ``.env`` and the real host. These checks read the fixture only: no
dashboard, no request, no POST.
"""
from __future__ import annotations

from RevenueChaser.core.providers import deepseek


def test_e2e_env_points_deepseek_at_the_mock(e2e_env, provider_routes):
    assert e2e_env["RC_DEEPSEEK_BASE"] == provider_routes.url


def test_e2e_env_gives_deepseek_a_dummy_key_and_opens_the_off_peak_gate(e2e_env):
    key = e2e_env["DEEPSEEK_API_KEY"]
    assert key and key == "e2e-test-token"
    # The gate would otherwise skip the call at peak hours and the test would depend on the clock.
    assert e2e_env["RC_DEEPSEEK_OFF_PEAK_ONLY"] == "0"


def test_the_client_resolves_the_mock_from_that_environment(e2e_env, provider_routes, monkeypatch):
    for name, value in e2e_env.items():
        monkeypatch.setenv(name, value)
    assert deepseek.base_url() == provider_routes.url
    assert not deepseek.off_peak_only()

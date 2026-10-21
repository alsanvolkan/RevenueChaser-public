"""SourceResult, env-overridable base URLs, ticker normalisation, redaction, credentials."""
from __future__ import annotations

import pytest

from RevenueChaser.core import credentials
from RevenueChaser.core.identity import sources as s


def test_status_is_validated():
    assert s.SourceResult("x", "ok").ok
    assert not s.SourceResult("x", "error", error="boom").ok
    with pytest.raises(ValueError):
        s.SourceResult("x", "weird")


def test_result_gets_a_fetched_at():
    r = s.SourceResult("x", "ok")
    assert r.rows == [] and r.fetched_at.endswith("Z") and len(r.fetched_at) == 20


@pytest.mark.parametrize("raw,want", [("aapl", "AAPL"), ("BRK.B", "BRK-B"), ("BRK-B", "BRK-B"),
                                       ("ABC$", "ABC-P"), (" msft ", "MSFT"), (None, "")])
def test_norm_ticker(raw, want):
    assert s.norm_ticker(raw) == want


def test_base_url_env_override_and_trailing_slash(monkeypatch):
    assert s.base_url("FDIC", "https://api.fdic.gov") == "https://api.fdic.gov"
    monkeypatch.setenv("RC_FDIC_BASE", "http://127.0.0.1:9/")
    assert s.base_url("FDIC", "https://api.fdic.gov") == "http://127.0.0.1:9"


def test_redact_removes_configured_keys_and_bearer_tokens(monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "figi-secret-123")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw-secret-456")
    text = s.redact("failed figi-secret-123 and uw-secret-456; Authorization: Bearer abcdef123456")
    assert "figi-secret-123" not in text and "uw-secret-456" not in text
    assert "abcdef123456" not in text and "<redacted>" in text


def test_fail_builds_a_redacted_error_result(monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "figi-secret-123")
    r = s.fail("openfigi", RuntimeError("bad key figi-secret-123"))
    assert r.status == "error" and "figi-secret-123" not in r.error
    assert r.error.startswith("RuntimeError")


def test_openfigi_provider_is_registered(monkeypatch):
    p = credentials.get("openfigi")
    assert p.env_var == "OPENFIGI_API_KEY"
    assert p.signup_url == "https://www.openfigi.com/api"
    assert "Identity refresh" in p.required_for and "optional" in p.required_for
    monkeypatch.setenv("OPENFIGI_API_KEY", "abc")
    assert credentials.key_for("openfigi") == "abc"

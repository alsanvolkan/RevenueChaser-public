"""Credential messages must tell the user what to set, where, and where to get it.

The old failure mode was a board that silently rendered "no data" when the key
was missing or expired. These tests pin the *content* of the replacement
messages, because a vague message is the bug.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import credentials

# Never a real key: a recognisable string whose only job is to be asserted ABSENT from every message (PL8, #26).
_DUMMY_KEY = "pl8-dummy-provider-key-value"


class TestProviderRegistry:
    def test_every_provider_declares_an_env_var_and_a_signup_url(self):
        assert credentials.PROVIDERS, "registry must not be empty"
        for slug, p in credentials.PROVIDERS.items():
            assert p.slug == slug
            assert p.env_var.isupper()
            assert p.signup_url.startswith("https://")
            assert p.label and p.required_for

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            credentials.get("nope")


class TestMissingMessage:
    def test_names_the_variable_the_file_and_the_signup_url(self):
        msg = credentials.missing_message("unusual_whales")
        assert "UNUSUAL_WHALES_API_KEY" in msg
        assert ".env" in msg
        assert "https://unusualwhales.com/api" in msg
        assert "missing" in msg.lower()

    def test_says_which_boards_break(self):
        assert "SPX Gamma" in credentials.missing_message("unusual_whales")

    def test_covers_the_other_providers(self):
        fred = credentials.missing_message("fred")
        assert "FRED_API_KEY" in fred and "stlouisfed" in fred
        openfigi = credentials.missing_message("openfigi")
        assert "OPENFIGI_API_KEY" in openfigi and "openfigi.com" in openfigi

    def test_the_fred_message_names_the_boards_it_degrades(self):
        msg = credentials.missing_message("fred")
        assert "Sector Rotation" in msg
        assert "Index membership" not in msg


class TestRejectedMessage:
    def test_distinguishes_rejected_from_missing(self):
        msg = credentials.rejected_message("unusual_whales", 401)
        assert "rejected" in msg.lower()
        assert "HTTP 401" in msg
        assert "expired" in msg
        assert "missing" not in msg.lower()

    def test_signup_url_is_present_so_the_user_can_renew(self):
        assert "https://unusualwhales.com/api" in credentials.rejected_message(
            "unusual_whales", 403)

    def test_provider_detail_is_appended_when_given(self):
        msg = credentials.rejected_message("unusual_whales", 401, detail="token dead")
        assert "token dead" in msg

    def test_status_is_optional(self):
        assert "HTTP" not in credentials.rejected_message("unusual_whales")


class TestDescribe:
    def test_missing_when_the_key_is_unset(self, monkeypatch):
        monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
        assert credentials.describe("unusual_whales") == \
            credentials.missing_message("unusual_whales")

    def test_rejected_when_the_key_is_set(self, monkeypatch):
        monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "abc")
        msg = credentials.describe("unusual_whales", 401)
        assert msg == credentials.rejected_message("unusual_whales", 401)

    def test_whitespace_only_key_counts_as_missing(self, monkeypatch):
        monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "   ")
        assert credentials.key_for("unusual_whales") == ""
        assert "missing" in credentials.describe("unusual_whales", 401).lower()

    def test_key_is_read_at_call_time_not_import_time(self, monkeypatch):
        monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "later-set")
        assert credentials.key_for("unusual_whales") == "later-set"


class TestProblem:
    """PL8 (#26): one resolver for every requirement a module can declare at submit time."""

    def test_a_provider_slug_is_missing_until_its_key_is_set(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        assert credentials.problem("fred") == credentials.missing_message("fred")
        monkeypatch.setenv("FRED_API_KEY", _DUMMY_KEY)
        assert credentials.problem("fred") is None

    def test_a_whitespace_only_provider_key_counts_as_missing(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "   ")
        assert credentials.problem("fred") == credentials.missing_message("fred")

    def test_an_extra_check_answers_through_its_own_callable(self, monkeypatch):
        """``EXTRA_CHECKS`` is empty since the Discord archive became its own app; the mechanism stays, so
        a registered check still answers through ``problem`` and still counts as a requirement name."""
        monkeypatch.setitem(credentials.EXTRA_CHECKS, "demo_check", lambda: "DEMO is not set.")
        assert credentials.problem("demo_check") == "DEMO is not set."
        assert "demo_check" in credentials.requirement_names()
        monkeypatch.setitem(credentials.EXTRA_CHECKS, "demo_check", lambda: None)
        assert credentials.problem("demo_check") is None

    def test_an_unknown_name_raises(self):
        with pytest.raises(KeyError):
            credentials.problem("nope")

    def test_requirement_names_covers_every_provider_and_every_extra_check(self):
        names = credentials.requirement_names()
        assert names == frozenset(credentials.PROVIDERS) | frozenset(credentials.EXTRA_CHECKS)
        assert {"unusual_whales", "fred"} <= names
        assert isinstance(names, frozenset)

    def test_no_message_ever_returns_a_key_value(self, monkeypatch):
        """Presence only: every provider's key is set to its own sentinel, and no message may quote any of them.

        One provider is not enough — the promise is about ``problem()`` as a whole, so each key gets a distinct
        sentinel and every message is checked against all of them.
        """
        sentinels = {slug: f"{_DUMMY_KEY}-{slug}" for slug in credentials.PROVIDERS}
        for slug, value in sentinels.items():
            monkeypatch.setenv(credentials.PROVIDERS[slug].env_var, value)
        for name in credentials.requirement_names():
            message = credentials.problem(name) or ""
            for slug, value in sentinels.items():
                assert value not in message, f"{name} quoted {slug}'s key"

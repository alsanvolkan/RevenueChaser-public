"""Credential messages must tell the user what to set, where, and where to get it.

The old failure mode was a board that silently rendered "no data" when the key
was missing or expired. These tests pin the *content* of the replacement
messages, because a vague message is the bug.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import credentials


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
        fmp = credentials.missing_message("fmp")
        assert "FMP_API_KEY" in fmp and "financialmodelingprep" in fmp
        fred = credentials.missing_message("fred")
        assert "FRED_API_KEY" in fred and "stlouisfed" in fred

    def test_fmp_message_names_etf_holdings_not_index_registry(self):
        msg = credentials.missing_message("fmp")
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


class TestDiscordProvider:
    """#72 S1: the DiscordChatExporter user token, plus the CLI path check. Values are never read."""

    def test_registered_with_the_token_variable(self):
        p = credentials.get("discord")
        assert p.env_var == "DISCORD_TOKEN" and p.signup_url.startswith("https://")

    def test_missing_message_names_the_variable(self, monkeypatch):
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
        assert "DISCORD_TOKEN" in credentials.describe("discord")

    def test_cli_problem_when_the_variable_is_unset(self, monkeypatch):
        monkeypatch.delenv("RC_DCE_CLI", raising=False)
        assert "RC_DCE_CLI" in credentials.dce_cli_problem()

    def test_cli_problem_when_the_path_is_not_a_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RC_DCE_CLI", str(tmp_path / "missing"))
        assert "RC_DCE_CLI" in credentials.dce_cli_problem()

    def test_no_cli_problem_for_an_existing_file(self, monkeypatch, tmp_path):
        exe = tmp_path / "dce"
        exe.write_text("")
        monkeypatch.setenv("RC_DCE_CLI", str(exe))
        assert credentials.dce_cli_problem() is None

    def test_guild_problem_when_the_variable_is_unset(self, monkeypatch):
        monkeypatch.delenv("RC_DISCORD_GUILD_ID", raising=False)
        # The same sentence app/dashboard.py:_discord_unready refuses /run/module_discord_discover with.
        assert credentials.discord_guild_problem() == (
            "RC_DISCORD_GUILD_ID is not set. Put the server's id in .env, then restart the dashboard.")

    def test_guild_problem_when_the_variable_is_only_whitespace(self, monkeypatch):
        monkeypatch.setenv("RC_DISCORD_GUILD_ID", "   ")
        assert credentials.discord_guild_problem() is not None

    def test_no_guild_problem_when_the_variable_is_set(self, monkeypatch):
        monkeypatch.setenv("RC_DISCORD_GUILD_ID", "123456789012345678")
        assert credentials.discord_guild_problem() is None

"""Keys come from the environment only (#360, framework D5, avo 1 Nov 2026): ``.env`` keeps paths and settings."""
from __future__ import annotations

import os

from RevenueChaser.core import config, credentials


def test_a_writer_key_in_dotenv_is_not_loaded(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EVREN_API_KEY=sentinel-e-360\nDEEPSEEK_API_KEY=sentinel-d-360\n"
                   "RC_TEST_SETTING_360=kept\n", encoding="utf-8")
    for name in ("EVREN_API_KEY", "DEEPSEEK_API_KEY", "RC_TEST_SETTING_360"):
        monkeypatch.delenv(name, raising=False)
    config.load_env_file(env)
    try:
        assert os.environ.get("RC_TEST_SETTING_360") == "kept"
        assert "EVREN_API_KEY" not in os.environ and "DEEPSEEK_API_KEY" not in os.environ
        assert credentials.key_for("evren") == "" and credentials.key_for("deepseek") == ""
    finally:
        os.environ.pop("RC_TEST_SETTING_360", None)


def test_a_real_environment_key_still_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EVREN_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("EVREN_API_KEY", "from-unit")
    config.load_env_file(env)
    assert credentials.key_for("evren") == "from-unit"


def test_the_missing_message_points_at_the_secrets_file():
    msg = credentials.missing_message("evren")
    assert "EVREN_API_KEY" in msg and "/etc/life/secrets.env" in msg and ".env file" not in msg

"""What the prices store (PR 7) adds to the shared parts: the ``prices`` serving kind, the two Alpaca credential
names (names only: a value is never read here) and the yfinance day cap."""
from __future__ import annotations

from RevenueChaser.core import credentials
from RevenueChaser.rcdata import caps, serving


def test_prices_is_a_serving_kind_of_version_1():
    assert serving.SERVING_SCHEMA_VERSION["prices"] == 1
    assert "prices" in serving.KINDS
    assert serving.live_path("prices").name == "prices.duckdb"


def test_the_alpaca_key_id_and_secret_are_two_declared_names():
    assert credentials.get("alpaca").env_var == "ALPACA_API_KEY_ID"
    assert credentials.get("alpaca_secret").env_var == "ALPACA_API_SECRET_KEY"
    assert "ALPACA_API_KEY_ID" in credentials.missing_message("alpaca")


def test_yfinance_has_a_day_cap():
    assert caps.cap_for("yfinance") == 3_000 + 2_000      # the float lookups and the fundamentals estimates
    assert caps.cap_for("alpaca") == 20_000

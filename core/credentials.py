"""Provider credentials — one place that knows which key is needed and why.

A missing or rejected API key used to surface as an empty board: providers
swallowed the HTTP error, returned ``{}``, and the dashboard rendered "no data".
An expired token and a quiet market looked identical.

This module is the single source of truth for:
  - which environment variable holds each provider's key,
  - where the user goes to get or renew one,
  - the exact message shown when the key is missing vs. rejected.

Nothing here performs I/O; :func:`describe` reads the already-loaded config so
callers can build an actionable error without importing a provider client.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Provider:
    """A credentialed data provider."""

    slug: str
    label: str          # human name shown in messages
    env_var: str        # environment variable / .env key
    signup_url: str     # where to obtain or renew the key
    required_for: str   # which boards break without it


PROVIDERS: dict[str, Provider] = {
    "unusual_whales": Provider(
        slug="unusual_whales",
        label="Unusual Whales",
        env_var="UNUSUAL_WHALES_API_KEY",
        signup_url="https://unusualwhales.com/api",
        required_for="Earnings Radar, SPX Gamma, LEAPS, Sector Rotation",
    ),
    "fred": Provider(
        slug="fred",
        label="FRED",
        env_var="FRED_API_KEY",
        signup_url="https://fredaccount.stlouisfed.org/apikeys",
        required_for="Sector Rotation RRG weekly closes; Earnings Radar R6 10Y beta (optional — missing key degrades R6 only)",
    ),
    "openfigi": Provider(
        slug="openfigi",
        label="OpenFIGI",
        env_var="OPENFIGI_API_KEY",
        signup_url="https://www.openfigi.com/api",
        required_for="Identity refresh: security type (optional; works slower without a key)",
    ),
    # PR 7 (G2): Alpaca market data, the prices store's daily bars and corporate actions. Two names: Alpaca
    # authenticates with a key id and its secret, sent as the APCA-API-KEY-ID / APCA-API-SECRET-KEY headers.
    "alpaca": Provider(
        slug="alpaca",
        label="Alpaca market data (key id)",
        env_var="ALPACA_API_KEY_ID",
        signup_url="https://app.alpaca.markets/paper/dashboard/overview",
        required_for="the prices store's daily bars and corporate actions (rcdata alpaca/bars, alpaca/corporate_actions)",
    ),
    "alpaca_secret": Provider(
        slug="alpaca_secret",
        label="Alpaca market data (secret key)",
        env_var="ALPACA_API_SECRET_KEY",
        signup_url="https://app.alpaca.markets/paper/dashboard/overview",
        required_for="the prices store's daily bars and corporate actions (with ALPACA_API_KEY_ID)",
    ),
    "deepseek": Provider(
        slug="deepseek",
        label="DeepSeek",
        env_var="DEEPSEEK_API_KEY",
        signup_url="https://platform.deepseek.com/api_keys",
        required_for="Relatedness profile extraction and theme naming (optional; off-peak UTC only — see core/offpeak.py)",
    ),
    "claude_subscription": Provider(
        slug="claude_subscription",
        label="Claude subscription",
        env_var="RC_CLAUDE_OAUTH_FILE",
        signup_url="https://claude.ai/settings/usage",
        required_for=(
            "the optional Claude allowance gate (core/subscription_usage). The "
            "token itself lives in the Claude Code login at ~/.claude/.credentials.json; "
            "this variable only overrides that path"
        ),
    ),
    "grok_subscription": Provider(
        slug="grok_subscription",
        label="SuperGrok subscription",
        env_var="RC_XAI_AUTH_FILE",
        signup_url="https://grok.com/?_s=usage",
        required_for=(
            "the optional Grok allowance gate (core/subscription_usage) and the optional "
            "Grok writer of Relatedness profiles and theme names (core/providers/grok.py, "
            "--provider grok). The token comes from Hermes's xAI OAuth login in auth.json; "
            "this variable only overrides that path"
        ),
    ),
}


def get(slug: str) -> Provider:
    """Look up a provider by slug; raises KeyError for an unknown one."""
    return PROVIDERS[slug]


def key_for(slug: str) -> str:
    """Current value of a provider's key, or ``""`` when unset.

    Read from the environment at call time rather than import time so a key set
    after startup (or patched in a test) is picked up.
    """
    return (os.getenv(PROVIDERS[slug].env_var) or "").strip()


def missing_message(slug: str) -> str:
    """Actionable message for a key that is not set at all."""
    p = PROVIDERS[slug]
    return (
        f"{p.label} API key is missing. Set {p.env_var} in the .env file at the "
        f"repo root, then restart the dashboard. Get a key at {p.signup_url}. "
        f"Needed for: {p.required_for}."
    )


def rejected_message(slug: str, status: int | None = None, detail: str = "") -> str:
    """Actionable message for a key the provider refused (401/403)."""
    p = PROVIDERS[slug]
    code = f" (HTTP {status})" if status else ""
    tail = f" Provider said: {detail.strip()}" if detail.strip() else ""
    return (
        f"{p.label} API key was rejected{code} — it is set but not valid "
        f"(expired, revoked, or the subscription lapsed). Renew it at "
        f"{p.signup_url}, then update {p.env_var} in .env and restart the "
        f"dashboard.{tail}"
    )


def describe(slug: str, status: int | None = None, detail: str = "") -> str:
    """Missing-vs-rejected message, chosen by whether the key is set."""
    if not key_for(slug):
        return missing_message(slug)
    return rejected_message(slug, status, detail)


# A requirement that is more than "an env var is non-empty": the check answers for itself, and its message is
# the one a submit shows. Empty since the Discord archive became its own app, which took the only two with it;
# the mechanism stays because ``jobs.register(..., requires=...)`` still accepts such a name.
EXTRA_CHECKS: dict[str, Callable[[], str | None]] = {}


def requirement_names() -> frozenset[str]:
    """Every name :func:`problem` accepts: the provider slugs plus :data:`EXTRA_CHECKS`."""
    return frozenset(PROVIDERS) | frozenset(EXTRA_CHECKS)


def problem(name: str) -> str | None:
    """Why the requirement ``name`` is unmet, or ``None`` when it is.

    A module declares the names it cannot run without (``jobs.register(..., requires=...)``) and the dashboard
    asks this before queueing it, so a missing credential is refused at the button rather than minutes later
    from inside a runner.

    **Presence only.** A provider slug is satisfied when its key is set — this never makes a request and never
    compares, logs or returns a key value. Validity is the provider client's business (``UWClient.verify()``
    runs a real authenticated probe).

    Raises ``KeyError`` for a name that is neither a provider slug nor an extra check.
    """
    if name in PROVIDERS:
        return None if key_for(name) else missing_message(name)
    check = EXTRA_CHECKS.get(name)
    if check is None:
        raise KeyError(f"unknown credential requirement: {name!r}")
    return check()

"""Claude / Grok allowance gate — pure decision plus fetch error handling.

No network: ``su._open`` (the one patch point of the no-redirect opener) is replaced by a
recorder, and every credential path is redirected into ``tmp_path`` through the documented env
overrides.
"""
from __future__ import annotations

import email.message
import io
import json
import logging
import urllib.error
import urllib.request
import urllib.response
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from RevenueChaser.core import subscription_usage as su

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _window(key: str, percent: float, hours: float = 2.0) -> su.Window:
    return su.Window(key=key, percent=percent, resets_at=NOW + timedelta(hours=hours))


class FakeUrlOpen:
    """Records requests; returns a payload or raises a canned error."""

    def __init__(self) -> None:
        self.calls: list[urllib.request.Request] = []
        self.body: dict = {}
        self.bodies: dict[str, dict] = {}
        self.error: Exception | None = None

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        body = json.dumps(self.bodies.get(request.full_url, self.body)).encode("utf-8")

        class _Resp:
            def read(self_inner):
                return body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Resp()


@pytest.fixture
def urlopen(monkeypatch):
    fake = FakeUrlOpen()
    monkeypatch.setattr(su, "_open", fake)
    return fake


@pytest.fixture
def claude_login(tmp_path, monkeypatch):
    """A valid Claude Code login file; returns its path."""
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sentinel-claude-token", "expiresAt": 1900000000000}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RC_CLAUDE_OAUTH_FILE", str(path))
    return path


@pytest.fixture
def grok_login(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": "sentinel-xai-token"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(path))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    return path


# ── the pure decision ───────────────────────────────────────────────────────

def test_within_budget_accepts_roomy_windows():
    assert su.is_within_budget([_window("five_hour", 37), _window("seven_day", 4)]) is True


@pytest.mark.parametrize(
    "key,percent,expected",
    [("five_hour", 79.9, True), ("five_hour", 80, False), ("seven_day", 89.9, True), ("seven_day", 90, False), ("seven_day", 100, False)],
)
def test_within_budget_thresholds(key, percent, expected):
    assert su.is_within_budget([_window(key, percent)]) is expected


def test_within_budget_blocks_on_a_single_exhausted_window():
    assert su.is_within_budget([_window("five_hour", 10), _window("seven_day", 100)]) is False


def test_within_budget_without_any_reading_is_blocked():
    assert su.is_within_budget([]) is False
    assert su.is_within_budget([None]) is False


def test_within_budget_honours_custom_caps():
    windows = [_window("five_hour", 50)]
    assert su.is_within_budget(windows, max_session=40) is False
    assert su.is_within_budget(windows, max_session=60) is True


def test_next_available_returns_the_earliest_blocking_reset():
    windows = [_window("five_hour", 85, hours=2), _window("seven_day", 95, hours=120)]
    assert su.next_available(windows, now=NOW) == NOW + timedelta(hours=2)


def test_next_available_none_when_everything_has_room():
    assert su.next_available([_window("five_hour", 37)], now=NOW) is None


def test_next_available_ignores_resets_already_in_the_past():
    stale = su.Window(key="five_hour", percent=100, resets_at=NOW - timedelta(minutes=1))
    assert su.next_available([stale], now=NOW) is None


def test_next_available_ignores_windows_without_a_reset():
    blocked = su.Window(key="seven_day", percent=100, resets_at=None)
    assert su.next_available([blocked], now=NOW) is None


def test_describe_reports_ok_then_paused_with_a_reset():
    ok = su.describe("Claude", [_window("five_hour", 37)], now=NOW)
    assert "OK to run" in ok and "session 37%" in ok

    paused = su.describe("Grok", [_window("seven_day", 100, hours=72)], now=NOW)
    assert "paused" in paused
    assert "Next reset: 2026-09-27 12:00 UTC" in paused
    assert "cap 90%" in paused


def test_describe_handles_no_reading():
    assert "unavailable" in su.describe("Claude", [])


# ── parsers, against the real captured payloads ──────────────────────────────

def test_parse_claude_usage_reads_the_real_payload():
    body = json.loads((FIXTURES / "claude_oauth_usage.json").read_text(encoding="utf-8"))
    windows = su.parse_claude_usage(body)
    keys = [w.key for w in windows]
    assert keys == ["five_hour", "seven_day"], "null windows (opus/sonnet) are skipped"
    assert windows[0].percent == 37.0
    assert windows[0].is_session and not windows[0].is_weekly
    assert windows[1].is_weekly
    assert windows[1].resets_at == datetime(2026, 9, 30, 8, 0, 0, 232010, tzinfo=timezone.utc)


def test_parse_claude_usage_tolerates_junk():
    assert su.parse_claude_usage({}) == []
    assert su.parse_claude_usage({"five_hour": {"utilization": None}}) == []
    assert su.parse_claude_usage({"five_hour": "nope"}) == []


def test_parse_grok_billing_reads_the_real_payload():
    body = json.loads((FIXTURES / "grok_billing_credits.json").read_text(encoding="utf-8"))
    windows = su.parse_grok_billing(body)
    assert len(windows) == 1
    assert windows[0].percent == 100.0
    assert windows[0].is_weekly and not windows[0].is_session
    assert windows[0].resets_at == datetime(2026, 9, 27, 12, 33, 4, 233547, tzinfo=timezone.utc)


def test_parse_grok_billing_treats_a_missing_percent_as_zero():
    windows = su.parse_grok_billing({"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"}}})
    assert windows[0].percent == 0.0


def test_parse_grok_billing_falls_back_to_the_billing_period():
    windows = su.parse_grok_billing({"config": {"billingPeriodEnd": "2026-10-01T00:00:00+00:00"}})
    assert windows[0].resets_at == datetime(2026, 10, 1, tzinfo=timezone.utc)


# ── fetchers ────────────────────────────────────────────────────────────────

def test_fetch_claude_parses_and_sends_the_bearer_token(claude_login, urlopen):
    urlopen.body = json.loads((FIXTURES / "claude_oauth_usage.json").read_text(encoding="utf-8"))
    windows = su.fetch_claude(NOW)
    assert [w.key for w in windows] == ["five_hour", "seven_day"]
    assert len(urlopen.calls) == 1
    request = urlopen.calls[0]
    assert request.full_url == su.DEFAULT_CLAUDE_USAGE_URL
    assert request.get_header("Authorization") == "Bearer sentinel-claude-token"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"


@pytest.mark.parametrize("override", [
    "https://example.invalid/usage",
    "http://127.0.0.1.evil.example/usage",        # loopback look-alike host name
    "http://127.0.0.1@evil.example/usage",        # loopback in the userinfo, evil host
    "https://localhost.evil.example/usage",
    "http://localhost.evil.example/usage",        # same, plain http
    "http://notlocalhost/usage",                  # ends with "localhost" but is another host
    "http://evil-127.0.0.1/usage",                # ends with the loopback address
    "http://127.0.0.10/usage",                    # starts with the loopback address
    "http://evil.example/#@127.0.0.1",
    "ftp://127.0.0.1/usage",                      # loopback but not http(s)
    "file:///etc/passwd",
    "not a url",
])
def test_fetch_claude_rejects_a_non_loopback_override(claude_login, urlopen, monkeypatch, caplog, override):
    """The bearer token must never be sent to a host an env var chose: the default URL is used."""
    monkeypatch.setenv("RC_CLAUDE_USAGE_URL", override)
    urlopen.body = {}
    with caplog.at_level(logging.WARNING, logger=su.logger.name):
        su.fetch_claude(NOW)
    assert [c.full_url for c in urlopen.calls] == [su.DEFAULT_CLAUDE_USAGE_URL]
    assert any("RC_CLAUDE_USAGE_URL" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("override", [
    "http://127.0.0.1:8765/usage",
    "https://127.0.0.1:8765/usage",
    "http://localhost:8765/usage",
    "http://LOCALHOST:8765/usage",
    "http://[::1]:8765/usage",
])
def test_fetch_claude_accepts_a_loopback_override(claude_login, urlopen, monkeypatch, override):
    monkeypatch.setenv("RC_CLAUDE_USAGE_URL", override)
    urlopen.body = {}
    su.fetch_claude(NOW)
    assert [c.full_url for c in urlopen.calls] == [override]


def test_fetch_grok_rejects_a_non_loopback_override(grok_login, urlopen, monkeypatch, caplog):
    monkeypatch.setenv("RC_GROK_BILLING_URL", "https://example.invalid/billing")
    urlopen.body = {"config": {}}         # a billing reply needs its config (review B S1)
    with caplog.at_level(logging.WARNING, logger=su.logger.name):
        su.fetch_grok(NOW)
    assert [c.full_url for c in urlopen.calls] == [su.DEFAULT_GROK_BILLING_URL]
    assert any("RC_GROK_BILLING_URL" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("override", [
    "http://localhost.evil.example/billing",
    "http://notlocalhost/billing",
    "http://127.0.0.10/billing",
])
def test_fetch_grok_rejects_a_look_alike_of_a_loopback_host(grok_login, urlopen, monkeypatch, override):
    monkeypatch.setenv("RC_GROK_BILLING_URL", override)
    urlopen.body = {"config": {}}
    su.fetch_grok(NOW)
    assert [c.full_url for c in urlopen.calls] == [su.DEFAULT_GROK_BILLING_URL]


def test_fetch_grok_accepts_a_loopback_override(grok_login, urlopen, monkeypatch):
    monkeypatch.setenv("RC_GROK_BILLING_URL", "http://127.0.0.1:8765/billing")
    urlopen.body = {"config": {}}
    su.fetch_grok(NOW)
    assert [c.full_url for c in urlopen.calls] == ["http://127.0.0.1:8765/billing"]


def test_the_default_usage_urls_are_https():
    assert su.DEFAULT_CLAUDE_USAGE_URL.startswith("https://")
    assert su.DEFAULT_GROK_BILLING_URL.startswith("https://")


def test_a_blank_override_uses_the_default(claude_login, urlopen, monkeypatch):
    monkeypatch.setenv("RC_CLAUDE_USAGE_URL", "   ")
    urlopen.body = {}
    su.fetch_claude(NOW)
    assert urlopen.calls[0].full_url == su.DEFAULT_CLAUDE_USAGE_URL


# --- redirects are never followed with the token (D6) -----------------------------------
#
# urllib's default redirect handler re-sends every header, ``Authorization`` included, to the
# ``Location`` host. The fake handler below plays the network: it answers every request with a
# 302 to another host and records what it was asked for, so no socket is involved.

class _Redirector(urllib.request.BaseHandler):
    handler_order = 100          # ahead of the real HTTPSHandler, which is never reached

    def __init__(self, seen):
        self.seen = seen

    def https_open(self, req):
        self.seen.append((req.full_url, req.get_header("Authorization")))
        headers = email.message.Message()
        headers["Location"] = "https://evil.invalid/steal"
        resp = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        resp.msg = "Found"
        return resp


@pytest.fixture
def redirecting_network(monkeypatch):
    """The real ``su._open`` with a fake transport that answers 302 to another host."""
    seen: list[tuple[str, str | None]] = []
    real_build = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: real_build(*h, _Redirector(seen)))
    return seen


def test_the_opener_does_not_follow_a_redirect(redirecting_network):
    req = urllib.request.Request("https://api.example/usage", headers={"Authorization": "Bearer t"})
    with pytest.raises(urllib.error.HTTPError) as err:
        su._open(req, 5)
    assert err.value.code == 302
    assert redirecting_network == [("https://api.example/usage", "Bearer t")]   # evil.invalid never asked


def test_a_redirect_from_the_usage_endpoint_is_an_error_and_the_token_goes_nowhere_else(
        claude_login, redirecting_network):
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.fetch_claude(NOW)
    assert "302" in str(err.value) and "sentinel-claude-token" not in str(err.value)
    assert [url for url, _ in redirecting_network] == [su.DEFAULT_CLAUDE_USAGE_URL]


def test_expired_claude_login_makes_no_network_call(tmp_path, monkeypatch, urlopen):
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sentinel-claude-token", "expiresAt": 1000}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RC_CLAUDE_OAUTH_FILE", str(path))
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.fetch_claude(NOW)
    assert "expired" in str(err.value)
    assert urlopen.calls == [], "an expired login must not be used or refreshed"


def test_missing_claude_login_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_CLAUDE_OAUTH_FILE", str(tmp_path / "absent.json"))
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.fetch_claude(NOW)
    message = str(err.value)
    assert "RC_CLAUDE_OAUTH_FILE" in message and "absent.json" in message


def test_fetch_grok_reads_hermes_auth_json(grok_login, urlopen):
    urlopen.body = json.loads((FIXTURES / "grok_billing_credits.json").read_text(encoding="utf-8"))
    windows = su.fetch_grok(NOW)
    assert windows[0].percent == 100.0
    assert urlopen.calls[0].get_header("Authorization") == "Bearer sentinel-xai-token"
    assert urlopen.calls[0].full_url == su.DEFAULT_GROK_BILLING_URL


def test_xai_env_token_wins_over_the_auth_file(grok_login, urlopen, monkeypatch):
    monkeypatch.setenv("XAI_OAUTH_TOKEN", "sentinel-env-token")
    urlopen.body = {"config": {}}
    su.fetch_grok(NOW)
    assert urlopen.calls[0].get_header("Authorization") == "Bearer sentinel-env-token"


def test_missing_grok_login_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.fetch_grok(NOW)
    assert "absent.json" in str(err.value)


# ── xai_token(): the one reader of the SuperGrok login (the allowance gate and the Grok writer, #388) ──

def _xai_login(tmp_path, monkeypatch, **tokens):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": "sentinel-xai-token",
                                                                       **tokens}}}}), encoding="utf-8")
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(path))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)


def _jwt(exp: float) -> str:
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp, 'sub': 'x'})}.sig"


def test_xai_token_reads_the_hermes_login(tmp_path, monkeypatch):
    _xai_login(tmp_path, monkeypatch)
    assert su.xai_token(NOW) == "sentinel-xai-token"


def test_xai_token_prefers_the_env_token(grok_login, monkeypatch):
    monkeypatch.setenv("XAI_OAUTH_TOKEN", "sentinel-env-token")
    assert su.xai_token(NOW) == "sentinel-env-token"


@pytest.mark.parametrize("expires_at", [
    (NOW - timedelta(minutes=1)).timestamp(),                      # epoch seconds
    (NOW - timedelta(minutes=1)).timestamp() * 1000,               # epoch milliseconds
    (NOW - timedelta(minutes=1)).isoformat(),                      # ISO text
])
def test_xai_token_refuses_an_expired_login(tmp_path, monkeypatch, expires_at):
    _xai_login(tmp_path, monkeypatch, expires_at=expires_at)
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.xai_token(NOW)
    assert "expired" in str(err.value) and "sentinel-xai-token" not in str(err.value)


def test_xai_token_accepts_a_login_that_has_not_expired(tmp_path, monkeypatch):
    _xai_login(tmp_path, monkeypatch, expires_at=(NOW + timedelta(hours=2)).timestamp())
    assert su.xai_token(NOW) == "sentinel-xai-token"


def test_xai_token_reads_the_expiry_of_a_jwt(monkeypatch, grok_login):
    monkeypatch.setenv("XAI_OAUTH_TOKEN", _jwt((NOW - timedelta(seconds=1)).timestamp()))
    with pytest.raises(su.SubscriptionUsageError):
        su.xai_token(NOW)
    fresh = _jwt((NOW + timedelta(hours=1)).timestamp())
    monkeypatch.setenv("XAI_OAUTH_TOKEN", fresh)
    assert su.xai_token(NOW) == fresh


def test_an_expired_grok_login_makes_no_billing_call(tmp_path, monkeypatch, urlopen):
    _xai_login(tmp_path, monkeypatch, expires_at=(NOW - timedelta(minutes=1)).timestamp())
    with pytest.raises(su.SubscriptionUsageError):
        su.fetch_grok(NOW)
    assert urlopen.calls == []


def test_rejected_login_is_reported_as_rejected(claude_login, urlopen):
    urlopen.error = urllib.error.HTTPError("https://x.invalid/", 403, "Forbidden", {}, None)
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.fetch_claude(NOW)
    assert "rejected" in str(err.value) and "403" in str(err.value)


def test_transport_failure_is_reported_not_raised_raw(claude_login, urlopen):
    urlopen.error = urllib.error.URLError("network down")
    with pytest.raises(su.SubscriptionUsageError) as err:
        su.fetch_claude(NOW)
    assert "failed" in str(err.value)


def test_no_error_message_leaks_a_token(tmp_path, monkeypatch, urlopen):
    """Every failure path must be safe to print in a dashboard or a log."""
    secret = "sentinel-claude-token"
    monkeypatch.setenv("RC_CLAUDE_OAUTH_FILE", str(tmp_path / "absent.json"))
    for error in (None, urllib.error.HTTPError("https://x.invalid/", 401, "Unauthorized", {}, None), urllib.error.URLError("boom")):
        urlopen.error = error
        try:
            su.fetch_claude(NOW)
        except su.SubscriptionUsageError as exc:
            assert secret not in str(exc)


# ── check() ─────────────────────────────────────────────────────────────────

def test_check_skips_providers_without_a_login(tmp_path, monkeypatch, urlopen):
    monkeypatch.setenv("RC_CLAUDE_OAUTH_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    assert su.check(NOW) == []


def test_check_reports_each_available_provider(claude_login, grok_login, urlopen):
    urlopen.bodies = {
        su.DEFAULT_CLAUDE_USAGE_URL: json.loads((FIXTURES / "claude_oauth_usage.json").read_text(encoding="utf-8")),
        su.DEFAULT_GROK_BILLING_URL: json.loads((FIXTURES / "grok_billing_credits.json").read_text(encoding="utf-8")),
    }
    lines = su.check(NOW)
    assert lines[0][0] == "Claude" and "OK to run" in lines[0][1]
    assert lines[1][0] == "Grok" and "paused" in lines[1][1]


def test_check_grok_line_uses_rc_grok_cap(claude_login, grok_login, urlopen, monkeypatch):
    """--status prints the same verdict the Grok gate acts on."""
    urlopen.bodies = {
        su.DEFAULT_CLAUDE_USAGE_URL: json.loads((FIXTURES / "claude_oauth_usage.json").read_text(encoding="utf-8")),
        su.DEFAULT_GROK_BILLING_URL: json.loads((FIXTURES / "grok_billing_credits.json").read_text(encoding="utf-8")),
    }
    monkeypatch.setattr(su, "fetch_grok", lambda now=None: [su.Window(key="weekly", percent=92.0, resets_at=NOW)])
    monkeypatch.delenv("RC_GROK_CAP", raising=False)
    assert "paused" in dict(su.check(NOW))["Grok"]
    monkeypatch.setenv("RC_GROK_CAP", "95")
    assert "OK to run" in dict(su.check(NOW))["Grok"]


def test_grok_weekly_cap_bounds(monkeypatch):
    monkeypatch.delenv("RC_GROK_CAP", raising=False)
    assert su.grok_weekly_cap() == 90.0
    for ok, want in (("95", 95.0), ("50", 50.0), (" 92.5 ", 92.5)):
        monkeypatch.setenv("RC_GROK_CAP", ok)
        assert su.grok_weekly_cap() == want
    for bad in ("96", "100", "0", "-1", "x", "", "nan"):
        monkeypatch.setenv("RC_GROK_CAP", bad)
        assert su.grok_weekly_cap() == 90.0

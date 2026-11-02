"""#368 RC's own xAI login for the Grok writer: device-flow login, rotating refresh, 0600 file, no token in messages.

Offline: every request goes through a fake transport; nothing reaches auth.x.ai.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import time

import pytest

from RevenueChaser.core import subscription_usage, xai_login

ACCESS = "sentinel-access-368"
REFRESH = "sentinel-refresh-368"
DISCOVERY = {"token_endpoint": "https://auth.x.ai/oauth2/token",
             "device_authorization_endpoint": "https://auth.x.ai/oauth2/device"}


def _jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class _Transport:
    """Answers by URL from a script; records (method, url, form) without ever printing a token."""

    def __init__(self, token_replies):
        self.calls: list[tuple[str, str, dict]] = []
        self.token_replies = list(token_replies)

    def __call__(self, method, url, data=None):
        self.calls.append((method, url, dict(data or {})))
        if url == xai_login.DISCOVERY_URL:
            return 200, DISCOVERY
        if url == DISCOVERY["device_authorization_endpoint"]:
            return 200, {"device_code": "dc-1", "user_code": "ABCD-1234", "interval": 0,
                         "verification_uri_complete": "https://accounts.x.ai/device?code=ABCD-1234", "expires_in": 600}
        step = self.token_replies.pop(0) if len(self.token_replies) > 1 else self.token_replies[0]
        return step


@pytest.fixture
def auth(tmp_path, monkeypatch, rawstore_tmp_db):
    path = tmp_path / "rc-grok" / "auth.json"
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(path))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(xai_login, "_sleep", lambda s: None)
    return path


def _write(path, access_exp, refresh=REFRESH):
    xai_login.save(path, {"access_token": _jwt(access_exp), "refresh_token": refresh})


def test_a_login_writes_a_private_rc_file_and_prints_no_token(auth, capsys):
    t = _Transport([(400, {"error": "authorization_pending"}),
                    (200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600})])
    xai_login.login(auth, transport=t)
    out = capsys.readouterr().out
    assert "https://accounts.x.ai/device?code=ABCD-1234" in out and ACCESS not in out and REFRESH not in out
    data = json.loads(auth.read_text(encoding="utf-8"))
    assert data["rc_xai_login"] == 1 and data["access_token"] == ACCESS and data["refresh_token"] == REFRESH
    if os.name != "nt":
        assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    grants = [c[2].get("grant_type") for c in t.calls if c[1] == DISCOVERY["token_endpoint"]]
    assert grants == ["urn:ietf:params:oauth:grant-type:device_code"] * 2
    assert all(c[2].get("client_id") == xai_login.CLIENT_ID for c in t.calls if c[0] == "POST")


def test_a_fresh_token_is_used_without_a_request(auth):
    _write(auth, time.time() + 7200)
    t = _Transport([(500, {})])
    assert xai_login.ensure(auth, transport=t) == _jwt_exp_token(auth)
    assert t.calls == []


def _jwt_exp_token(path):
    return json.loads(path.read_text(encoding="utf-8"))["access_token"]


def test_a_token_near_expiry_is_refreshed_and_saved_before_use(auth):
    _write(auth, time.time() + 60)                          # under the 30-minute margin
    new = _jwt(time.time() + 7200)
    t = _Transport([(200, {"access_token": new, "refresh_token": "rotated-368"})])
    assert xai_login.ensure(auth, transport=t) == new
    data = json.loads(auth.read_text(encoding="utf-8"))
    assert data["access_token"] == new and data["refresh_token"] == "rotated-368"
    form = [c[2] for c in t.calls if c[1] == DISCOVERY["token_endpoint"]][0]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == REFRESH


def test_a_refused_refresh_asks_for_a_new_login_and_names_no_token(auth):
    _write(auth, time.time() - 10)
    t = _Transport([(401, {"error": "invalid_grant", "echo": REFRESH})])
    with pytest.raises(xai_login.LoginError) as exc:
        xai_login.ensure(auth, transport=t)
    assert "python -m RevenueChaser.core.xai_login" in str(exc.value)
    assert REFRESH not in str(exc.value) and ACCESS not in str(exc.value)
    assert json.loads(auth.read_text(encoding="utf-8"))["refresh_token"] == REFRESH   # nothing overwritten


def test_only_https_on_auth_x_ai_is_allowed():
    for url in ("http://auth.x.ai/oauth2/token", "https://evil.example/token", "https://auth.x.ai:8443/t"):
        with pytest.raises(ValueError):
            xai_login.check_url(url)
    xai_login.check_url("https://auth.x.ai/oauth2/token")


def test_the_grok_reader_refreshes_an_rc_login(auth, monkeypatch):
    _write(auth, time.time() + 60)
    new = _jwt(time.time() + 7200)
    monkeypatch.setattr(xai_login, "default_transport",
                        _Transport([(200, {"access_token": new, "refresh_token": "rotated-368"})]))
    assert subscription_usage.xai_token() == new


def test_a_hermes_file_is_read_as_before(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": "hermes-368"}}}}),
                    encoding="utf-8")
    monkeypatch.setenv("RC_XAI_AUTH_FILE", str(path))
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(xai_login, "default_transport", lambda *a, **k: pytest.fail("no request for Hermes"))
    assert subscription_usage.xai_token() == "hermes-368"


def test_the_settings_card_checks_presence_without_a_request(auth, monkeypatch):
    from RevenueChaser.module_relatedness import writers
    monkeypatch.setattr(xai_login, "default_transport", lambda *a, **k: pytest.fail("no request on render"))
    assert writers.missing("grok") == "RC_XAI_AUTH_FILE"
    _write(auth, time.time() - 10)                           # expired, but present: refreshed at run time
    assert writers.missing("grok") is None

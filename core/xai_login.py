"""RC's own xAI login for the Grok writer (#368): a device-flow login and a rotating refresh, in one private file.

Hermes kept the SuperGrok token fresh on Windows; homesrv has no Hermes, and the usage monitor's xAI login rotates its
refresh token (a second refresher would log it out). So RC holds a grant of its own, at ``RC_XAI_AUTH_FILE``:

- ``python -m RevenueChaser.core.xai_login`` (avo, once, at a terminal, as the user the units run as) prints the
  verification link, waits for the approval and writes the file (0600, atomic). It never prints a token.
- ``ensure`` returns the access token; when 30 minutes or less remain it refreshes under the ``xai-login`` job lock
  (``core.job_lock``, shared by the dashboard and the relatedness unit), writes the new pair **before** using it, and
  never repeats a refused refresh: a 400/401/403 raises ``LoginError`` asking for a new login.
- Requests go to HTTPS on ``auth.x.ai`` only (the discovery document's endpoints are checked too). No token, refresh
  token or response body appears in a message or a log line.

The file carries ``"rc_xai_login": 1`` so ``core.subscription_usage.xai_token`` tells it from a Hermes ``auth.json``.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"        # the Grok CLI's public OAuth client id
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
AUTH_HOST = "auth.x.ai"
REFRESH_MARGIN_S = 1800
DEAD_STATUS = (400, 401, 403)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MARK = "rc_xai_login"
LOGIN_HINT = "run `python -m RevenueChaser.core.xai_login` at a terminal as the service user, then re-run"


class LoginError(RuntimeError):
    """The RC xAI login is missing, unreadable or refused; the message names the next step, never a token."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def check_url(url: str) -> None:
    """``ValueError`` unless ``url`` is HTTPS on ``auth.x.ai`` with no user, password or port."""
    p = urlparse(url)
    try:
        port = p.port
    except ValueError:
        port = -1
    if p.scheme != "https" or (p.hostname or "").lower() != AUTH_HOST or p.username or p.password or port:
        raise ValueError("url not allowed for the xAI login")


def default_transport(method: str, url: str, data: dict | None = None) -> tuple[int, dict]:
    """One request: ``(status, JSON body or {})``. Redirects are not followed."""
    check_url(url)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        r = client.request(method, url, data=data, headers={"Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _call(transport, method, url, data=None) -> tuple[int, dict]:
    check_url(url)
    try:
        return transport(method, url, data)
    except httpx.HTTPError as exc:
        raise LoginError(f"xAI login request failed ({type(exc).__name__})") from None


def _endpoints(transport) -> dict:
    status, doc = _call(transport, "GET", DISCOVERY_URL)
    if status != 200:
        raise LoginError(f"xAI discovery answered HTTP {status}")
    out = {}
    for key in ("token_endpoint", "device_authorization_endpoint"):
        url = doc.get(key)
        try:
            check_url(url if isinstance(url, str) else "")
        except ValueError:
            raise LoginError(f"xAI discovery gave no usable {key}") from None
        out[key] = url
    return out


def _jwt_exp(token: str) -> float | None:
    try:
        part = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
    except (AttributeError, IndexError, ValueError):
        return None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def _pair(reply: dict, now: float, previous: dict | None = None) -> dict | None:
    """The stored form of a token reply, or None without an access token and a refresh token."""
    access, refresh = reply.get("access_token"), reply.get("refresh_token")
    if not (isinstance(refresh, str) and refresh) and previous:
        refresh = previous.get("refresh_token")              # a refresh that does not rotate keeps the old one
    if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
        return None
    exp = _jwt_exp(access)
    if exp is None and isinstance(reply.get("expires_at"), (int, float)):
        exp = float(reply["expires_at"])                     # a stored pair being saved again (review S2)
    if exp is None and isinstance(reply.get("expires_in"), (int, float)):
        exp = now + float(reply["expires_in"])
    out = {MARK: 1, "access_token": access, "refresh_token": refresh, "saved_at": now}
    if exp is not None:
        out["expires_at"] = exp
    return out


def save(path, tokens: dict) -> None:
    """Atomic 0600 write of a pair (``tokens`` needs ``access_token`` and ``refresh_token``)."""
    pair = _pair(tokens, time.time())
    if pair is None:
        raise LoginError("refusing to save an xAI login without an access and a refresh token")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in path.parent.glob(path.name + ".*.tmp"):     # a kill between mkstemp and replace (review N6)
        try:
            stale.unlink()
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))   # mkstemp is 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(pair, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if hasattr(os, "O_DIRECTORY"):                        # the rename itself survives a power loss (review N2)
            dfd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_rc_login(data) -> bool:
    return isinstance(data, dict) and data.get(MARK) == 1


def read(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LoginError(f"the RC xAI login at {path} is missing or unreadable; {LOGIN_HINT}") from None
    if not is_rc_login(data) or not data.get("access_token") or not data.get("refresh_token"):
        raise LoginError(f"the file at {path} is not an RC xAI login; {LOGIN_HINT}")
    return data


def present(path) -> bool:
    """Presence only (the Settings card, zero network): a readable RC login with a refresh token. Any other file
    is not RC's (a Hermes login is judged by its own offline reader, ``subscription_usage.xai_token``)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return is_rc_login(data) and bool(data.get("refresh_token"))


def _valid(data: dict, now: float) -> bool:
    exp = data.get("expires_at") or _jwt_exp(data.get("access_token") or "")
    return isinstance(exp, (int, float)) and exp > now


def _fresh(data: dict, now: float) -> bool:
    exp = data.get("expires_at") or _jwt_exp(data.get("access_token") or "")
    return isinstance(exp, (int, float)) and exp - now > REFRESH_MARGIN_S


def ensure(path, transport=None, now: float | None = None) -> str:
    """The access token of the RC login at ``path``, refreshed first when 30 minutes or less remain."""
    from . import job_lock
    transport = transport or default_transport
    now = time.time() if now is None else now
    data = read(path)
    if _fresh(data, now):
        return data["access_token"]
    try:
        # 60 s: a holder's worst case is two 15 s requests and a write (review N3).
        with job_lock.acquire("xai-login", wait=60.0) as got:
            if not got:
                if _valid(data, now):
                    return data["access_token"]
                raise LoginError("another process holds the xAI login lock; re-run")
            data = read(path)                                 # another process may have refreshed meanwhile
            if _fresh(data, now):
                return data["access_token"]
            try:
                ep = _endpoints(transport)
                status, reply = _call(transport, "POST", ep["token_endpoint"],
                                      {"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                       "client_id": CLIENT_ID})
            except LoginError:
                status, reply = None, {}                      # network: transient
            if status in DEAD_STATUS:
                raise LoginError(f"xAI refused the RC login's refresh (HTTP {status}); {LOGIN_HINT}")
            new = _pair(reply, now, previous=data) if status == 200 else None
            if new is None:
                # A transient failure keeps a still-valid token (review S3); an expired one is never returned.
                if _valid(data, now):
                    return data["access_token"]
                raise LoginError(f"xAI refresh failed (HTTP {status or 'no answer'}) and the RC login has expired; "
                                 "re-run later")
            save(path, new)                                   # persisted BEFORE the new access token is used
            return new["access_token"]
    except OSError as exc:                                    # the lock or the write (review S4); the path only
        raise LoginError(f"the RC xAI login at {path} could not be read or written ({type(exc).__name__})") from None


def login(path, transport=None, out=print, force: bool = False) -> None:
    """The device flow: print the link, poll until approved, save the pair. Never prints a token.

    It refuses to replace a file that is not an RC login (Hermes's ``auth.json``, the usage monitor's login; review B1)
    unless ``force``."""
    transport = transport or default_transport
    path = Path(path)
    if path.exists() and not force:
        try:
            mine = is_rc_login(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            mine = False
        if not mine:
            raise LoginError(f"{path} exists and is not an RC xAI login (another program's?); set RC_XAI_AUTH_FILE "
                             "to RC's own file, or pass --force to replace it")
    ep = _endpoints(transport)
    status, dev = _call(transport, "POST", ep["device_authorization_endpoint"],
                        {"client_id": CLIENT_ID, "scope": SCOPE})
    if status != 200 or not dev.get("device_code"):
        raise LoginError(f"xAI device authorization answered HTTP {status}")
    link = dev.get("verification_uri_complete") or dev.get("verification_uri") or ""
    host = (urlparse(link).hostname or "").lower()
    if urlparse(link).scheme != "https" or not (host == "x.ai" or host.endswith(".x.ai")):
        raise LoginError("xAI gave a verification link outside x.ai; not shown")   # review N5
    out(f"Open {link} and approve (code {dev.get('user_code', '')}). Waiting ...")
    # 0 is a real value here (review S1): only a missing field takes the RFC 8628 defaults.
    interval = float(dev["interval"]) if isinstance(dev.get("interval"), (int, float)) else 5.0
    life = float(dev["expires_in"]) if isinstance(dev.get("expires_in"), (int, float)) else 600.0
    deadline = time.time() + life
    while time.time() < deadline:
        _sleep(interval)
        try:
            status, reply = _call(transport, "POST", ep["token_endpoint"],
                                  {"grant_type": DEVICE_GRANT, "device_code": dev["device_code"],
                                   "client_id": CLIENT_ID})
        except LoginError:
            continue                                          # one network error does not end the login (N5)
        err = reply.get("error") if status != 200 else None
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if status != 200:
            raise LoginError(f"xAI login ended: HTTP {status} {err or ''}".strip())
        pair = _pair(reply, time.time())
        if pair is None:
            raise LoginError("xAI login answered without an access and a refresh token")
        save(path, pair)
        out(f"Saved the RC xAI login to {path} (mode 0600).")
        return
    raise LoginError("the xAI device code expired before approval; run the login again")


def main(argv=None) -> int:
    from .subscription_usage import xai_auth_file
    p = argparse.ArgumentParser(prog="python -m RevenueChaser.core.xai_login",
                                description="RC's own xAI login for the Grok writer (#368). Prints no token.")
    p.add_argument("--path", default=None, help="the login file (default: RC_XAI_AUTH_FILE)")
    p.add_argument("--force", action="store_true", help="replace a file at the path that is not an RC login")
    args = p.parse_args(argv)
    path = Path(args.path) if args.path else xai_auth_file()
    try:
        login(path, force=args.force)
    except LoginError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

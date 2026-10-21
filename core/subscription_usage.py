"""Subscription usage gates for Claude and Grok — pure decision + thin fetchers.

DeepSeek bills by *time of day* (``core/offpeak.py``). Claude and Grok bill by
*subscription allowance*, so the equivalent gate is "is there budget left?":
callers stop cleanly when a window is spent and resume after the reset. Like
``core/offpeak``, the module never sleeps — the dashboard has a single job
worker that must not be blocked for hours — and the decision half is I/O-free
so it can be unit-tested with made-up numbers.

Neither fetcher refreshes an OAuth token: rotating Claude's refresh token would
log Claude Code out, and Hermes owns the xAI grant. An expired login is
reported, never renewed. ``xai_token`` is the one reader of the xAI login; the
Grok chat client (``core/providers/grok.py``, #388) uses it and this gate too.

The bearer token only ever goes to the provider's own ``https`` endpoint: the
request is made through an opener that does not follow redirects (urllib would
re-send ``Authorization`` to the ``Location`` host), and a URL override is
accepted only for a loopback host, which is what the tests and a local mock need.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import credentials

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
USER_AGENT = "RevenueChaser/1.0 (subscription usage gate)"

# Window keys, as the two providers name them.
SESSION_KEYS = ("five_hour", "session")
WEEKLY_KEYS = (
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "period",
    "weekly",
    "week",
)

DEFAULT_MAX_SESSION = 80.0
DEFAULT_MAX_WEEK = 90.0
MAX_GROK_CAP = 95.0           # the highest weekly ceiling ``RC_GROK_CAP`` may set


class SubscriptionUsageError(RuntimeError):
    """A subscription's usage could not be read (missing or expired login)."""


@dataclass(frozen=True)
class Window:
    """One allowance window: how much of it is used and when it resets."""

    key: str
    percent: float
    resets_at: datetime | None = None
    label: str = ""

    @property
    def is_session(self) -> bool:
        return self.key in SESSION_KEYS

    @property
    def is_weekly(self) -> bool:
        return self.key in WEEKLY_KEYS


def _as_utc(now: datetime | None) -> datetime:
    """Coerce ``now`` to an aware UTC datetime (naive input is read as UTC)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _limit_for(window: Window, max_session: float, max_week: float) -> float:
    return max_session if window.is_session else max_week


def is_within_budget(
    windows,
    *,
    max_session: float = DEFAULT_MAX_SESSION,
    max_week: float = DEFAULT_MAX_WEEK,
) -> bool:
    """True when every window still has room to run a job.

    A window at or above its ceiling blocks; so does any window that reports
    100% or more even if a custom ceiling is higher. No windows at all means
    "no reading", which is treated as blocked — better to stop than to bill
    blind.
    """
    usable = [w for w in windows if w is not None]
    if not usable:
        return False
    for w in usable:
        if w.percent >= 100.0:
            return False
        if w.percent >= _limit_for(w, max_session, max_week):
            return False
    return True


def next_available(
    windows,
    *,
    max_session: float = DEFAULT_MAX_SESSION,
    max_week: float = DEFAULT_MAX_WEEK,
    now: datetime | None = None,
) -> datetime | None:
    """Earliest reset among the windows that are blocking, or None if clear."""
    dt = _as_utc(now)
    blocked = [
        w
        for w in windows
        if w is not None
        and w.resets_at is not None
        and (w.percent >= 100.0 or w.percent >= _limit_for(w, max_session, max_week))
    ]
    future = [w.resets_at.astimezone(timezone.utc) for w in blocked]
    future = [t for t in future if t > dt]
    return min(future) if future else None


def describe(
    provider: str,
    windows,
    *,
    max_session: float = DEFAULT_MAX_SESSION,
    max_week: float = DEFAULT_MAX_WEEK,
    now: datetime | None = None,
) -> str:
    """One-line status for job messages / logs."""
    usable = [w for w in windows if w is not None]
    if not usable:
        return f"{provider} subscription: usage unavailable."
    worst = max(usable, key=lambda w: w.percent)
    rank = "session" if worst.is_session else "weekly"
    if is_within_budget(usable, max_session=max_session, max_week=max_week):
        return f"{provider} subscription: {rank} {worst.percent:.0f}% used — OK to run."
    nxt = next_available(usable, max_session=max_session, max_week=max_week, now=now)
    when = f" Next reset: {nxt.strftime('%Y-%m-%d %H:%M')} UTC." if nxt else ""
    ceiling = max_session if worst.is_session else max_week
    return (
        f"{provider} subscription: {rank} {worst.percent:.0f}% used "
        f"(cap {ceiling:.0f}%) — paused.{when}"
    )


# ── parsing (pure) ──────────────────────────────────────────────────────────

def _percent(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_claude_usage(body: dict) -> list[Window]:
    """Windows from Claude's ``/api/oauth/usage`` payload."""
    labels = {
        "five_hour": "Current session",
        "seven_day": "This week",
        "seven_day_opus": "This week · Opus",
        "seven_day_sonnet": "This week · Sonnet",
    }
    windows: list[Window] = []
    for key, label in labels.items():
        raw = body.get(key)
        if not isinstance(raw, dict):
            continue
        pct = _percent(raw.get("utilization"))
        if pct is None:
            continue
        windows.append(
            Window(key=key, percent=pct, resets_at=_parse_iso(raw.get("resets_at")), label=label)
        )
    return windows


def parse_grok_billing(body: dict) -> list[Window]:
    """Windows from Grok's ``/v1/billing?format=credits`` payload."""
    raw_cfg = body.get("config") if isinstance(body, dict) else None
    if not isinstance(raw_cfg, dict):
        # No config (an error body, a changed shape) is no reading: the Grok gate stops instead of reading 0% (review B S1).
        raise SubscriptionUsageError("SuperGrok billing reply holds no config")
    cfg = raw_cfg
    pct = _percent(cfg.get("creditUsagePercent"))
    if pct is None:
        pct = 0.0  # proto3 omits a zero after a reset
    raw_period = cfg.get("currentPeriod")
    period = raw_period if isinstance(raw_period, dict) else {}
    kind = str(period.get("type") or "")
    key = "seven_day" if "WEEKLY" in kind or not kind else "period"
    reset = _parse_iso(period.get("end") or cfg.get("billingPeriodEnd"))
    return [Window(key=key, percent=pct, resets_at=reset, label="SuperGrok limit")]


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── fetchers ────────────────────────────────────────────────────────────────

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _url(env_var: str, default: str) -> str:
    """``default``, or the ``env_var`` override when it is an http(s) URL on a loopback host.

    Any other override is ignored with a warning (the variable's name, not its value), so a
    stray or hostile environment variable cannot make this module send a token elsewhere.
    """
    override = os.getenv(env_var, "").strip()
    if not override:
        return default
    try:
        parts = urllib.parse.urlsplit(override)
        host = parts.hostname
    except ValueError:
        parts, host = None, None
    if parts is not None and parts.scheme in ("http", "https") and host in _LOOPBACK_HOSTS:
        return override
    logger.warning("%s ignored: a usage URL override must be http(s) on a loopback host (%s); "
                   "using the default endpoint.", env_var, ", ".join(sorted(_LOOPBACK_HOSTS)))
    return default


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib then raises ``HTTPError`` carrying the 3xx code."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req: urllib.request.Request, timeout: float):
    """The one place a usage request leaves the process (tests patch this)."""
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _get_json(url: str, headers: dict[str, str], provider_slug: str) -> dict:
    req = urllib.request.Request(url, headers={**headers, "User-Agent": USER_AGENT}, method="GET")
    try:
        with _open(req, 15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SubscriptionUsageError(credentials.rejected_message(provider_slug, exc.code)) from exc
        raise SubscriptionUsageError(
            f"{credentials.get(provider_slug).label} usage request failed (HTTP {exc.code})."
        ) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise SubscriptionUsageError(
            f"{credentials.get(provider_slug).label} usage request failed: {exc}."
        ) from exc


def claude_credentials_path() -> Path:
    override = os.getenv("RC_CLAUDE_OAUTH_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / ".credentials.json"


def fetch_claude(now: datetime | None = None) -> list[Window]:
    """Claude's subscription windows. Never refreshes the login."""
    path = claude_credentials_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SubscriptionUsageError(
            f"{credentials.missing_message('claude_subscription')} Expected the login at {path}."
        ) from exc
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise SubscriptionUsageError(
            f"{credentials.missing_message('claude_subscription')} Expected the login at {path}."
        )
    expires_ms = oauth.get("expiresAt") or 0
    now_i = _as_utc(now).timestamp()
    if expires_ms and float(expires_ms) / 1000.0 < now_i:
        raise SubscriptionUsageError(
            "Claude Code login expired — run any `claude` command to renew it "
            "(RevenueChaser will not refresh the token itself; that would sign Claude Code out)."
        )
    body = _get_json(
        _url("RC_CLAUDE_USAGE_URL", DEFAULT_CLAUDE_USAGE_URL),
        {
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
        "claude_subscription",
    )
    return parse_claude_usage(body)


def xai_auth_file() -> Path:
    override = os.getenv("RC_XAI_AUTH_FILE", "").strip()
    if override:
        return Path(override)
    home = os.getenv("HERMES_HOME", "").strip()
    base = Path(home) if home else Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "hermes"
    return base / "auth.json"


_EXPIRY_KEYS = ("expires_at", "expiresAt", "expiry", "expires")


def _epoch(value) -> float | None:
    """An expiry as epoch seconds: a number (seconds, or milliseconds when it is that large) or ISO text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            return _epoch(float(value))
        except ValueError:
            dt = _parse_iso(value.strip())
            return dt.timestamp() if dt else None
    return None


def _jwt_exp(token: str) -> float | None:
    """The ``exp`` claim of a JWT access token (read only, never verified), or None for any other token."""
    import base64

    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except (ValueError, UnicodeError):
        return None
    return _epoch(claims.get("exp")) if isinstance(claims, dict) else None


def xai_token(now: datetime | None = None) -> str:
    """SuperGrok's OAuth access token, read now: ``XAI_OAUTH_TOKEN``, else Hermes's ``auth.json``.

    The one reader of the xAI login: the allowance gate (``fetch_grok``) and the Grok chat client
    (``core/providers/grok.py``, #388) both call it, at call time, so a token Hermes refreshed is picked up. It
    fails closed: a missing login, or one whose stored expiry (or JWT ``exp``) has passed, raises
    ``SubscriptionUsageError`` before any request. The token never appears in a message.
    """
    token = os.getenv("XAI_OAUTH_TOKEN", "").strip()
    path = xai_auth_file()
    expires = None
    if not token:
        try:
            auth = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubscriptionUsageError(
                f"{credentials.missing_message('grok_subscription')} Expected Hermes's login at {path}."
            ) from exc
        state = (auth.get("providers") or {}).get("xai-oauth") or {} if isinstance(auth, dict) else {}
        tokens = state.get("tokens") or {} if isinstance(state, dict) else {}
        token = (tokens.get("access_token") or "").strip() if isinstance(tokens, dict) else ""
        for holder in (tokens, state):
            if expires is None and isinstance(holder, dict):
                expires = next((e for e in (_epoch(holder.get(k)) for k in _EXPIRY_KEYS) if e is not None), None)
    if not token:
        raise SubscriptionUsageError(
            f"{credentials.missing_message('grok_subscription')} Expected Hermes's login at {path}."
        )
    if expires is None:
        expires = _jwt_exp(token)
    if expires is not None and expires <= _as_utc(now).timestamp():
        raise SubscriptionUsageError(
            "SuperGrok login expired — run any `hermes` command (Hermes renews it) and re-run "
            "(RevenueChaser will not refresh the token itself; Hermes owns the xAI grant)."
        )
    return token


def fetch_grok(now: datetime | None = None) -> list[Window]:
    """SuperGrok's allowance windows, from Hermes's xAI OAuth grant."""
    token = xai_token(now)
    body = _get_json(
        _url("RC_GROK_BILLING_URL", DEFAULT_GROK_BILLING_URL),
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
        "grok_subscription",
    )
    return parse_grok_billing(body)


def grok_weekly_cap() -> float:
    """Grok's weekly ceiling in percent: ``RC_GROK_CAP`` (above 0, at most ``MAX_GROK_CAP``), else the default."""
    raw = os.getenv("RC_GROK_CAP")
    if raw is None:
        return DEFAULT_MAX_WEEK
    try:
        cap = float(raw)
    except ValueError:
        cap = float("nan")
    if not (0.0 < cap <= MAX_GROK_CAP):               # also rejects nan; inf is above the maximum
        logger.warning("RC_GROK_CAP=%r is not a number in (0, %g]; using %g", raw, MAX_GROK_CAP, DEFAULT_MAX_WEEK)
        return DEFAULT_MAX_WEEK
    return cap


def check(now: datetime | None = None) -> list[tuple[str, str]]:
    """``describe()`` line per provider whose login exists; ``(provider, line)``.

    Providers that cannot be read are skipped silently: this is a status
    read-out, and a missing optional login is not an error.
    """
    out: list[tuple[str, str]] = []
    for provider, fetcher in (("Claude", fetch_claude), ("Grok", fetch_grok)):
        try:
            windows = fetcher(now)
        except SubscriptionUsageError:
            continue
        cap = grok_weekly_cap() if provider == "Grok" else DEFAULT_MAX_WEEK
        out.append((provider, describe(provider, windows, max_week=cap, now=now)))
    return out

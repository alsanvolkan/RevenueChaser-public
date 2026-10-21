"""Source adapters for the identity master (issue #85).

One module per provider. Every adapter:

* returns a :class:`SourceResult` and **never raises** — an exception becomes
  ``status="error"`` with a redacted message, so one dead provider cannot take a
  refresh down and, through :mod:`core.identity.cache`, cannot blank a stored field;
* reads its base URL from ``RC_<NAME>_BASE`` at call time (default: the real URL), so
  the e2e harness and unit tests can redirect it;
* does no store I/O — persistence is :mod:`core.identity.cache`'s job.

Row conventions (what the cache lifts into typed columns): ``ticker``, ``security_type``,
``security_type2``, ``sector``, ``industry``, ``sub_industry``, ``cik``, ``sic``,
``is_etf``, ``forms`` (list of str), ``shell_closed``. Anything else stays in the payload.
A row may carry its own ``status``; a row with ``status == "error"`` is never cached.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ... import credentials

STATUSES = ("ok", "error", "skipped")


def now_iso() -> str:
    """UTC timestamp, second precision. Module-level so tests can pin it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SourceResult:
    """What one adapter run produced.

    ``status``: ``ok`` (rows are real, possibly empty), ``error`` (the provider failed;
    ``rows`` holds whatever was collected before the failure) or ``skipped`` (not run,
    e.g. a required key is missing). ``error`` is already redacted.
    """

    source: str
    status: str
    rows: list[dict] = field(default_factory=list)
    error: str | None = None
    fetched_at: str = ""

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"SourceResult.status must be one of {STATUSES}, not {self.status!r}")
        if not self.fetched_at:
            self.fetched_at = now_iso()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def base_url(name: str, default: str) -> str:
    """``RC_<NAME>_BASE`` from the environment (read now, not at import), else ``default``."""
    return (os.getenv(f"RC_{name}_BASE") or default).strip().rstrip("/")


def norm_ticker(raw) -> str:
    """Canonical ticker: upper-case, ``BRK.B`` -> ``BRK-B``, Nasdaq preferred ``$`` -> ``-P``."""
    return str(raw or "").strip().upper().replace(".", "-").replace("$", "-P")


_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{6,}")


def redact(message) -> str:
    """``message`` as text with every configured API key value (and bearer tokens) removed."""
    text = str(message)
    for provider in credentials.PROVIDERS.values():
        value = (os.getenv(provider.env_var) or "").strip()
        if len(value) >= 6:        # a stub like "x" must not shred ordinary words
            text = text.replace(value, "<redacted>")
    return _BEARER.sub(r"\1<redacted>", text)


def fail(source: str, exc_or_msg) -> SourceResult:
    """An ``error`` result from an exception or message (redacted, type name kept)."""
    if isinstance(exc_or_msg, BaseException):
        msg = f"{type(exc_or_msg).__name__}: {exc_or_msg}" if str(exc_or_msg) else type(exc_or_msg).__name__
    else:
        msg = str(exc_or_msg)
    return SourceResult(source, "error", [], redact(msg)[:300])

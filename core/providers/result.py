"""Typed provider result (ADR-1).

A provider call that returns only ``{}`` cannot tell "the market is quiet" from
"the request failed". ``ProviderResult`` carries that distinction to new code;
old callers keep the plain dict API through the client's compatibility wrapper
(``UWClient._get``) during the migration window.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

STATUSES = ("ok", "partial", "failed")


@dataclass(frozen=True)
class ProviderResult:
    """``status`` is ``ok`` (data is real, possibly empty), ``partial`` (some
    sub-requests failed) or ``failed`` (``data`` is an empty placeholder)."""

    status: str
    data: Any = None
    error: Optional[dict] = None
    endpoint: str = ""

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"ProviderResult.status must be one of {STATUSES}, not {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class ProviderHealth:
    """Call accounting for one provider, in the shape every board banner reads (Common K3, #383).

    ``health()`` gives ``{provider, calls, failed, auth_error, failures, breaker_open, skipped_calls}``: the
    keys ``UWClient.health()`` gives, plus ``provider``, so ``provider_health_html`` and the result history read
    any provider the same way. ``failures`` keeps the first ``MAX_FAILURES`` (``{endpoint, error}``). Thread-safe.
    """

    MAX_FAILURES = 20

    def __init__(self, provider: str):
        import threading
        self.provider = provider
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._calls = self._failed = 0
            self._failures: list[dict] = []

    def call(self, error: str | None = None, endpoint: str = "") -> None:
        """Count one call; ``error`` marks it failed."""
        with self._lock:
            self._calls += 1
            if error is not None:
                self._failed += 1
                if len(self._failures) < self.MAX_FAILURES:
                    self._failures.append({"endpoint": endpoint, "error": error})

    def health(self) -> dict:
        with self._lock:
            return {"provider": self.provider, "calls": self._calls, "failed": self._failed, "auth_error": None,
                    "failures": [dict(f) for f in self._failures], "breaker_open": False, "skipped_calls": 0}

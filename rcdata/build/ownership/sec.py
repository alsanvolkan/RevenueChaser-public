"""SEC requests of the ownership store: paced, counted against ``sec``, with the builder's User-Agent.

The SEC's fair-access rule is 10 requests a second per User-Agent, shared by ``www.sec.gov`` and ``data.sec.gov``.
The builder spaces its own requests ``1 / PER_SECOND`` apart (4.5 a second, under half the limit) and also takes a
token of ``core/sec_throttle.py``'s bucket, which every other SEC caller on the machine spends from (the dashboard's
identity refresh, the filings jobs), so together they stay under the limit. On a 13F deadline day (up to 5,400
filings, one request each) the builder needs about 20 minutes at that rate; each run stops at its time budget and
the rest waits for the next hour.

The User-Agent is ``base.user_agent()`` (the contact from ``RC_HTTP_CONTACT``; the SEC refuses a client without
one), as for the spine's SEC file.
"""
from __future__ import annotations

from typing import Callable

import httpx

from ....core import sec_throttle
from .. import base

PROVIDER = "sec"
PER_SECOND = 4.5

# The machine-wide SEC bucket (a seam: tests replace it, the bucket sleeps on the real clock).
_shared_pace: Callable[[], bool] = sec_throttle.pace


def www_base() -> str:
    from ....core.providers import sec_edgar
    return sec_edgar.SEC_WWW_BASE.rstrip("/")


class Pacer:
    """At least ``1 / PER_SECOND`` between two requests (the run's clock and sleep), then one token of the shared
    bucket (``shared``, ``core/sec_throttle.pace`` by default)."""

    def __init__(self, ctx: base.RunContext, shared: Callable[[], bool] | None = None,
                 per_second: float = PER_SECOND):
        self._clock, self._sleep = ctx.clock, ctx.sleep
        self._shared = shared
        self.interval = 1.0 / per_second
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        (self._shared or _shared_pace)()
        self._last = self._clock()


def get(ctx: base.RunContext, pacer: Pacer, url: str, accept: tuple[int, ...] = (),
        headers: dict | None = None) -> httpx.Response:
    """One paced SEC GET through ``base.http_get`` (its retry ladder; its backoff keeps a retry apart too)."""
    pacer.wait()
    return base.http_get(ctx, PROVIDER, url, headers=headers, accept=accept)

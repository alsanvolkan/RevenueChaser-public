"""Fixtures for the identity tests (helpers live in ``helpers.py``)."""
from __future__ import annotations

import httpx
import pytest

from RevenueChaser.core.identity.tests.helpers import Router


@pytest.fixture
def http(monkeypatch):
    router = Router()
    monkeypatch.setattr(httpx, "get", lambda url, *a, **kw: router._dispatch("GET", url, kw))
    monkeypatch.setattr(httpx, "post", lambda url, *a, **kw: router._dispatch("POST", url, kw))
    return router


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Adapters and the shared SEC client must never really sleep in a test."""
    from RevenueChaser.core.providers import sec_edgar
    monkeypatch.setattr(sec_edgar.time, "sleep", lambda s: None)
    monkeypatch.setattr(sec_edgar, "_throttle", lambda: None)


@pytest.fixture(autouse=True)
def clean_identity_env(monkeypatch):
    """No ambient keys or base-URL overrides leak into a test."""
    for k in ("OPENFIGI_API_KEY", "RC_OPENFIGI_BASE", "RC_NASDAQTRADER_BASE", "RC_WIKIPEDIA_BASE",
              "RC_FDIC_BASE", "RC_UW_BASE", "RC_SPDR_BASE"):
        monkeypatch.delenv(k, raising=False)

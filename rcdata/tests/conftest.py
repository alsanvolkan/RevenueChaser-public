"""rcdata test isolation: every test gets its own market roots, so ops.sqlite and the serving files never leak.

The root conftest points ``RC_MARKET_DIR`` / ``RC_MARKET_RAW_DIR`` at one scratch folder per process; that is shared
by every test the process runs, so a queue or a check row one test left would be the next test's input. rcdata reads
``config.MARKET_DIR`` at call time, so moving the two attributes is enough.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def market_roots(tmp_path, monkeypatch):
    from RevenueChaser.core import config
    ssd, hdd = tmp_path / "market", tmp_path / "market_raw"
    monkeypatch.setattr(config, "MARKET_DIR", ssd)
    monkeypatch.setattr(config, "MARKET_RAW_DIR", hdd)
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", None)
    monkeypatch.setenv("RC_MARKET_DIR", str(ssd))
    monkeypatch.setenv("RC_MARKET_RAW_DIR", str(hdd))
    monkeypatch.setenv("RC_MARKET_RAW_COPY_DIR", "")
    # The process-wide read log is flushed at exit; a test's counts must not land in the scratch root after the
    # session removed it, so each test counts into its own log, which goes with it.
    from RevenueChaser.rcdata import ops
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))
    return ssd, hdd


class HttpMock:
    """Answers every request an ``httpx`` client (or ``httpx.get``) sends, at the transport: the HTTP boundary.

    ``add(substring, body, status=200, headers=None)`` answers the first route whose substring is in the URL; a
    callable ``body`` gets the request and returns ``(status, bytes, headers)``. Unknown URLs answer 404.
    ``requests`` keeps every request in order, so a test can assert the query string and count calls.
    """

    def __init__(self):
        self.routes: list[tuple[str, object, int, dict]] = []
        self.requests = []

    def add(self, substring: str, body=b"", status: int = 200, headers: dict | None = None) -> None:
        self.routes.append((substring, body, status, headers or {}))

    def calls(self, substring: str = "") -> list:
        return [r for r in self.requests if substring in str(r.url)]

    def handle(self, request):
        import json as _json

        import httpx
        self.requests.append(request)
        for sub, body, status, headers in self.routes:
            if sub in str(request.url):
                if callable(body):
                    status, body, headers = body(request)
                if not isinstance(body, (bytes, str)):
                    body = _json.dumps(body)
                if isinstance(body, str):
                    body = body.encode("utf-8")
                return httpx.Response(status, content=body, headers=headers, request=request)
        return httpx.Response(404, content=b"no route", request=request)


@pytest.fixture
def http_mock(monkeypatch):
    import httpx
    mock = HttpMock()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: mock.handle(request))
    return mock


@pytest.fixture
def fixture_text():
    from pathlib import Path
    root = Path(__file__).resolve().parent / "fixtures"
    return lambda name: (root / name).read_text(encoding="utf-8")

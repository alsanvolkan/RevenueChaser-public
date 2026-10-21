"""Helpers for the identity tests: recorded fixtures and an httpx router.

The fixtures under ``fixtures/`` are trimmed copies of the R1 research raw cache (public
data: SEC, OpenFIGI, Nasdaq Trader, Wikipedia, UW screener, FDIC). They hold no key values.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
FIX = Path(__file__).resolve().parent / "fixtures"


def fixture_json(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeResp:
    def __init__(self, payload=None, status: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        if isinstance(payload, (bytes, bytearray)):
            self.content = bytes(payload)
        elif isinstance(payload, str):
            self.content = payload.encode("utf-8")
        else:
            self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        if isinstance(self._payload, (str, bytes)):
            return json.loads(self.content)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=httpx.Request("GET", "http://mock"), response=self)


class Router:
    """Routes ``httpx.get`` / ``httpx.post`` by URL substring; records every call.

    A route's value is a payload, a :class:`FakeResp`, an exception instance (raised), or a
    callable taking the call dict and returning one of those. The first matching route wins
    (add the most specific first). An unrouted URL answers 404 and is recorded.
    """

    def __init__(self):
        self.routes: list[tuple[str, object, int, dict]] = []
        self.calls: list[dict] = []
        self.unrouted: list[str] = []

    def add(self, substring: str, payload, status: int = 200, headers: dict | None = None):
        self.routes.append((substring, payload, status, headers or {}))

    def _dispatch(self, method, url, kwargs):
        call = {"method": method, "url": url, "params": kwargs.get("params"),
                "json": kwargs.get("json"), "headers": kwargs.get("headers") or {}}
        self.calls.append(call)
        for sub, payload, status, headers in self.routes:
            if sub in url:
                value = payload(call) if callable(payload) else payload
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, FakeResp):
                    return value
                return FakeResp(value, status, headers)
        self.unrouted.append(url)
        return FakeResp({}, 404)

    def urls(self, substring: str = "") -> list[str]:
        return [c["url"] for c in self.calls if substring in c["url"]]



"""UWClient.expiry_breakdown + option_contracts (chain fetch + paging)."""
from __future__ import annotations

from RevenueChaser.core.providers.unusual_whales import UWClient


class _FakeClient:
    """Records calls and returns a scripted payload per call index."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))

        class _R:
            status_code = 200

            def __init__(self, payload):
                self._p = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._p

        idx = len(self.calls) - 1
        return _R(self.pages[idx] if idx < len(self.pages) else {"data": []})


def _client(pages) -> tuple[UWClient, _FakeClient]:
    c = UWClient(key="x")
    fake = _FakeClient(pages)
    c._client = fake  # type: ignore[assignment]
    return c, fake


def test_expiry_breakdown_returns_rows():
    c, fake = _client([{"data": [{"expiry": "2026-10-17", "open_interest": 5,
                                  "volume": 1, "chains": 9}]}])
    rows = c.expiry_breakdown("SPY")
    assert rows[0]["expiry"] == "2026-10-17"
    assert fake.calls[0][0].endswith("/stock/SPY/expiry-breakdown")


def test_option_contracts_pages_until_short_page():
    full = {"data": [{"option_symbol": f"X{i}"} for i in range(500)]}
    short = {"data": [{"option_symbol": "last"}]}
    c, fake = _client([full, short])
    rows = c.option_contracts("SPY", expiry="2026-10-17", limit=500, max_pages=6)
    assert len(rows) == 501
    assert len(fake.calls) == 2
    assert fake.calls[0][1]["page"] == 1 and fake.calls[1][1]["page"] == 2
    assert fake.calls[0][1]["expiry"] == "2026-10-17"


def test_option_contracts_stops_at_max_pages():
    full = {"data": [{"option_symbol": f"X{i}"} for i in range(500)]}
    c, fake = _client([full] * 10)
    rows = c.option_contracts("SPY", limit=500, max_pages=3)
    assert len(rows) == 1500
    assert len(fake.calls) == 3


def test_option_contracts_handles_empty():
    c, _ = _client([{"data": []}])
    assert c.option_contracts("SPY") == []

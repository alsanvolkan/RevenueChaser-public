"""A fake IBKR Gateway socket for the options tests: it answers the TWS API handshake, ``startApi`` and
``reqHistoricalData`` the way the gateway does (framing read off ib_async 2.1.0), and keeps every message the client
sent, so a test can assert what was asked. No real socket is ever opened (the root conftest refuses them anyway).
"""
from __future__ import annotations

import struct
from datetime import date, timedelta

from RevenueChaser.core import calendar

SERVER_VERSION = 176
# The account list the gateway sends after startApi: a placeholder, never a real id. The client must drop it unread.
ACCOUNT_PLACEHOLDER = "ACCT-PLACEHOLDER-0"


def frame(*fields) -> bytes:
    body = "".join(f"{f}\0" for f in fields).encode()
    return struct.pack(">I", len(body)) + body


def year_of_bars(last: date, n: int = 252) -> list[tuple[date, float]]:
    days, d = [], last
    while len(days) < n:
        if calendar.is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    return [(d, 0.2 + i / 10_000) for i, d in enumerate(reversed(days))]


class FakeGateway:
    """Its ``connect`` replaces the client's socket seam, ``core.providers.ibkr_tws.CONNECT``."""

    def __init__(self, last_bar: date):
        self.last_bar = last_bar
        self.sent: list[list[str]] = []          # every framed message the client sent, as fields
        self.handshake: bytes | None = None
        self.connects = 0
        self.errors: dict[str, tuple[int, str]] = {}    # symbol -> (code, message) answered instead of bars
        self.extra_bars: dict[str, list[tuple[date, float]]] = {}
        self.down = False
        self._out = b""
        self._in = b""

    # ── the socket the client gets ──
    def connect(self, address, timeout=None):
        if self.down:
            raise ConnectionRefusedError(111, "Connection refused")
        self.connects += 1
        self._out, self._in, self.handshake = b"", b"", None
        return self

    def settimeout(self, t):
        pass

    def close(self):
        pass

    def sendall(self, data: bytes) -> None:
        self._in += data
        if self.handshake is None:
            if not self._in.startswith(b"API\0") or len(self._in) < 8:
                return
            n = struct.unpack(">I", self._in[4:8])[0]
            self.handshake, self._in = self._in[:8 + n], self._in[8 + n:]
            self._out += frame(SERVER_VERSION, "20261026 17:00:00 EST")
        while len(self._in) >= 4:
            n = struct.unpack(">I", self._in[:4])[0]
            if len(self._in) < 4 + n:
                return
            fields = self._in[4:4 + n].decode().split("\0")[:-1]
            self._in = self._in[4 + n:]
            self.sent.append(fields)
            self._answer(fields)

    def recv(self, n: int) -> bytes:
        out, self._out = self._out[:n], self._out[n:]
        return out

    # ── the gateway's side ──
    def _answer(self, fields: list[str]) -> None:
        msg = fields[0]
        if msg == "71":                            # startApi
            self._out += frame(15, 1, ACCOUNT_PLACEHOLDER) + frame(9, 1, 1)
            self._out += frame(4, 2, -1, 2104, "Market data farm connection is OK:usfarm", "")
        elif msg == "20":                          # reqHistoricalData
            req_id, symbol = fields[1], fields[3]
            if symbol in self.errors:
                code, text = self.errors[symbol]
                self._out += frame(4, 2, req_id, code, text, "")
                return
            bars = year_of_bars(self.last_bar) + self.extra_bars.get(symbol, [])
            out = [17, req_id, "20251027  00:00:00", "20261026  00:00:00", len(bars)]
            for d, v in bars:
                out += [d.strftime("%Y%m%d"), v, v + 0.01, v - 0.01, v, -1, v, 1]
            self._out += frame(*out)

    def message_ids(self) -> list[str]:
        return [f[0] for f in self.sent]

    def history_requests(self) -> list[list[str]]:
        return [f for f in self.sent if f[0] == "20"]

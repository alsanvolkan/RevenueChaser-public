"""A minimal TWS API client for the IBKR Gateway: historical bars, contract ids and news headlines, nothing else.

**Market data only.** The IBKR rule of this project: the gateway is used for market data, contract data and
history, never for an account, a position or an order. ``ib_async``'s ``IB.connect`` asks for the positions on every
connect (``IB.connectAsync`` sends ``reqPositionsAsync``, ``ib_async/ib.py`` 2.1.0), so it is not used: this module
speaks the socket protocol itself, as ``core/ibkr_login.py`` does for its probe, and has no code for any request but
the two below. Its framing was read off ``ib_async`` 2.1.0:

- the version handshake of ``core/ibkr_login.py`` (``b"API\\0"``, a 4-byte big-endian length, ``v157..178``), answered
  by one message ``[server version, connection time]``;
- ``startApi`` (``[71, 2, client id, ""]``, ``client.py`` ``startApi``); the gateway answers with the next valid id
  (9), the account list (15) and farm notices (4). Only the next valid id is read; **the account list is dropped
  unread**, never kept and never logged;
- ``reqContractDetails`` (``[9, 8, req id, <contract>, includeExpired, secIdType, secId(, issuerId from server
  176)]``, ``client.py`` ``reqContractDetails``), answered by ``CONTRACT_DATA`` (10; from server 164 ``[10, req id,
  symbol, secType, lastTimes, strike, right, exchange, currency, localSymbol, marketName, tradingClass, conId, ...]``,
  one field more before 164, ``decoder.py`` ``contractDetails``) and ``CONTRACT_DATA_END`` (52). Only the conId is
  read: the News board needs it to ask for a name's headlines;
- ``reqHistoricalNews`` (``[86, req id, conId, providerCodes, start, end, totalResults, options]``), answered by
  ``HISTORICAL_NEWS`` (86: ``[86, req id, time, providerCode, articleId, headline]``) rows and
  ``HISTORICAL_NEWS_END`` (87). Headlines only: the article body (``reqNewsArticle``) is never asked for. The News
  board's source since avo's choice, 9 Nov 2026 20:54 Amsterdam ("Ibkr kullanalim");
- ``reqMktData`` on a broad-tape news contract (``[1, 11, req id, <contract>, deltaNeutral, "mdoff,292", snapshot,
  regulatorySnapshot, options]``, ``client.py`` ``reqMktData``; contract ``secType=NEWS``, symbol ``<code>:<code>_ALL``,
  exchange ``<code>``), answered by ``TICK_NEWS`` (84: ``[84, req id, time ms, providerCode, articleId, headline,
  extraData]``, ``decoder.py`` ``tickNews``). Headlines only, as above; the News board's market lane (#673);
- ``reqHistoricalData`` (``[20, req id, <contract>, includeExpired, endDateTime, barSize, duration, useRTH,
  whatToShow, formatDate, keepUpToDate, chartOptions]``, ``client.py`` ``reqHistoricalData``), answered by
  ``HISTORICAL_DATA`` (17: ``[17, req id, start, end, n, then per bar date, open, high, low, close, volume, average,
  bar count]``, ``decoder.py`` ``historicalData``) or an error (4: ``[4, version, req id, code, text, ...]``).

Every message is NUL-terminated fields behind a 4-byte big-endian length. Pacing (IBKR allows 60 historical requests
in 10 minutes) is the caller's (``rcdata/build/options/ibkr_iv.py``).
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from datetime import date, datetime

from .. import ibkr_login

# The socket seam: the tests replace it with a fake gateway, so the suite opens no socket.
CONNECT = socket.create_connection
# This client's id on the gateway. A second API client with the same id is refused (326), so it is a fixed id no
# other RevenueChaser code uses.
CLIENT_ID = 73
CONNECT_TIMEOUT_S = 5.0
# A year of daily bars comes back in a second or two; a minute leaves room for a busy historical farm.
READ_TIMEOUT_S = 60.0
MAX_FRAME = 16 * 1024 * 1024
START_API, REQ_HISTORICAL_DATA, REQ_CONTRACT_DETAILS, REQ_HISTORICAL_NEWS, REQ_MKT_DATA = 71, 20, 9, 86, 1
NEXT_VALID_ID, ERR_MSG, HISTORICAL_DATA = "9", "4", "17"
CONTRACT_DATA, CONTRACT_DATA_END, HISTORICAL_NEWS, HISTORICAL_NEWS_END = "10", "52", "86", "87"
TICK_NEWS = "84"
# Error codes the gateway sends as notices (farm connection status and the like), never a request's failure.
NOTICE_CODES = range(2100, 2200)
# "Connectivity restored, data lost": the gateway forgot every market data subscription; the caller resubscribes.
DATA_LOST = 1101


class TWSError(RuntimeError):
    """The gateway is not there, closed the connection, or did not answer in time."""


class HistoryError(TWSError):
    """The gateway refused one history request: ``code`` and ``text`` are its error message."""

    def __init__(self, code: int, text: str):
        super().__init__(f"IBKR error {code}: {text}")
        self.code, self.text = code, text


@dataclass(frozen=True)
class Contract:
    symbol: str
    sec_type: str
    exchange: str
    currency: str = "USD"

    def fields(self) -> list:
        """The contract as ``reqHistoricalData`` sends it: conId, symbol, secType, lastTradeDateOrContractMonth,
        strike, right, multiplier, exchange, primaryExchange, currency, localSymbol, tradingClass."""
        return [0, self.symbol, self.sec_type, "", 0.0, "", "", self.exchange, "", self.currency, "", ""]


# Index names the gateway knows as an index on its own exchange; everything else is a stock or ETF routed SMART.
INDEXES = {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "RUSSELL", "VIX": "CBOE"}


def news_contract(code: str) -> Contract:
    """The broad-tape contract of a news provider (``BRFG`` -> ``BRFG:BRFG_ALL`` on exchange ``BRFG``)."""
    code = code.strip().upper()
    return Contract(f"{code}:{code}_ALL", "NEWS", code, "")


def contract_for(ticker: str) -> Contract:
    """The contract of a ticker: an index on its exchange, else a US stock through SMART (``BRK.B`` is ``BRK B``)."""
    t = ticker.strip().upper()
    if t in INDEXES:
        return Contract(t, "IND", INDEXES[t])
    return Contract(t.replace(".", " "), "STK", "SMART")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def encode(*fields) -> bytes:
    body = "".join(f"{_text(f)}\0" for f in fields).encode()
    return struct.pack(">I", len(body)) + body


class TWSClient:
    """One connection to the gateway. ``with TWSClient(host, port) as c: c.history(req_id, contract)``."""

    def __init__(self, host: str, port: int, *, client_id: int = CLIENT_ID, timeout: float = READ_TIMEOUT_S):
        self.host, self.port, self.client_id, self.timeout = host, port, client_id, timeout
        self.server_version: int | None = None
        self._sock = None
        self._buf = b""

    def __enter__(self) -> "TWSClient":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        try:
            self._sock = CONNECT((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
            self._sock.settimeout(self.timeout)
            self._sock.sendall(ibkr_login._handshake())
            reply = self._read()
            self.server_version = int(reply[0])
            self._sock.sendall(encode(START_API, 2, self.client_id, ""))
            while self._read()[0] != NEXT_VALID_ID:   # the account list and farm notices go by unread
                pass
        except (OSError, ValueError, IndexError) as e:
            self.close()
            where = f"{self.host}:{self.port}"
            raise TWSError(f"no API session with the gateway at {where}: {type(e).__name__}: {e}") from e

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _read(self) -> list[str]:
        """The next message's fields. Raises ``TWSError`` on a closed connection or a timeout."""
        while True:
            if len(self._buf) >= 4:
                n = struct.unpack(">I", self._buf[:4])[0]
                if n > MAX_FRAME:
                    raise TWSError(f"a {n}-byte message is not a TWS frame")
                if len(self._buf) >= 4 + n:
                    msg, self._buf = self._buf[4:4 + n], self._buf[4 + n:]
                    return msg.decode(errors="replace").split("\0")[:-1]
            try:
                data = self._sock.recv(65536)
            except OSError as e:
                raise TWSError(f"the gateway did not answer: {type(e).__name__}: {e}") from e
            if not data:
                raise TWSError("the gateway closed the connection")
            self._buf += data

    def history(self, req_id: int, contract: Contract, *, duration: str = "1 Y", bar_size: str = "1 day",
                what: str = "OPTION_IMPLIED_VOLATILITY", use_rth: bool = True) -> list[dict]:
        """The daily bars of ``contract`` up to now: ``[{date, open, high, low, close}]``, oldest first."""
        self._sock.sendall(encode(REQ_HISTORICAL_DATA, req_id, *contract.fields(), False, "", bar_size, duration,
                                  use_rth, what, 1, False, ""))
        while True:
            fields = self._read()
            if fields[0] == ERR_MSG and len(fields) >= 5 and fields[2] == str(req_id):
                code = int(fields[3])
                if code not in NOTICE_CODES:
                    raise HistoryError(code, fields[4])
            elif fields[0] == HISTORICAL_DATA and fields[1] == str(req_id):
                return _bars(fields)


    def _request_error(self, fields: list[str], req_id: int) -> None:
        """Raise ``HistoryError`` when ``fields`` is this request's error (a notice code is not one)."""
        if fields[0] == ERR_MSG and len(fields) >= 5 and fields[2] == str(req_id):
            code = int(fields[3])
            if code not in NOTICE_CODES:
                raise HistoryError(code, fields[4])

    def con_id(self, req_id: int, contract: Contract) -> int:
        """The gateway's contract id of ``contract`` (the first match). ``HistoryError`` 200 for an unknown name."""
        fields = [REQ_CONTRACT_DETAILS, 8, req_id, *contract.fields(), False, "", ""]
        if (self.server_version or 0) >= 176:
            fields.append("")                                   # issuerId
        self._sock.sendall(encode(*fields))
        found = None
        at = 12 if (self.server_version or 0) >= 164 else 13
        while True:
            got = self._read()
            self._request_error(got, req_id)
            if got[0] == CONTRACT_DATA and req_id == int(got[at - 11]) and found is None:
                found = int(got[at])
            elif got[0] == CONTRACT_DATA_END and got[2] == str(req_id):
                if found is None:
                    raise HistoryError(200, "no contract details came back")
                return found

    def news(self, req_id: int, con_id: int, providers: str, start: str, end: str, total: int) -> list[dict]:
        """Headlines of one contract, newest first: ``[{time, provider, article_id, headline}]``.

        ``start`` and ``end`` are ``yyyy-MM-dd HH:mm:ss.0`` (UTC), or empty; ``providers`` joined with ``+``."""
        self._sock.sendall(encode(REQ_HISTORICAL_NEWS, req_id, con_id, providers, start, end, total, ""))
        out = []
        while True:
            got = self._read()
            self._request_error(got, req_id)
            if got[0] == HISTORICAL_NEWS and got[1] == str(req_id):
                out.append({"time": got[2], "provider": got[3], "article_id": got[4], "headline": got[5]})
            elif got[0] == HISTORICAL_NEWS_END and got[1] == str(req_id):
                return out

    def subscribe_news(self, req_id: int, contract: Contract) -> None:
        """Start the headlines of a broad-tape ``contract`` (``news_contract``); they arrive through ``next_news``."""
        self._sock.sendall(encode(REQ_MKT_DATA, 11, req_id, *contract.fields(), False, "mdoff,292", False, False, ""))

    def next_news(self) -> dict | None:
        """The next message if it is a headline: ``{req_id, time_ms, provider, article_id, headline}``; ``None`` for
        any other message or a quiet ``timeout``. A refused subscription raises ``HistoryError`` with ``req_id`` set."""
        try:
            got = self._read()
        except TWSError as e:
            if isinstance(e.__cause__, TimeoutError):          # a quiet tape is not a dead gateway
                return None
            raise
        if got[0] == ERR_MSG and len(got) >= 5 and got[3] == str(DATA_LOST):
            raise TWSError(f"IBKR {DATA_LOST}: {got[4]}")        # the gateway dropped every subscription
        if got[0] == ERR_MSG and len(got) >= 5 and int(got[3] or 0) not in NOTICE_CODES and got[2] not in ("", "-1"):
            err = HistoryError(int(got[3]), got[4])
            err.req_id = int(got[2])
            raise err
        if got[0] == TICK_NEWS and len(got) >= 6:
            return {"req_id": int(got[1]), "time_ms": got[2], "provider": got[3], "article_id": got[4],
                    "headline": got[5]}
        return None


def _bars(fields: list[str]) -> list[dict]:
    n, rest = int(fields[4]), fields[5:]
    out = []
    for i in range(n):
        d, o, h, lo, c = rest[8 * i:8 * i + 5]
        out.append({"date": _bar_date(d), "open": float(o), "high": float(h), "low": float(lo), "close": float(c)})
    return out


def _bar_date(text: str) -> date:
    """A daily bar's date (``formatDate=1``: ``YYYYMMDD``, sometimes with a time after it)."""
    return datetime.strptime(text.strip()[:8], "%Y%m%d").date()

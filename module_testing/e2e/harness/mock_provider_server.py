"""A local stand-in for every HTTP provider, with a request counter.

Why a real server rather than patching ``httpx``: the bugs this tier exists to
catch lived in the HTTP layer itself — a 401 that got swallowed into an empty
board, a 404 that aborted a snapshot with an opaque message. Patching
``httpx.get`` skips exactly the code under suspicion (and misses ``httpx.post``
and every ``httpx.Client`` instance, which is why ``conftest.py``'s
``mock_httpx`` was not a usable base). A loopback server exercises the real
client, real status codes, real JSON decoding.

The counter is the other half. Because every outbound call must come here, the
documented "refresh performs zero network calls" becomes a hard assertion:
snapshot the count, refresh, assert it did not move.

Routing is longest-prefix over the request path, so a test can override one
endpoint without restating the rest:

    server.route("/api/stock/SPX/gex-levels", status=401)
    server.route("/api/earnings/premarket", payload={"data": []})
"""
from __future__ import annotations

import json
from datetime import date
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

FIXTURES = Path(__file__).resolve().parent / "provider_fixtures"


@dataclass
class Route:
    """One canned response. ``payload`` may be a callable taking the path."""

    payload: Any = None
    status: int = 200
    content_type: str = "application/json"
    raw: bytes | None = None
    with_body: bool = False      # callable payload also receives the request body (POST)
    fn: Callable | None = None   # ``fn(path, query, body) -> (status, payload)``: the whole reply per request


@dataclass
class Request:
    method: str
    path: str
    query: str
    body: str = ""


class MockProviderServer:
    """Serves fixture JSON on loopback and records every request."""

    def __init__(self, fixtures_dir: Path = FIXTURES):
        self.fixtures_dir = fixtures_dir
        self._routes: dict[str, Route] = {}
        self._requests: list[Request] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ──
    def start(self) -> "MockProviderServer":
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="rc-mock-provider")
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server not started"
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ── routing ──
    def route(self, path_prefix: str, payload: Any = None, status: int = 200,
              raw: bytes | None = None, content_type: str = "application/json",
              with_body: bool = False) -> None:
        """Register (or override) the response for a path prefix.

        ``with_body=True`` makes a callable ``payload`` take ``(path, body)`` so a POST
        endpoint (OpenFIGI) can answer per request instead of with one canned body.
        """
        self._routes[path_prefix] = Route(payload=payload, status=status, raw=raw,
                                          content_type=content_type, with_body=with_body)

    def route_fn(self, path_prefix: str, fn: Callable[[str, str, str], tuple[int, Any]]) -> None:
        """Answer every request under ``path_prefix`` with ``fn(path, query, body) -> (status, payload)``.

        For a test that must see the query string: the second page of a paginated endpoint failing
        while the first one works, or one expiry answering 500 and the others 200.
        """
        self._routes[path_prefix] = Route(fn=fn)

    def route_many(self, mapping: dict[str, Any]) -> None:
        for prefix, payload in mapping.items():
            self.route(prefix, payload)

    def load_fixture(self, relative: str) -> Any:
        """Read a JSON fixture, e.g. ``unusual_whales/gex_levels.json``."""
        return json.loads((self.fixtures_dir / relative).read_text(encoding="utf-8"))

    def resolve(self, path: str) -> Route | None:
        """Longest matching prefix wins, so specific overrides beat general ones."""
        best: Route | None = None
        best_len = -1
        for prefix, route in self._routes.items():
            if path.startswith(prefix) and len(prefix) > best_len:
                best, best_len = route, len(prefix)
        return best

    # ── observation ──
    def record(self, request: Request) -> None:
        with self._lock:
            self._requests.append(request)

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    def requests(self, path_contains: str = "") -> list[Request]:
        with self._lock:
            return [r for r in self._requests if path_contains in r.path]

    def reset_requests(self) -> None:
        with self._lock:
            self._requests.clear()

    def reset(self) -> None:
        self._routes.clear()
        self.reset_requests()


def _make_handler(server: MockProviderServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence stderr spam
            pass

        def _handle(self, method: str, body: str = "") -> None:
            path, _, query = self.path.partition("?")
            server.record(Request(method=method, path=path, query=query, body=body))
            route = server.resolve(path)
            if route is None:
                # Deliberately 404 rather than an empty 200: an unrouted call is
                # a fixture gap, and a silent [] would hide it exactly the way
                # the production code used to hide a dead API key.
                self._send(404, b'{"error":"no fixture route for this path"}',
                           "application/json")
                return
            if route.fn is not None:
                status, reply = route.fn(path, query, body)
                self._send(status, json.dumps(reply if reply is not None else {}).encode("utf-8"),
                           "application/json")
                return
            if route.raw is not None:
                self._send(route.status, route.raw, route.content_type)
                return
            payload = route.payload
            if callable(payload):
                payload = payload(path, body) if route.with_body else payload(path)
            body = json.dumps(payload if payload is not None else {}).encode("utf-8")
            self._send(route.status, body, route.content_type)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            self._handle("POST", body)

    return Handler


def default_routes(server: MockProviderServer) -> None:
    """Register every fixture under ``provider_fixtures/`` by its declared path.

    Each fixture file is ``{"path": "/api/...", "status": 200, "payload": ...}``
    so the route it answers is visible in the fixture itself rather than in a
    separate mapping table that drifts.
    """
    for f in sorted(server.fixtures_dir.rglob("*.json")):
        try:
            # ``{{today}}`` -> the run's date, so a fixture tape never ages out of a "last 5 days" window
            spec = json.loads(f.read_text(encoding="utf-8").replace("{{today}}", date.today().isoformat()))
        except json.JSONDecodeError as exc:  # pragma: no cover - fixture typo
            raise ValueError(f"bad fixture JSON: {f}: {exc}") from exc
        if not isinstance(spec, dict) or "path" not in spec:
            continue  # data-only fixture (e.g. yfinance rows), not an HTTP route
        server.route(spec["path"], payload=spec.get("payload"),
                     status=int(spec.get("status", 200)))


SPDR_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def spdr_xlsx(rows: list[tuple[str, str]], fund: str) -> bytes:
    """An SSGA daily-holdings workbook for ``fund``: 4 preamble rows, the header on row 5, then one
    row per ``(ticker, name)``, a blank row and a footer text row (the shape ``spdr.parse`` reads)."""
    import io

    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fund Name:", f"{fund.upper()} Select Sector SPDR Fund"])
    ws.append(["Ticker Symbol:", fund.upper()])
    ws.append(["Holdings:", "As of 29-Sep-2026"])
    ws.append([])
    ws.append(["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"])
    for ticker, name in rows:
        ws.append([name, ticker, "000000000", "0000000", 1.0, "-", 100, "USD"])
    ws.append([])
    ws.append(["Past performance is not a reliable indicator of future performance."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def route_spdr(server: MockProviderServer, holdings: dict[str, list[tuple[str, str]]],
               prefix: str = "/spdr") -> None:
    """Route all 11 ``holdings-daily-us-en-<fund>.xlsx`` files as raw xlsx bytes.

    ``holdings`` maps a fund (``"xlk"``) to its ``(ticker, name)`` rows. Every fund gets at least one
    row (a filler ticker no other source knows), because the SPDR source is all-or-nothing and a
    fund with no holdings would fail the whole check.
    """
    from RevenueChaser.core.identity.sources.spdr import FUNDS
    for fund in FUNDS:
        rows = list(holdings.get(fund) or []) or [(f"F{fund.upper()}", f"Filler {fund.upper()}")]
        server.route(f"{prefix}/holdings-daily-us-en-{fund}.xlsx", raw=spdr_xlsx(rows, fund),
                     content_type=SPDR_XLSX_TYPE)


_SAFE = re.compile(r"^[A-Za-z0-9_./-]+$")


def assert_safe_path(path: str) -> None:
    """Guard against a fixture name escaping the fixtures dir."""
    if not _SAFE.match(path) or ".." in path:
        raise ValueError(f"unsafe fixture path: {path}")

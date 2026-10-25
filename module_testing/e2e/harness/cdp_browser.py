"""Drive headless Chrome over the DevTools protocol, for layout checks at an exact phone width.

``--window-size`` cannot do this: new headless Chrome clamps the window to 500 px, so a "390 px" run
measures a 500 px page. Device-metrics emulation sets the viewport itself (and ``mobile``, so the page's
``<meta name="viewport">`` is honoured), and media emulation sets ``prefers-color-scheme``, both of which
a layout check needs and neither of which a command-line flag offers.

No pip dependency: Chrome is found as ``$RC_CHROME`` or on ``PATH`` (the board HTML guard looks the same
way), and the DevTools WebSocket is a few dozen lines over a loopback socket, which the e2e tier allows
(root ``conftest.py``). Nothing here reaches past ``127.0.0.1``.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
TIMEOUT = 30.0


def find_chrome() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


class _WebSocket:
    """The client half of RFC 6455, as much of it as the DevTools endpoint uses: text frames, masked out."""

    def __init__(self, port: int, path: str):
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        self._in = self._sock.makefile("rb")
        status = self._in.readline()
        if b" 101 " not in status:
            raise RuntimeError(f"DevTools refused the WebSocket: {status!r}")
        while self._in.readline() not in (b"\r\n", b""):
            pass

    def send(self, text: str) -> None:
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self._sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n: int) -> bytes:
        data = self._in.read(n)
        if data is None or len(data) < n:
            raise RuntimeError("DevTools closed the WebSocket")
        return data

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(n)
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise RuntimeError("DevTools closed the WebSocket")
            if opcode in (0x9, 0xA):          # ping / pong: the endpoint does not send them, but be polite
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class Page:
    """One tab in a private headless Chrome: ``open`` a URL at a width and a colour scheme, then measure."""

    def __init__(self, chrome: str):
        self._profile = tempfile.mkdtemp(prefix="rc-cdp-")
        args = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--hide-scrollbars", "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
                "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")   # Chrome refuses to sandbox as root (a container)
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self._profile) / "DevToolsActivePort"
        deadline = time.monotonic() + TIMEOUT
        while not (port_file.exists() and len(port_file.read_text().splitlines()) >= 2):
            if time.monotonic() > deadline or self._proc.poll() is not None:
                self.close()
                raise RuntimeError("Chrome did not open its DevTools port")
            time.sleep(0.05)
        port, path = port_file.read_text().splitlines()[:2]
        self._ws = _WebSocket(int(port), path)
        self._id = 0
        self._events: list[dict] = []
        target = self._call("Target.createTarget", url="about:blank")["targetId"]
        self._session = self._call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.send("Page.enable")
        self.send("Runtime.enable")

    def _call(self, method: str, session: str | None = None, **params) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self._ws.send(json.dumps(message))
        while True:
            reply = json.loads(self._ws.recv())
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            if "method" in reply:
                self._events.append(reply)

    def send(self, method: str, **params) -> dict:
        return self._call(method, self._session, **params)

    def _await_event(self, method: str) -> None:
        deadline = time.monotonic() + TIMEOUT
        while True:
            for i, event in enumerate(self._events):
                if event.get("method") == method:
                    del self._events[: i + 1]
                    return
            if time.monotonic() > deadline:
                raise RuntimeError(f"no {method} within {TIMEOUT:.0f}s")
            self._events.append(json.loads(self._ws.recv()))

    def emulate(self, width: int, height: int, scheme: str = "dark", scale: float = 1) -> None:
        """A ``mobile`` viewport of exactly ``width`` CSS px, with touch, in ``scheme`` (light/dark)."""
        mobile = width < 900
        self.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=scale, mobile=mobile)
        if mobile:
            self.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        else:
            self.send("Emulation.setTouchEmulationEnabled", enabled=False)
        self.send("Emulation.setEmulatedMedia",
                  features=[{"name": "prefers-color-scheme", "value": scheme}])

    def open(self, url: str, ready: str = "document.readyState === 'complete'") -> None:
        """Load ``url`` afresh, then wait until ``ready`` (a JS expression) holds and no animation runs.

        Via ``about:blank`` first: a URL that differs from the current one only by its ``#fragment`` is a
        same-document navigation, which fires no load event and would not reload."""
        for target in ("about:blank", url):
            self._events.clear()
            self.send("Page.navigate", url=target)
            self._await_event("Page.loadEventFired")
        self.wait_for(ready)
        self.settle()

    def wait_for(self, expression: str, timeout: float = TIMEOUT) -> None:
        """Poll until ``expression`` is truthy in the page: a condition, never a fixed sleep."""
        deadline = time.monotonic() + timeout
        while not self.eval(expression):
            if time.monotonic() > deadline:
                raise RuntimeError(f"timed out after {timeout:.0f}s waiting for: {expression}")
            time.sleep(0.05)

    def settle(self) -> None:
        """Wait until no CSS transition or animation is running (a drawer sliding in, say)."""
        self.wait_for("document.getAnimations().every(function(a){ return a.playState !== 'running'; })")

    def eval(self, expression: str):
        """The value of ``expression`` in the page (a Promise is awaited), as JSON-able data."""
        result = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(f"page threw: {result['exceptionDetails'].get('text')} "
                               f"{result['exceptionDetails'].get('exception', {}).get('description', '')}")
        return result["result"].get("value")

    def screenshot(self, path: Path, full_page: bool = False) -> Path:
        shot = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=full_page)
        path = Path(path)
        path.write_bytes(base64.b64decode(shot["data"]))
        return path

    def close(self) -> None:
        ws = getattr(self, "_ws", None)
        if ws is not None:
            ws.close()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        shutil.rmtree(self._profile, ignore_errors=True)

    def __enter__(self) -> "Page":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

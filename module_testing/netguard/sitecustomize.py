"""The suite's offline socket guard, one copy for the pytest process and for every Python child it starts (#258).

The root ``conftest.py`` installs it in its own process, then puts this folder on ``PYTHONPATH`` with
``RC_NETGUARD=1``. A child interpreter's ``site`` imports ``sitecustomize`` at startup, so the child installs the
guard before any test code of its own runs. A child may also reach loopback (it is a server a test talks to, or the
client of one), bind a wildcard address and use a Unix socket; nothing else.

shortcut: a child started with ``-S`` or ``-I``, or with an environment that drops ``PYTHONPATH`` or
``RC_NETGUARD``, runs unguarded, and in a guarded child this file hides any other ``sitecustomize`` on the path.
Chain to that one if a runner ever ships a ``sitecustomize`` the suite needs.
"""
from __future__ import annotations

import os
import socket

#: Lets the root conftest tell this module from a system ``sitecustomize`` already in ``sys.modules``.
RC_NETGUARD = True
LOOPBACK = ("127.0.0.1", "::1", "localhost")
BIND_HOSTS = (None, "", "0.0.0.0", "::")


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a real socket."""


# The unpatched primitives, kept on ``socket`` itself, so a second install (a child's ``sitecustomize``, then the
# conftest of a pytest running in that child) replaces the wrappers instead of stacking them.
_REAL = getattr(socket, "_rc_netguard_real", None) or (
    socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo)
socket._rc_netguard_real = _REAL


def install(*, loopback: bool, child: bool = False) -> None:
    """Make every non-allowed connect and name lookup raise ``NetworkAccessAttempted``.

    ``loopback`` opens 127.0.0.1/::1/localhost (the e2e tier, and every child). The ops store's Redis
    (``RC_REDIS_URL``) is the one other address allowed: a local service CI starts beside the job.
    """
    real_connect, real_connect_ex, real_create_connection, real_getaddrinfo = _REAL
    redis_addr = None
    if os.environ.get("RC_REDIS_URL", "").strip():
        from urllib.parse import urlsplit
        u = urlsplit(os.environ["RC_REDIS_URL"].strip())
        redis_addr = (u.hostname, u.port or 6379)

    def _allowed(address) -> bool:
        if child and isinstance(address, (str, bytes)):           # AF_UNIX: not the network
            return True
        if not isinstance(address, (tuple, list)) or not address:
            return False
        if loopback and address[0] in LOOPBACK:
            return True
        return redis_addr is not None and len(address) >= 2 and (address[0], address[1]) == redis_addr

    def _blocked(*args, **kwargs):
        raise NetworkAccessAttempted(
            "This test tried to open a real network connection" + (" from a child process" if child else "")
            + ". Unit tests must mock at the HTTP boundary — use the `mock_httpx` fixture, or patch the client "
            "method directly. Live-API checks belong in a standalone script under core/, not in the pytest suite."
        )

    def _connect(self, address, *a, **kw):
        if _allowed(address):
            return real_connect(self, address, *a, **kw)
        _blocked()

    def _connect_ex(self, address, *a, **kw):
        if _allowed(address):
            return real_connect_ex(self, address, *a, **kw)
        _blocked()

    def _create_connection(address, *a, **kw):
        if _allowed(address):
            return real_create_connection(address, *a, **kw)
        _blocked()

    def _getaddrinfo(host, *a, **kw):
        if ((loopback and host in LOOPBACK) or (redis_addr is not None and host == redis_addr[0])
                or (child and host in BIND_HOSTS)):
            return real_getaddrinfo(host, *a, **kw)
        _blocked()

    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.create_connection = _create_connection
    socket.getaddrinfo = _getaddrinfo


if __name__ == "sitecustomize" and os.environ.get("RC_NETGUARD") == "1":
    install(loopback=True, child=True)

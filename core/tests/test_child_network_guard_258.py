"""#258: a Python child the suite starts runs under the same offline guard as the pytest process.

The root conftest puts ``module_testing/netguard`` on the children's ``PYTHONPATH`` with ``RC_NETGUARD=1``; the child's
``site`` imports that folder's ``sitecustomize`` and installs the guard. Loopback stays open in a child.
"""
from __future__ import annotations

import subprocess
import sys


def _child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_a_child_that_opens_a_non_loopback_socket_fails_on_the_guard():
    out = _child("import socket; socket.create_connection(('192.0.2.1', 80), timeout=2)")   # TEST-NET-1
    assert out.returncode != 0
    assert "NetworkAccessAttempted" in out.stderr, out.stderr[-2000:]


def test_a_child_that_resolves_a_public_name_fails_on_the_guard():
    out = _child("import socket; socket.getaddrinfo('example.com', 443)")
    assert "NetworkAccessAttempted" in out.stderr, out.stderr[-2000:]


def test_a_child_may_still_reach_loopback():
    out = _child("import socket\n"
                 "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)\n"
                 "socket.create_connection(srv.getsockname(), timeout=5).close()\n"
                 "print('ok')")
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]

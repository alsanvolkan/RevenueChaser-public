"""#162: the IBKR Gateway login probe and its record.

Every test here injects its own connector, so no socket is opened and the suite stays offline by
construction (the root ``conftest.py`` would raise ``NetworkAccessAttempted`` on a real one).
"""
from __future__ import annotations

import json
import socket
import struct
from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import ibkr_login


# ── the fake gateway ────────────────────────────────────────────────────────────────────────────

class _FakeSock:
    """A socket that answers ``recv`` from a scripted reply and records what was sent."""

    def __init__(self, reply: bytes = b"", *, raise_on_recv: Exception | None = None):
        self.reply, self.sent, self.closed = reply, b"", False
        self.raise_on_recv, self.timeout = raise_on_recv, None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if self.raise_on_recv is not None:
            raise self.raise_on_recv
        out, self.reply = self.reply[:n], self.reply[n:]
        return out

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _framed(*fields: str) -> bytes:
    body = ("\0".join(fields) + "\0").encode()
    return struct.pack(">I", len(body)) + body


def _connect(sock):
    def connect(address, timeout=None):
        sock.address = address
        return sock
    return connect


def _refuse(exc):
    def connect(address, timeout=None):
        raise exc
    return connect


# ── the probe's three readings ──────────────────────────────────────────────────────────────────

def test_an_answered_handshake_reads_logged_in_and_keeps_the_server_version():
    sock = _FakeSock(_framed("178", "20261023 23:40:00 CEST"))
    reading = ibkr_login.probe("127.0.0.1", 4001, connect=_connect(sock))
    assert reading.state == ibkr_login.UP
    assert reading.server_version == 178
    assert "178" in reading.detail


def test_the_probe_sends_the_tws_version_handshake_and_nothing_else():
    sock = _FakeSock(_framed("178", "t"))
    ibkr_login.probe("127.0.0.1", 4001, connect=_connect(sock))
    assert sock.sent.startswith(b"API\0")
    length = struct.unpack(">I", sock.sent[4:8])[0]
    body = sock.sent[8:]
    assert len(body) == length, "the handshake body must match its own length prefix"
    assert body == f"v{ibkr_login.MIN_CLIENT_VERSION}..{ibkr_login.MAX_CLIENT_VERSION}".encode()
    assert len(sock.sent) == 8 + length, "nothing may be sent after the handshake"


def test_a_silent_port_reads_api_silent_not_logged_in():
    sock = _FakeSock(b"")
    reading = ibkr_login.probe("127.0.0.1", 4001, connect=_connect(sock))
    assert reading.state == ibkr_login.SILENT
    assert reading.server_version is None


def test_a_recv_timeout_reads_api_silent():
    reading = ibkr_login.probe("127.0.0.1", 4001,
                               connect=_connect(_FakeSock(raise_on_recv=socket.timeout("timed out"))))
    assert reading.state == ibkr_login.SILENT


def test_a_refused_connection_reads_down():
    reading = ibkr_login.probe("127.0.0.1", 4001, connect=_refuse(ConnectionRefusedError(111, "refused")))
    assert reading.state == ibkr_login.DOWN
    assert "refused" in reading.detail.lower()


def test_an_unresolvable_host_reads_down_and_never_raises():
    reading = ibkr_login.probe("nowhere.invalid", 4001, connect=_refuse(socket.gaierror("name not known")))
    assert reading.state == ibkr_login.DOWN


def test_a_reply_that_cannot_be_parsed_still_counts_as_answered():
    """A framing mistake must degrade to "answered, version unknown", never to a false "down"."""
    reading = ibkr_login.probe("127.0.0.1", 4001, connect=_connect(_FakeSock(b"\xff\xff not a frame")))
    assert reading.state == ibkr_login.UP
    assert reading.server_version is None


def test_the_probe_closes_the_socket_it_opened():
    sock = _FakeSock(_framed("178", "t"))
    ibkr_login.probe("127.0.0.1", 4001, connect=_connect(sock))
    assert sock.closed


def test_the_probe_passes_its_timeout_to_the_connect_and_the_socket():
    sock = _FakeSock(_framed("178", "t"))
    seen = {}

    def connect(address, timeout=None):
        seen["timeout"] = timeout
        return sock

    ibkr_login.probe("127.0.0.1", 4001, timeout=1.5, connect=connect)
    assert seen["timeout"] == 1.5 and sock.timeout == 1.5


# ── the record ──────────────────────────────────────────────────────────────────────────────────

# ``day`` counts from the 1st and may run past the end of the month (the trim test needs 40-odd
# readings), so it is an offset, not a calendar day number.
_BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _at(day: int, hour: int = 12) -> datetime:
    return _BASE + timedelta(days=day - 1, hours=hour)


def _reading(state: str, detail: str = "d", version: int | None = None) -> ibkr_login.Reading:
    return ibkr_login.Reading(state=state, detail=detail, server_version=version)


def test_the_first_record_opens_the_current_state_at_its_own_time(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP, version=178), path=path, now=_at(20))
    state = ibkr_login.read(path=path)
    assert state["state"] == ibkr_login.UP
    assert state["since"] == _at(20).isoformat()
    assert state["last_up"] == _at(20).isoformat()
    assert state["last_down"] is None
    assert state["checked_at"] == _at(20).isoformat()


def test_an_unchanged_state_moves_the_check_time_but_not_since(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(21))
    state = ibkr_login.read(path=path)
    assert state["since"] == _at(20).isoformat(), "an unchanged state keeps the time it started"
    assert state["checked_at"] == _at(21).isoformat()
    assert state["changes"] == [], "an unchanged reading is not a change"


def test_a_drop_records_when_it_dropped_and_keeps_the_last_time_it_was_up(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(21))
    ibkr_login.record(_reading(ibkr_login.DOWN, "refused"), path=path, now=_at(22))
    state = ibkr_login.read(path=path)
    assert state["state"] == ibkr_login.DOWN
    assert state["since"] == _at(22).isoformat(), "the drop is dated by the reading that saw it"
    assert state["last_up"] == _at(21).isoformat(), "the last time it answered must survive the drop"
    assert state["last_down"] == _at(22).isoformat()
    assert [(c["from"], c["to"], c["at"]) for c in state["changes"]] == [
        (ibkr_login.UP, ibkr_login.DOWN, _at(22).isoformat())]


def test_the_record_survives_a_restart_because_it_is_read_back_from_the_file(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    ibkr_login.record(_reading(ibkr_login.DOWN), path=path, now=_at(21))
    # A new process reads the same file: nothing is held in memory.
    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread["state"] == ibkr_login.DOWN and reread["last_up"] == _at(20).isoformat()
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(22))
    assert ibkr_login.read(path=path)["last_down"] == _at(21).isoformat()


def test_only_the_newest_changes_are_kept(tmp_path):
    path = tmp_path / "state.json"
    states = [ibkr_login.UP, ibkr_login.DOWN] * (ibkr_login.MAX_CHANGES + 3)
    for day, state in enumerate(states, start=1):
        ibkr_login.record(_reading(state), path=path, now=_at(day))
    changes = ibkr_login.read(path=path)["changes"]
    assert len(changes) == ibkr_login.MAX_CHANGES
    assert changes == sorted(changes, key=lambda c: c["at"]), "changes read oldest to newest"
    assert changes[-1]["at"] == _at(len(states)).isoformat(), "the newest change is kept"


def test_an_unreadable_record_reads_as_unknown_never_as_down(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = ibkr_login.read(path=path)
    assert state["state"] == ibkr_login.UNKNOWN
    assert state["since"] is None and state["changes"] == []


def test_a_missing_record_reads_as_unknown(tmp_path):
    state = ibkr_login.read(path=tmp_path / "nothing.json")
    assert state["state"] == ibkr_login.UNKNOWN
    assert state["checked_at"] is None


def test_a_record_whose_state_is_not_one_of_ours_reads_as_unknown(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"state": "logged_in_maybe", "since": _at(20).isoformat()}), encoding="utf-8")
    assert ibkr_login.read(path=path)["state"] == ibkr_login.UNKNOWN


def test_the_write_is_atomic_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    assert [p.name for p in sorted(tmp_path.iterdir())] == ["state.json"]


def test_a_record_that_cannot_be_written_never_raises(tmp_path):
    """Bookkeeping: an unwritable folder must not fail the job (``core/sec_files.py:239``'s rule)."""
    path = tmp_path / "state.json"
    path.mkdir()                                  # a directory where the file should be
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))   # must not raise


# ── the switch and the configured target ────────────────────────────────────────────────────────

def test_the_check_is_off_until_the_switch_is_set(monkeypatch):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    assert ibkr_login.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_the_switch_accepts_the_usual_yes_words(monkeypatch, value):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", value)
    assert ibkr_login.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_the_switch_stays_off_for_a_no_word(monkeypatch, value):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", value)
    assert ibkr_login.enabled() is False


def test_the_target_defaults_to_loopback_4001(monkeypatch):
    monkeypatch.delenv("RC_IBKR_HOST", raising=False)
    monkeypatch.delenv("RC_IBKR_PORT", raising=False)
    assert ibkr_login.target() == ("127.0.0.1", 4001)


def test_the_target_is_overridable(monkeypatch):
    monkeypatch.setenv("RC_IBKR_HOST", "10.0.0.5")
    monkeypatch.setenv("RC_IBKR_PORT", "4002")
    assert ibkr_login.target() == ("10.0.0.5", 4002)


def test_an_unparsable_port_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("RC_IBKR_PORT", "four thousand")
    assert ibkr_login.target()[1] == 4001


# ── the words the card shows ────────────────────────────────────────────────────────────────────

def test_the_card_payload_says_not_set_up_when_the_switch_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    card = ibkr_login.collect(now=_at(23), path=tmp_path / "state.json")
    assert card["state"] == ibkr_login.NOT_SET
    assert "RC_IBKR_LOGIN_CHECK" in card["text"]


def test_the_card_payload_reads_logged_in_with_how_long(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP, version=178), path=path, now=_at(20))
    card = ibkr_login.collect(now=_at(22), path=path)
    assert card["state"] == ibkr_login.UP
    assert "Logged in" in card["text"]
    assert "2 days" in card["for_text"]


def test_the_card_payload_calls_a_drop_login_needed_and_says_when(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    ibkr_login.record(_reading(ibkr_login.DOWN, "refused"), path=path, now=_at(21))
    card = ibkr_login.collect(now=_at(21, 15), path=path)
    assert card["state"] == ibkr_login.DOWN
    assert "Login needed" in card["text"]
    assert card["since"] == _at(21).isoformat()
    assert card["last_up"] == _at(20).isoformat()


def test_a_reading_older_than_the_window_is_called_out_as_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    fresh = ibkr_login.collect(now=_at(20) + timedelta(minutes=ibkr_login.STALE_MINUTES - 1), path=path)
    stale = ibkr_login.collect(now=_at(20) + timedelta(minutes=ibkr_login.STALE_MINUTES + 1), path=path)
    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert "no check has run" in stale["checked_text"].lower()


def test_the_card_payload_reads_unknown_before_the_first_check(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    card = ibkr_login.collect(now=_at(23), path=tmp_path / "state.json")
    assert card["state"] == ibkr_login.UNKNOWN
    assert "no check has run yet" in card["text"].lower()
    assert card["stale"] is False, "nothing recorded yet is not a stale reading"


def test_the_card_payload_never_probes(monkeypatch, tmp_path):
    """The board reads the file only; a socket from a Refresh would be a side effect and a timeout."""
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    monkeypatch.setattr(ibkr_login, "probe", lambda *a, **kw: pytest.fail("collect() must not probe"))
    ibkr_login.collect(now=_at(23), path=tmp_path / "state.json")


def test_a_silent_port_reads_as_its_own_words_not_as_logged_in(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.SILENT, "handshake brought nothing back"), path=path, now=_at(20))
    card = ibkr_login.collect(now=_at(20, 13), path=path)
    assert card["state"] == ibkr_login.SILENT
    assert "not answering" in card["text"]
    assert "Logged in" not in card["text"]


def test_a_silent_reading_counts_as_not_up_for_last_up_and_last_down(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    ibkr_login.record(_reading(ibkr_login.SILENT), path=path, now=_at(21))
    state = ibkr_login.read(path=path)
    assert state["last_up"] == _at(20).isoformat()
    assert state["last_down"] == _at(21).isoformat(), "a port that answers nothing is not logged in"


def test_the_card_names_the_target_it_is_reporting_on(monkeypatch, tmp_path):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    monkeypatch.setenv("RC_IBKR_HOST", "10.0.0.5")
    monkeypatch.setenv("RC_IBKR_PORT", "4002")
    card = ibkr_login.collect(now=_at(23), path=tmp_path / "state.json")
    assert card["target"] == "10.0.0.5:4002"


def test_the_not_set_card_names_the_target_so_the_default_is_visible(monkeypatch, tmp_path):
    monkeypatch.delenv("RC_IBKR_LOGIN_CHECK", raising=False)
    monkeypatch.delenv("RC_IBKR_HOST", raising=False)
    monkeypatch.delenv("RC_IBKR_PORT", raising=False)
    card = ibkr_login.collect(now=_at(23), path=tmp_path / "state.json")
    assert "127.0.0.1:4001" in card["text"]


def test_a_naive_now_is_read_as_utc_rather_than_crashing(tmp_path):
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=datetime(2026, 10, 20, 12))
    assert ibkr_login.read(path=path)["since"] == _at(20).isoformat()


@pytest.mark.parametrize("seconds,expected", [(0, "under a minute"), (59, "under a minute"),
                                              (60, "1 minute"), (120, "2 minutes"),
                                              (3600, "1 hour"), (86400, "1 day"), (172800, "2 days")])
def test_how_long_names_the_biggest_unit_that_is_not_zero(monkeypatch, tmp_path, seconds, expected):
    monkeypatch.setenv("RC_IBKR_LOGIN_CHECK", "1")
    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP), path=path, now=_at(20))
    card = ibkr_login.collect(now=_at(20) + timedelta(seconds=seconds), path=path)
    assert card["for_text"] == expected


def test_a_write_that_fails_part_way_leaves_the_previous_record_whole(tmp_path, monkeypatch):
    """The write goes to a temporary file and is moved into place, so a half-written record is
    never the record. A straight write onto the live file would leave truncated JSON behind, and
    the card would then read "unknown" on a gateway it had already measured."""
    import pathlib

    path = tmp_path / "state.json"
    ibkr_login.record(_reading(ibkr_login.UP, version=178), path=path, now=_at(20))
    good = path.read_text(encoding="utf-8")

    real_write = pathlib.Path.write_text

    def half_a_write(self, text, *a, **kw):
        real_write(self, text[: len(text) // 3], *a, **kw)   # the disk fills up mid-write
        raise OSError("no space left on device")

    monkeypatch.setattr(pathlib.Path, "write_text", half_a_write)
    ibkr_login.record(_reading(ibkr_login.DOWN), path=path, now=_at(21))   # must not raise
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == good, "the live record was written over in place"
    assert ibkr_login.read(path=path)["state"] == ibkr_login.UP

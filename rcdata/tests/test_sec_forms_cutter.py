"""#754: the edgartools section cutter, both sides of the pipe.

Two kinds of test, because the two sides fail in different ways:

- **The client's machinery**, against a fake worker in-process: the version handshake, a timeout, a crash, the
  restart after either, the recycle, the attempt cap counted across runs, and what is and is not a failed *run*.
  None of it needs a subprocess, and all of it is what a 5,149-document pass depends on.
- **The real worker as a subprocess**, under this interpreter with a stub ``edgar`` package on ``PYTHONPATH``.
  edgartools is not installed here and is not installable in CI (35 packages, Python 3.11+), so the stub is how
  the protocol, the handshake, the isolation and the answer file are tested at all. What the stub cannot test is
  whether the real library's sections carry the field names :func:`cutter_worker._section` reads; the worker's
  docstring says so plainly, and the first homesrv run is what confirms them.

**Nothing here reaches the SEC**, and the stub asserts it cannot: it tries a socket and records what it got.

Red on the merge-base: ``rcdata.build.sec_forms`` does not exist there, so every test fails at import.
"""
from __future__ import annotations

import errno
import gzip
import json
import os
import re
import subprocess
import sys
import time
import types

import pytest

from RevenueChaser.rcdata.build.sec_forms import cutter

# ── the stub edgar package the real worker runs against ───────────────────────────────────────────────────────

STUB_INIT = '''
import json, os, socket

__version__ = "6.0.0"

# The probe runs here as well as in ``documents``, because the handshake imports this module and the network must
# already be gone by then: a block installed after the import would leave the library's module-level clients a
# clear run at the SEC, and a probe only in ``documents`` could not see it.
try:
    _s = socket.socket()
    _s.settimeout(0.5)
    _s.connect(("127.0.0.1", 9))
    _at_import = "open"
except OSError as e:
    _at_import = type(e).__name__
with open(os.path.join(os.environ["TMPDIR"], "stub.jsonl"), "a") as _fh:
    _fh.write(json.dumps({"at_import": _at_import}) + "\\n")
'''

#: ``edgar.documents.config``. Its own ``ParserConfig.form`` comment in 6.0.0 says "required for section
#: detection", and the worker has to pass the form this way or ``Document.sections`` has no form to route on.
STUB_CONFIG = '''
class ParserConfig:
    def __init__(self, form=None, **kw):
        self.form = form
'''

#: ``edgar.documents``. **Shaped from the pinned wheel's own source, not from our worker's import line.** The
#: first version of this stub provided ``HybridSectionDetector`` here because that is where the worker looked for
#: it, and in 6.0.0 it is not here at all -- it lives in ``edgar.documents.extractors`` and the library reaches it
#: through ``Document.sections``. So the stub agreed with the mistake, CI went green, and the pass would have
#: failed every one of 5,149 documents three times over. A stub is written from the real package's exports.
STUB_DOCUMENTS = '''
import json, os, socket

LOG = os.path.join(os.environ["TMPDIR"], "stub.jsonl")


def _log(what):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(what) + "\\n")


# Every primitive the worker replaces is tried, not just one: a client that resolves a name first and a client
# that calls connect_ex or sends a datagram all have to be stopped, and a test that asks about one of them lets
# the others be deleted. Every call carries a short timeout and goes to a port the suite's own network guard
# refuses anyway -- without one, a probe would wait on SYN retries or on DNS and this test's cost would depend on
# the machine it runs on. In the worker it never waits at all, because the call is gone.
def _socket():
    s = socket.socket()
    s.settimeout(0.5)
    return s


_tried = {}
for _what, _call in (("connect", lambda: _socket().connect(("127.0.0.1", 9))),
                     ("connect_ex", lambda: _socket().connect_ex(("127.0.0.1", 9))),
                     ("create_connection", lambda: socket.create_connection(("127.0.0.1", 9), 0.5)),
                     ("getaddrinfo", lambda: socket.getaddrinfo("localhost", 9)),
                     ("gethostbyname", lambda: socket.gethostbyname("localhost")),
                     ("sendto", lambda: _socket().sendto(b"x", ("127.0.0.1", 9))),
                     ("sendmsg", lambda: _socket().sendmsg([b"x"], [], 0, ("127.0.0.1", 9))),
                     ("gethostbyname_ex", lambda: socket.gethostbyname_ex("localhost")),
                     ("getnameinfo", lambda: socket.getnameinfo(("127.0.0.1", 9), 0))):
    try:
        _call()
        _tried[_what] = "open"
    except OSError as e:
        _tried[_what] = type(e).__name__
_log(_tried)
_log({"identity": os.environ.get("EDGAR_IDENTITY"), "figi": "OPENFIGI_API_KEY" in os.environ,
      "mode": os.environ.get("RC_SEC_FORMS_CUTTER")})


def _warn_as(logger_name, func, message):
    """Log a warning that carries ``func`` as its ``funcName``, the way the library's own call at that site
    does. ``logging`` takes ``funcName`` from the frame, so the record is made by hand."""
    import logging
    logger = logging.getLogger(logger_name)
    logger.handle(logger.makeRecord(logger_name, logging.WARNING, "edgartools.py", 1, message, (), None, func))


class _Section:
    """``edgar.documents.document.Section``'s public fields, under its own names and with its own defaults."""

    def __init__(self, name, title, item=None, part=None, covered_items=(), method="toc", confidence=1.0,
                 warnings=(), body="", start=0, end=0, validated=False):
        self.name, self.title, self.item, self.part = name, title, item, part
        self.covered_items, self.detection_method, self.confidence = covered_items, method, confidence
        self.warnings, self.validated = list(warnings), validated
        self.start_offset, self.end_offset = start, end
        self._body = body

    @property
    def kind(self):
        return "item" if self.item else "named"

    def text(self):
        return self._body


class _Broken(_Section):
    def text(self):
        raise RuntimeError("this section's text cannot be read")


class _Slow(_Section):
    def text(self):
        import time
        time.sleep(60)
        return "never"


class Document:
    def __init__(self, html, form):
        self.html, self.form = html, form

    @property
    def sections(self):
        # rich prints, and it flushes: a print left in Python's block buffer would never reach the pipe at all,
        # so an unflushed one here would make this stub agree with a worker that isolates nothing. The raw write
        # to fd 1 is the other half: that is what a C extension or a child does, and ``sys.stdout = sys.stderr``
        # would not stop it.
        print("[bold]detecting[/bold]", flush=True)
        os.write(1, b"a raw write to fd 1\\n")
        if "boom" in self.html:
            raise ValueError("the parser gave up on this filing")
        if "halfway" in self.html:
            # One good section and then one that raises when its text is read: the only shape that gets a .tmp
            # file opened and then fails. A document that fails before the first section never opens one, so it
            # cannot tell a worker that cleans up from one that does not.
            return {"item_1": _Section("item_1", "Business", item="1", body="Fine."),
                    "item_1a": _Broken("item_1a", "Risk Factors", item="1A")}
        if "hangmid" in self.html:
            # The tmp file is already open and one row is in it when this hangs, which is what a filer's
            # pathological table does: ``Document.sections`` and every ``text()`` are read inside the ``with``.
            # A worker killed here cannot remove its own tmp, so the client has to.
            return {"item_1": _Section("item_1", "Business", item="1", body="Fine."),
                    "item_1a": _Slow("item_1a", "Risk Factors", item="1A")}
        if "hang" in self.html:
            import time
            time.sleep(60)
        if "crash" in self.html:
            os._exit(9)
        if "starved" in self.html:
            # The 15 GB box the dashboard shares, and a 2.4 MB filing with a pathological table. Not this
            # filing's fault: the next run on a quiet box cuts it.
            raise MemoryError("cannot allocate memory for this document")
        if "prompt" in self.html:
            # A library that asks a question. There is no person on this stdin, and if stdin were the protocol
            # this would eat the next request and the document would hang to the timeout instead.
            input("which form is this? ")
        if "silent" in self.html:
            return {"item_1": _Section("item_1", "Business", item="1", body="")}
        if "swallowed" in self.html:
            # What a failure *inside* detection looks like from outside it: an ``except Exception`` that logs a
            # warning and returns nothing in its place (hybrid_section_detector.py:695). Logged the way the
            # library logs it -- its own logger name and its own function -- because that pair is what the
            # worker counts, and a stub that logged it from anywhere would agree with a worker that counted
            # anything.
            _warn_as("edgar.documents.extractors.hybrid_section_detector", "_try_pattern_detection",
                     "Pattern detection failed: boom")
            return {}
        if "heading" in self.html:
            # The fourth swallow site: one heading whose section could not be built
            # (hybrid_section_detector.py:770). It is how ``_try_heading_detection`` comes back with nothing
            # while every heading it needed was there, and it logs at WARNING like the other four -- so a
            # residual paragraph that called the uncounted sites "DEBUG or nothing at all" was wrong about it.
            _warn_as("edgar.documents.extractors.hybrid_section_detector", "_extract_section_from_heading",
                     "Failed to extract section from heading: boom")
            _warn_as("edgar.documents.extractors.hybrid_section_detector", "detect_sections",
                     "All detection strategies failed, no sections found")
            return {}
        if "benign" in self.html:
            # A warning about something else entirely: an inline-XBRL format the library does not support
            # (strategies/xbrl_extraction.py:544), which says nothing at all about detection. Round 7 measured
            # what counting it costs: a 40-F that happens to log one became a box fault, and a full parse of it
            # every back-off for ever.
            _warn_as("edgar.documents.strategies.xbrl_extraction", "extract",
                     "Unsupported inline XBRL format")
            return {}
        if "nothing" in self.html:
            # The library's ordinary "there are no sections in this filing" exit **also logs a warning**
            # (hybrid_section_detector.py:128), which is the whole reason the worker cannot simply count them.
            # Round 7 measured what a plain count does with this: every sectionless filing in the corpus is
            # called a swallowed failure, deferred, and re-parsed every back-off for ever.
            _warn_as("edgar.documents.extractors.hybrid_section_detector", "detect_sections",
                     "All detection strategies failed, no sections found")
            return {}
        if "aslist" in self.html:
            return [_Section("item_1", "Business", item="1", body="A list, not a mapping.")]
        return {"item_1": _Section("item_1", "Business", item="1", covered_items=("1", "2"),
                                   confidence=0.0, start=10, end=99, validated=True,
                                   body=f"We make widgets, form {self.form}. " + self.html),
                "item_1a": _Section("item_1a", "Risk Factors", item="1A", part="I", method="pattern",
                                    confidence=0.6, warnings=["no stop found"], body="Risky.")}


def parse_html(html, config=None):
    return Document(html, getattr(config, "form", None))
'''


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """A stub ``edgar`` on ``PYTHONPATH``, with the worker pointed at this interpreter."""
    # ``edgar.documents`` is a package in 6.0.0, not a module, and ``edgar.documents.config`` is inside it. The
    # stub has the same shape, so an import the worker writes against the real layout works here and one written
    # against a flattened guess does not.
    pkg = tmp_path / "stub" / "edgar" / "documents"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text(STUB_INIT, encoding="utf-8")
    (pkg / "__init__.py").write_text(STUB_DOCUMENTS, encoding="utf-8")
    (pkg / "config.py").write_text(STUB_CONFIG, encoding="utf-8")
    log = tmp_path / "stub.jsonl"
    # Prepended and not set: the root ``conftest.py`` puts ``module_testing/netguard`` on ``PYTHONPATH`` for
    # every Python child the suite starts, and a test that replaces the variable takes it off all of them.
    #
    # It does not guard *this* child, and the comment that said it did was wrong: the guard installs only on
    # ``RC_NETGUARD=1`` (module_testing/netguard/sitecustomize.py:89) and ``worker_env()`` drops every ``RC_*``
    # on purpose, so what keeps this subprocess offline is the worker's own ``_block_network`` and nothing else.
    # That is the thing under test, so it is just as well -- but the variable is still prepended rather than
    # replaced, because a fixture that quietly disarms a suite-wide guard is a trap whoever copies it next.
    before = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path / "stub"), *([before] if before else [])]))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv(cutter.PYTHON_ENV, sys.executable)
    monkeypatch.setenv("EDGAR_IDENTITY", "must-not-reach-the-worker")
    monkeypatch.setenv("OPENFIGI_API_KEY", "must-not-reach-the-worker")
    monkeypatch.setenv(cutter.MODE_ENV, "edgartools")
    return tmp_path, log


def _stub_log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _answers(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ── the pin ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_pin_is_read_from_the_requirements_file_and_nowhere_else():
    """One place writes the version. A second literal in the code is a second thing to forget on a bump, and a
    handshake comparing against a stale literal would refuse the venv that is actually right.

    "Nowhere else" is the half this test used to skip: it asserted the value and never looked. The code is read
    for a version-shaped literal, and prose may mention one -- a docstring saying which version was measured is
    the point -- so only executable lines are checked.
    """
    assert cutter.REQUIREMENTS.exists(), "the cutter's requirements file is the pin's home"
    assert cutter.pinned_version() == "6.0.0"
    assert "edgartools==" in cutter.REQUIREMENTS.read_text(encoding="utf-8")

    import ast
    from pathlib import Path

    for path in (Path(cutter.__file__), Path(cutter.WORKER)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.fullmatch(r"\d+\.\d+\.\d+", node.value), (
                    f"{path.name} line {node.lineno} holds the version-shaped literal {node.value!r}. The pin "
                    "lives in requirements-edgartools.txt; a copy here is a second thing to forget on a bump.")


def test_a_requirements_file_that_pins_nothing_is_a_failed_run(tmp_path, monkeypatch):
    """A handshake that cannot say what it wanted would pass on any venv, which is the whole point of the pin."""
    loose = tmp_path / "requirements-edgartools.txt"
    loose.write_text("# a comment\nedgartools>=6\n", encoding="utf-8")
    monkeypatch.setattr(cutter, "REQUIREMENTS", loose)
    with pytest.raises(cutter.CutterUnusable, match="does not pin edgartools"):
        cutter.pinned_version()
    monkeypatch.setattr(cutter, "REQUIREMENTS", tmp_path / "not-here.txt")
    with pytest.raises(cutter.CutterUnusable):
        cutter.pinned_version()


def test_a_comment_mentioning_the_version_is_not_the_pin(tmp_path, monkeypatch):
    """The line is read up to its ``#``, so a note about the old version does not become the answer."""
    path = tmp_path / "requirements-edgartools.txt"
    path.write_text("# was edgartools==5.9.0 before 12 Nov\nedgartools==6.0.0  # and this is the pin\n",
                    encoding="utf-8")
    monkeypatch.setattr(cutter, "REQUIREMENTS", path)
    assert cutter.pinned_version() == "6.0.0"


# ── the mode ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_mode_defaults_to_edgartools_and_header_is_the_rollback(monkeypatch):
    monkeypatch.delenv(cutter.MODE_ENV, raising=False)
    assert cutter.mode() == "edgartools"
    monkeypatch.setenv(cutter.MODE_ENV, "HEADER")
    assert cutter.mode() == "header"


def test_a_mode_nobody_recognises_fails_instead_of_choosing_one(monkeypatch):
    """Not a silent default: ``RC_SEC_FORMS_CUTTER=edgar`` reading as "edgartools" would make the rollback a typo
    away from not happening, and reading as "header" would cut 5,149 filings with the wrong rule."""
    monkeypatch.setenv(cutter.MODE_ENV, "edgar")
    with pytest.raises(cutter.CutterUnusable, match="is not a cutter"):
        cutter.mode()


# ── the environment handed to the worker ──────────────────────────────────────────────────────────────────────

def test_the_worker_s_environment_is_a_whitelist_and_not_a_blacklist(monkeypatch):
    """About 35 third-party packages run in there. The assertion is an **upper bound** on the whole set, not a
    list of names that must be absent: a blacklist of the keys we happen to think of passes that weaker test
    while letting `PGPASSWORD`, `WRDS_USERNAME`, `HTTPS_PROXY` and the next variable somebody adds through."""
    for name, value in (("OPENFIGI_API_KEY", "x"), ("DEEPSEEK_API_KEY", "x"), ("EDGAR_IDENTITY", "x"),
                        ("RC_SEC_USER_AGENT", "x"), ("PGPASSWORD", "x"), ("WRDS_USERNAME", "x"),
                        ("HTTPS_PROXY", "http://x"), ("AWS_SECRET_ACCESS_KEY", "x")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(cutter.MODE_ENV, "edgartools")
    env = cutter.worker_env()
    allowed = set(cutter._WORKER_ENV) | {"PYTHONNOUSERSITE"}
    extra = {k for k in env if k not in allowed and not k.startswith("LC_")}
    assert extra == set(), f"the worker's environment is a whitelist; these got through: {sorted(extra)}"
    assert "EDGAR_IDENTITY" not in env, "nothing may identify us to the SEC from inside the cutter"
    assert not [k for k in env if k.startswith("RC_")], "the worker reads no RevenueChaser config, not even the mode"
    assert env.get("PYTHONNOUSERSITE") == "1"
    assert "SSL_CERT_FILE" not in cutter._WORKER_ENV, (
        "carried over from spine/wrds.py, which needs it to open TLS to Wharton; a worker that must never open a "
        "socket has no use for a certificate bundle, and a whitelist is only worth having if every name on it is "
        "there for a reason this worker has")


# ── the client's machinery, against a fake worker ─────────────────────────────────────────────────────────────

class FakeWorker:
    """What :data:`cutter._factory` makes in these tests. ``plan`` is read one document at a time: ``"ok"``,
    ``"fail"``, or an exception to raise."""

    made: list["FakeWorker"] = []

    def __init__(self, plan=None):
        self.plan = list(plan or [])
        self.done = 0
        # The client reads the worker's pid to name the ``.tmp`` a killed worker leaves behind. A pid of its own
        # and not this process's, so a test cannot pass by cleaning up a file the client itself named.
        self.proc = types.SimpleNamespace(pid=4242)
        self.closed = self.killed = False
        self.seen: list[tuple[str, str]] = []
        FakeWorker.made.append(self)

    def cut(self, html, form, out):
        self.seen.append((str(html), form))
        step = self.plan.pop(0) if self.plan else "ok"
        if isinstance(step, BaseException):
            raise step
        self.done += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"item": "Item 1", "text": "x", "chars": 1}) + "\n")
        return 1, 0

    def close(self):
        self.closed = True

    def kill(self):
        self.killed = self.closed = True


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """The cutter with a fake worker and its own answers directory."""
    FakeWorker.made = []
    plans: list = []

    def factory():
        return FakeWorker(plans.pop(0) if plans else None)

    monkeypatch.setattr(cutter, "_factory", factory)
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    return plans


def test_a_document_already_cut_by_an_earlier_run_is_not_cut_again(fake, tmp_path):
    """This is what "a 5.5-hour pass resumes" is: one file per document, so a killed run loses the document in
    flight and nothing else. A checkpoint would lose everything since the last one."""
    with cutter.Cutter() as c:
        first = c.cut("0000-1", tmp_path / "a.htm", "10-K")
        again = c.cut("0000-1", tmp_path / "a.htm", "10-K")
    assert first == again and first.exists()
    assert (c.cut_now, c.cached) == (1, 1)
    assert len(FakeWorker.made[0].seen) == 1, "the second call never reached the worker"


def test_a_failed_document_is_counted_on_disk_so_the_count_is_real_across_runs(fake, tmp_path):
    """Three separate ``Cutter`` objects, as three hourly runs would be. Without the sidecar each run would start
    the count again and a document that kills the worker would be retried for ever."""
    fake.extend([[cutter.DocumentFailed("boom")], [cutter.DocumentFailed("boom")],
                 [cutter.DocumentFailed("boom")]])
    for run in (1, 2, 3):
        with cutter.Cutter() as c:
            assert c.cut("0000-2", tmp_path / "a.htm", "10-K") is None
        assert cutter.tries_so_far("0000-2") == run
    with cutter.Cutter() as c:
        assert c.cut("0000-2", tmp_path / "a.htm", "10-K") is None
        assert "gave up after 3 attempts" in c.failed["0000-2"]
    assert len(FakeWorker.made) == 3, "past the cap no worker is started for it at all"


def test_a_document_that_finally_cuts_loses_its_failure_note(fake, tmp_path):
    """Otherwise a document that failed once would be one run away from the cap for the rest of the corpus's
    life, and a later edgartools bump's recut would start it already spent."""
    fake.extend([[cutter.DocumentFailed("boom")], ["ok"]])
    with cutter.Cutter() as c:
        assert c.cut("0000-3", tmp_path / "a.htm", "10-K") is None
    assert cutter.failed_path("0000-3").exists()
    with cutter.Cutter() as c:
        assert c.cut("0000-3", tmp_path / "a.htm", "10-K") is not None
    assert not cutter.failed_path("0000-3").exists() and cutter.tries_so_far("0000-3") == 0


def test_a_truncated_failure_note_does_not_retire_a_document(fake, tmp_path):
    """A half-written sidecar is the one state a killed run can leave. Read as "give up" it would lose a document
    for good; read as zero it costs one more attempt, which is the cheaper way to be wrong."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter.failed_path("0000-4").write_text('{"tries": ', encoding="utf-8")
    assert cutter.tries_so_far("0000-4") == 0
    with cutter.Cutter() as c:
        assert c.cut("0000-4", tmp_path / "a.htm", "10-K") is not None


def test_the_worker_is_replaced_every_recycle_n_documents(fake, tmp_path):
    """~35 packages parsing 2 MB of HTML each are not trusted to give memory back, and the builder shares a 15 GB
    box with the dashboard."""
    with cutter.Cutter(recycle_n=2) as c:
        for i in range(5):
            assert c.cut(f"0000-r{i}", tmp_path / "a.htm", "10-K") is not None
    assert len(FakeWorker.made) == 3, "2 + 2 + 1"
    assert all(w.closed for w in FakeWorker.made[:-1]), "a recycled worker is closed, not leaked"


def test_an_ordinary_refused_document_keeps_the_worker_it_answered_with(fake, tmp_path):
    """A worker that answered in protocol is alive and fine: the filing was bad, not the process. Replacing it
    anyway cost a live interpreter per refusal -- with pandas, pyarrow and lxml in it -- because dropping the
    reference parks a running child in ``subprocess._active`` with its pipes open, so it never sees EOF and dies
    only when the builder exits. Measured at the time: 15 refusals left 15 processes alive, after
    ``gc.collect()`` and after ``Cutter.close()``, on the 15 GB box the dashboard shares."""
    fake.append([cutter.DocumentFailed("the parser gave up on this filing"), "ok"])
    with cutter.Cutter() as c:
        assert c.cut("0000-29", tmp_path / "a.htm", "10-K") is None
        assert c.cut("0000-30", tmp_path / "b.htm", "10-K") is not None
    assert len(FakeWorker.made) == 1, "one worker for both documents"
    assert c.restarts == 0 and not FakeWorker.made[0].killed


def test_a_lost_worker_is_killed_and_replaced(fake, tmp_path):
    """The other half: a worker that died, hung or answered out of protocol cannot be trusted with another
    document, and is closed rather than merely dropped."""
    fake.extend([[cutter.WorkerLost("did not answer in 180s")], ["ok"]])
    with cutter.Cutter() as c:
        assert c.cut("0000-31", tmp_path / "a.htm", "10-K") is None
        assert c.cut("0000-32", tmp_path / "b.htm", "10-K") is not None
    assert len(FakeWorker.made) == 2 and c.restarts == 1
    assert FakeWorker.made[0].killed, "a lost worker is killed, not left for the garbage collector"


def test_five_failures_in_a_row_stop_the_run(fake, tmp_path):
    """It bounds the worst case in time: without it, every document timing out costs 5,149 x 180 s, which is 257
    hours of the hourly builder holding its lock."""
    fake.append([cutter.DocumentFailed("the parser gave up on this filing")] * 5)
    names = [f"0000-b{i}" for i in range(5)]
    with cutter.Cutter(break_after=5) as c:
        for name in names[:4]:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is None
        assert all(cutter.tries_so_far(n) == 1 for n in names[:4])
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut(names[4], tmp_path / "a.htm", "10-K")


def test_the_stopped_run_keeps_the_attempts_of_the_filings_that_were_answered_about(fake, tmp_path):
    """The stall this replaced, and it is the most expensive thing a green suite has hidden here.

    Giving every attempt back meant a filing the parser refuses never reached the cap, because the only thing
    that clears the run of failures is a document *this run* cut -- and on a resumed pass almost every good
    document is cached, so the worker sees only the bad ones. Five of them anywhere in the corpus stopped every
    hourly run at the same five documents, gave their attempts back, and nothing after the fifth was ever cut
    again. Charged, they retire after three runs and the pass goes on.
    """
    fake.append([cutter.DocumentFailed("the parser gave up on this filing")] * 5)
    names = [f"0000-k{i}" for i in range(5)]
    with cutter.Cutter(break_after=5) as c:
        for name in names[:4]:
            c.cut(name, tmp_path / "a.htm", "10-K")
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut(names[4], tmp_path / "a.htm", "10-K")
    assert [cutter.tries_so_far(n) for n in names] == [1, 1, 1, 1, 1], "the filings' own failures are spent"
    assert all(cutter.failed_path(n).exists() for n in names)


def test_the_stopped_run_gives_back_the_attempts_of_the_documents_whose_worker_was_lost(fake, tmp_path):
    """The other half. A worker that hangs or dies says nothing about the filing it was holding: five of those in
    a row is the box, and a box that fails every document must leave the corpus as it found it, or three runs
    retire all 5,149 for a reason none of them had anything to do with."""
    # One plan a worker, not one plan of five: a lost worker is replaced, so each document meets a new one.
    for _ in range(5):
        fake.append([cutter.WorkerLost("the cutter worker did not answer in 180s")])
    names = [f"0000-l{i}" for i in range(5)]
    with cutter.Cutter(break_after=5) as c:
        for name in names[:4]:
            c.cut(name, tmp_path / "a.htm", "10-K")
        assert all(cutter.tries_so_far(n) == 1 for n in names[:4])
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut(names[4], tmp_path / "a.htm", "10-K")
    assert [cutter.tries_so_far(n) for n in names] == [0, 0, 0, 0, 0], "no attempt was spent"
    # The note stays, holding nothing but the give-back: it is the only memory that this document has already had
    # one, and without it the give-back repeats in every run for ever.
    assert all(cutter.gave_back_before(n) == 1 for n in names)


def test_a_resumed_pass_whose_good_documents_are_all_cached_still_retires_its_bad_ones(fake, tmp_path):
    """The shape of every run after the first: the worker is handed nothing but the documents that failed before.
    Three of those runs must retire them, and the fourth must get past them."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    for i in range(9):                       # the corpus's good documents, cut by an earlier run
        with gzip.open(cutter.answer_path(f"0000-g{i}"), "wt", encoding="utf-8") as fh:
            fh.write("{}\n")
    bad = [f"0000-m{i}" for i in range(2)]
    for attempt in range(cutter.MAX_TRIES):
        fake.append([cutter.DocumentFailed("the parser gave up on this filing")] * 2)
        with cutter.Cutter(break_after=5) as c:
            for i in range(9):
                assert c.cut(f"0000-g{i}", tmp_path / "a.htm", "10-K") is not None
            for name in bad:
                assert c.cut(name, tmp_path / "a.htm", "10-K") is None
            assert c.cached == 9
        assert [cutter.tries_so_far(n) for n in bad] == [attempt + 1] * 2, "a cached document charges nothing"
    FakeWorker.made = []
    with cutter.Cutter(break_after=5) as c:
        for name in bad:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is None
            assert "gave up after" in c.failed[name]
        assert not FakeWorker.made, "a retired document is not sent to the worker at all"


def test_a_document_that_cuts_breaks_the_run_of_failures(fake, tmp_path):
    """The breaker counts *consecutive* failures. A pass where one filing in ten is genuinely bad must run to the
    end, which it cannot do if the count never resets."""
    fake.append([cutter.DocumentFailed("bad")] * 4 + ["ok"] + [cutter.DocumentFailed("bad")] * 4)
    with cutter.Cutter(break_after=5) as c:
        for i in range(9):
            c.cut(f"0000-c{i}", tmp_path / "a.htm", "10-K")
        assert c.cut_now == 1 and len(c.failed) == 8, "nine documents, none of them a stopped run"


def test_an_io_failure_is_the_box_and_not_the_filing(fake, tmp_path):
    """A full disk, a read-only answer root, no file handles left. The worker says ``io``, the document is
    charged no attempt and the pass carries on: charging each of 5,149 documents an attempt would retire the
    corpus in three runs for a reason none of them had anything to do with.

    It used to be fed "the document is not on disk", which is the case the review reversed: an unreadable *file*
    is now that document's own failure, so the test was pinning a rule the code no longer has.
    """
    fake.append([cutter.CutterUnusable("the cutter could not write or read a.htm: OSError: [Errno 28] No space "
                                       "left on device")])
    with cutter.Cutter() as c:
        assert c.cut("0000-33", tmp_path / "a.htm", "10-K") is None
        assert "No space left" in c.failed["0000-33"]
    assert cutter.tries_so_far("0000-33") == 0, "charging each of 5,149 documents would retire the corpus"
    assert cutter.stops_so_far("0000-33") == 1


def test_an_unreadable_document_is_charged_unless_the_corpus_itself_has_gone(fake, tmp_path):
    """The decision the worker cannot make, because it is handed one path a request: the directory a file sits in
    says nothing about the mount above it. The corpus is one directory per CIK (``core/edgar_docs.py:31``), so
    asking about the file's own parent read a company whose folder had not been downloaded as an unmounted
    corpus -- and that cost the document no attempt, so every hourly run met it again."""
    fake.append([cutter.DocumentUnreadable("this document cannot be read: FileNotFoundError")])
    (tmp_path / "1234").mkdir()                  # a corpus with something in it, which is what "there" means
    with cutter.Cutter(corpus_root=tmp_path) as c:
        assert c.cut("0000-46", tmp_path / "1234" / "a.htm", "10-K") is None, "the corpus is there; charge it"
    assert cutter.tries_so_far("0000-46") == 1 and cutter.stops_so_far("0000-46") == 0

    fake.append([cutter.DocumentUnreadable("this document cannot be read: FileNotFoundError")])
    with cutter.Cutter(corpus_root=tmp_path / "not-mounted") as c:
        assert c.cut("0000-47", tmp_path / "not-mounted" / "1234" / "a.htm", "10-K") is None
        assert "not mounted where it should be" in c.failed["0000-47"]
    assert cutter.tries_so_far("0000-47") == 0, "nothing in the corpus can be read, so no document is at fault"
    assert cutter.stops_so_far("0000-47") == 1, "but the run that failed on it is remembered"


def test_without_a_corpus_root_an_unreadable_document_is_still_charged(fake, tmp_path):
    """The safe direction when the caller has not said where the corpus is: a charged document is retired after
    three runs and the sidecar says why, while a run that stops leaves no trace at all."""
    fake.append([cutter.DocumentUnreadable("this document cannot be read: BadGzipFile")])
    (tmp_path / "1234").mkdir(exist_ok=True)
    with cutter.Cutter() as c:
        assert c.corpus_root is None
        assert c.cut("0000-48", tmp_path / "a.htm.gz", "10-K") is None
    assert cutter.tries_so_far("0000-48") == 1


def test_a_hanging_document_has_its_attempt_back_once_and_then_keeps_it(fake, tmp_path):
    """The stall that survived the first fix. A document that hangs the worker had its attempt returned whenever
    the breaker stopped the run -- and on a resumed pass every good document is cached, so five hanging documents
    arrived in a row in *every* run, were given their attempts back in every run, never reached the cap, and
    nothing after the fifth was ever cut again. One give-back is remembered; the second is not given."""
    names = [f"0000-h{i}" for i in range(5)]
    for run in range(2):
        for _ in range(5):
            fake.append([cutter.WorkerLost("the cutter worker did not answer in 180s")])
        with cutter.Cutter(break_after=5) as c:
            for name in names[:4]:
                c.cut(name, tmp_path / "a.htm", "10-K")
            with pytest.raises(cutter.CutterUnusable, match="in a row"):
                c.cut(names[4], tmp_path / "a.htm", "10-K")
        spent = [cutter.tries_so_far(n) for n in names]
        assert spent == [0, 0, 0, 0, 0] if run == 0 else spent == [1, 1, 1, 1, 1], (
            f"run {run + 1}: {spent}. The first loss is forgiven; the second is the document's own doing.")
    assert all(cutter.gave_back_before(n) == 1 for n in names), "the give-back is remembered, so it ends"

    for _ in range(5):
        fake.append([cutter.WorkerLost("the cutter worker did not answer in 180s")])
    with cutter.Cutter(break_after=5) as c:
        for name in names[:4]:
            c.cut(name, tmp_path / "a.htm", "10-K")
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut(names[4], tmp_path / "a.htm", "10-K")
    assert [cutter.tries_so_far(n) for n in names] == [2, 2, 2, 2, 2], "and it keeps spending them"


def test_a_charged_attempt_does_not_erase_the_give_back_the_breaker_remembered(fake, tmp_path):
    """``_note_failure`` writes the whole sidecar, so the ordinary charge path has to carry the count forward.
    Defaulting it to zero would hand the document a clean slate and make the give-back endless again."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter.failed_path("0000-49").write_text(json.dumps({"tries": 0, "gave_back": 1, "error": "x", "at": "now"}),
                                             encoding="utf-8")
    fake.append([cutter.DocumentFailed("the parser gave up on this filing")])
    with cutter.Cutter() as c:
        assert c.cut("0000-49", tmp_path / "a.htm", "10-K") is None
    assert cutter.tries_so_far("0000-49") == 1 and cutter.gave_back_before("0000-49") == 1


def test_a_negative_count_is_not_a_cut_document(tmp_path, monkeypatch):
    """``json`` accepts ``-1`` and ``if sections and not chars`` lets it through, so the document would be cached
    as cut with a row count nothing could use. It is out of protocol, like a count that is missing."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setenv(cutter.PYTHON_ENV, sys.executable)
    script = tmp_path / "negative.py"
    # It writes the answer file first, as the real worker does -- it renames before it answers. Without that the
    # "nothing is cached" assertion below holds for the script and not for the worker, because the client's own
    # rescue path would hand the renamed file back as a cut document.
    script.write_text('import gzip, json, sys\n'
                      'print(json.dumps({"edgar_version": "6.0.0"}), flush=True)\n'
                      'for line in sys.stdin:\n'
                      '    out = json.loads(line)["out"]\n'
                      '    with gzip.open(out, "wt", encoding="utf-8") as fh:\n'
                      '        fh.write(json.dumps({"item": "1", "text": "cannot be trusted"}) + "\\n")\n'
                      '    print(json.dumps({"ok": True, "sections": -1, "chars": 7}), flush=True)\n',
                      encoding="utf-8")
    monkeypatch.setattr(cutter, "WORKER", script)
    with cutter.Cutter() as c:
        assert c.cut("0000-50", tmp_path / "a.htm", "10-K") is None
        assert "negative count" in c.failed["0000-50"]
        assert c.restarts == 1, "a count that cannot be true is the protocol, so the worker is replaced"
    assert not cutter.answer_path("0000-50").exists(), "and the file it already renamed is not kept"


def test_a_zero_byte_answer_file_is_not_a_cut_document(fake, tmp_path):
    """What a crash between ``open`` and the first write can leave under the real name, and ``gzip.open`` reads
    one as zero rows without complaining -- so the document would be cached for ever as cut and empty. A gzip
    member is never zero bytes."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter.answer_path("0000-34").write_bytes(b"")
    assert not cutter._is_cut(cutter.answer_path("0000-34"))
    with cutter.Cutter() as c:
        assert c.cut("0000-34", tmp_path / "a.htm", "10-K") is not None
        assert (c.cut_now, c.cached) == (1, 0), "it was cut now, not read as an earlier run's work"


def test_a_worker_that_wrote_the_file_and_then_was_lost_counts_as_cut(fake, tmp_path):
    """A timeout on the answer line after the worker's own rename. The document *is* cut: calling it failed would
    have the caller write a fallback row over good data, and the next run would hand back the same file as cached
    anyway -- so one document would be "failed" in one run and "cut" in the next, with the sidecar left behind."""
    out = cutter.answer_path("0000-35")

    class WroteThenDied(FakeWorker):
        def cut(self, html, form, answer):
            answer.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(answer, "wt", encoding="utf-8") as fh:
                fh.write(json.dumps({"item": "1", "text": "real", "chars": 4}) + "\n")
            raise cutter.WorkerLost("did not answer in 180s")

    fake_made = FakeWorker.made
    with cutter.Cutter() as c:
        c.worker = WroteThenDied()
        FakeWorker.made = fake_made
        assert c.cut("0000-35", tmp_path / "a.htm", "10-K") == out
        assert c.cut_now == 1
    assert cutter.tries_so_far("0000-35") == 0 and not cutter.failed_path("0000-35").exists()
    assert _answers(out)[0]["text"] == "real"


def test_a_cached_document_loses_a_failure_note_left_beside_it(fake, tmp_path):
    """Another process cut it, or a run was killed between the worker's rename and the client's bookkeeping.
    Either way the note must go, or the document stays one run from the cap for the rest of the corpus's life."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    with gzip.open(cutter.answer_path("0000-36"), "wt", encoding="utf-8") as fh:
        fh.write("{}\n")
    cutter.failed_path("0000-36").write_text('{"tries": 2, "error": "x", "at": "now"}', encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-36", tmp_path / "a.htm", "10-K") is not None and c.cached == 1
    assert not cutter.failed_path("0000-36").exists()


def test_a_worker_that_cannot_start_fails_the_run_and_charges_no_attempt(monkeypatch, tmp_path):
    """The expensive silent failure: a broken or upgraded venv burning every document's three tries in one run
    and turning 5,149 reports into fallback rows that look cut."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")

    def refuse():
        raise cutter.CutterUnusable("no edgartools interpreter")

    monkeypatch.setattr(cutter, "_factory", refuse)
    with cutter.Cutter() as c:
        with pytest.raises(cutter.CutterUnusable):
            c.cut("0000-7", tmp_path / "a.htm", "10-K")
    assert cutter.tries_so_far("0000-7") == 0 and not cutter.failed_path("0000-7").exists()


def test_an_unset_interpreter_names_the_variable(monkeypatch):
    monkeypatch.delenv(cutter.PYTHON_ENV, raising=False)
    with pytest.raises(cutter.CutterUnusable, match=cutter.PYTHON_ENV):
        cutter.Worker()
    monkeypatch.setenv(cutter.PYTHON_ENV, "/no/such/python")
    with pytest.raises(cutter.CutterUnusable, match="/no/such/python"):
        cutter.Worker()


# ── the real worker, as a subprocess, against the stub ────────────────────────────────────────────────────────

def test_the_worker_reports_its_version_before_anything_is_asked_of_it(stub, tmp_path, monkeypatch):
    """The handshake is unprompted, so a venv that cannot import edgartools is known before a document is sent
    and no document is charged for it. The test asserts that the handshake *was consumed*: it used to assert only
    ``done == 0``, and passed with ``_handshake`` deleted -- leaving the version line to be read as the first
    document's answer."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    worker = cutter.Worker()
    try:
        assert worker.done == 0
        assert worker.cut(doc, "10-K", tmp_path / "answers" / "a.jsonl.gz") == (2, 0), (
            "the first answer read must be this document's, not the handshake line")
    finally:
        worker.close()


def test_a_venv_with_the_wrong_version_fails_the_run(stub, monkeypatch, tmp_path):
    """A venv upgraded underneath us would otherwise write rows labelled 6.0.0 that another version cut, and
    nothing downstream could tell them apart."""
    (tmp_path / "stub" / "edgar" / "__init__.py").write_text('__version__ = "6.1.0"\n', encoding="utf-8")
    # The pin is 6.0.0 and this venv says 6.1.0.
    with pytest.raises(cutter.CutterUnusable, match="6.1.0"):
        cutter.Worker()


@pytest.mark.parametrize("broken,says", [
    ("edgar/__init__.py", "no pandas"),
    ("edgar/documents/__init__.py", "cannot import name"),
])
def test_a_venv_whose_import_fails_fails_the_run_and_carries_the_import_error(stub, tmp_path, broken, says):
    """Both files, because the first version of this worker imported only ``edgar`` in the handshake and reached
    for the parser on the first document -- so an `edgar.documents` that does not hold what it wants arrived as a
    *document* failure, once per document, and retired all 5,149 of them in three runs. Breaking only
    ``edgar/__init__.py``, as this test used to, could not see that.

    The error text is the assertion. "No module named 'edgar'" sends somebody to the venv and "cannot import
    name" sends them to the worker's own import line; a message without it sends them nowhere.
    """
    path = tmp_path / "stub" / broken
    if broken.endswith("documents/__init__.py"):
        path.write_text("raise ImportError(\"cannot import name 'parse_html' from 'edgar.documents'\")\n",
                        encoding="utf-8")
    else:
        path.write_text("raise ImportError('no pandas')\n", encoding="utf-8")
    with pytest.raises(cutter.CutterUnusable, match=says):
        cutter.Worker()


def test_one_document_through_the_real_worker_writes_edgartools_own_fields_unmapped(stub, tmp_path, monkeypatch):
    """The answer file holds their output and none of ours, so a change to our own rules re-types the cached
    answers in minutes and only an edgartools bump costs the 5.5 hours."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        out = c.cut("0000-8", doc, "10-K")
    rows = _answers(out)
    assert [r["item"] for r in rows] == ["1", "1A"]
    assert [r["key"] for r in rows] == ["item_1", "item_1a"]
    assert [r["name"] for r in rows] == ["item_1", "item_1a"]
    assert rows[0]["detection_method"] == "toc" and rows[1]["detection_method"] == "pattern"
    assert rows[1]["warnings"] == ["no stop found"] and rows[1]["title"] == "Risk Factors"
    assert rows[1]["part"] == "I" and rows[0]["part"] is None
    assert rows[0]["chars"] == len(rows[0]["text"]) and "a filing" in rows[0]["text"]
    # Every public field of the dataclass, because getting one back later costs the whole 5.5-hour pass.
    assert rows[0]["covered_items"] == ["1", "2"], (
        "a filer who writes 'Items 1 and 2. Business and Properties' gets one section; without this field "
        "Item 2 is invisible to the mapping and that filing's business section silently has no properties")
    assert (rows[0]["start_offset"], rows[0]["end_offset"]) == (10, 99)
    assert rows[0]["validated"] is True and rows[0]["kind"] == "item"
    # 0.0, not None: a falsy value is still a value, and "if value" instead of "if value is not None" would
    # store the least confident section in the corpus as having no confidence at all.
    assert rows[0]["confidence"] == 0.0
    assert not set(rows[0]) & {"quality", "rules_version", "flag"}, "our mapping is cut.py's, not the worker's"
    assert c.empty == 0


def test_a_gzipped_document_is_read_as_one(stub, tmp_path, monkeypatch):
    """The corpus is stored gzipped; a worker that read it as bytes would hand edgartools a header."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm.gz"
    with gzip.open(doc, "wt", encoding="utf-8") as fh:
        fh.write("<html>a zipped filing</html>")
    with cutter.Cutter() as c:
        out = c.cut("0000-9", doc, "10-K")
    assert "a zipped filing" in _answers(out)[0]["text"]


def test_the_worker_has_no_network_and_no_identity(stub, tmp_path, monkeypatch):
    """Enforced inside the worker rather than by the caller: a proxy variable only stops a client that reads
    proxy variables, and this stops all of them. The stub tries a socket when ``edgar.documents`` is imported, as
    a library building a module-level client would -- which is the first document and not the handshake, because
    the worker imports the parser only after the network is gone."""
    _, log = stub
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-17", doc, "10-K") is not None
    said = _stub_log(log)
    # The first line is written when ``edgar`` itself is imported, which is the handshake: the block has to be in
    # place by then, because a library builds its module-level clients on import.
    assert said[0] == {"at_import": "NetworkBlocked"}, (
        f"{said[0]}: the network must already be gone when the handshake imports edgar")
    # Every primitive the worker replaces, and nothing less: a block line nobody probes can be deleted, which is
    # what the battery found for ``sendmsg``, ``gethostbyname_ex`` and ``getnameinfo``.
    assert set(said[1]) == {"connect", "connect_ex", "create_connection", "getaddrinfo", "gethostbyname",
                            "gethostbyname_ex", "getnameinfo", "sendto", "sendmsg"}
    assert set(said[1].values()) == {"NetworkBlocked"}, (
        f"{said[1]}: every primitive must raise what the worker installed, and nothing else can answer first. "
        "The suite's own child guard is *not* behind this one: it installs on ``RC_NETGUARD=1`` "
        "(module_testing/netguard/sitecustomize.py:89) and the client's whitelist drops every ``RC_*``, so this "
        "subprocess has only what the worker put there. 'open' means it put nothing there. A name lookup "
        "already leaves the box, and a datagram needs no connect at all.")
    assert said[2] == {"identity": None, "figi": False, "mode": None}


def test_the_worker_removes_edgar_identity_even_when_it_is_handed_one(stub, tmp_path):
    """Defence in depth, and the only test that can see it: the client's whitelist already drops
    ``EDGAR_IDENTITY``, so with the usual environment the worker's own ``pop`` is unobservable and would be
    deleted as dead code the next time somebody tidied up. Here the worker is started by hand with the variable
    set, which is what a future caller that builds its own environment would do."""
    _, log = stub
    env = dict(cutter.worker_env(), EDGAR_IDENTITY="rc@example.test", OPENFIGI_API_KEY="smuggled")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-P", str(cutter.WORKER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    try:
        assert "edgar_version" in json.loads(proc.stdout.readline())
        proc.stdin.write(json.dumps({"html": str(doc), "form": "10-K",
                                     "out": str(tmp_path / "a.jsonl.gz")}) + "\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["ok"]
    finally:
        proc.kill()
        proc.wait(timeout=30)
    said = _stub_log(log)
    environment = next(line for line in said if "identity" in line)
    assert environment["identity"] is None, "the worker must drop it itself, not trust the caller to"
    assert environment["figi"] is True, "this test only means something because the key did get through"


def test_a_worker_that_exits_without_a_handshake_says_the_venv_has_no_edgartools(tmp_path, monkeypatch):
    """The message is the test. "ended without a handshake" sends somebody to the venv; the generic "that is not
    a handshake ''" of an empty line sends them to the protocol, which is not where the problem is."""
    monkeypatch.setenv(cutter.PYTHON_ENV, sys.executable)
    script = tmp_path / "silent.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(cutter, "WORKER", script)
    with pytest.raises(cutter.CutterUnusable, match="without a handshake"):
        cutter.Worker()


@pytest.mark.parametrize("body,what", [
    ("boom", "the parser refuses the document before a section exists"),
    ("halfway", "one section is written and the next one's text cannot be read"),
])
def test_a_document_that_fails_leaves_no_half_written_answer_file(stub, tmp_path, monkeypatch, body, what):
    """A file under its real name means the document is cut, so the partial write goes to ``.tmp`` and is
    removed. A ``.tmp`` left behind would be harmless on its own and 5,149 of them would not.

    Both failures, because only the second one opens a file: a document that dies before the first section never
    creates a ``.tmp`` at all, so on its own it cannot tell a worker that cleans up from one that does not --
    which is what the mutation check showed when the first case was the only one here.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    bad = tmp_path / f"{body}.htm"
    bad.write_text(f"<html>{body}</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-18", bad, "10-K") is None, what
    left = sorted(p.name for p in (tmp_path / "answers").glob("*") if p.suffix != ".json")
    assert left == [], f"the failed document left {left} behind"


def test_a_cutter_unusable_raised_while_cutting_is_still_the_run_s_problem(fake, tmp_path):
    """The contract is that only :class:`DocumentFailed` is a document's problem. Catching the base class here
    instead would make a broken venv charge every document an attempt and turn 5,149 reports into fallback rows
    that look cut -- the one failure that is both silent and expensive."""
    fake.append([cutter.CutterUnusable("the venv went away mid-pass")])
    with cutter.Cutter() as c:
        assert c.cut("0000-19", tmp_path / "a.htm", "10-K") is None
        assert "the venv went away" in c.failed["0000-19"]
    assert cutter.tries_so_far("0000-19") == 0, "the box's fault costs the document no attempt"
    assert cutter.stops_so_far("0000-19") == 1, "and the run that failed on it is remembered"


def test_a_library_that_prints_does_not_break_the_protocol(stub, tmp_path, monkeypatch):
    """edgartools prints through ``rich``; the stub prints a progress line on every detect. On the protocol's own
    stdout that line would read as the answer."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-10", doc, "10-K") is not None
        assert c.cut("0000-11", doc, "10-K") is not None, "the second answer is not the first's printed line"


def test_a_document_the_parser_refuses_is_answered_and_the_worker_stays(stub, tmp_path, monkeypatch):
    """One document's failure is not the run's. The answer carries the error; no answer file is written, because
    a file under its real name means the document is cut."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    bad, good = tmp_path / "boom.htm", tmp_path / "ok.htm"
    bad.write_text("<html>boom</html>", encoding="utf-8")
    good.write_text("<html>a filing</html>", encoding="utf-8")
    worker = cutter.Worker()
    try:
        with pytest.raises(cutter.DocumentFailed, match="gave up on this filing"):
            worker.cut(bad, "10-K", tmp_path / "answers" / "bad.jsonl.gz")
        assert not (tmp_path / "answers" / "bad.jsonl.gz").exists()
        assert worker.cut(good, "10-K", tmp_path / "answers" / "good.jsonl.gz") == (2, 0)
        assert worker.done == 2, ("every answered document, the refusal included: both cost the process its "
                                  "memory, so both count towards the recycle. Without this line the real worker "
                                  "is never recycled, however many documents the pass sends it -- and no test "
                                  "using the fake one can see that, because the fake keeps its own count")
    finally:
        worker.close()


def test_a_worker_that_dies_mid_document_is_a_failed_document_and_not_a_failed_run(stub, tmp_path, monkeypatch):
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "crash.htm"
    doc.write_text("<html>crash</html>", encoding="utf-8")
    good = tmp_path / "ok.htm"
    good.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-12", doc, "10-K") is None
        assert cutter.tries_so_far("0000-12") == 1
        assert c.cut("0000-13", good, "10-K") is not None, "the next document gets a new worker"


def test_a_hung_document_is_killed_at_the_timeout_and_the_pass_goes_on(stub, tmp_path, monkeypatch):
    """WRDS's client blocks for ever (``spine/wrds.py:158``). A filer's pathological table must cost one timeout
    and not the run: the measured mean is 3.9 s a document, so a timeout is a hang."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setattr(cutter, "TIMEOUT_S", 2.0)
    hung = tmp_path / "hang.htm"
    hung.write_text("<html>hang</html>", encoding="utf-8")
    good = tmp_path / "ok.htm"
    good.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-14", hung, "10-K") is None
        assert "did not answer" in c.failed["0000-14"]
        assert c.cut("0000-15", good, "10-K") is not None


def test_the_partial_file_a_killed_worker_leaves_is_removed_by_the_client(stub, tmp_path, monkeypatch):
    """A worker killed at a timeout cannot remove its own ``.tmp``, and the tmp is open for the whole of
    detection -- ``Document.sections`` and every ``text()`` are read inside the ``with``. 5,149 documents in a
    pass, so one leftover a hang is a directory of them.

    This is also the test the name itself needed: ``_cleanup_tmp`` built the name from the *client's* pid while
    the worker writes its own, so it removed a file that never existed, and nothing noticed because the two
    failures that were tested are both cleaned up by the worker itself.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setattr(cutter, "TIMEOUT_S", 3.0)
    slow = tmp_path / "hangmid.htm"
    slow.write_text("<html>hangmid</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        pid = c._worker().proc.pid
        assert c.cut("0000-45", slow, "10-K") is None
        left = sorted(q.name for q in cutter.answers_dir().glob("*.tmp"))
        assert not left, f"the worker's own partial file is still there: {left}"
    assert not cutter.answer_path("0000-45").exists()
    assert pid != os.getpid(), "the file was named by the worker's pid, which is not the client's"


def test_an_answer_out_of_protocol_stops_the_worker_instead_of_being_read_past(tmp_path, monkeypatch):
    """The next line could belong to the next document, so a row of one filing's text would be stored as
    another's. ``spine/wrds.py`` stops for the same reason; here the worker is replaced rather than the run
    ended."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setenv(cutter.PYTHON_ENV, sys.executable)
    script = tmp_path / "noise.py"
    script.write_text('import json, sys\n'
                      'print(json.dumps({"edgar_version": "6.0.0"}), flush=True)\n'
                      'for line in sys.stdin:\n'
                      '    print("Downloading... 42%", flush=True)\n', encoding="utf-8")
    monkeypatch.setattr(cutter, "WORKER", script)
    with cutter.Cutter() as c:
        assert c.cut("0000-16", tmp_path / "a.htm", "10-K") is None
        assert "out of protocol" in c.failed["0000-16"]


def test_one_file_missing_from_a_readable_directory_is_that_document_s_own_failure(stub, tmp_path, monkeypatch):
    """What the phase is *for*, and the rule that round 2 of the review corrected.

    Answered as the box's fault, a single bad file stopped the run -- and a ``CutterUnusable`` is remembered
    nowhere, so the next hourly run met the same document and stopped again, for ever, with nothing after it ever
    cut. The directory holding it decides: readable, and this file is this document's problem.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    with cutter.Cutter() as c:
        assert c.cut("0000-37", tmp_path / "never-written.htm", "10-K") is None
        assert "FileNotFoundError" in c.failed["0000-37"]
        assert c.worker is not None, "the worker had nothing to do with it and is kept"
    assert cutter.tries_so_far("0000-37") == 1, "charged, so three runs retire it and the pass goes on"


def test_a_corpus_that_is_not_mounted_costs_the_document_nothing(stub, tmp_path, monkeypatch):
    """The whole path, end to end: the worker says it could not read the file, the client asks whether the corpus
    is still there, and nothing is charged. The worker is not asked to decide, because it is handed one path a
    request and the directory a file sits in says nothing about the mount above it."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    gone = tmp_path / "not-mounted"
    with cutter.Cutter(corpus_root=gone) as c:
        assert c.cut("0000-38", gone / "1234" / "a.htm", "10-K") is None
        assert "not mounted where it should be" in c.failed["0000-38"]
    assert cutter.tries_so_far("0000-38") == 0, "no attempt is charged for the box's fault"
    assert cutter.stops_so_far("0000-38") == 1


def test_a_gz_file_that_is_not_gzip_is_that_document_s_own_failure(stub, tmp_path, monkeypatch):
    """``gzip.BadGzipFile`` is an ``OSError``, so a truncated download used to read as the box's fault and stop
    every run at the same file. Measured: ``issubclass(gzip.BadGzipFile, OSError)``."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    truncated = tmp_path / "half-downloaded.htm.gz"
    truncated.write_bytes(b"<html>not gzip at all</html>")
    with cutter.Cutter() as c:
        assert c.cut("0000-42", truncated, "10-K") is None
        assert "cannot be read" in c.failed["0000-42"]
    assert cutter.tries_so_far("0000-42") == 1


def test_a_directory_where_a_filing_should_be_is_that_document_s_own_failure(stub, tmp_path, monkeypatch):
    """What an interrupted download or a wrongly built corpus index leaves behind. ``IsADirectoryError`` is an
    ``OSError`` too, so this took the same path as an unmounted corpus before."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    (tmp_path / "a-directory.htm").mkdir()
    with cutter.Cutter() as c:
        assert c.cut("0000-43", tmp_path / "a-directory.htm", "10-K") is None
        assert "cannot be read" in c.failed["0000-43"]
    assert cutter.tries_so_far("0000-43") == 1


def test_an_answer_directory_that_cannot_be_written_stops_the_run_on_the_first_document(fake, tmp_path,
                                                                                       monkeypatch):
    """A read-only mount or a raw root that is not there. It reached the caller as a bare ``OSError`` from
    ``Worker.cut``'s own ``mkdir``, which sits before every phase, so the class's promise -- only
    ``CutterUnusable`` and ``DocumentFailed`` leave it -- was broken on the very first document."""
    blocked = tmp_path / "a-file-not-a-directory"
    blocked.write_text("the raw root is a file here", encoding="utf-8")
    monkeypatch.setattr(cutter, "answers_dir", lambda: blocked / "answers")
    with pytest.raises(cutter.CutterUnusable, match="cannot write into"):
        cutter.Worker.cut(cutter.Worker.__new__(cutter.Worker), tmp_path / "a.htm", "10-K",
                          cutter.answer_path("0000-44"))


def test_a_venv_that_never_answers_the_handshake_fails_the_run_at_a_timeout(tmp_path, monkeypatch):
    """A stale NFS mount under ``site-packages``, or an import that waits on something. The handshake runs about
    thirty times in a full pass -- at the start, at every recycle, after every lost worker -- so a blocking read
    there holds the hourly builder and its lock for ever, with nothing in the log to say what on."""
    monkeypatch.setenv(cutter.PYTHON_ENV, sys.executable)
    monkeypatch.setattr(cutter, "HANDSHAKE_TIMEOUT_S", 2.0)
    script = tmp_path / "mute.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(cutter, "WORKER", script)
    with pytest.raises(cutter.CutterUnusable, match="sent no handshake"):
        cutter.Worker()


@pytest.mark.parametrize("answer,what", [
    ('{"ok": true}', "no count at all"),
    # ``json`` accepts ``Infinity`` and ``int(float("inf"))`` raises ``OverflowError``, which is not a
    # ``ValueError`` -- so it escaped the first version of the guard and reached the caller as itself.
    # ``tries_so_far`` has ``OverflowError`` in its list for the same reason.
    ('{"ok": true, "sections": Infinity, "chars": 1}', "a count that is not a number"),
])
def test_an_answer_that_claims_a_cut_without_a_count_is_out_of_protocol(tmp_path, monkeypatch, answer, what):
    """``{"ok": true}`` and nothing else. Read leniently as "cut, 0 sections", the document would be cached for
    ever as a filing with no sections in it -- the same silent loss as the empty-text answer, arriving through a
    different hole. It is the worker's protocol that is broken, so it is replaced rather than trusted again."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setenv(cutter.PYTHON_ENV, sys.executable)
    script = tmp_path / "countless.py"
    # It writes the answer file first, which is what the real worker does -- it renames before it answers. The
    # first version of this test used a script that wrote nothing, so "nothing is cached" held for the script and
    # not for the worker: the client's own rescue path ("it wrote the file and then we lost it") would have
    # handed back a file written by a worker that could not say what was in it, and that path skips the
    # empty-text check too.
    script.write_text('import gzip, json, sys\n'
                      'print(json.dumps({"edgar_version": "6.0.0"}), flush=True)\n'
                      'for line in sys.stdin:\n'
                      '    out = json.loads(line)["out"]\n'
                      '    with gzip.open(out, "wt", encoding="utf-8") as fh:\n'
                      '        fh.write(json.dumps({"item": "1", "text": "cannot be trusted"}) + "\\n")\n'
                      f'    print({answer!r}, flush=True)\n', encoding="utf-8")
    monkeypatch.setattr(cutter, "WORKER", script)
    out = cutter.answer_path("0000-39")
    with cutter.Cutter() as c:
        assert c.cut("0000-39", tmp_path / "a.htm", "10-K") is None
        assert "without saying how much" in c.failed["0000-39"]
        assert c.empty == 0 and c.cut_now == 0, "nothing is cached as a document with no sections in it"
        assert c.restarts == 1, "the protocol is broken, so the worker is replaced and not asked again"
    assert not out.exists(), "a file by a worker that cannot say what is in it is not kept"
    assert cutter.tries_so_far("0000-39") == 1


def test_the_answer_directory_carries_the_pin_so_two_versions_cannot_mix(monkeypatch, tmp_path):
    """Keyed by ``CUTTER_VERSION`` alone, a bumped pin and a rebuilt venv would pass the handshake and then write
    into the directory the old version's cuts are in, with nothing in the files to say which cut which. That
    mixture is the thing the pin exists to prevent, so the pin is in the path."""
    assert cutter.pinned_version() in cutter.answers_dir().name
    other = tmp_path / "requirements-edgartools.txt"
    other.write_text("edgartools==7.1.2\n", encoding="utf-8")
    was = cutter.answers_dir()
    monkeypatch.setattr(cutter, "REQUIREMENTS", other)
    assert cutter.answers_dir() != was, "a different pin is a different directory"
    assert cutter.answer_path("0000-40").parent != was


def test_a_sidecar_that_cannot_be_written_fails_the_run(fake, tmp_path, monkeypatch):
    """The attempt count is the only thing between a bad document and an endless retry. A run that cannot write
    it and carries on has turned the cap off for every document in the pass, and would say so nowhere."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the answers directory should be", encoding="utf-8")
    monkeypatch.setattr(cutter, "failed_path", lambda accession: blocked / f"{accession}.failed.json")
    fake.append([cutter.DocumentFailed("the parser gave up on this filing")])
    with cutter.Cutter() as c:
        with pytest.raises(cutter.CutterUnusable, match="cannot write"):
            c.cut("0000-41", tmp_path / "a.htm", "10-K")


def test_the_form_reaches_the_library_and_a_request_without_one_is_refused(stub, tmp_path, monkeypatch):
    """A 20-F cut as a 10-K is wrong data nobody sees, and ``Document.sections`` routes on the form: it sends
    10-K, 10-Q, 8-K, 20-F and title-based forms to the hybrid detector and everything else to the pattern
    extractor. An earlier version defaulted a missing form to "10-K", which would have cut a 40-F as one."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        out = c.cut("0000-20", doc, "20-F")
    assert "form 20-F" in _answers(out)[0]["text"], "the form must reach ParserConfig, not be dropped"

    worker = cutter.Worker()
    try:
        with pytest.raises(cutter.DocumentFailed, match="names no form"):
            worker.cut(doc, "", tmp_path / "answers" / "x.jsonl.gz")
    finally:
        worker.close()


def test_a_non_ascii_filing_keeps_its_characters(stub, tmp_path, monkeypatch):
    """The HTML is read as UTF-8. Read as latin-1 it would still "work" and put mojibake in every filing that
    names a foreign subsidiary, which is most 20-F filers."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>Société Générale and Türk Telekom</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        out = c.cut("0000-21", doc, "20-F")
    assert "Société Générale and Türk Telekom" in _answers(out)[0]["text"]


def test_a_document_with_no_sections_is_not_cached_as_a_cut_document(stub, tmp_path, monkeypatch):
    """It used to be cached as cut, on the reasoning that the cutter had read it and found nothing. Round 4 of
    the review measured what produces this answer for a form the library *does* know: edgartools swallows an
    exception during detection and returns nothing (``hybrid_section_detector.py:657, 672, 695``;
    ``toc_section_detector.py:179``), so a document that failed to parse inside the library arrives as "ok, no
    sections" -- and cached, that is a filing stored for ever as having no sections at all."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "nothing.htm"
    doc.write_text("<html>nothing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-22", doc, "10-K") is None
        assert (c.empty, c.cut_now, c.no_vocabulary) == (1, 0, 0)
        assert "no section" in c.failed["0000-22"]
    assert not cutter.answer_path("0000-22").exists(), "nothing is cached, so a later run can try again"
    assert cutter.tries_so_far("0000-22") == 1


def test_a_known_form_cut_into_nothing_five_times_in_a_row_stops_the_run(stub, tmp_path, monkeypatch):
    """For a form the library *does* know, five in a row is detection failing inside it, or a field this worker
    reads having moved. Writing the form off one filing at a time would retire them all over three runs with no
    diagnosis left anywhere."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "nothing.htm"
    doc.write_text("<html>nothing</html>", encoding="utf-8")
    with cutter.Cutter(break_after=5) as c:
        for i in range(4):
            assert c.cut(f"0000-n{i}", doc, "10-K") is None
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut("0000-n4", doc, "10-K")
        assert c.empty == 5


def test_a_form_the_library_has_no_vocabulary_for_is_terminal_and_costs_nothing(stub, tmp_path, monkeypatch):
    """A **40-F** and a **10-KT** are cut into nothing by edgartools 6.0.0 every time, for every filing:
    ``document.py:938`` routes only 10-K, 10-Q, 8-K, 20-F and title-based forms to the detector,
    ``form_schema.py:1205`` gives the rest ``DEFAULT_SCHEMA`` with ``title_based`` false, and
    ``pattern_section_extractor.py:105`` holds patterns for eight forms. Both forms are in our corpus.

    Round 4 made a zero-section answer a charged failure, and round 5 measured what that does to these: on a
    warm pass the good documents are cached and clear nothing, so the first five of these stop the run, and N
    such filings cost 2N/5 stopped hourly runs before the cap retires them -- a hundred of them is forty stopped
    runs, with anything filed behind them uncut for two days. It is a fact about the form: reported, remembered
    so it is not parsed again every hour, charged nothing, and waiting for the cutter of its own that those two
    forms are getting.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "nothing.htm"
    doc.write_text("<html>nothing</html>", encoding="utf-8")
    good = tmp_path / "ok.htm"
    good.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter(break_after=5) as c:
        for i in range(8):
            assert c.cut(f"0000-f{i}", doc, "40-F") is None, "eight in a row, and the run is not stopped"
        assert c.no_vocabulary == 8
        assert c.cut("0000-24b", good, "10-K") is not None
    assert cutter.tries_so_far("0000-f0") == 0, "no attempt is spent on a form the library cannot read"

    assert cutter.read_sidecar("0000-f0")["no_vocabulary"] == 1, "written down, not measured again every hour"

    doc.unlink()                                 # and the next run answers from the note, without a parse
    with cutter.Cutter() as c:
        assert c.cut("0000-f0", doc, "40-F") is None
        assert c.no_vocabulary == 1
        assert "detects no section in a 40-F" in c.failed["0000-f0"], (
            "the note answered: a parse of a file that is no longer there would have been a read failure")
    assert cutter.tries_so_far("0000-f0") == 0


def test_a_zero_section_answer_the_library_complained_about_is_not_the_filing_s_fault(stub, tmp_path,
                                                                                      monkeypatch):
    """The last place a swallowed exception could still retire a filing.

    Detection in 6.0.0 swallows every exception and returns nothing in its place -- four sites, each an
    ``except Exception``, which a ``MemoryError`` is -- so a failure inside the library arrives as "ok, 0
    sections". For a form the library *does* know, round 4 charged that to the filing. On a box short of memory
    for N hours that retires about 5N/3 filings as "no sections in it", and the worker's own ``MemoryError``
    branch never sees any of it, because the library caught it first.

    Each of those sites logs a warning before it gives up, which is the one thing that tells the two apart. The
    worker counts them (``cutter_worker._Swallowed``) and a zero the library complained about is charged nothing.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    docs = {}
    for name in ("swallowed", "nothing", "benign"):
        docs[name] = tmp_path / f"{name}.htm"
        docs[name].write_text(f"<html>{name}</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-75", docs["swallowed"], "10-K") is None
        assert "swallowed" in c.failed["0000-75"] and "not this filing's" in c.failed["0000-75"]
        assert c.cut("0000-76", docs["nothing"], "10-K") is None
        assert c.cut("0000-81", docs["benign"], "10-K") is None
        heading = tmp_path / "heading.htm"
        heading.write_text("<html>heading</html>", encoding="utf-8")
        assert c.cut("0000-87", heading, "10-K") is None, (
            "and the fourth site counts even though the detector then logged its ordinary zero exit too")
    assert cutter.tries_so_far("0000-87") == 0 and cutter.stops_so_far("0000-87") == 1
    assert cutter.tries_so_far("0000-75") == 0, "the cause is unknown, so the filing is not charged for it"
    assert cutter.stops_so_far("0000-75") == 1, "it is deferred instead, and tried again after the back-off"

    # The two zeros that must stay the filing's own, and the reason a plain count of warnings cannot be the
    # signal: **both of these log one too.** The library's ordinary "no sections found" exit does
    # (hybrid_section_detector.py:128), and so do several harmless things about a filing's XBRL and its table of
    # contents. Counting every warning calls every sectionless filing in the corpus a swallowed failure, defers
    # it, and re-parses it every back-off for ever -- which is a retry for ever with the wrong reason in the
    # report, in place of the retirement after three attempts that a sectionless filing is owed.
    assert cutter.tries_so_far("0000-76") == 1, "a zero the library reached by looking is this filing's own"
    assert cutter.stops_so_far("0000-76") == 0
    assert cutter.tries_so_far("0000-81") == 1, "and a warning about something else is not about detection"
    assert cutter.stops_so_far("0000-81") == 0


@pytest.mark.parametrize("body", ["benign", "swallowed", "nothing"])
def test_a_form_with_no_vocabulary_is_terminal_whatever_the_library_logged_on_the_way(body, stub, tmp_path,
                                                                                     monkeypatch):
    """The order of the two questions, and the form is asked first.

    A 40-F gets no sections because the library has no patterns for the form at all
    (``pattern_section_extractor.py:105`` holds them for eight forms and ``:145`` answers ``{}`` for the rest),
    and that is true whatever else happened on the way: a warning about the filing's XBRL, a failure the library
    caught and carried on without, or nothing at all. Zero is the only answer this form was ever going to get,
    so it is terminal and free, which is the one outcome that costs the hourly pass nothing.

    Asking about the log first is what round 6 did, and it put every such filing that logged anything into the
    other branch: not charged, but three runs in the breaker's row and then a full parse of it every back-off
    for the rest of the corpus's life, for a form whose answer cannot change.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / f"{body}.htm"
    doc.write_text(f"<html>{body}</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-82", doc, "40-F") is None
        assert c.no_vocabulary == 1 and "detects no section in a 40-F" in c.failed["0000-82"]
    assert (cutter.tries_so_far("0000-82"), cutter.stops_so_far("0000-82")) == (0, 0)


def test_which_log_records_count_as_a_swallowed_failure():
    """The table is a copy of the library's ``except Exception`` sites on the detection path, so it is worth
    saying out loud what is on each side of it. The ordinary "no sections found" exit is a warning from
    ``detect_sections`` -- the same module as four of the sites -- which is why the function and not only the
    module decides, and why the name has to be the one the library really logs from: a stub that said ``detect``
    there would leave the whole of round 7's finding free to come back unseen.
    """
    sites = _worker_module().SWALLOW_SITES
    for module, func in [("hybrid_section_detector", "_try_pattern_detection"),
                         ("hybrid_section_detector", "_try_heading_detection"),
                         ("hybrid_section_detector", "_try_index_detection"),
                         ("hybrid_section_detector", "_extract_section_from_heading"),
                         ("toc_section_detector", "detect"),
                         ("index_section_detector", "detect")]:
        assert (module, func) in sites
    for module, func in [("hybrid_section_detector", "detect_sections"),   # ":128": looked, found nothing
                         ("hybrid_section_detector", "detect"),            # not a function the library has
                         ("xbrl_extraction", "extract"),               # nothing to do with detection
                         ("toc_analyzer", "analyze"),
                         ("parser", "parse")]:
        assert (module, func) not in sites


def test_a_stop_stamped_in_the_future_defers_nothing(tmp_path, monkeypatch):
    """Round 7's measurement. Reading a stamp later than now as "now" looks like the safe thing and is not:
    ``now + STOP_BACKOFF_S - now`` is the whole back-off, recomputed in every run, so a clock ten days fast held
    a document for ten days and six hours and a hand-edited ``10**30`` held one for good."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    for name, stamp in (("0000-83", int(time.time()) + 10 * 86400), ("0000-84", 10 ** 30)):
        cutter._note_failure(name, "the box", stops=cutter.MAX_STOPS, stopped_at=stamp)
        assert cutter.deferred_for(name) == 0, "it is probed in the next run, which stamps it from this clock"


def test_the_deferral_reads_the_cap_the_cutter_was_built_with(tmp_path, monkeypatch):
    """Two numbers for one rule is two rules. With the module's cap read here and the cutter's read by the
    breaker, a cutter built with a smaller cap deferred nothing while its probes went on for ever, and one built
    with a larger cap fed its breaker with documents the pass had already deferred."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter._note_failure("0000-85", "the box", stops=1, stopped_at=int(time.time()))
    assert cutter.deferred_for("0000-85", 1) > 0, "one stop is the cap this caller set"
    assert cutter.deferred_for("0000-85", 5) == 0
    assert cutter.deferred_for("0000-85") == 0, "and the module's cap is 3"


def test_a_cutter_built_with_its_own_cap_defers_at_that_cap(fake, tmp_path):
    """And the cap the cutter was built with is the one its own pass reads. With the module's read in the
    deferral, a cutter built with ``max_stops=1`` charged a stop, deferred nothing, and probed the same document
    in every run for ever -- while its breaker went on counting those probes as evidence about the box."""
    fake.append([cutter.CutterUnusable("the cutter could not write or read a.htm: OSError: [Errno 5] I/O "
                                       "error")])
    with cutter.Cutter(break_after=5, max_stops=1) as c:
        assert c.cut("0000-86", tmp_path / "a.htm", "10-K") is None
        assert c.skipped == 0, "the stop is charged on the way in, not read on the way in"
    assert cutter.stops_so_far("0000-86") == 1
    assert cutter.deferred_for("0000-86", 1) > 0
    FakeWorker.made = []
    fake.append(["ok"])
    with cutter.Cutter(break_after=5, max_stops=1) as c:
        assert c.cut("0000-86", tmp_path / "a.htm", "10-K") is None
        assert c.skipped == 1 and not FakeWorker.made


def test_a_note_that_says_no_vocabulary_cannot_retire_a_form_the_library_knows(fake, tmp_path):
    """``no_vocabulary`` is terminal, and terminal is the one thing a file anybody can edit must not be able to
    hand a 10-K. The form is asked again rather than taken from the note: that also means the 40-F notes written
    before the cutter those two forms are getting lands do not keep their filings terminal afterwards."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter._note_failure("0000-77", "hand-edited", no_vocabulary=1)
    fake.append(["ok"])
    with cutter.Cutter() as c:
        assert c.cut("0000-77", tmp_path / "a.htm", "10-K") is not None, "a 10-K is cut whatever the note says"
        assert c.no_vocabulary == 0


def test_the_form_the_worker_is_sent_is_upper_cased_like_the_table_that_called_it_known(fake, tmp_path):
    """``KNOWN_FORMS`` is read through ``_base_form``, which upper-cases; the library is not so forgiving
    (``document.py:928`` only strips ``/A``, ``form_schema.py:1237`` is an exact lookup). A ``"10-k"`` out of an
    index would be called known here and cut into nothing by every run -- charged, and five of them a stopped
    run."""
    fake.append(["ok"])
    with cutter.Cutter() as c:
        assert c.cut("0000-78", tmp_path / "a.htm", " 10-k ") is not None
    assert FakeWorker.made[-1].seen[-1][1] == "10-K"


def test_a_zero_section_answer_does_not_delete_an_answer_another_pass_wrote(fake, tmp_path):
    """The worker deletes a zero-section file instead of renaming it, so a file under the real name when the
    answer says zero was written by another pass over the same corpus -- one that got further than this one did.
    Handing it back beats deleting somebody else's good answer."""
    class _ZeroOverAnswer(FakeWorker):
        def cut(self, html, form, out):
            self.seen.append((str(html), form))
            out.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(out, "wt", encoding="utf-8") as fh:
                fh.write(json.dumps({"item": "Item 1", "text": "another pass cut this", "chars": 20}) + "\n")
            return 0, 0

    with cutter.Cutter() as c:
        c.worker = _ZeroOverAnswer()
        out = c.cut("0000-79", tmp_path / "a.htm", "10-K")
        assert out is not None and c.cut_now == 1 and c.empty == 0
    assert "another pass cut this" in _answers(out)[0]["text"]
    assert not cutter.failed_path("0000-79").exists()


def test_a_sidecar_nested_deeply_enough_to_overflow_the_json_parser_is_read_as_no_attempts(fake, tmp_path):
    """``json.loads`` raises ``RecursionError`` on deeply nested input, measured at ``'[' * 100000``, and a
    ``RecursionError`` is not a ``ValueError``. Left uncaught it reaches the caller bare, at the same document
    in every run -- which is the whole class of bug this module spent five rounds on."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter.failed_path("0000-80").write_text("[" * 100_000, encoding="utf-8")
    assert cutter.read_sidecar("0000-80")["tries"] == 0
    assert cutter.error_so_far("0000-80") == ""
    fake.append(["ok"])
    with cutter.Cutter() as c:
        assert c.cut("0000-80", tmp_path / "a.htm", "10-K") is not None


@pytest.mark.parametrize("form,known", [
    ("10-K", True), ("10-K/A", True), ("10-Q", True), ("20-F", True), ("8-K", True),
    ("424B5", True), ("S-1", True), ("DEF 14A", True), ("s-3", True),
    ("40-F", False), ("10-KT", False), ("6-K", False), ("", False), ("20-FR", False),
])
def test_which_forms_the_library_has_a_vocabulary_for(form, known):
    """The table is a copy of the library's routing, so it is worth saying out loud which side each form is on.
    An amendment and a ``424B`` variant carry their base form's vocabulary, and the case is the filer's."""
    assert (cutter._base_form(form) in cutter.KNOWN_FORMS) is known


def test_sections_with_no_text_at_all_are_not_cached_as_a_cut_document(stub, tmp_path, monkeypatch):
    """What a field whose name moved looks like from here, and the reason this check exists rather than a
    docstring saying "a document whose sections are all null is the signal": nothing read the signal, so the
    files were cached under the cutter version and the whole corpus read as cut and empty.

    One document's failure and not the run's, which is round 2 of the review's correction: in edgartools 6.0.0 a
    TOC section's text is ``""`` when its anchor is missing or its boundary pair is inverted
    (``toc_section_extractor.py:1885, 1927``; ``toc_section_detector.py:145``), so a single small filing can
    answer this on its own -- and as a failed *run* it would have stopped every hourly pass for ever. A moved
    field does it to every document, so five in a row still stop the run, with this text in the message.
    """
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "silent.htm"
    doc.write_text("<html>silent</html>", encoding="utf-8")
    good = tmp_path / "ok.htm"
    good.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-23", doc, "10-K") is None
        assert "no text at all" in c.failed["0000-23"]
        assert c.empty == 0, "it is not a document with no sections in it; it is a document we refuse"
        worker = c.worker
        assert c.cut("0000-24a", good, "10-K") is not None
        assert c.worker is worker and c.restarts == 0, (
            "the worker is kept, not replaced: it answered correctly and the answer is the filing's problem. "
            "Raised as a WorkerLost this would restart an interpreter a filing, and the breaker would give the "
            "attempts back -- which is the stall round 3 of the review found.")
    assert not cutter.answer_path("0000-23").exists(), "nothing a run cannot trust is cached"
    assert cutter.tries_so_far("0000-23") == 1


def test_a_moved_field_still_stops_the_run_through_the_breaker(stub, tmp_path, monkeypatch):
    """The diagnosis the empty-text check exists for has to survive its downgrade to a document failure: a field
    this worker reads under a name the library moved makes *every* document answer with sections and no text, so
    the fifth one stops the pass and the message says what to check."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "silent.htm"
    doc.write_text("<html>silent</html>", encoding="utf-8")
    with cutter.Cutter(break_after=5) as c:
        for i in range(4):
            assert c.cut(f"0000-v{i}", doc, "10-K") is None
        with pytest.raises(cutter.CutterUnusable, match="in a row") as caught:
            c.cut("0000-v4", doc, "10-K")
    assert "SECTION_FIELDS" in str(caught.value), "the breaker carries the last failure's own diagnosis"


def test_a_list_of_sections_is_read_too(stub, tmp_path, monkeypatch):
    """6.0.0 returns a ``Dict[str, Section]``. A later version returning a list must not read as zero sections,
    which would cache every filing as cut and empty."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "aslist.htm"
    doc.write_text("<html>aslist</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        out = c.cut("0000-24", doc, "10-K")
    rows = _answers(out)
    assert len(rows) == 1 and rows[0]["key"] is None and rows[0]["item"] == "1"


def test_a_library_that_asks_a_question_does_not_eat_a_request(stub, tmp_path, monkeypatch):
    """There is no person on this stdin. If the protocol's own stdin were left in place, a library's ``input()``
    would swallow the next document's request and that document would hang to the timeout -- the WRDS worker has
    the same test, because its library really does prompt."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setattr(cutter, "TIMEOUT_S", 10.0)
    asks = tmp_path / "prompt.htm"
    asks.write_text("<html>prompt</html>", encoding="utf-8")
    good = tmp_path / "ok.htm"
    good.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-27", good, "10-K") is not None
        worker = c.worker
        assert c.cut("0000-25", asks, "10-K") is None, "an EOF on stdin, not a swallowed request"
        # The worker it answered with, still: an ``EOFError`` is this document's own failure. This is the half
        # the first version of this test left out -- it asserted only that the document failed and that the next
        # one cut, which a *swallowed* request also satisfies, by hanging to the timeout and being replaced. The
        # mutant that drops the ``/dev/null`` on fd 0 survived the battery for exactly that reason.
        assert c.worker is worker and c.restarts == 0, "stdin at EOF, not the request pipe read dry"
        assert c.cut("0000-26", good, "10-K") is not None


def test_the_worker_runs_with_P_so_its_own_folder_is_not_on_sys_path(stub, monkeypatch):
    """``-P`` keeps the script's directory off ``sys.path``. Without it, a file dropped beside the worker in the
    repository -- or anything named like a stdlib module -- is importable by 35 third-party packages."""
    seen = {}

    class Spy(cutter.Worker):
        pass

    import subprocess as sp
    real = sp.Popen

    def watch(args, **kw):
        seen["args"] = list(args)
        return real(args, **kw)

    monkeypatch.setattr(sp, "Popen", watch)
    worker = cutter.Worker()
    worker.close()
    assert "-P" in seen["args"] and seen["args"][-1].endswith("cutter_worker.py")


def test_the_worker_is_killed_and_reaped_rather_than_left_as_a_zombie(stub, tmp_path, monkeypatch):
    """``kill`` without ``wait`` leaves a zombie per lost worker, and a pass can lose a few dozen."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    monkeypatch.setattr(cutter, "TIMEOUT_S", 2.0)
    hung = tmp_path / "hang.htm"
    hung.write_text("<html>hang</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        first = c._worker()
        assert c.cut("0000-27", hung, "10-K") is None
    assert first.proc.poll() is not None, "the hung worker was killed"
    assert first.proc.returncode is not None, "and reaped, so it is not a zombie"


def test_the_last_worker_is_closed_when_the_cutter_is(stub, tmp_path, monkeypatch):
    """The recycle test only looks at the workers that were replaced, so nothing there would notice a ``close``
    that does nothing -- and the last worker of a 5,149-document pass is the one that outlives it."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "a.htm"
    doc.write_text("<html>a filing</html>", encoding="utf-8")
    c = cutter.Cutter()
    assert c.cut("0000-28", doc, "10-K") is not None
    last = c.worker
    c.close()
    assert c.worker is None and last.proc.poll() is not None, "the last worker is not left running"


def _worker_module():
    """``cutter_worker`` imported in this process, for the parts that are pure functions.

    The subprocess tests cannot reach an ``errno`` that needs a flaky mount or a full disk, and the suite runs as
    root in CI, where ``os.access`` says yes to a mode-000 directory. So the table that decides whose fault an
    unreadable file is gets a direct test. The module imports nothing of ours and nothing of edgartools at import
    time, which is what makes this safe -- and :func:`test_the_worker_imports_nothing_of_ours` is what keeps it so.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("rc_cutter_worker_under_test", cutter.WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("number,name,theirs", [
    (errno.ENOENT, "it is not there", True),
    (errno.EISDIR, "it is a directory", True),
    (errno.ENOTDIR, "something in the path is a file", True),
    (errno.ENAMETOOLONG, "the name is impossible", True),
    (errno.ELOOP, "the symlinks go round", True),
    (errno.EPERM, "not on the list at all", False),
    (errno.ENOMEM, "the box has no memory", False),
    (errno.EACCES, "a restore with the wrong owner", False),
    (errno.EIO, "a flaky mount", False),
    (errno.ESTALE, "an NFS handle that went away", False),
    (errno.EMFILE, "no file handles left", False),
    (errno.ENOSPC, "a full disk", False),
])
def test_only_a_fault_about_the_file_itself_is_the_document_s_own(tmp_path, monkeypatch, number, name, theirs):
    """Whose fault an unreadable document is, by ``errno`` and nothing else.

    It used to be decided by asking whether the file's own directory was readable, which looked right and was
    not: the corpus is one directory per CIK (``core/edgar_docs.py:31``), so a company whose folder had not been
    downloaded read as "the corpus is not mounted" and stopped every hourly run at that document for ever. And in
    the other direction a permission or a flaky mount, which is never one filing's doing, was charged to it --
    five documents retired every three hours for as long as the fault lasted, recoverable only by hand.
    """
    worker = _worker_module()
    doc = tmp_path / "a.htm.gz"
    doc.write_bytes(b"")

    def refuse(*a, **kw):
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(worker, "gzip", types.SimpleNamespace(open=refuse, BadGzipFile=gzip.BadGzipFile))
    try:
        worker._read(str(doc))
    except Exception as raised:  # noqa: BLE001 - which class it is *is* the assertion
        caught = raised
    else:
        raise AssertionError("it returned text for a file it could not open")
    assert isinstance(caught, worker.BadDocument) is theirs, (
        f"errno {number} ({name}): raised {type(caught).__name__}, so 'the file's own problem' is "
        f"{not theirs} and should be {theirs}")


@pytest.mark.parametrize("body,what", [
    (b"", "a zero-byte file"),
    (b"   \n", "whitespace and nothing else"),
])
def test_an_empty_document_is_refused_rather_than_cut_into_nothing(stub, tmp_path, monkeypatch, body, what):
    """edgartools reads an empty file without complaining (``parser.py:123-131`` builds an empty document and
    ``pattern_section_extractor.py:145`` answers ``{}``), so the answer would be "cut, no sections" -- and a
    file that is empty because its download was cut off would be stored as a filing with nothing in it."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    doc = tmp_path / "empty.htm"
    doc.write_bytes(body)
    with cutter.Cutter() as c:
        assert c.cut("0000-52", doc, "10-K") is None
        assert "empty" in c.failed["0000-52"]
    assert cutter.tries_so_far("0000-52") == 1


def test_a_gzip_document_that_stops_in_the_middle_is_that_document_s_own_failure(stub, tmp_path, monkeypatch):
    """A download cut off part way. It raises ``EOFError``, which is **not** an ``OSError``, so the errno table
    never sees it -- and the docstring that called ``BadGzipFile`` "a half-finished download" was describing this
    case, which was the one it did not handle."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    whole = tmp_path / "whole.htm.gz"
    with gzip.open(whole, "wt", encoding="utf-8") as fh:
        fh.write("<html>a filing</html>" * 200)
    cut_off = tmp_path / "cut-off.htm.gz"
    cut_off.write_bytes(whole.read_bytes()[:40])
    with cutter.Cutter() as c:
        assert c.cut("0000-53", cut_off, "10-K") is None
        assert "EOFError" in c.failed["0000-53"]
    assert cutter.tries_so_far("0000-53") == 1

    # And it is a failure to *read* the file, not a parse: the two both charge an attempt, so the only thing that
    # tells them apart is the question the read phase asks -- is the corpus still there? Left out of the except
    # tuple, an ``EOFError`` falls through to the parse branch and this document would be charged while nothing
    # in the corpus could be read at all.
    with cutter.Cutter(corpus_root=tmp_path / "mountpoint") as c:
        (tmp_path / "mountpoint").mkdir()
        assert c.cut("0000-53b", cut_off, "10-K") is None
        assert "not mounted where it should be" in c.failed["0000-53b"]
    assert cutter.tries_so_far("0000-53b") == 0


def test_a_document_three_runs_failed_on_is_skipped_so_the_pass_can_go_on(fake, tmp_path):
    """The counter the first four review rounds kept rediscovering the need for.

    A failure called the box's fault charges the document no attempt and was remembered nowhere, so *any*
    per-document cause of one stopped every hourly run at the same document for ever. Four rounds found four such
    causes and each fix moved the boundary between "the filing's fault" and "the box's"; the hole was the missing
    memory. Now the run carries on and the count is kept, and after three runs the document is passed over.
    """
    for run in range(3):
        fake.append([cutter.CutterUnusable("the cutter could not write or read a.htm: OSError: [Errno 5] I/O "
                                           "error")])
        with cutter.Cutter() as c:
            assert c.cut("0000-54", tmp_path / "a.htm", "10-K") is None
            assert "I/O error" in c.failed["0000-54"]
        assert cutter.stops_so_far("0000-54") == run + 1
        assert cutter.tries_so_far("0000-54") == 0, "no attempt was ever spent on it"

    fake.append(["ok"])
    FakeWorker.made = []
    with cutter.Cutter() as c:
        assert c.cut("0000-54", tmp_path / "a.htm", "10-K") is None
        assert c.skipped == 1 and "passed over" in c.failed["0000-54"]
        assert not FakeWorker.made, "a document inside its back-off is not sent to the worker at all"
        assert c.cut("0000-55", tmp_path / "b.htm", "10-K") is not None, "and the pass carries on"


def test_an_outage_defers_the_documents_it_touched_and_loses_none_of_them(fake, tmp_path, monkeypatch):
    """The whole of the box-fault rule, over six hourly runs of an outage and the two runs after it.

    Round 5 gave every stop back when the breaker fired, so an outage left the corpus exactly as it found it.
    Round 6 measured the two holes in that, and both are the kind that lose documents for good:

    - **the breaker does not fire in the steady state.** Almost everything is cached, so a run has the hour's
      new filings and no more -- 0 to 4 documents. Three outage hours put three stops on each of two new
      filings, the fifth failure never comes, nothing gives them back, and they were skipped for ever.
    - **giving them back stalls on a lasting fault.** Five documents that fail every run -- one CIK folder
      restored with the wrong owner, and ``EACCES`` is the box's by the errno table -- trip the breaker, get
      their stops back, and do it again for ever, with everything behind them in the pass never cut.

    So a stop now defers and never retires: after ``max_stops`` runs the document is passed over for
    :data:`cutter.STOP_BACKOFF_S`, and then tried again.
    """
    names = [f"0000-o{i}" for i in range(2)]     # two new filings, which is what an hourly run really has
    for run in range(3):                         # three hourly runs with the corpus not mounted
        fake.append([cutter.DocumentUnreadable("this document cannot be read: FileNotFoundError")] * 2)
        with cutter.Cutter(break_after=5, corpus_root=tmp_path / "not-mounted") as c:
            for name in names:
                assert c.cut(name, tmp_path / "not-mounted" / f"{name}.htm", "10-K") is None
            assert c.skipped == 0, "the pass is not holding them yet"
        assert [cutter.stops_so_far(n) for n in names] == [run + 1] * 2
        assert [cutter.tries_so_far(n) for n in names] == [0, 0], "and not one attempt among them"

    fake.append(["ok"] * 2)                      # the mount is back, but the back-off is not over
    FakeWorker.made = []
    with cutter.Cutter() as c:
        for name in names:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is None
        assert c.skipped == 2 and not FakeWorker.made

    later = time.time() + cutter.STOP_BACKOFF_S + 60
    monkeypatch.setattr(cutter.time, "time", lambda: later)
    fake.append(["ok"] * 2)
    with cutter.Cutter() as c:                   # and now they are tried again, and cut
        for name in names:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is not None
        assert (c.cut_now, c.skipped) == (2, 0)
    assert not any(cutter.failed_path(n).exists() for n in names), "and the notes are gone with the failures"


def test_five_documents_with_a_lasting_box_fault_do_not_stop_every_run_for_ever(fake, tmp_path):
    """The second hole, which is what giving a stop back costs. One CIK folder restored with the wrong owner:
    ``EACCES`` is not in the worker's ``THEIR_FAULT`` table, so every one of that company's filings answers
    ``io`` in every run, and a company has five or more annual filings and proxies.

    With the stops given back, runs 1..N were identical: five faults, the breaker fires, the stops go back, and
    nothing behind the fifth document in the pass is ever cut. Now the stops stay, so after three runs the five
    are inside their back-off and the pass walks straight past them.
    """
    bad = [f"0000-p{i}" for i in range(5)]
    good = "0000-p9"
    for run in range(3):
        fake.append([cutter.CutterUnusable("the cutter could not write or read a.htm: PermissionError: "
                                           "[Errno 13] Permission denied")] * 5)
        with cutter.Cutter(break_after=5) as c:
            for name in bad[:4]:
                assert c.cut(name, tmp_path / "a.htm", "10-K") is None
            with pytest.raises(cutter.CutterUnusable, match="in a row"):
                c.cut(bad[4], tmp_path / "a.htm", "10-K")
        assert [cutter.stops_so_far(n) for n in bad] == [run + 1] * 5, f"run {run + 1} gave a stop back"

    fake.append(["ok"])                          # the fourth run: all five are deferred, and the pass goes on
    with cutter.Cutter(break_after=5) as c:
        for name in bad:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is None
        assert c.skipped == 5
        assert c.cut("0000-p9", tmp_path / "b.htm", "10-K") is not None, "what is behind them is cut at last"
        assert good not in c.failed


def test_a_probe_after_the_back_off_does_not_feed_the_breaker(fake, tmp_path, monkeypatch):
    """What makes the deferral hold. Five documents past the cap are probed in the same run; if those probes
    counted as evidence they would reach ``break_after`` on their own and stop the run -- which is the stall of
    the test above, once every back-off instead of every hour.
    """
    bad = [f"0000-q{i}" for i in range(5)]
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    long_ago = int(time.time()) - cutter.STOP_BACKOFF_S - 60
    for name in bad:
        cutter._note_failure(name, "three runs failed on it for the box's reasons",
                             stops=cutter.MAX_STOPS, stopped_at=long_ago)
        assert cutter.deferred_for(name) == 0, "the back-off is over, so it is probed"
    fake.append([cutter.CutterUnusable("the cutter could not write or read a.htm: OSError: [Errno 5] I/O "
                                       "error")] * 5)
    with cutter.Cutter(break_after=5) as c:
        for name in bad:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is None, "no probe raises"
    assert [cutter.stops_so_far(n) for n in bad] == [cutter.MAX_STOPS + 1] * 5
    assert [cutter.deferred_for(n) > 0 for n in bad] == [True] * 5, "each one is deferred again"


def test_a_stamp_from_a_clock_that_went_backwards_does_not_defer_a_document_for_ever(tmp_path, monkeypatch):
    """An NTP step or a container with a wrong clock writes a stamp in the future. Read as it comes, it holds the
    document for as long as the clock is wrong; read as "now", the document is tried in the next run."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter._note_failure("0000-73", "the box", stops=cutter.MAX_STOPS, stopped_at=int(time.time()) + 10 * 86400)
    assert cutter.deferred_for("0000-73") <= cutter.STOP_BACKOFF_S
    cutter._note_failure("0000-74", "the box", stops=cutter.MAX_STOPS - 1, stopped_at=int(time.time()))
    assert cutter.deferred_for("0000-74") == 0, "below the cap nothing is deferred at all"


def test_a_box_fault_among_documents_that_cut_is_the_document_s_own(fake, tmp_path):
    """The other side of the same rule, and the reason the stop is kept at all: a filing whose own file is
    unreadable while everything around it cuts has nothing to blame but itself. One bad file never stops a run
    now -- it is the five in a row that do."""
    fake.append(["ok", cutter.CutterUnusable("the cutter could not write or read b.htm: OSError: [Errno 13] "
                                             "Permission denied"), "ok"])
    with cutter.Cutter(break_after=5) as c:
        assert c.cut("0000-64", tmp_path / "a.htm", "10-K") is not None
        assert c.cut("0000-65", tmp_path / "b.htm", "10-K") is None
        assert c.cut("0000-66", tmp_path / "c.htm", "10-K") is not None
        assert c.cut_now == 2
    assert cutter.stops_so_far("0000-65") == 1, "the documents around it cut, so the stop is its own"


def test_the_run_the_breaker_stopped_gives_back_the_attempts_and_keeps_the_stops(fake, tmp_path):
    """Two lost workers, then three documents whose files cannot be read while the corpus has gone. The breaker
    stops the run on the fifth.

    The **attempts** go back, once each: an attempt is a step towards retiring the document for good, and the
    breaker has just said the run was the box's fault. The **stops** stay, because a stop retires nothing -- it
    defers the document for a few hours -- and giving them back is what let five lasting faults stop every run
    for ever.
    """
    for _ in range(2):
        fake.append([cutter.WorkerLost("the cutter worker did not answer in 180s")])  # each one is replaced
    fake.append([cutter.DocumentUnreadable("this document cannot be read: FileNotFoundError")] * 3)
    lost, boxed = ["0000-56", "0000-57"], ["0000-67", "0000-68", "0000-69"]
    with cutter.Cutter(break_after=5, corpus_root=tmp_path / "not-mounted") as c:
        for name in lost + boxed[:2]:
            assert c.cut(name, tmp_path / "a.htm", "10-K") is None
        assert [cutter.tries_so_far(n) for n in lost] == [1, 1]
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut(boxed[2], tmp_path / "a.htm", "10-K")
    assert [cutter.tries_so_far(n) for n in lost] == [0, 0], "the attempts went back"
    assert [cutter.stops_so_far(n) for n in boxed] == [1, 1, 1], "the stops stayed, and defer nothing yet"
    assert [cutter.deferred_for(n) for n in boxed] == [0, 0, 0]
    assert "did not answer" in cutter.error_so_far(lost[0]), "and the note still says what went wrong"


def test_an_attempt_goes_back_once_and_a_stop_is_not_given_back_at_all(fake, tmp_path):
    """The two charges are not owed the same thing, and neither is owed what the other is.

    An attempt goes back **once** per document, remembered in ``gave_back``: without that memory the give-back
    never ended, because the document hung the worker again in the next run with a clean slate. A stop goes back
    **never**: it is not a step towards anything terminal, and three of them buy the box a few hours of the
    pass's attention rather than the document's life.
    """
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter._note_failure("0000-70", "lost a worker in an earlier run, and the breaker gave the attempt back",
                         gave_back=1)
    assert cutter.gave_back_before("0000-70") == 1
    fake.append([cutter.WorkerLost("the cutter worker did not answer in 180s")])
    fake.append([cutter.DocumentUnreadable("this document cannot be read: FileNotFoundError")])
    with cutter.Cutter(break_after=2, corpus_root=tmp_path / "not-mounted") as c:
        assert c.cut("0000-70", tmp_path / "a.htm", "10-K") is None
        assert cutter.tries_so_far("0000-70") == 1
        with pytest.raises(cutter.CutterUnusable, match="in a row"):
            c.cut("0000-71", tmp_path / "b.htm", "10-K")
    assert cutter.tries_so_far("0000-70") == 1, "its one give-back was spent in an earlier run"
    assert cutter.stops_so_far("0000-71") == 1, "and the stop the breaker's own run charged is kept"


def test_a_handshake_that_fails_mid_pass_gives_the_breaker_s_attempts_back(fake, tmp_path, monkeypatch):
    """The same rule for the other way a run ends: the worker could not be replaced. A venv upgraded underneath
    an hourly pass does this, and the documents already charged had nothing to do with it."""
    fake.append([cutter.WorkerLost("the cutter worker did not answer in 180s")])
    with cutter.Cutter(break_after=5) as c:
        assert c.cut("0000-59", tmp_path / "a.htm", "10-K") is None
        assert cutter.tries_so_far("0000-59") == 1
        monkeypatch.setattr(cutter, "_factory", lambda: (_ for _ in ()).throw(
            cutter.CutterUnusable("the cutter venv has edgartools 6.1.0, not the pinned 6.0.0")))
        with pytest.raises(cutter.CutterUnusable, match="not the pinned"):
            c.cut("0000-60", tmp_path / "b.htm", "10-K")
    assert cutter.tries_so_far("0000-59") == 0


@pytest.mark.parametrize("body,what", [
    ("null", "valid JSON that is not an object"),
    ("[]", "a list"),
    ('{"tries": "two"}', "a count that is not a number"),
    ('{"tries": -4}', "a negative count"),
    ('{"tries": NaN}', "a count that is not finite"),
    ('{"tries": 1e999}', "a count too big to be one"),
    ("{not json at all", "a truncated write"),
])
def test_a_sidecar_nobody_could_use_is_read_as_no_attempts_rather_than_crashing(fake, tmp_path, body, what):
    """It is a file on disk that anybody can edit, and a run that crashes on it crashes at the same document in
    every run. ``null`` used to reach the caller as an ``AttributeError`` out of ``Cutter.cut``, which promises
    nothing but ``DocumentFailed`` and ``CutterUnusable``."""
    cutter.answers_dir().mkdir(parents=True, exist_ok=True)
    cutter.failed_path("0000-61").write_text(body, encoding="utf-8")
    assert cutter.read_sidecar("0000-61") == {name: 0 for name in cutter.COUNTERS}
    fake.append([cutter.DocumentFailed("the parser gave up on this filing")])
    with cutter.Cutter() as c:
        assert c.cut("0000-61", tmp_path / "a.htm", "10-K") is None
    assert cutter.tries_so_far("0000-61") == 1, "it is charged from zero, not retired by an unreadable file"


def test_a_corpus_root_that_is_a_file_or_empty_is_gone(tmp_path):
    """``is_dir`` and not ``exists``, and empty counts as gone: an unmounted mountpoint is an empty directory,
    and ``core/config.py:76`` recreates ``EDGAR_DOCS_DIR`` at import -- so a builder started while the disk is
    not mounted makes the root exist before the cutter ever looks at it."""
    a_file = tmp_path / "root-is-a-file"
    a_file.write_text("not a corpus", encoding="utf-8")
    empty = tmp_path / "mountpoint"
    empty.mkdir()
    full = tmp_path / "corpus"
    (full / "1234").mkdir(parents=True)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    assert cutter.Cutter(corpus_root=a_file)._corpus_gone()
    assert cutter.Cutter(corpus_root=empty)._corpus_gone(), "an empty mountpoint is not a corpus"
    assert cutter.Cutter(corpus_root=dangling)._corpus_gone()
    assert cutter.Cutter(corpus_root=tmp_path / "never-made")._corpus_gone()
    assert not cutter.Cutter(corpus_root=full)._corpus_gone()
    assert not cutter.Cutter()._corpus_gone(), "no root named is not a claim that it has gone"


def test_a_parse_failure_is_charged_even_when_the_corpus_has_gone(fake, tmp_path):
    """The corpus check belongs to the one failure it is about. A filing the parser refused is the filing's own
    doing whatever the state of the mount, and treating every failure as the mount's would charge nothing, stop
    the run, and leave the parse failure to be met again in the next run for ever."""
    fake.append([cutter.DocumentFailed("the parser gave up on this filing")])
    with cutter.Cutter(corpus_root=tmp_path / "not-mounted") as c:
        assert c.cut("0000-62", tmp_path / "a.htm", "10-K") is None
    assert cutter.tries_so_far("0000-62") == 1 and cutter.stops_so_far("0000-62") == 0


def test_a_failure_note_that_cannot_be_removed_does_not_break_the_cut(fake, tmp_path, monkeypatch):
    """Forgetting a note is housekeeping: the note only ever makes the cutter try one time fewer. Raising from
    there would break what ``Cutter.cut`` promises its caller, for a file that the next attempt replaces."""
    in_the_way = tmp_path / "a-directory.failed.json"
    in_the_way.mkdir()
    monkeypatch.setattr(cutter, "failed_path", lambda accession: in_the_way)
    with cutter.Cutter() as c:
        assert c.cut("0000-63", tmp_path / "a.htm", "10-K") is not None, "the document is cut; the note is noise"


def test_a_document_the_box_had_no_memory_for_is_not_charged_to_the_filing(stub, tmp_path, monkeypatch):
    """``MemoryError`` is an ``Exception`` and not an ``OSError``, so it landed in the parse branch and was
    charged -- on the 15 GB box the dashboard shares, where the next run would most likely cut the same filing
    without complaint."""
    monkeypatch.setattr(cutter, "answers_dir", lambda: tmp_path / "answers")
    starved = tmp_path / "starved.htm"
    starved.write_text("<html>starved</html>", encoding="utf-8")
    good = tmp_path / "ok.htm"
    good.write_text("<html>a filing</html>", encoding="utf-8")
    with cutter.Cutter() as c:
        assert c.cut("0000-51", starved, "10-K") is None
        assert "MemoryError" in c.failed["0000-51"]
        assert c.cut("0000-51b", good, "10-K") is not None, "the run carries on: the next document is the proof"
    assert cutter.tries_so_far("0000-51") == 0
    assert cutter.stops_so_far("0000-51") == 1


def test_the_worker_imports_nothing_of_ours(stub):
    """It runs under another interpreter, where the RevenueChaser package is not installed. An import of ours
    would turn a cut into an ImportError on the server and a passing test here."""
    import ast
    from pathlib import Path

    source = Path(cutter.WORKER).read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import would reach our package"
            names.add((node.module or "").split(".")[0])
    assert names <= {"errno", "gzip", "json", "logging", "os", "socket", "sys", "edgar",
                     "__future__"}, sorted(names)

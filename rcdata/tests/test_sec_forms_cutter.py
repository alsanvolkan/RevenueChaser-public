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

import gzip
import json
import os
import subprocess
import sys

import pytest

from RevenueChaser.rcdata.build.sec_forms import cutter

# ── the stub edgar package the real worker runs against ───────────────────────────────────────────────────────

STUB_INIT = '''
__version__ = "6.0.0"
'''

STUB_DOCUMENTS = '''
import json, os, socket

# Not an ``RC_*`` name: the whitelist drops those on purpose, and a test that smuggled one in would be
# testing a hole it had just made. ``TMPDIR`` is whitelisted because a worker needs somewhere to write.
LOG = os.path.join(os.environ["TMPDIR"], "stub.jsonl")


def _log(what):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(what) + "\\n")


# At import time, like a real library building a client: what the network looks like from in here, and whether a
# provider key or EDGAR_IDENTITY survived into this process. Every primitive the worker replaces is tried, not
# just one: a client that resolves a name first and a client that calls connect_ex both have to be stopped, and a
# test that asks about one of them lets the other three be deleted.
# Every call carries a short timeout, and the address is one the suite's own network guard refuses anyway.
# Without a timeout a probe here would wait on SYN retries or on DNS, which would make this test's cost depend
# on the machine it runs on -- and in the worker it never waits at all, because the call is gone.
def _socket():
    s = socket.socket()
    s.settimeout(0.5)
    return s


_tried = {}
for _what, _call in (("connect", lambda: _socket().connect(("127.0.0.1", 9))),
                     ("connect_ex", lambda: _socket().connect_ex(("127.0.0.1", 9))),
                     ("create_connection", lambda: socket.create_connection(("127.0.0.1", 9), 0.5)),
                     ("getaddrinfo", lambda: socket.getaddrinfo("localhost", 9))):
    try:
        _call()
        _tried[_what] = "open"
    except OSError as e:
        _tried[_what] = type(e).__name__
_log(_tried)
_log({"identity": os.environ.get("EDGAR_IDENTITY"), "figi": "OPENFIGI_API_KEY" in os.environ,
      "mode": os.environ.get("RC_SEC_FORMS_CUTTER")})


class _Section:
    def __init__(self, item, title, method, confidence, warnings, body):
        self.item, self.title = item, title
        self.detection_method, self.confidence, self.warnings = method, confidence, warnings
        self._body = body

    def text(self):
        return self._body


class _Broken:
    item = title = detection_method = None
    confidence, warnings = None, []

    def text(self):
        raise RuntimeError("this section's text cannot be read")


class HybridSectionDetector:
    def __init__(self, doc, form):
        self.doc, self.form = doc, form

    def detect_sections(self):
        # rich prints, and it flushes: a print left in Python's block buffer would never reach the pipe at all,
        # so an unflushed one here would make this stub agree with a worker that isolates nothing.
        print("[bold]detecting[/bold]", flush=True)
        if "boom" in self.doc:
            raise ValueError("the parser gave up on this filing")
        if "halfway" in self.doc:
            # One good section and then one that raises when its text is read: the only shape that gets a .tmp
            # file opened and then fails. A document that fails before the first section never opens one, so it
            # cannot tell a worker that cleans up from one that does not.
            return {"Item 1": _Section("Item 1", "Business", "toc", 0.9, [], "Fine."),
                    "Item 1A": _Broken()}
        if "hang" in self.doc:
            import time
            time.sleep(60)
        if "crash" in self.doc:
            os._exit(9)
        return {"Item 1": _Section("Item 1", "Business", "toc", 0.97, [], "We make widgets. " + self.doc),
                "Item 1A": [_Section("Item 1A", "Risk Factors", "pattern", 0.6, ["no stop found"], "Risky.")]}


def parse_html(html):
    return html
'''


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """A stub ``edgar`` on ``PYTHONPATH``, with the worker pointed at this interpreter."""
    pkg = tmp_path / "stub" / "edgar"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(STUB_INIT, encoding="utf-8")
    (pkg / "documents.py").write_text(STUB_DOCUMENTS, encoding="utf-8")
    log = tmp_path / "stub.jsonl"
    # Prepended and not set: the root ``conftest.py`` puts ``module_testing/netguard`` on ``PYTHONPATH`` so a
    # Python child the suite starts gets the same offline guard, and replacing the variable would take that guard
    # off this subprocess -- the one place in the suite that runs a third-party library's import path.
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
    handshake comparing against a stale literal would refuse the venv that is actually right."""
    assert cutter.REQUIREMENTS.exists(), "the cutter's requirements file is the pin's home"
    assert cutter.pinned_version() == "6.0.0"
    text = cutter.REQUIREMENTS.read_text(encoding="utf-8")
    assert "edgartools==" in text


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

def test_no_provider_key_and_no_edgar_identity_reach_the_worker_s_environment(monkeypatch):
    """A whitelist and not a copy: about 35 third-party packages run in there."""
    monkeypatch.setenv("OPENFIGI_API_KEY", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("EDGAR_IDENTITY", "x")
    monkeypatch.setenv("RC_SEC_USER_AGENT", "x")
    monkeypatch.setenv(cutter.MODE_ENV, "edgartools")
    env = cutter.worker_env()
    assert not [k for k in env if "KEY" in k or "TOKEN" in k or "SECRET" in k]
    assert "EDGAR_IDENTITY" not in env, "nothing may identify us to the SEC from inside the cutter"
    assert not [k for k in env if k.startswith("RC_")], "the worker reads no RevenueChaser config, not even the mode"
    assert env.get("PYTHONNOUSERSITE") == "1"


# ── the client's machinery, against a fake worker ─────────────────────────────────────────────────────────────

class FakeWorker:
    """What :data:`cutter._factory` makes in these tests. ``plan`` is read one document at a time: ``"ok"``,
    ``"fail"``, or an exception to raise."""

    made: list["FakeWorker"] = []

    def __init__(self, plan=None):
        self.plan = list(plan or [])
        self.done = 0
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
        return 1

    def close(self):
        self.closed = True

    def kill(self):
        self.killed = True


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


def test_a_failed_document_kills_the_worker_and_the_next_one_gets_a_new_one(fake, tmp_path):
    """WRDS never restarts (``spine/wrds.py:153-156``), which is right for one attended call and wrong for
    5,149: one filing that kills the parser must not end the pass for the 5,000 after it."""
    fake.extend([[cutter.DocumentFailed("died")], ["ok"]])
    with cutter.Cutter() as c:
        assert c.cut("0000-5", tmp_path / "a.htm", "10-K") is None
        assert c.cut("0000-6", tmp_path / "b.htm", "10-K") is not None
        assert c.restarts == 1
    assert len(FakeWorker.made) == 2
    assert FakeWorker.made[1].seen == [(str(tmp_path / "b.htm"), "10-K")]


def test_the_worker_is_replaced_every_recycle_n_documents(fake, tmp_path):
    """~35 packages parsing 2 MB of HTML each are not trusted to give memory back, and the builder shares a 15 GB
    box with the dashboard."""
    with cutter.Cutter(recycle_n=2) as c:
        for i in range(5):
            assert c.cut(f"0000-r{i}", tmp_path / "a.htm", "10-K") is not None
    assert len(FakeWorker.made) == 3, "2 + 2 + 1"
    assert all(w.closed for w in FakeWorker.made[:-1]), "a recycled worker is closed, not leaked"


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

def test_the_worker_reports_its_version_before_anything_is_asked_of_it(stub):
    """The handshake is unprompted, so a venv that cannot import edgartools is known before a document is sent
    and no document is charged for it."""
    worker = cutter.Worker()
    try:
        assert worker.done == 0
    finally:
        worker.close()


def test_a_venv_with_the_wrong_version_fails_the_run(stub, monkeypatch, tmp_path):
    """A venv upgraded underneath us would otherwise write rows labelled 6.0.0 that another version cut, and
    nothing downstream could tell them apart."""
    (tmp_path / "stub" / "edgar" / "__init__.py").write_text('__version__ = "6.1.0"\n', encoding="utf-8")
    with pytest.raises(cutter.CutterUnusable, match="6.1.0"):
        cutter.Worker()


def test_a_venv_without_edgartools_fails_the_run_and_says_the_import_failed(stub, tmp_path):
    (tmp_path / "stub" / "edgar" / "__init__.py").write_text("raise ImportError('no pandas')\n", encoding="utf-8")
    with pytest.raises(cutter.CutterUnusable, match="edgartools"):
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
    assert [r["item"] for r in rows] == ["Item 1", "Item 1A"]
    assert rows[0]["detection_method"] == "toc" and rows[0]["confidence"] == 0.97
    assert rows[1]["warnings"] == ["no stop found"] and rows[1]["title"] == "Risk Factors"
    assert rows[0]["chars"] == len(rows[0]["text"]) and "a filing" in rows[0]["text"]
    assert not set(rows[0]) & {"quality", "rules_version", "flag"}, "our mapping is cut.py's, not the worker's"


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
    assert set(said[0]) == {"connect", "connect_ex", "create_connection", "getaddrinfo"}
    assert set(said[0].values()) == {"NetworkBlocked"}, (
        f"{said[0]}: every primitive must raise what the worker installed. 'NetworkAccessAttempted' means the "
        "suite's own guard answered first and the worker's own block is untested; 'open' means neither did.")
    assert said[1] == {"identity": None, "figi": False, "mode": None}


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
    assert said[1]["identity"] is None, "the worker must drop it itself, not trust the caller to"
    assert said[1]["figi"] is True, "this test only means something because the key did get through"


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
        with pytest.raises(cutter.CutterUnusable):
            c.cut("0000-19", tmp_path / "a.htm", "10-K")
    assert cutter.tries_so_far("0000-19") == 0 and not cutter.failed_path("0000-19").exists()


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
        assert worker.cut(good, "10-K", tmp_path / "answers" / "good.jsonl.gz") == 2
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
    assert names <= {"gzip", "json", "os", "socket", "sys", "edgar", "__future__"}, sorted(names)

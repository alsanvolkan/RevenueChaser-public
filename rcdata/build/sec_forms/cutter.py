"""The client of :mod:`cutter_worker`: starts it, pins its version, and keeps a 5.5-hour pass resumable.

One subprocess per builder process, under the interpreter named by ``RC_EDGARTOOLS_PYTHON``. The worker holds
``edgartools`` and nothing of ours; this file holds everything that decides *whether a document was cut*, because
that is the part a 5,149-document pass has to survive and the part the tests can reach without edgartools
installed.

**What is on disk after a cut, and why each file is there**

- ``<answers>/<cut>/<accession>.jsonl.gz`` -- edgartools' own output for one document, unmapped. A file that
  exists is not cut again, which is the whole of "a 5.5-hour pass resumes": it resumes at file granularity, not
  at a checkpoint, so a killed run loses at most the document in flight.
- ``<answers>/<cut>/<accession>.failed.json`` -- ``{"tries": n, "error": "...", "at": "<iso>"}``. Without it a
  document that makes the worker crash would be retried from scratch by every later run forever, and with it the
  attempt count is real **across** runs. It is removed when the document finally cuts.
- ``<cut>`` is ``cutter-<CUTTER_VERSION>`` (see :data:`CUTTER_VERSION`): the cache is keyed by the version that
  wrote it, so a pin bump is a new directory rather than a mixture of two cuts in one.

**What it does when the worker misbehaves** -- the four things ``spine/wrds.py`` has no need of, because WRDS is
one attended call and this is 5,149 unattended ones:

- a per-document timeout (:data:`TIMEOUT_S`), because a parser that walks a filer's pathological table can hang
  and WRDS's client simply blocks forever (``spine/wrds.py:158``);
- **kill and restart** after a timeout or a crash, where WRDS never restarts (``:153-156``): one document that
  kills the worker must not end the pass for the 5,000 after it;
- a recycle every :data:`RECYCLE_N` documents, because ~35 third-party packages parsing 2 MB of HTML each are
  not trusted to give memory back on a 15 GB box;
- a per-document attempt cap (:data:`MAX_TRIES`), after which the document is **not** cut by us silently: it is
  handed back as a failure and ``cut.py`` writes the fallback row that says so.

**What is a failed run rather than a failed document.** A missing ``RC_EDGARTOOLS_PYTHON``, an interpreter that is
not there, a worker that will not start, a failed handshake and a version mismatch all raise :class:`CutterUnusable`
and **count no attempt against any document**. A broken or upgraded venv would otherwise burn every document's
three tries in one run and turn 5,149 reports into fallback rows -- the one failure mode that is both silent and
expensive, because the rows would look cut.

The rollback is not "fix the venv": it is ``RC_SEC_FORMS_CUTTER=header``, which never starts a worker at all.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import base

#: The version of *our* side of the cut: the worker's pin, the protocol and what we store per section. It names
#: the answer directory, so a bump recuts instead of mixing two cuts. Bumped with the edgartools pin, and also on
#: its own if what we read off a section changes.
CUTTER_VERSION = "1"

PYTHON_ENV = "RC_EDGARTOOLS_PYTHON"     # the python of the venv that has edgartools; no default (a server path)
MODE_ENV = "RC_SEC_FORMS_CUTTER"        # "edgartools" (the default) or "header" (the rollback: no worker)
WORKER = Path(__file__).with_name("cutter_worker.py")
#: The pin's one home. Four files mention 6.0.0 -- this one, the worker's docstring, the plan and the issue -- and
#: only this one is read, so a bump that misses a prose mention is wrong in a comment and not in the data.
REQUIREMENTS = Path(__file__).resolve().parents[3] / "requirements-edgartools.txt"

TIMEOUT_S = 180.0        # one document; the measured mean is 3.9 s, so this is a hang and not a slow filing
HANDSHAKE_TIMEOUT_S = 60.0   # importing ~35 packages, cold; not a document, so it gets its own budget
RECYCLE_N = 200          # documents before the worker is replaced
MAX_TRIES = 3            # attempts at one document, counted across runs through its .failed.json
BREAK_AFTER = 5          # consecutive failed documents before the run is called the box's fault, not theirs


class CutterUnusable(base.SourceError):
    """The worker cannot be used at all: no interpreter, no start, no handshake, or the wrong version. **No
    document is charged an attempt for this** -- see the module docstring."""


class DocumentFailed(base.SourceError):
    """This one document did not cut, and the worker that said so is **alive and fine**: it answered in protocol,
    the next document goes to the same one. It is the filing's own fault -- the parser refused it."""


class WorkerLost(DocumentFailed):
    """This one document did not cut and the worker cannot be trusted with another: it died, it timed out, or it
    answered out of protocol. A subclass of :class:`DocumentFailed` because the document is charged the same
    attempt either way; the difference is only whether the process is kept.

    The distinction is why it exists: an earlier version replaced the worker on *every* failed document and only
    dropped the reference, so each ordinary refusal left a live interpreter -- with pandas, pyarrow and lxml in
    it -- parked by ``Popen.__del__`` with its pipes open, on the 15 GB box the dashboard shares. Measured at the
    time: 15 failures left 15 processes in state ``S``, alive after ``gc.collect()`` and after ``Cutter.close()``.
    """


def pinned_version() -> str:
    """The version ``requirements-edgartools.txt`` pins. A file that is missing, or that pins the package some
    other way than ``edgartools==<version>``, is a :class:`CutterUnusable`: a handshake that cannot say what it
    wanted would pass on any venv at all, which is the mistake the pin exists to prevent."""
    try:
        text = REQUIREMENTS.read_text("utf-8")
    except OSError as e:
        raise CutterUnusable(f"no {REQUIREMENTS.name}: {type(e).__name__}") from e
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.lower().startswith("edgartools=="):
            found = line.split("==", 1)[1].strip()
            if found:
                return found
    raise CutterUnusable(f"{REQUIREMENTS.name} does not pin edgartools as 'edgartools==<version>'")


def mode() -> str:
    """``"edgartools"`` or ``"header"``. ``header`` is the rollback: no worker is started and every item row is
    our own rule by choice, which is why the fallback check is noted and not applied in that mode."""
    chosen = (os.getenv(MODE_ENV) or "").strip().lower() or "edgartools"
    if chosen not in ("edgartools", "header"):
        raise CutterUnusable(f"{MODE_ENV}={chosen!r} is not a cutter: use 'edgartools' or 'header'")
    return chosen


def answers_dir() -> Path:
    """Where one document's answer file lives. Under the raw root, because it is a source's own output kept
    verbatim; our typing of it is the Parquet step.

    Named by **both** versions. Keyed by ``CUTTER_VERSION`` alone, bumping the pin and the venv without
    remembering to bump ours would pass the handshake and then mix two edgartools' cuts in one directory, with
    nothing in the files to say which cut which -- and that mixture is the thing the whole version pin exists to
    prevent.
    """
    return base.raw_root() / "sec_forms" / f"cutter-{CUTTER_VERSION}-edgartools-{pinned_version()}"


def answer_path(accession: str) -> Path:
    return answers_dir() / f"{accession}.jsonl.gz"


def failed_path(accession: str) -> Path:
    return answers_dir() / f"{accession}.failed.json"


def tries_so_far(accession: str) -> int:
    """How many attempts earlier runs have already spent on this document. A sidecar we cannot read is treated as
    zero rather than as "give up": a truncated file must not retire a document for good."""
    try:
        return int(json.loads(failed_path(accession).read_text("utf-8"))["tries"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return 0


def _is_cut(out: Path) -> bool:
    """Whether this answer file counts as a cut document.

    Not just "it exists": an empty file is what a crash between ``open`` and the first write leaves behind under
    the real name on some filesystems, and ``gzip.open`` reads one as zero rows without complaining -- so a
    document would be cached for ever as "cut, no sections". A gzip member is never zero bytes.
    """
    try:
        return out.stat().st_size > 0
    except OSError:
        return False


def _cleanup_tmp(out: Path, pid: int) -> None:
    """The partial file the worker left for this document, if it was killed before it could remove its own.

    ``pid`` is the **worker's**, which is the whole point: this used to build the name from the client's pid and
    so removed a file that never existed under that name, while the comment claimed the next attempt would
    truncate it -- which it does not either, because every replacement worker has a new pid. A killed worker is
    the only way one survives (the worker removes its own on any ordinary failure), and a timeout during
    detection is exactly that: ``Document.sections`` is evaluated with the tmp file already open.

    Only that worker's: another builder process's ``.tmp`` is its business, which is why the name carries a pid.
    """
    try:
        out.with_name(f"{out.name}.{pid}.tmp").unlink(missing_ok=True)
    except OSError:                         # a read-only or full disk; there is nothing else to be done here
        pass


def _note_failure(accession: str, tries: int, error: str) -> None:
    """The sidecar that remembers this document's attempts. A failure to write it is a failed **run**: the count
    is the only thing standing between a bad document and an endless retry, and carrying on without it would
    silently turn the cap off for every document in the pass."""
    path = failed_path(accession)
    body = {"tries": tries, "error": error[:300],
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    try:
        # The ``mkdir`` is inside the try with the write: it fails for the same reasons (a read-only mount, a
        # full disk, the raw root gone) and a ``FileExistsError`` out of this function would reach the caller as
        # a bare ``OSError`` instead of the failed run it is.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(body), "utf-8")
        os.replace(tmp, path)
    except OSError as e:
        raise CutterUnusable(f"the cutter cannot write {path.name}: {type(e).__name__}: {e}") from e


#: What the worker's environment keeps. A whitelist and not a copy: ~35 third-party packages run in there, and no
#: provider key of the builder's (OpenFIGI, DeepSeek, the SEC user agent) has any business reaching them.
#: ``EDGAR_IDENTITY`` is absent on purpose -- the worker removes it too, so neither side can identify us to the
#: SEC -- and so is every ``RC_*``, the mode included, because the worker has no RevenueChaser config to read and
#: the mode is the caller's decision about whether to start it at all.
_WORKER_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "PYTHONPATH")


def worker_env() -> dict[str, str]:
    """The worker's environment. Server only (Linux), as ``spine/wrds.py``'s is.

    ``PYTHONPATH`` is kept, as it is there: it is how the server points a venv at anything extra, and what the
    worker may import is pinned by a test that reads its imports rather than by what is on its path. It is also
    what lets the suite run the real worker against a stub ``edgar``, which is the only way any of this is tested
    without edgartools installed. ``RC_*`` is **not** kept, not even the mode: the worker has no RevenueChaser
    config to read, and the mode is the caller's decision about whether to start it at all.

    ``SSL_CERT_FILE``, ``SSL_CERT_DIR`` and ``LD_LIBRARY_PATH`` were carried over from ``spine/wrds.py`` and are
    gone: WRDS needs them because it opens a TLS connection to Wharton, and a worker that must never open a
    socket has no use for a certificate bundle. A whitelist is only worth having if every name on it is there for
    a reason this worker has.
    """
    env = {k: v for k, v in os.environ.items() if k in _WORKER_ENV or k.startswith("LC_")}
    env["PYTHONNOUSERSITE"] = "1"       # ~/.local packages are not part of the pinned venv
    return env


class Worker:
    """One ``cutter_worker.py`` subprocess, started and version-checked at construction.

    Constructing it is what fails a run: by the time :meth:`cut` is called the interpreter is there, the import
    worked and the version matched, so anything after that is one document's problem.
    """

    def __init__(self, python: str | None = None):
        python = python or (os.getenv(PYTHON_ENV) or "").strip()
        if not python:
            raise CutterUnusable(f"no edgartools interpreter: set {PYTHON_ENV} to the python of the venv that "
                                 "has edgartools")
        if not Path(python).exists():
            raise CutterUnusable(f"no edgartools interpreter at {python}: set {PYTHON_ENV} to the python of the "
                                 "venv that has edgartools")
        self.done = 0
        try:
            self.proc = subprocess.Popen([python, "-P", str(WORKER)], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, text=True, encoding="utf-8", env=worker_env())
        except OSError as e:
            raise CutterUnusable(f"the cutter worker did not start: {type(e).__name__}: {e}") from e
        self._handshake()

    def _handshake(self) -> None:
        """The worker's first line, sent unprompted. A mismatch is a failed run and not a warning: a venv
        upgraded underneath us would otherwise write rows labelled 6.0.0 that another version cut, and nothing
        downstream could tell them apart."""
        try:
            # Through the same timed read as an answer: a venv whose import hangs (a stale NFS mount under
            # site-packages) would otherwise block the builder for ever, and this runs at the start of the pass,
            # at every recycle and after every lost worker -- about thirty times in a full pass, plus failures.
            line = _read_line(self.proc, HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            raise self._stop(CutterUnusable(f"the cutter worker sent no handshake in {HANDSHAKE_TIMEOUT_S:.0f}s: "
                                            "its venv is most likely unreadable or still importing"))
        except OSError as e:
            raise self._stop(CutterUnusable(f"the cutter worker is gone: {type(e).__name__}"))
        if not line:
            raise self._stop(CutterUnusable(f"the cutter worker ended (exit {self.proc.poll()}) without a "
                                            "handshake: its venv has no edgartools, or the import failed"))
        try:
            hello = json.loads(line)
            if not isinstance(hello, dict):
                raise ValueError("not an object")
        except ValueError:
            raise self._stop(CutterUnusable("the cutter worker's first line is not a handshake "
                                            f"({line.strip()[:80]!r})"))
        if hello.get("fatal") or "edgar_version" not in hello:
            # The error text is the whole diagnosis: "No module named 'edgar'" sends somebody to the venv and
            # "cannot import name 'X' from 'edgar.documents'" sends them to this worker's own import line. A
            # message without it sends them nowhere, which is where the first version of this sent them.
            raise self._stop(CutterUnusable("the cutter worker could not import what the cut needs: "
                                            f"{hello.get('error') or '(it said nothing)'}"))
        found = str(hello["edgar_version"])
        want = pinned_version()
        if found != want:
            raise self._stop(CutterUnusable(f"the cutter venv has edgartools {found or '(no version)'}, not the "
                                            f"pinned {want}: install requirements-edgartools.txt, or bump the pin "
                                            "through the comparison in cutter_worker.py"))

    def _stop(self, error: BaseException) -> BaseException:
        self.kill()
        return error

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=30)
        except Exception:  # noqa: BLE001 - it is being killed; anything it says on the way out is noise
            pass

    def cut(self, html: Path, form: str, out: Path) -> int:
        """One document. The answer's own timeout kills the worker, because a hung parser does not answer a
        second request either; the caller starts a new one."""
        try:
            # The answer root, made once a document. A read-only mount or a raw root that is not there reached
            # the caller from here as a bare ``OSError`` -- on the first document, before any of the phase
            # machinery below could say it was the box's fault.
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise CutterUnusable(f"the cutter cannot write into {out.parent}: {type(e).__name__}: {e}") from e
        request = json.dumps({"html": str(html), "form": form, "out": str(out)}) + "\n"
        try:
            self.proc.stdin.write(request)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise self._stop(WorkerLost(f"the cutter worker is gone: {type(e).__name__}"))
        try:
            line = _read_line(self.proc, TIMEOUT_S)
        except TimeoutError:
            raise self._stop(WorkerLost(f"the cutter worker did not answer in {TIMEOUT_S:.0f}s"))
        if not line:
            raise self._stop(WorkerLost(f"the cutter worker died on this document (exit {self.proc.poll()})"))
        try:
            answer = json.loads(line)
            if not isinstance(answer, dict):
                raise ValueError("not an object")
        except ValueError:
            # Never read past a line that is not an answer: the next one could belong to the next document.
            raise self._stop(WorkerLost(f"the cutter worker answered out of protocol ({line.strip()[:80]!r})"))
        self.done += 1
        if answer.get("ok"):
            # ``answer["sections"]`` and not ``.get(... ) or 0``: an answer that says it cut a document without
            # saying how much of it is out of protocol, and ``spine/wrds.py`` reads its own count the same way.
            # Out of protocol and not a ``KeyError`` out of this method, though: this class promises the caller
            # only ``DocumentFailed`` and ``CutterUnusable``, and a count that is missing or is not a number is
            # the same kind of fault as a progress bar on the pipe -- the worker cannot be trusted with the next
            # document either.
            try:
                sections, chars = int(answer["sections"]), int(answer["chars"])
            except (KeyError, TypeError, ValueError, OverflowError):
                # The file goes too. The worker renames before it answers, so leaving it would have the client's
                # own rescue path ("it wrote the file and then we lost it") hand back a file written by a worker
                # that cannot say what is in it -- and that path skips the empty-text check as well.
                # ``OverflowError`` is in the list because ``json`` accepts ``Infinity`` and ``int(inf)`` raises
                # it; ``tries_so_far`` has it for the same reason.
                out.unlink(missing_ok=True)
                raise self._stop(WorkerLost("the cutter worker said it cut a document without saying how much "
                                            f"({line.strip()[:80]!r})")) from None
            # Sections but no text at all is not something a real filing does; it is what a field whose name moved
            # looks like, and the docstring used to call it "the signal that a name moved" while nothing read the
            # signal. Reading it here, on the first document, is what stops 5,149 files being cached as cut and
            # empty -- and it fails the run, because every later document would be wrong the same way.
            if sections and not chars:
                # The worker has already written the file -- it renames before it answers -- so it is removed
                # here. Leaving it would cache the very thing this check exists to refuse, and the next run
                # would hand it back as a cut document.
                out.unlink(missing_ok=True)
                # This document's failure and not the run's, which is the opposite of what it was. A moved field
                # does it to *every* document, so five in a row trip the breaker and the run stops with this
                # text in the message -- the diagnosis arrives either way. But a single filing can do it on its
                # own: in edgartools 6.0.0 a TOC section's text is ``""`` when its anchor is missing or its
                # boundary pair is inverted (toc_section_extractor.py:1885, 1927; toc_section_detector.py:145),
                # so a small filing whose only detected sections are all empty would otherwise have stopped
                # every hourly pass for the rest of the corpus's life.
                raise DocumentFailed(
                    f"the cutter answered {sections} sections and no text at all for {html.name}: either this "
                    "filing's sections are all empty, or a field this worker reads has moved in edgartools. "
                    "Nothing is cached; if the whole pass says this, check SECTION_FIELDS in cutter_worker.py "
                    "against the installed version.")
            return sections
        phase = str(answer.get("phase") or "parse")
        error = str(answer.get("error") or "the cutter worker failed without saying why")
        if phase != "parse":
            # Reading the document or writing the answer failed. That is the box, not the filing: an unmounted
            # corpus or a full disk would otherwise charge every one of 5,149 documents an attempt and retire the
            # corpus in three runs, for a reason none of them had anything to do with.
            raise CutterUnusable(f"the cutter could not {phase} {html.name}: {error}")
        raise DocumentFailed(error)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:                 # already gone: nothing to flush
            pass
        try:
            self.proc.wait(timeout=60)
        except Exception:  # noqa: BLE001
            self.kill()


def _read_line(proc: Any, timeout: float) -> str:
    """One line from the worker, or ``TimeoutError``. A thread and not ``select``: the pipe is a text stream with
    its own buffer, so a readiness check on the file descriptor can say "ready" for a partial line, and the
    worker is killed either way, so a thread left on a dead pipe costs nothing."""
    import threading
    box: list[str] = []

    def read() -> None:
        try:
            box.append(proc.stdout.readline())
        except Exception:  # noqa: BLE001 - the pipe is closed under us; the empty box is the answer
            pass

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError
    return box[0] if box else ""


_factory: Callable[[], Worker] = Worker      # the seam the tests replace


class Cutter:
    """The pass's one cutter: the worker, its restarts and its recycles, with per-document bookkeeping.

    Used as a context manager, so a killed run does not leave a subprocess behind::

        with Cutter() as cutter:
            for accession, html, form in documents:
                cutter.cut(accession, html, form)

    :meth:`cut` is the whole contract: it returns the answer path when the document is cut (now or by an earlier
    run) and ``None`` when it is not, having already written the sidecar that remembers why. It raises only
    :class:`CutterUnusable`, which is the run's problem and not a document's.

    **The breaker is the part that makes "a failed document" safe.** Charging an attempt per failure is only right
    while the failures are the filings' own; a reason that belongs to the box fails all of them, and three runs
    later the whole corpus is retired with nothing left to retry. The worker says which phase failed, so most of
    those arrive as :class:`CutterUnusable` already -- but not every one can be told apart from inside the worker
    (a library that fails on a missing data file of its own looks like a parse), so after
    :data:`BREAK_AFTER` consecutive failures this stops the run. It also bounds the worst case in time: without
    it, every document timing out costs 5,149 x 180 s, which is 257 hours, and the hourly builder would hold its
    lock for all of them.

    **Stopping the run gives back only the attempts of the documents whose worker was lost** -- see
    :meth:`_give_back`, which has the stall that giving back all of them caused.
    """

    def __init__(self, max_tries: int = MAX_TRIES, recycle_n: int = RECYCLE_N, break_after: int = BREAK_AFTER):
        self.max_tries = max_tries
        self.recycle_n = recycle_n
        self.break_after = break_after
        self.worker: Worker | None = None
        self.cut_now = 0          # documents this run actually sent to the worker and got answers for
        self.cached = 0           # documents an earlier run had already cut
        self.empty = 0            # documents the cutter read but found no section in; cached as cut, counted here
        self.failed: dict[str, str] = {}      # accession -> the last error, for the run's report
        self.restarts = 0
        #: The current run of consecutive failures: ``(accession, the worker was lost)`` a document.
        self._in_a_row: list[tuple[str, bool]] = []

    def __enter__(self) -> "Cutter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _worker(self) -> Worker:
        if self.worker is not None and self.worker.done >= self.recycle_n:
            self.worker.close()
            self.worker = None
        if self.worker is None:
            self.worker = _factory()        # CutterUnusable from here fails the run, as it must
        return self.worker

    def cut(self, accession: str, html: Path, form: str) -> Path | None:
        """Cut one document, or say it could not be. Never raises for this document alone."""
        out = answer_path(accession)
        if _is_cut(out):
            self.cached += 1
            # A document that failed and was then cut -- by another process, or by a run that was killed between
            # the worker's rename and the client's bookkeeping -- must not keep the note that says it failed, or
            # it stays one run from the cap for the rest of the corpus's life.
            failed_path(accession).unlink(missing_ok=True)
            return out
        spent = tries_so_far(accession)
        if spent >= self.max_tries:
            self.failed[accession] = f"gave up after {spent} attempts in earlier runs"
            return None
        worker = self._worker()             # may raise CutterUnusable: no attempt is charged
        pid = worker.proc.pid               # kept now: a lost worker's own pid names the .tmp it left behind
        try:
            sections = worker.cut(html, form, out)
        except DocumentFailed as e:
            if isinstance(e, WorkerLost):
                # Dead, hung or out of protocol: closed, not merely dropped, and replaced for the next document.
                worker.kill()
                self.worker = None
                self.restarts += 1
            if _is_cut(out):
                # The worker renamed the file and then we lost it -- a timeout on the answer line, say. The
                # document IS cut; calling it failed would have the caller write a fallback row over good data,
                # and the next run would hand back the same file as cached anyway.
                self.cut_now += 1
                self._in_a_row.clear()
                failed_path(accession).unlink(missing_ok=True)
                return out
            _cleanup_tmp(out, pid)
            # The attempt is charged here and nowhere else, which is what keeps the count honest across runs.
            _note_failure(accession, spent + 1, str(e))
            self.failed[accession] = str(e)
            self._in_a_row.append((accession, isinstance(e, WorkerLost)))
            if len(self._in_a_row) >= self.break_after:
                self._give_back()
                stopped, self._in_a_row = list(self._in_a_row), []
                raise CutterUnusable(
                    f"{len(stopped)} documents in a row failed to cut, the last with {str(e)!r}. That is most "
                    "likely the box and not the filings, so the run stops here rather than working through the "
                    "corpus one failure at a time.")
            return None
        self._in_a_row.clear()
        failed_path(accession).unlink(missing_ok=True)
        self.cut_now += 1
        if not sections:
            # Cached as cut, because it is: the cutter read the document and found nothing to cut. Counted, since
            # a form the library has no vocabulary for would answer this for every one of its filings, and the
            # count is the only place that would show.
            self.empty += 1
        return out

    def _give_back(self) -> None:
        """Undo the attempts charged for the failures in this run that were **not** the filings' own doing.

        Only the lost workers: a document the worker answered about, saying the parse was at fault, keeps its
        attempt and is retired after :data:`MAX_TRIES` -- which is what the cap is for.

        Giving every one of them back was a stall with no way out, and it is worth spelling out because a green
        test suite covered it. Nothing outside this method clears the run of failures except a document this run
        actually cut: a *cached* document does not, and on a resumed pass almost every good document is cached,
        so the only documents the worker sees are the bad ones. Five uncapped bad filings anywhere in the corpus
        therefore met the breaker in every run, got their attempts back, never reached the cap, and every
        document after the fifth -- including every newly filed one -- was never cut again.

        The trade-off, stated rather than hidden: a library that fails on a missing data file of its own looks
        like a parse failure, and those documents now spend their attempts. The run still stops after five, so
        it is an hour's worth of noise and not a corpus, and the sidecar says what each one said.
        """
        for accession, lost in self._in_a_row:
            if not lost:
                continue
            spent = tries_so_far(accession)
            if spent <= 1:
                failed_path(accession).unlink(missing_ok=True)
            else:
                _note_failure(accession, spent - 1, "attempt given back: the run was stopped by the breaker")

    def close(self) -> None:
        if self.worker is not None:
            self.worker.close()
            self.worker = None

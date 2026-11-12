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
RECYCLE_N = 200          # documents before the worker is replaced
MAX_TRIES = 3            # attempts at one document, counted across runs through its .failed.json


class CutterUnusable(base.SourceError):
    """The worker cannot be used at all: no interpreter, no start, no handshake, or the wrong version. **No
    document is charged an attempt for this** -- see the module docstring."""


class DocumentFailed(base.SourceError):
    """This one document did not cut. The worker has been restarted; the next document is tried normally."""


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
    verbatim; our typing of it is the Parquet step."""
    return base.raw_root() / "sec_forms" / f"cutter-{CUTTER_VERSION}"


def answer_path(accession: str) -> Path:
    return answers_dir() / f"{accession}.jsonl.gz"


def failed_path(accession: str) -> Path:
    return answers_dir() / f"{accession}.failed.json"


def tries_so_far(accession: str) -> int:
    """How many attempts earlier runs have already spent on this document. A sidecar we cannot read is treated as
    zero rather than as "give up": a truncated file must not retire a document for good."""
    try:
        return int(json.loads(failed_path(accession).read_text("utf-8"))["tries"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _note_failure(accession: str, tries: int, error: str) -> None:
    path = failed_path(accession)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"tries": tries, "error": error[:300],
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body), "utf-8")
    os.replace(tmp, path)


#: What the worker's environment keeps. A whitelist and not a copy: ~35 third-party packages run in there, and no
#: provider key of the builder's (OpenFIGI, DeepSeek, the SEC user agent) has any business reaching them.
#: ``EDGAR_IDENTITY`` is absent on purpose -- the worker removes it too, so neither side can identify us to the
#: SEC -- and so is every ``RC_*`` but the mode, because the worker has no RevenueChaser config to read.
_WORKER_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "PYTHONPATH", "SSL_CERT_FILE",
               "SSL_CERT_DIR", "LD_LIBRARY_PATH")


def worker_env() -> dict[str, str]:
    """The worker's environment. Server only (Linux), as ``spine/wrds.py``'s is.

    ``PYTHONPATH`` is kept, as it is there: it is how the server points a venv at anything extra, and what the
    worker may import is pinned by a test that reads its imports rather than by what is on its path. It is also
    what lets the suite run the real worker against a stub ``edgar``, which is the only way any of this is tested
    without edgartools installed. ``RC_*`` is **not** kept, not even the mode: the worker has no RevenueChaser
    config to read, and the mode is the caller's decision about whether to start it at all.
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
            line = self.proc.stdout.readline()
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
            raise self._stop(CutterUnusable(f"the cutter worker could not start edgartools: {hello.get('error')}"))
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
        out.parent.mkdir(parents=True, exist_ok=True)
        request = json.dumps({"html": str(html), "form": form, "out": str(out)}) + "\n"
        try:
            self.proc.stdin.write(request)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise self._stop(DocumentFailed(f"the cutter worker is gone: {type(e).__name__}"))
        try:
            line = _read_line(self.proc, TIMEOUT_S)
        except TimeoutError:
            raise self._stop(DocumentFailed(f"the cutter worker did not answer in {TIMEOUT_S:.0f}s"))
        if not line:
            raise self._stop(DocumentFailed(f"the cutter worker died on this document (exit {self.proc.poll()})"))
        try:
            answer = json.loads(line)
            if not isinstance(answer, dict):
                raise ValueError("not an object")
        except ValueError:
            # Never read past a line that is not an answer: the next one could belong to the next document.
            raise self._stop(DocumentFailed(f"the cutter worker answered out of protocol ({line.strip()[:80]!r})"))
        self.done += 1
        if answer.get("ok"):
            return int(answer.get("sections") or 0)
        raise DocumentFailed(str(answer.get("error") or "the cutter worker failed without saying why"))

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
    """

    def __init__(self, max_tries: int = MAX_TRIES, recycle_n: int = RECYCLE_N):
        self.max_tries = max_tries
        self.recycle_n = recycle_n
        self.worker: Worker | None = None
        self.cut_now = 0          # documents this run actually sent to the worker and got answers for
        self.cached = 0           # documents an earlier run had already cut
        self.failed: dict[str, str] = {}      # accession -> the last error, for the run's report
        self.restarts = 0

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
        if out.exists():
            self.cached += 1
            return out
        spent = tries_so_far(accession)
        if spent >= self.max_tries:
            self.failed[accession] = f"gave up after {spent} attempts in earlier runs"
            return None
        worker = self._worker()             # may raise CutterUnusable: no attempt is charged
        try:
            worker.cut(html, form, out)
        except DocumentFailed as e:
            # The worker is dead or suspect either way, so it is not reused. The attempt is charged here and
            # nowhere else, which is what keeps the count honest across runs.
            self.worker = None
            self.restarts += 1
            _note_failure(accession, spent + 1, str(e))
            self.failed[accession] = str(e)
            return None
        failed_path(accession).unlink(missing_ok=True)
        self.cut_now += 1
        return out

    def close(self) -> None:
        if self.worker is not None:
            self.worker.close()
            self.worker = None

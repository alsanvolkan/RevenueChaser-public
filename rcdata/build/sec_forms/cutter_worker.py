"""The section cutter worker: runs under the interpreter that has ``edgartools`` (``RC_EDGARTOOLS_PYTHON``).

**Provenance.** The cutting is done by **edgartools**, https://github.com/dgunning/edgartools, MIT licence,
Dwight Gunning. Used from it: ``edgar.documents`` -- ``parse_html`` and ``HybridSectionDetector``. Pinned to
**6.0.0** (``requirements-edgartools.txt``); ``479d350`` was that project's ``main`` on 12 November 2026, the day
of the measurement. Nothing is copied in, so the licence is satisfied by the pinned dependency.

**Why it is worth a second interpreter.** Measured on 20 stored annual reports, 12 November 2026
(``claude-coordinator`` ``notes/revenuechaser/notes/2026-11-12_1100_sec-edgartools-on-20.md``): its detector found
**78 of 78** item/filing pairs against our own rule's 67, with none the other way, and it found a full Item 8 in
three filings our rule threw away as a table of contents. It needs Python 3.11+ and brings about 35 packages
(pandas, pyarrow, lxml, rich); the builder runs 3.14. So it lives in its own venv and talks over a pipe.

**How to update the pin.** Bump the version, rerun the 20-document comparison with the seed and the filing list in
that result file (``random.Random(20261112)``), and read the item-length table against the stored one. A length
that changed on a filing that was right before **stops the update**. Only if the table holds: bump the version
here, bump ``CUTTER_VERSION`` in ``cutter.py``, and recut -- the cached answers are keyed by that version, so a
recut is what a bump costs.

**Protocol**, one JSON object per line on a private copy of stdin/stdout:

    handshake (sent once, unprompted, before the first request)
              {"edgar_version": "6.0.0"}
              {"ok": false, "fatal": true, "error": "<type: message>"}      (and the worker exits)
    request   {"html": "<abs path>", "form": "10-K", "out": "<abs path>.jsonl.gz"}
    answer    {"ok": true, "sections": <n>, "chars": <n>}
              {"ok": false, "phase": "parse"|"read"|"io", "error": "<type: message>"}

``phase`` is who is at fault, and it decides whether the document is charged an attempt: ``parse`` is this
filing's own doing (a parser that gives up, a file of its own that cannot be read while the corpus around it is
fine) and ``read``/``io`` is the box (the corpus root gone, the answer root read-only, a full disk), which fails
the run instead of 5,149 documents one at a time.

**The answer file holds edgartools' own output, unmapped**: one JSON line per detected section, with the fields
this worker can read off it and nothing of ours. Our mapping to ``quality`` / ``method`` / ``confidence`` /
``warning`` lives in ``cut.py`` and runs at ``to_parquet``, so a change to our own rules re-types the cached
answers in minutes and only an edgartools bump costs the full pass.

**What is read off a section, and how sure we are of it.** The measurement above used ``Section.text()``,
``detection_method``, ``confidence`` and ``warnings``, and that is what :func:`_section` reads. It reads them
through :func:`_attr`, which tries the attribute and then the mapping key, and it does **not** fail a document
because one of them is missing -- a field that is not there is stored as null, and ``cut.py`` decides what a null
means. The attribute names come from that measurement's notes; they were **not** verified against an installed
edgartools in the session that wrote this file, because edgartools is not in this container and the suite runs
against a stub. The first real run on homesrv is what confirms them, and a document whose sections all come out
with null fields is the signal that a name moved.

**Isolation, enforced here and not by the caller.** Before ``edgar`` is imported, ``socket.socket.connect`` and
``socket.getaddrinfo`` are replaced by functions that raise an ``OSError`` subclass, so the library sees what a
closed port gives it; a proxy variable only stops a client that reads proxy variables, this stops all of them.
``EDGAR_IDENTITY`` is removed from the environment, so nothing can identify itself to the SEC even if a code path
tried. Its stdout is isolated like ``spine/wrds_worker.py``'s, because edgartools prints through ``rich`` and a
stray line would read as out of protocol.

Standard library and ``edgar`` only: **this file must not import the RevenueChaser package.**
"""
from __future__ import annotations

import errno
import gzip
import json
import os
import socket
import sys

#: This worker does not know what version it is supposed to be. It reports what it has and the client compares
#: that against ``requirements-edgartools.txt``, which is the one place the pin is written -- a second literal
#: here is a second thing to forget on a bump, and the handshake would then pass on a venv nobody meant.


class NetworkBlocked(OSError):
    """What a socket call raises inside this worker. An ``OSError`` subclass on purpose: the measurement ran
    against a closed port, so this is the failure the library was measured handling."""


def _block_network() -> None:
    """No socket, before ``edgar`` is imported and so before any module-level client it builds.

    Each of these was measured to be a way out on its own: ``connect`` and ``connect_ex`` for a resolved address,
    ``create_connection`` for the convenience wrapper, ``getaddrinfo`` / ``gethostbyname`` / ``gethostbyname_ex``
    / ``getnameinfo`` because a name lookup already leaves the box, and ``sendto`` / ``sendmsg`` because UDP needs
    no ``connect`` at all (a datagram did arrive through the earlier version of this list).

    **It is not a guarantee, and this docstring used to claim it was.** Two holes are left on purpose, because
    closing them from inside the process is not possible: ``_socket.socket``, the C base class, can be reached
    directly, and a child process starts with its own clean copy of everything. What rules those out is the
    operating system, not Python -- the unit the server runs this under should carry ``PrivateNetwork=yes`` or
    ``IPAddressDeny=any``. What this does buy is that the library's own code paths, which use the names above,
    cannot reach the SEC by accident, and in 6.0.0 the only ``gethostbyname`` caller is ``edgar/diagnose_ssl``.

    Nothing here is undone: this process only ever cuts local files.
    """
    def _refuse(*_a, **_k):
        raise NetworkBlocked("the section cutter worker has no network by design")

    socket.socket.connect = _refuse            # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse         # type: ignore[method-assign]
    socket.socket.sendto = _refuse             # type: ignore[method-assign]
    socket.socket.sendmsg = _refuse            # type: ignore[method-assign]
    socket.create_connection = _refuse         # type: ignore[assignment]
    socket.getaddrinfo = _refuse               # type: ignore[assignment]
    socket.gethostbyname = _refuse             # type: ignore[assignment]
    socket.gethostbyname_ex = _refuse          # type: ignore[assignment]
    socket.getnameinfo = _refuse               # type: ignore[assignment]
    os.environ.pop("EDGAR_IDENTITY", None)


def _isolate_streams():
    """Keep the library off the protocol: ``rich`` prints, and a progress bar on stdout reads as an answer.

    The protocol keeps private copies of fds 0 and 1; fd 1 becomes stderr, so whatever is printed reaches the
    person's terminal rather than the client's parser, and fd 0 becomes ``/dev/null`` -- unlike the WRDS worker
    this one never has a person to prompt, so there is no terminal to hand it.
    """
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    sys.stdout.flush()
    os.dup2(2, 1)
    fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
    return proto_in, proto_out


_PROTO = None


def _say(answer: dict) -> None:
    _PROTO.write(json.dumps(answer) + "\n")
    _PROTO.flush()


def _error(e: BaseException) -> str:
    """The failure for the answer line: type, message, capped at 300 characters.

    It carries no *environment value*, which is the rule that matters: no key, no credential. It may well carry a
    path, because an ``OSError``'s own message does and that path is usually the whole diagnosis -- an earlier
    version of this docstring claimed otherwise and was wrong. Those paths reach ``<accession>.failed.json`` on
    the same server, so nothing leaves the box; they must not reach an issue or a commit message.
    """
    return f"{type(e).__name__}: {e}"[:300]


def _attr(section, *names):
    """One field of a section, by attribute and then by mapping key; None when it carries none of the names."""
    for name in names:
        value = getattr(section, name, None)
        if value is None and hasattr(section, "get"):
            try:
                value = section.get(name)
            except Exception:  # noqa: BLE001 - a mapping that refuses a key is simply not carrying it
                value = None
        if value is not None:
            return value
    return None


def _plain(value):
    """A field as the answer file carries it: text, number, bool, null, or a list of text."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return str(value)


#: Every public field of ``edgar.documents.document.Section`` in 6.0.0, read from the wheel's own source. Stored
#: verbatim, because getting a field back later costs the whole 5.5-hour pass: the answer files are the cache.
#: ``covered_items`` is the one that would hurt most to lose -- a filer who writes "Items 1 and 2. Business and
#: Properties" gets one section with ``item='1'`` and ``covered_items=('1', '2')``, so without it Item 2 is
#: invisible to the mapping in ``cut.py`` and the business section of such a filing silently has no properties.
SECTION_FIELDS = ("name", "title", "item", "part", "covered_items", "detection_method", "confidence", "validated",
                  "warnings", "start_offset", "end_offset", "kind")


def _text_of(section) -> str:
    """``Section.text()``, which is a method in 6.0.0, and a plain ``text`` attribute where it is one."""
    value = _attr(section, "text")
    if callable(value):
        value = value()
    return "" if value is None else str(value)


def _sections(detected):
    """``Document.sections`` as a list of ``(key, section)``. It is a ``Dict[str, Section]`` in 6.0.0, and the key
    is the section's own name; a list is accepted too, so a future shape change is not a crash."""
    if detected is None:
        return []
    if hasattr(detected, "items"):
        out = []
        for key, value in detected.items():
            for one in (value if isinstance(value, (list, tuple)) else [value]):
                out.append((str(key), one))
        return out
    return [(None, one) for one in detected]


def _section(key, section) -> dict:
    """One section as the answer file stores it: edgartools' own fields, under their own names, unmapped.

    No alias chains. An earlier version guessed at several possible names per field and stored whichever answered,
    which meant the file could not say which one did -- and a guess that is wrong everywhere reads as a document
    full of nulls rather than as a mistake. The names here are the dataclass's, read from the pinned wheel.
    """
    text = _text_of(section)
    row = {field: _plain(_attr(section, field)) for field in SECTION_FIELDS}
    row["key"] = key
    row["chars"] = len(text)
    row["text"] = text
    return row


class BadDocument(Exception):
    """This one file cannot be read, and the corpus directory around it is fine.

    The distinction is the whole reason this class exists. Every unreadable file used to be answered as ``read``
    or ``io`` -- the box's fault, so the run stops and no attempt is charged -- and a single bad file in the
    corpus then stopped *every* hourly run at the same document for ever, with nothing remembered and nothing
    after it ever cut. A ``.gz`` that is not gzip is an ``OSError`` (``gzip.BadGzipFile``), so is a directory
    where a filing should be, and so is a file that is not there: all three are one document's problem when the
    directory holding it is readable, and all three are the box's problem when it is not.

    The cost of the rule, stated rather than hidden: a corpus half way through an rsync has readable directories
    and missing files, so those documents are charged an attempt each and are retired after three runs. The run
    reports them, the sidecar says why, and deleting it retries them. The alternative -- a pass that stops at the
    same document every hour for the rest of the corpus's life -- is worse, and the caller should not hand over a
    path that is not on disk in the first place.
    """


#: The ``errno`` values that say *this file* is the problem. Everything else -- a permission, a flaky mount, no
#: more file handles, no memory -- is the box's, whatever directory it happened in, and must not be charged to a
#: filing. An earlier version decided this by asking whether the file's own directory was readable, which looked
#: right and was not: the corpus is one directory per CIK (``core/edgar_docs.py:31``), so a company whose folder
#: had not been downloaded read as "the corpus is not mounted" and stopped every hourly run at that document for
#: ever. The directory a file sits in says nothing about the mount above it.
THEIR_FAULT = frozenset({errno.ENOENT, errno.EISDIR, errno.ENOTDIR, errno.ENAMETOOLONG, errno.ELOOP})


def _read(html_path: str) -> str:
    """One document's HTML, gzipped or not.

    A failure to read it is :class:`BadDocument` when the reason is about this file (it is not there, it is a
    directory, its name is impossible) and an ``OSError`` when it is about the box (``EACCES`` from a restore
    with the wrong owner, ``EIO`` or ``ESTALE`` from a flaky mount, ``EMFILE``, ``ENOSPC``). ``BadGzipFile`` is a
    ``gzip`` file that is not gzip -- a half-finished download -- and carries no ``errno``, which is this file's
    own problem by definition. Whether it is *charged* to the document is the client's decision, not this one.
    """
    opener = gzip.open if html_path.endswith(".gz") else open
    try:
        with opener(html_path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except gzip.BadGzipFile as e:
        raise BadDocument(f"this document cannot be read: BadGzipFile: {e}") from e
    except OSError as e:
        if e.errno in THEIR_FAULT:
            raise BadDocument(f"this document cannot be read: {type(e).__name__}: {e}") from e
        raise


def _cut(html_path: str, form: str, out: str) -> tuple[int, int]:
    """Cut one document; ``(sections, characters)``.

    Through ``parse_html`` and ``Document.sections``, which are both in ``edgar.documents.__all__`` -- not through
    ``HybridSectionDetector``, which lives in ``edgar.documents.extractors`` and is private to the library's own
    routing. Two reasons, and the first one is the whole reason this function was wrong before:

    - **a private path is a guess.** An earlier version imported the detector from ``edgar.documents``, where it
      is not; the handshake imports only ``edgar``, so the failure arrived once per document and retired all
      5,149 of them after three runs each.
    - **the library routes by form, and we must not override that.** ``Document.sections`` sends 10-K, 10-Q, 8-K,
      20-F and title-based forms to the hybrid detector and everything else to the pattern extractor. The 20
      document measurement that chose this library used 10-K and 20-F, so it measured exactly the hybrid path
      this call still takes for them -- while a 40-F now gets the extractor the library thinks is right for it
      instead of a detector with no vocabulary for that form.

    The form is passed as ``ParserConfig(form=...)``, which the config's own comment calls "required for section
    detection": without it ``Document.sections`` has no form to route on.
    """
    from edgar.documents import parse_html  # noqa: PLC0415 - imported in the handshake; this is the cached module
    from edgar.documents.config import ParserConfig  # noqa: PLC0415

    html = _read(html_path)
    document = parse_html(html, ParserConfig(form=form))
    # A tmp name carrying our pid, so two builder processes on one document cannot write each other's partial
    # file -- and so the loser of the race fails on its own file rather than on a name the winner already renamed.
    # No assertion pins this one: it only shows when two processes cut the same document at the same instant, and
    # a test that races two workers would pass or fail by timing. It is here for the same reason the sidecar's
    # tmp name carries a pid, and the mutant that drops it is a known survivor of the battery.
    tmp, n, chars = f"{out}.{os.getpid()}.tmp", 0, 0
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for key, section in _sections(document.sections):
                row = _section(key, section)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
                chars += row["chars"]
            fh.flush()
            # The rename is atomic, but without this the content need not be on disk when the name is: a
            # power cut between the two leaves a file under the real name with nothing in it, which the
            # client would read as "cut, no sections" for ever. Not pinnable by a test either -- it takes
            # a crash of the box, not of the process -- so the battery lists it as a known survivor.
            os.fsync(fh.fileno())
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, out)
    return n, chars


def main() -> int:
    global _PROTO
    _block_network()
    proto_in, _PROTO = _isolate_streams()
    try:
        # The imports *this module* needs, here: an import that fails is the venv's problem, and answering it
        # here makes it fatal. Left to the first cut, it would arrive as a document failure, once per document.
        # Not "every import the cut needs", which an earlier version of this comment claimed: the library itself
        # imports lazily, inside ``Document.sections`` (document.py:937, 943, 951) and inside ``Section.text()``
        # (:154), and nothing here can make those happen early. One of those failing arrives as a parse failure
        # per document, which the client's breaker stops after five -- loud, but not at the handshake.
        import edgar  # noqa: PLC0415 - the import itself is what the handshake reports on
        from edgar.documents import parse_html  # noqa: PLC0415, F401 - imported for the handshake to prove it
        from edgar.documents.config import ParserConfig  # noqa: PLC0415, F401
        _say({"edgar_version": str(getattr(edgar, "__version__", "") or "")})
    except BaseException as e:  # noqa: BLE001 - a venv that cannot import edgar fails the run, not a document
        _say({"ok": False, "fatal": True, "error": _error(e)})
        return 2
    for line in proto_in:
        if not line.strip():
            continue
        req = None
        try:
            req = json.loads(line)
            # The phase, so the client can tell "this filing is bad" from "this box is bad". Reading the document
            # and writing the answer are I/O: a missing corpus mount or a full disk fails every document for a
            # reason that has nothing to do with any of them, and charging each one an attempt would retire the
            # whole corpus. ``parse`` and ``read`` are the filing's own; ``io`` is the box's.
            html, out = str(req["html"]), str(req["out"])
            form = req.get("form")
            if not form:
                raise ValueError("the request names no form")
            # Whether the document is there is not checked here: ``_read`` opens it and decides whose fault any
            # failure to read it is, which is one decision in one place rather than this check and that one.
            n, chars = _cut(html, str(form), out)
            _say({"ok": True, "sections": n, "chars": chars})
        except BadDocument as e:
            # This document's own file. The client decides whether to charge it, because only the client knows
            # whether the corpus root is still there -- a mount that has gone makes every file unreadable.
            _say({"ok": False, "phase": "read", "error": _error(e)})
        except (OSError, MemoryError) as e:
            # The box: writing the answer failed, or reading it failed for a reason that was not about the file.
            # ``MemoryError`` is here and not in the parse branch below because this runs on the 15 GB box the
            # dashboard shares, and a filing is not at fault for arriving when the box is full.
            _say({"ok": False, "phase": "io", "error": _error(e)})
        except Exception as e:  # noqa: BLE001 - one document's failure is answered; the worker stays
            _say({"ok": False, "phase": "parse", "error": _error(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

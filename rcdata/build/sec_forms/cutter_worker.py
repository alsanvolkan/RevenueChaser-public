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
    answer    {"ok": true, "sections": <n>}
              {"ok": false, "error": "<type: message>"}

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

    Both are replaced, because a name that is already resolved still needs ``connect`` and a client that resolves
    first fails earlier with a clearer error. Nothing here is undone: this process only ever cuts local files.
    """
    def _refuse(*_a, **_k):
        raise NetworkBlocked("the section cutter worker has no network by design")

    socket.socket.connect = _refuse            # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse         # type: ignore[method-assign]
    socket.create_connection = _refuse         # type: ignore[assignment]
    socket.getaddrinfo = _refuse               # type: ignore[assignment]
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
    """The failure for the answer line: type, message, capped. No path of ours and no environment value."""
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


def _text_of(section) -> str:
    """``Section.text()`` as the measurement used it, and a plain ``text`` attribute where it is one."""
    value = _attr(section, "text")
    if callable(value):
        value = value()
    return "" if value is None else str(value)


def _sections(detected):
    """Whatever ``detect_sections()`` returns, as a list. A mapping's key is kept as ``key``, because for a
    mapping that is where the item number is; a list's entries carry their own."""
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
    """One section as the answer file stores it: edgartools' own fields, unmapped."""
    text = _text_of(section)
    return {"key": key,
            "item": _plain(_attr(section, "item", "item_number", "number", "name")) or key,
            "part": _plain(_attr(section, "part", "part_number")),
            "title": _plain(_attr(section, "title", "heading")),
            "detection_method": _plain(_attr(section, "detection_method", "method")),
            "confidence": _plain(_attr(section, "confidence", "score")),
            "warnings": _plain(_attr(section, "warnings", "warning")) or [],
            "chars": len(text),
            "text": text}


def _cut(html_path: str, form: str, out: str) -> int:
    from edgar.documents import HybridSectionDetector, parse_html  # noqa: PLC0415 - after _block_network()

    opener = gzip.open if html_path.endswith(".gz") else open
    with opener(html_path, "rt", encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    detected = HybridSectionDetector(parse_html(html), form).detect_sections()
    tmp, n = out + ".tmp", 0
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for key, section in _sections(detected):
                fh.write(json.dumps(_section(key, section), ensure_ascii=False) + "\n")
                n += 1
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, out)
    return n


def main() -> int:
    global _PROTO
    _block_network()
    proto_in, _PROTO = _isolate_streams()
    try:
        import edgar  # noqa: PLC0415 - the import itself is what the handshake reports on
        _say({"edgar_version": str(getattr(edgar, "__version__", "") or "")})
    except BaseException as e:  # noqa: BLE001 - a venv that cannot import edgar fails the run, not a document
        _say({"ok": False, "fatal": True, "error": _error(e)})
        return 2
    for line in proto_in:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            _say({"ok": True, "sections": _cut(req["html"], req.get("form") or "10-K", req["out"])})
        except Exception as e:  # noqa: BLE001 - one document's failure is answered; the worker stays
            _say({"ok": False, "error": _error(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

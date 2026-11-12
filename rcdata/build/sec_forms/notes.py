"""The note blocks of an annual report's inline XBRL: the filer's own ``ix:nonNumeric`` text blocks (#753).

``rcdata/build/fundamentals/ixbrl.py`` reads the same document's **numeric** facts (``ix:nonFraction``). This reads
the text ones, which is where a filer's prose about its debt, its segments or its accounting policies actually is.

**Why the filer's own blocks and not a heading regex.** Measured on 20 stored annual reports (notes:
``2026-11-12_1110_sec-annual-report-census.md``): a regex over heading lines found a debt note in 8 of 20 with 3
false positives -- "Corporate debt securities" is an investment the company *owns*, not its borrowing -- while the
filer's own text block found 16 of 20 with none wrong. Over all 5,149 stored annual reports, 99.4% carry inline
XBRL and 78.9% a debt note block. A tagged block is the filer's statement of where a note starts and ends; a
heading is our guess at it.

**Which elements are kept.** A fact whose local name ends in ``TextBlock`` or ``Explanatory``, which is the
census's own rule and covers both taxonomies (us-gaap ``...TextBlock``, IFRS ``DisclosureOf...Explanatory``). Every
other ``ix:nonNumeric`` -- the cover facts: ticker, exchange, filer category, auditor name -- is left out and
**counted**, so "nothing was kept" and "nothing was tagged" never look alike.

**Continuations.** A block longer than its one element continues in the element its ``continuedAt`` names, which
carries a ``continuedAt`` of its own, and so on: the text is the chain, in order. A chain that points at a missing
id, or back at an id already in it, stops there and is counted rather than raising or looping.

**Nesting, and why the rows carry it.** The census found a median of 76 blocks but 23 top-level ones per filing:
filers tag a note and then tag its subsections inside it, so the same sentence belongs to two or three blocks. Flat,
the stored bytes and every character count triple. So each row says whether it is ``top_level`` and, if not, which
``seq`` encloses it, and the census's "block chars" column -- what acceptance compares against -- sums the
top-level rows only.

Standard library only (``re``, ``html.parser``, ``dataclasses``, ``datetime``), plus ``rcdata.sec_sections`` for the
one HTML-to-text helper, which imports nothing else either. No network, no model call, no taxonomy or label file:
a concept is stored as filed, and a board that wants a nicer name makes one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

from ... import sec_sections

#: The version of this reader's output, stored on every row. Bump it when the kept set, the stitching or the text
#: normalisation changes, because the stored rows cannot be compared across two of them.
NOTES_VERSION = "notes-1"

#: A fact is a note block when its local name ends in one of these (the census's rule, §3 of the plan).
BLOCK_SUFFIXES = ("TextBlock", "Explanatory")

#: Tags that end a line of text, so a table row or a paragraph does not run into the next one.
_BREAK_TAGS = frozenset(("p", "div", "br", "tr", "li", "ul", "ol", "table", "thead", "tbody", "h1", "h2", "h3",
                         "h4", "h5", "h6", "blockquote", "section", "article"))
_SPACES = re.compile(r"[^\S\n]+")
#: Runs of line breaks become one. A filer's markup is where the breaks come from, and the two paths through
#: :func:`text_of` disagree about how many a paragraph is worth -- the parser's own path sees ``<p>`` open and
#: close, the escaped path goes through ``strip_html``. One break either way means a block's ``chars`` says the
#: same thing whichever path produced it, which is what the acceptance sum compares.
_NEWLINES = re.compile(r"\n+")


def local(qname: str | None) -> str:
    """``us-gaap:DebtDisclosureTextBlock`` -> ``DebtDisclosureTextBlock``."""
    return str(qname or "").rsplit(":", 1)[-1]


def taxonomy(qname: str | None) -> str:
    """``us-gaap:DebtDisclosureTextBlock`` -> ``us-gaap``; a name with no prefix has none."""
    name = str(qname or "")
    return name.rsplit(":", 1)[0] if ":" in name else ""


def is_block(qname: str | None) -> bool:
    return local(qname).endswith(BLOCK_SUFFIXES)


def kind_of(qname: str | None) -> str:
    """``policy`` / ``table`` / ``disclosure`` -- what the filer's own name says the block is.

    ``policy``: an accounting policy, in either taxonomy (``...PolicyTextBlock``, and IFRS's
    ``DescriptionOfAccountingPolicy...Explanatory``). ``table``: the tabular part a filer tags separately
    (``...TableTextBlock``), which holds the same numbers as the note it sits in. Everything else is a
    ``disclosure``, which is the note itself.
    """
    name = local(qname)
    if name.endswith("PolicyTextBlock") or name.endswith("PoliciesTextBlock"):
        return "policy"
    if name.endswith("Explanatory") and "DescriptionOfAccountingPolic" in name:
        return "policy"
    if name.endswith("TableTextBlock") or name.endswith("TablesTextBlock"):
        return "table"
    return "disclosure"


def text_of(parts: list[str], escaped: bool) -> str:
    """The parts of one block as stored text: no tags, no runs of blank lines, no trailing space on a line.

    ``escape="true"`` means the filer escaped the note's markup, so the parser handed us ``<p>`` as text rather
    than as a tag; that text goes through ``sec_sections.strip_html`` to become prose. The unescaped case has
    already lost its tags to the parser, and the same call is harmless there.
    """
    raw = "".join(parts)
    if escaped or "<" in raw:
        raw = sec_sections.strip_html(raw)
    lines = [_SPACES.sub(" ", line).strip() for line in raw.split("\n")]
    return _NEWLINES.sub("\n", "\n".join(lines)).strip()


@dataclass(frozen=True)
class Note:
    """One note block of one document. ``seq`` is the order the blocks open in the file, from 0."""

    concept: str           #: as filed, with its prefix: ``us-gaap:DebtDisclosureTextBlock``
    taxonomy: str          #: ``us-gaap``, ``ifrs-full``, or the filer's own prefix
    local_name: str        #: the name without its prefix
    kind: str              #: policy / table / disclosure
    seq: int
    top_level: bool        #: no other kept block encloses it, so its text is counted once
    parent_seq: int | None  #: the enclosing kept block's ``seq``, or None when ``top_level``
    period_end: date | None
    text: str
    chars: int

    def row(self, cik: int | str, accession: str) -> dict:
        """The stored row: the key the table is unique on, then the block."""
        return {"cik": int(cik), "accession": str(accession), "concept": self.concept, "taxonomy": self.taxonomy,
                "local_name": self.local_name, "kind": self.kind, "seq": self.seq, "top_level": self.top_level,
                "parent_seq": self.parent_seq, "period_end": self.period_end, "text": self.text,
                "chars": self.chars, "rules_version": NOTES_VERSION}


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


class _Reader(HTMLParser):
    """One pass over the document: the contexts, the kept blocks, and every continuation element by id.

    The text of a tag that encloses another kept block is kept whole, as the page reads it, so a parent block holds
    its children's words too; ``top_level`` is what stops that being counted twice. Tag names arrive lower-cased.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts: dict[str, date | None] = {}
        self.blocks: list[dict] = []
        self.conts: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        #: Every open ``ix:nonNumeric`` and ``ix:continuation``, outermost first. A fact that is not a note block
        #: is on it as ``skip``: it collects no text, but it has to be there, or its end tag would close the block
        #: it sits inside and every later row's nesting would be wrong.
        self._stack: list[dict] = []
        self._ctx: str | None = None
        self._field: str | None = None
        self._ctx_text: list[str] = []

    # ── context dates ──
    def _count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def handle_starttag(self, tag, attrs):
        name, a = local(tag), {k: v for k, v in attrs}
        if tag == "ix:nonnumeric":
            self._open_fact(a)
            return
        if tag == "ix:continuation":
            self._open_cont(a)
            return
        if name == "context":
            self._ctx = a.get("id")
        elif name in ("enddate", "instant") and self._ctx is not None:
            self._field, self._ctx_text = name, []
        if tag in _BREAK_TAGS and self._stack:
            self._add("\n")

    def handle_startendtag(self, tag, attrs):
        """``<br/>`` and the like never reach ``handle_endtag``, and neither does an empty ``ix:`` element."""
        if tag in ("ix:nonnumeric", "ix:continuation"):
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)
            return
        if tag in _BREAK_TAGS and self._stack:
            self._add("\n")

    def handle_data(self, data):
        if self._field is not None:
            self._ctx_text.append(data)
        self._add(data)

    def handle_endtag(self, tag):
        name = local(tag)
        if tag == "ix:nonnumeric":
            self._close_fact()
            return
        if tag == "ix:continuation":
            self._close_cont()
            return
        if name in ("enddate", "instant") and self._field == name:
            self.contexts[self._ctx or ""] = _day("".join(self._ctx_text))
            self._field = None
        elif name == "context":
            self._ctx = None
        if tag in _BREAK_TAGS and self._stack:
            self._add("\n")

    # ── the frames ──
    def _add(self, data: str) -> None:
        for frame in self._stack:
            if frame["what"] != "skip":
                frame["parts"].append(data)

    def _enclosing(self) -> tuple[str, object] | None:
        """What the element now opening sits inside: the innermost kept block, or a continuation.

        A fact we did not keep is transparent: a note block tagged inside a cover fact belongs to whatever
        encloses *that*, because the cover fact is not a row and cannot be a parent.
        """
        for frame in reversed(self._stack):
            if frame["what"] == "block":
                return ("block", frame["seq"])
            if frame["what"] == "cont":
                return ("cont", frame["id"])
        return None

    def _open_fact(self, a: dict) -> None:
        concept = a.get("name")
        if str(a.get("xsi:nil") or "").lower() == "true":
            self._count("nil")
            self._stack.append({"what": "skip"})
            return
        if not is_block(concept):
            self._count("not_a_block")
            self._stack.append({"what": "skip"})
            return
        block = {"what": "block", "seq": len(self.blocks), "concept": concept,
                 "escape": str(a.get("escape") or "").lower() == "true",
                 "continued_at": a.get("continuedat"), "context": a.get("contextref"),
                 "enclosing": self._enclosing(), "parts": []}
        self.blocks.append(block)
        self._stack.append(block)

    def _close_fact(self) -> None:
        self._close("block")

    def _open_cont(self, a: dict) -> None:
        cont = {"what": "cont", "id": a.get("id"), "continued_at": a.get("continuedat"),
                "enclosing": self._enclosing(), "parts": []}
        if cont["id"]:
            self.conts[cont["id"]] = cont
        self._stack.append(cont)

    def _close_cont(self) -> None:
        self._close("cont")

    def _close(self, what: str) -> None:
        """Close the innermost open element of this kind, so a stray end tag closes nothing."""
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["what"] in (what, "skip"):
                del self._stack[i]
                return


def _chain(block: dict, conts: dict[str, dict], counts: dict) -> list[dict]:
    """The continuation elements of one block, in order; a missing or repeating id ends the chain and is counted."""
    out: list[dict] = []
    seen: set[str] = set()
    at = block.get("continued_at")
    while at:
        if at in seen:
            counts["continuation_loop"] = counts.get("continuation_loop", 0) + 1
            break
        cont = conts.get(at)
        if cont is None:
            counts["continuation_missing"] = counts.get("continuation_missing", 0) + 1
            break
        seen.add(at)
        out.append(cont)
        at = cont.get("continued_at")
    return out


def parse(html: str, counts: dict | None = None) -> tuple[list[Note], dict]:
    """``(notes, counts)`` -- the note blocks of one annual report, and what was left out and why.

    ``counts`` keys: ``not_a_block`` (an ``ix:nonNumeric`` that is a cover fact, not a note), ``nil``, ``empty``
    (a kept block whose text is blank once stitched), ``continuation_missing``, ``continuation_loop``.
    """
    counts = counts if counts is not None else {}
    reader = _Reader()
    reader.feed(html or "")
    reader.close()
    for key, value in reader.counts.items():
        counts[key] = counts.get(key, 0) + value

    # Which block owns each continuation, so a block nested in a continuation knows its parent.
    owner: dict[str, int] = {}
    chains: dict[int, list[dict]] = {}
    for block in reader.blocks:
        chain = _chain(block, reader.conts, counts)
        chains[block["seq"]] = chain
        for cont in chain:
            if cont["id"] is not None:
                owner.setdefault(cont["id"], block["seq"])

    def parent_of(block: dict) -> int | None:
        """The innermost kept block enclosing this one.

        A block tagged inside a continuation belongs to the block that continuation continues -- that is the whole
        reason the owner map above exists. A continuation nobody claims is transparent: we follow what encloses it.
        """
        where, seen = block["enclosing"], set()
        while where is not None:
            what, ref = where
            if what == "block":
                return None if int(ref) == block["seq"] else int(ref)
            if ref is None or ref in seen:
                return None
            seen.add(ref)
            seq = owner.get(ref)
            if seq is not None:
                return None if seq == block["seq"] else seq
            cont = reader.conts.get(ref)
            where = cont["enclosing"] if cont is not None else None
        return None

    notes: list[Note] = []
    for block in reader.blocks:
        parts = list(block["parts"])
        for cont in chains[block["seq"]]:
            # A continuation nested inside this very block is already in its own text; adding it would double it.
            if cont["enclosing"] != ("block", block["seq"]):
                parts.extend(cont["parts"])
        text = text_of(parts, block["escape"])
        if not text:
            counts["empty"] = counts.get("empty", 0) + 1
            continue
        parent = parent_of(block)
        concept = str(block["concept"] or "")
        notes.append(Note(concept=concept, taxonomy=taxonomy(concept), local_name=local(concept),
                          kind=kind_of(concept), seq=int(block["seq"]), top_level=parent is None,
                          parent_seq=parent, period_end=reader.contexts.get(block["context"] or ""),
                          text=text, chars=len(text)))
    return notes, counts


def top_level_chars(notes: list[Note]) -> int:
    """The document's note text in characters, counted once: the census's ``block chars`` column."""
    return sum(n.chars for n in notes if n.top_level)

"""The note blocks of an annual report's inline XBRL: the filer's own ``ix:nonNumeric`` text blocks (#753).

``rcdata/build/fundamentals/ixbrl.py`` reads the same document's **numeric** facts (``ix:nonFraction``). This reads
the text ones, which is where a filer's prose about its debt, its segments and its accounting policies actually is.

**Why the filer's own blocks and not a heading regex.** Measured on 20 stored annual reports: a regex over heading
lines found a debt note in 8 of 20 with 3 false positives -- "Corporate debt securities" is an investment the
company *owns*, not its borrowing -- while the filer's own text block found 16 of 20 with none wrong. Over all
5,149 stored annual reports, 99.4% carry inline XBRL and 78.9% a debt note block. A tagged block is the filer's
statement of where a note starts and ends; a heading is our guess at it.

**Which elements are kept.** A fact whose local name ends in ``TextBlock`` or ``Explanatory``, which covers both
taxonomies (us-gaap ``...TextBlock``, IFRS ``DisclosureOf...Explanatory``). Every other ``ix:nonNumeric`` -- the
cover facts: ticker, exchange, filer category, auditor name -- is left out and **counted**, so "nothing was
tagged" and "we kept none of it" never look alike in a build report.

**Continuations.** A block longer than its one element continues in the element its ``continuedAt`` names, which
carries a ``continuedAt`` of its own, and so on: the text is the chain, in order. Every way a chain can be wrong
is counted rather than raising, looping or silently losing text -- a missing id, a repeated id, two ids the same,
two blocks claiming one continuation, a continuation nobody claims.

**Nesting, and why the rows carry it.** The census found a median of 76 blocks but 23 top-level per filing: filers
tag a note and then tag its subsections inside it, so the same sentence belongs to two or three blocks. Flat, the
stored bytes and every character count roughly triple. So each row says whether it is ``top_level`` and, if not,
which ``seq`` encloses it, and the census's "block chars" column -- what acceptance compares against -- sums the
top-level rows only (:func:`top_level_chars`).

**Why this module is here and not under ``rcdata.build``.** ``rcdata/build/__init__.py`` builds the dataset
registry and imports duckdb and httpx. The serving side of #757 needs :data:`NOTES_VERSION` and :func:`kind_of`
to write and read a row, and a board may want ``kind`` to pick a note; neither should pay for the builder. So this
sits beside ``rcdata/sec_sections.py``, for the same reason, and imports **nothing but the standard library**. The
builder's ``rcdata/build/sec_forms/`` imports it.

No network, no model call, no taxonomy or label file: a concept is stored as filed, and a board that wants a nicer
name makes one.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, replace
from datetime import date
from html.parser import HTMLParser

#: The version of this reader's output, stored on every row. Bump it when the kept set, the stitching or the text
#: normalisation changes, because stored rows cannot be compared across two of them. It does **not** cover
#: ``html.parser``'s own behaviour, which can change with the Python version; a stored row's text is reproducible
#: only on the interpreter that wrote it, which is why the builder's Python is pinned by the deployment and not
#: by this string.
NOTES_VERSION = "notes-1"

#: A fact is a note block when its local name ends in one of these (the census's rule, §3 of the plan).
BLOCK_SUFFIXES = ("TextBlock", "Explanatory")

#: Tags that end a line. ``td`` and ``th`` are here because a filer's table has no whitespace between cells, and
#: without them a debt table reads ``Term loan$5002025``: one line per cell is ugly and legible, which is the
#: trade this layer wants (the boards parse, we store).
_BREAK_TAGS = frozenset(("p", "div", "br", "tr", "td", "th", "li", "ul", "ol", "table", "thead", "tbody", "tfoot",
                         "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article", "caption"))
#: Their text is markup, not prose, and a filer does put a ``<style>`` block inside a note.
_DROP_TAGS = frozenset(("script", "style", "noscript"))
_SPACES = re.compile(r"[^\S\n]+")
#: Runs of line breaks become one. The breaks come from the filer's markup, and one either way means a block's
#: ``chars`` says the same thing however deep the tags went.
_NEWLINES = re.compile(r"\n+")
#: The HTML tags a filer's note is written with. A name outside this set is not markup, whatever it looks like:
#: ``<LIBOR + 2%>`` and ``<Table of Contents>`` are prose, and the first two attempts at this module deleted both.
_HTML_TAGS = frozenset((
    "a", "article", "b", "big", "blockquote", "br", "caption", "center", "col", "colgroup", "dd", "div", "dl",
    "dt", "em",
    "font", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "i", "img", "label", "li", "nobr", "ol", "p", "pre", "s",
    "script", "section", "small", "span", "strike", "strong", "style", "sub", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "u", "ul",
))
#: A tag whose presence means the block really is a document: a filer's escaped note has paragraphs or a table.
#: Without one of these, a lone ``<b>`` is somebody writing about markup, and the text is left exactly as filed.
_STRUCTURAL_TAGS = frozenset(("p", "div", "br", "table", "tr", "td", "th", "li", "ul", "ol", "blockquote"))
#: A tag-shaped run, for the rare block whose markup arrived **escaped** (``&lt;p&gt;``) and so reached us as
#: text. Narrow on three counts, because every character between two angle brackets may be somebody's sentence:
#: the ``<`` is followed immediately by the name, the name must be in :data:`_HTML_TAGS`, and anything after it
#: must be attribute-shaped (``name="value"``). So ``<p>``, ``</p>``, ``<br/>`` and ``<span class="x">`` match,
#: while ``a<b and c>d``, ``EBITDA<x and leverage>y``, ``<LIBOR + 2%>``, ``a < b``, ``<=``, ``<3`` and ``<1>`` do
#: not. Both earlier versions of this module ate at least one of those and counted the loss as a success.
_ESCAPED_TAG = re.compile(r"""<(/?)([a-zA-Z][a-zA-Z0-9]{0,9})
                              ((?:\s+[a-zA-Z_:][\w.:-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>]*))*)\s*/?>""",
                          re.VERBOSE)
#: One entity, with its semicolon. ``html.unescape`` also accepts the legacy names without one, which turns the
#: query string ``?id=1&copy=2`` into ``?id=1©=2``; the second pass must not do that to a note.
_ENTITY = re.compile(r"&(#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")
#: What a tag still looks like after the escaped-markup pass. The pattern above is deliberately narrow -- it
#: refuses a valueless attribute (``<td nowrap>``) and an HTML comment, and widening it to take them would also
#: take ``<Table of Contents>``, which is somebody's sentence. So the leftovers stay in the text and are counted.
_TAGGISH = re.compile(r"</?[a-zA-Z]|<[!?]")
#: An escaped ``<style>`` or ``<script>`` opening. The real-markup path drops the text inside one through
#: :data:`_DROP_TAGS`; without :func:`_drop_escaped` the escaped path would store a stylesheet as a note's prose.
#: The name must be the whole name, so ``<style-guide>`` -- which a note really does mention -- is not an opening.
_ESCAPED_DROP = re.compile(r"<(script|style|noscript)(?:\s[^<>]*)?>", re.IGNORECASE)
#: Its closing, one pattern per name, so a ``<style>`` is not closed by a ``</script>``.
_ESCAPED_DROP_END = {n: re.compile(r"</%s\s*>" % n, re.IGNORECASE) for n in ("script", "style", "noscript")}


def _drop_escaped(raw: str) -> str:
    """``raw`` without its escaped ``<style>`` / ``<script>`` blocks, including the text inside them.

    A scan rather than one ``re.sub`` with ``.*?``: a block holding many openings that nothing closes makes that
    pattern re-scan to the end of the text once per opening, which is quadratic -- measured at 12.7 s on a 176 KB
    block of 16,000 unclosed openings, and that is a shape a filer's broken HTML can reach in a 5,149-document
    pass. Here each closing is found once, from where the last one ended, and an opening nothing closes ends the
    scan and leaves the rest of the text exactly as it was.
    """
    low, out, at = raw.lower(), [], 0
    while True:
        open_at = _ESCAPED_DROP.search(raw, at)
        if open_at is None:
            break
        end = _ESCAPED_DROP_END[open_at.group(1).lower()].search(low, open_at.end())
        if end is None:
            break
        out.append(raw[at:open_at.start()])
        at = end.end()
    out.append(raw[at:])
    return "".join(out)


def local(qname: str | None) -> str:
    """``us-gaap:DebtDisclosureTextBlock`` -> ``DebtDisclosureTextBlock``; ``ix11:nonNumeric`` -> ``nonNumeric``."""
    return str(qname or "").rsplit(":", 1)[-1]


def taxonomy(qname: str | None) -> str:
    """``us-gaap:DebtDisclosureTextBlock`` -> ``us-gaap``; a name with no prefix has none."""
    name = str(qname or "")
    return name.rsplit(":", 1)[0] if ":" in name else ""


def is_block(qname: str | None) -> bool:
    return local(qname).endswith(BLOCK_SUFFIXES)


def kind_of(qname: str | None) -> str:
    """``policy`` / ``table`` / ``disclosure`` -- what the filer's own name says the block is.

    ``policy``: an accounting policy, in either taxonomy. The IFRS test is the words *accounting polic* anywhere in
    an ``Explanatory`` name, not one spelling of it: ``DescriptionOfAccountingPolicyForBorrowingsExplanatory`` and
    ``DisclosureOfMaterialAccountingPolicyInformationExplanatory`` are both policies, and a rule written against
    the first alone calls the second a disclosure. ``table``: the tabular part a filer tags separately, which
    holds the same numbers as the note around it. Everything else is a ``disclosure``, which is the note itself.
    """
    name = local(qname)
    if name.endswith(("PolicyTextBlock", "PoliciesTextBlock")):
        return "policy"
    if name.endswith("Explanatory") and re.search(r"AccountingPolic", name):
        return "policy"
    if name.endswith(("TableTextBlock", "TablesTextBlock")):
        return "table"
    return "disclosure"


def text_of(parts: list[str], counts: dict | None = None) -> str:
    """The parts of one block as stored text: no tags, no repeated line breaks, no trailing space on a line.

    The parser has already taken the real tags out, so what arrives here is prose, and prose keeps its angle
    brackets: a note says "customers below < 5% of revenue", "EBITDA<x and leverage>y", "a floor of
    <LIBOR + 2%>". None of that is markup and none of it is touched.

    The one exception is a filer that **escaped** its own markup, which reaches us as the text ``<p>``. A block is
    treated as that only when it holds a tag-shaped run (see :data:`_ESCAPED_TAG`) whose name is a real HTML tag
    **and** at least one of those is structural -- a paragraph, a line break, a table, a list item. A lone
    ``<b>`` in a sentence about markup is not a document, so nothing is removed and the text is stored as filed.
    What was recognised is counted either way: ``escaped_markup`` when the block was treated as a document,
    ``tag_shaped_text`` when it was left alone, so neither decision is invisible in a build report.

    A block that was escaped was escaped whole, so its entities were escaped twice (``&amp;nbsp;``) and the parser
    undid one pass. The second pass runs here, and only on that block, and only on entities written with their
    semicolon -- ``html.unescape`` over the whole string would turn ``?id=1&copy=2`` into ``?id=1©=2``.
    """
    raw = "".join(parts)
    if "<" in raw:
        found = [m for m in _ESCAPED_TAG.finditer(raw) if m.group(2).lower() in _HTML_TAGS]
        structural = any(m.group(2).lower() in _STRUCTURAL_TAGS for m in found)
        if found and structural:
            raw = _drop_escaped(raw)
            found = [m for m in _ESCAPED_TAG.finditer(raw) if m.group(2).lower() in _HTML_TAGS]
            out, at = [], 0
            for m in found:
                out.append(raw[at:m.start()])
                out.append("\n" if m.group(2).lower() in _BREAK_TAGS else "")
                at = m.end()
            out.append(raw[at:])
            stripped = "".join(out)
            raw = _ENTITY.sub(lambda m: _html.unescape(m.group(0)), stripped)
            if counts is not None:
                counts["escaped_markup"] = counts.get("escaped_markup", 0) + 1
                # Asked of the text *before* the entity pass, which turns the note's own ``&lt;`` into a ``<``:
                # "a floor of &lt;LIBOR + 2%&gt;" is a clean block whose prose happens to end up with brackets in
                # it, and asking afterwards called every one of those a leftover tag.
                if _TAGGISH.search(stripped):
                    # The block was treated as a document and still holds something tag-shaped: a valueless
                    # attribute, an HTML comment, a prefixed tag, a name this module does not know. Its text is
                    # stored with that in it, so the row is readable but not clean, and a build report that said
                    # only ``escaped_markup`` would call it a success.
                    counts["escaped_markup_residue"] = counts.get("escaped_markup_residue", 0) + 1
        elif found and counts is not None:
            counts["tag_shaped_text"] = counts.get("tag_shaped_text", 0) + 1
    lines = [_SPACES.sub(" ", line).strip() for line in raw.split("\n")]
    return _NEWLINES.sub("\n", "\n".join(lines)).strip()


@dataclass(frozen=True)
class Note:
    """One note block of one document. ``seq`` is the order the blocks open in the file, from 0."""

    concept: str            #: as filed, with its prefix: ``us-gaap:DebtDisclosureTextBlock``
    taxonomy: str           #: ``us-gaap``, ``ifrs-full``, or the filer's own prefix
    local_name: str         #: the name without its prefix
    kind: str               #: policy / table / disclosure
    seq: int
    top_level: bool         #: no kept block encloses it, so its text is counted once
    parent_seq: int | None  #: the enclosing kept block's ``seq``, or None when ``top_level``
    context_ref: str | None  #: the fact's own ``contextRef``, as filed
    period_end: date | None
    text: str
    chars: int

    def row(self, cik: int | str, accession: str, document: str) -> dict:
        """The stored row. The key is ``(cik, accession, document, concept, seq)``.

        ``document`` is in the key because ``seq`` counts within one file: an accession can hold more than one
        inline-XBRL document, each starting again at 0, and without it the second one's rows would collide with
        the first's (#754 parses one document at a time and passes its name).

        ``context_ref`` is stored but not in the key, because it is the only thing that tells two otherwise
        identical rows apart: a combined-registrant filing tags the same concept once per legal entity, same
        period end, differing only by context. Nothing here resolves a context to its entity -- that needs the
        dimensions, and this layer stores text.
        """
        return {"cik": int(cik), "accession": str(accession), "document": str(document), "concept": self.concept,
                "taxonomy": self.taxonomy, "local_name": self.local_name, "kind": self.kind, "seq": self.seq,
                "top_level": self.top_level, "parent_seq": self.parent_seq, "context_ref": self.context_ref,
                "period_end": self.period_end, "text": self.text, "chars": self.chars,
                "rules_version": NOTES_VERSION}


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


class _Reader(HTMLParser):
    """One pass over the document: the contexts, the kept blocks, and every continuation element by id.

    The text of a tag that encloses another kept block is kept whole, as the page reads it, so a parent block holds
    its children's words too; ``top_level`` is what stops that being counted twice. Tag names arrive lower-cased.

    Every open ``ix:nonNumeric`` and ``ix:continuation`` is on one stack, and a fact we do not keep is on it as a
    ``skip`` frame: it collects no text, but it has to be there, or its end tag would close the block it sits
    inside and every later row's nesting would be wrong. Each frame carries the ids of its ancestors, which is how
    the stitch knows a chain element it has already read through its parent.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts: dict[str, date | None] = {}
        self.blocks: list[dict] = []
        self.conts: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        self._stack: list[dict] = []
        self._ctx: str | None = None
        self._field: str | None = None
        self._ctx_text: list[str] = []
        self._drop = 0          # inside <script>/<style>: markup, not prose
        self._exclude = 0       # inside ix:exclude: the spec's way of cutting a page header out of a fact
        self._next_id = 0

    def _count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def _frame_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # ── tags ───────────────────────────────────────────────────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        name, a = local(tag), {k: v for k, v in attrs}
        if name == "nonnumeric" and ":" in tag:
            self._open_fact(a)
            return
        if name == "continuation" and ":" in tag:
            self._open_cont(a)
            return
        if name == "exclude" and ":" in tag:
            self._exclude += 1
            self._count("excluded")
            return
        if tag in _DROP_TAGS:
            self._drop += 1
            return
        if name == "context":
            self._ctx = a.get("id")
        elif name in ("enddate", "instant") and self._ctx is not None:
            self._field, self._ctx_text = name, []
        if tag in _BREAK_TAGS and self._stack:
            self._add("\n")

    def handle_startendtag(self, tag, attrs):
        """``<br/>`` never reaches ``handle_endtag``, and neither does an empty ``ix:`` element."""
        name = local(tag)
        if ":" in tag and name in ("nonnumeric", "continuation"):
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)
            return
        if ":" in tag and name == "exclude":
            return
        if tag in _BREAK_TAGS and self._stack:
            self._add("\n")

    def handle_data(self, data):
        if self._field is not None:
            self._ctx_text.append(data)
        if not self._drop:
            self._add(data)

    def handle_endtag(self, tag):
        name = local(tag)
        if name == "nonnumeric" and ":" in tag:
            self._close("block")
            return
        if name == "continuation" and ":" in tag:
            self._close("cont")
            return
        if name == "exclude" and ":" in tag:
            self._exclude = max(0, self._exclude - 1)
            return
        if tag in _DROP_TAGS:
            self._drop = max(0, self._drop - 1)
            return
        if name in ("enddate", "instant") and self._field == name:
            self.contexts[self._ctx or ""] = _day("".join(self._ctx_text))
            self._field = None
        elif name == "context":
            # Recorded even with no end date, so that "this context has only a start date" and "this
            # ``contextRef`` names no context in the document" stay two different counts.
            if self._ctx:
                self.contexts.setdefault(self._ctx, None)
            self._ctx = None
        if tag in _BREAK_TAGS and self._stack:
            self._add("\n")

    # ── frames ─────────────────────────────────────────────────────────────────────────────────────────────────
    def _add(self, data: str) -> None:
        if self._exclude:
            return
        for frame in self._stack:
            if frame["what"] != "skip":
                frame["parts"].append(data)

    def _ancestors(self) -> tuple[int, ...]:
        return tuple(f["fid"] for f in self._stack if f["what"] != "skip")

    def _enclosing(self) -> tuple[str, object] | None:
        """What the element now opening sits inside: the innermost kept block, or a continuation.

        A fact we did not keep is transparent: a note tagged inside a cover fact belongs to whatever encloses
        *that*, because a cover fact is not a row and cannot be a parent.
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
            self._stack.append({"what": "skip", "fid": self._frame_id()})
            return
        if not is_block(concept):
            self._count("not_a_block")
            self._stack.append({"what": "skip", "fid": self._frame_id()})
            return
        block = {"what": "block", "fid": self._frame_id(), "seq": len(self.blocks), "concept": concept,
                 "continued_at": a.get("continuedat"), "context": a.get("contextref"),
                 "enclosing": self._enclosing(), "ancestors": self._ancestors(), "parts": []}
        self.blocks.append(block)
        self._stack.append(block)

    def _open_cont(self, a: dict) -> None:
        cont = {"what": "cont", "fid": self._frame_id(), "id": a.get("id"), "continued_at": a.get("continuedat"),
                "enclosing": self._enclosing(), "ancestors": self._ancestors(), "parts": []}
        if cont["id"]:
            if cont["id"] in self.conts:
                # Two elements with one id: the document is wrong and one of them will never be read. Counted,
                # because the text that is lost is a paragraph of somebody's note.
                self._count("continuation_duplicate_id")
            self.conts[cont["id"]] = cont
        else:
            self._count("continuation_no_id")
        self._stack.append(cont)

    def _close(self, what: str) -> None:
        """Close the innermost open frame of this kind, so a stray end tag closes nothing it did not open."""
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["what"] in (what, "skip"):
                del self._stack[i]
                return

    def finish(self) -> None:
        """What the document left open, which is how a truncated stored file shows up.

        An unclosed block swallows every later block as its child, so the whole filing's top-level sum is wrong.
        An unclosed ``ix:exclude`` is worse and quieter: every later block comes out empty, because nothing after
        it is collected at all. An unclosed ``<style>`` puts ``html.parser`` in CDATA mode and the rest of the
        document is never parsed. None of the three can be repaired here; all three are counted, and a key is
        added only when it happened, so ``unclosed: 0`` never appears in a report.
        """
        self.close()
        left = len([f for f in self._stack if f["what"] != "skip"])
        skipped = len([f for f in self._stack if f["what"] == "skip"])
        if left:
            self._count("unclosed", left)
        if skipped:
            self._count("unclosed_cover_fact", skipped)
        if self._exclude:
            self._count("unclosed_exclude", self._exclude)
        if self._drop:
            self._count("unclosed_markup_tag", self._drop)


def _chain(block: dict, conts: dict[str, dict], counts: dict) -> list[dict]:
    """The continuation elements of one block, in order; every way the chain can be wrong is counted."""
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


def _foreign(frames: set[int], related: set[int], row_of_fid: dict[int, int]) -> bool:
    """Is any of ``frames`` a frame belonging to a row this block is not related to?

    A frame belongs to a row either as that row's own element or as a continuation the row claims. ``related``
    holds this block and its whole line -- every row above it and every row below it -- because a note's own
    subsections and the note they are part of share their text by design, and that is not an overlap. Only a
    frame belonging to a row *off* this line means the same words are in two unrelated top-level rows.

    Both earlier versions of this check asked the wrong question and were caught by a healthy document: the
    first asked only about block frames, so an element inside another block's *continuation* was silent; the
    second asked about the frames enclosing this block, which a parent that encloses its child through a
    continuation is not -- so every multi-page note whose sub-block crosses a page break was reported as a fault.
    """
    for fid in frames:
        row = row_of_fid.get(fid)
        if row is not None and row not in related:
            return True
    return False


def parse(html: str, counts: dict | None = None) -> tuple[list[Note], dict]:
    """``(notes, counts)`` -- the note blocks of one inline-XBRL document, and what was left out and why.

    ``counts`` is the build report's half of the answer, and every key is a thing a reader could otherwise lose
    silently. A key appears only when it happened.

    What was left out: ``not_a_block`` (an ``ix:nonNumeric`` that is a cover fact), ``nil``, ``empty`` (a kept
    block whose text is blank once stitched), ``excluded`` (an ``ix:exclude``, the spec's page furniture).

    What the text did: ``escaped_markup`` (per block, a block whose markup arrived escaped and was turned back
    into prose), ``escaped_markup_residue`` (such a block that still holds something tag-shaped afterwards, so
    its stored text is readable but not clean), ``tag_shaped_text`` (a block holding something tag-shaped that was
    **not** treated as markup, so its text is stored exactly as filed).

    What the chain did: ``continuation_missing``, ``continuation_loop`` (per block that walks into one),
    ``continuation_duplicate_id``, ``continuation_no_id``, ``continuation_shared`` (per continuation two blocks
    both claim, so its text is in both rows and the top-level sum is long), ``continuation_doubled`` (a chain
    element enclosing an earlier element of its own chain, so one paragraph is stored twice in this row),
    ``continuation_orphan`` (a
    continuation no chain reaches, so its text is in none), ``continuation_overlap`` (a chain element sitting
    inside a frame belonging to a row off this block's line, same effect as shared).

    What the nesting did: ``parent_cycle`` (two kept blocks each the other's parent, broken at the lowest ``seq``
    of the cycle, which would otherwise leave the filing with no top-level row at all),
    ``parent_dropped_as_empty`` (a parent that cut no text, so the row was re-pointed at its nearest kept
    ancestor), ``child_text_outside_parent`` (a line of a child's text that is nowhere in the top-level row above
    it, so the sum is short by that much).

    What the document did: ``unclosed`` (blocks or continuations still open at the end -- a truncated file, after
    which every later block is a child of one of them), ``unclosed_cover_fact``, ``unclosed_exclude`` (after
    which no text is collected at all), ``unclosed_markup_tag`` (``html.parser`` stays in CDATA mode and the rest
    of the document is never read), ``context_missing`` (a ``contextRef`` naming no context in the document,
    which also counts under the next one), ``period_end_missing``.
    """
    counts = counts if counts is not None else {}
    reader = _Reader()
    reader.feed(html or "")
    reader.finish()
    for key, value in reader.counts.items():
        counts[key] = counts.get(key, 0) + value

    # Which block owns each continuation, so a block nested in a continuation knows its parent, and so a
    # continuation two blocks both claim is visible rather than quietly counted twice.
    owner: dict[str, int] = {}
    chains: dict[int, list[dict]] = {}
    claimed: set[int] = set()
    shared: set[str] = set()
    for block in reader.blocks:
        chain = _chain(block, reader.conts, counts)
        chains[block["seq"]] = chain
        for cont in chain:
            claimed.add(cont["fid"])
            if cont["id"] is not None:
                if cont["id"] in owner and owner[cont["id"]] != block["seq"]:
                    shared.add(cont["id"])   # counted per continuation, not per block that reached it
                owner.setdefault(cont["id"], block["seq"])
    if shared:
        counts["continuation_shared"] = counts.get("continuation_shared", 0) + len(shared)
    orphans = [c for c in reader.conts.values() if c["fid"] not in claimed]
    if orphans:
        counts["continuation_orphan"] = counts.get("continuation_orphan", 0) + len(orphans)

    by_block = {b["seq"]: b for b in reader.blocks}

    def parent_of(block: dict) -> int | None:
        """The innermost kept block enclosing this one.

        A block tagged inside a continuation belongs to the block that continuation continues -- that is what the
        owner map is for. A continuation nobody claims is transparent: we follow what encloses it instead.
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

    #: The row every frame belongs to: a block's own element, and each continuation the owner map gives a block.
    row_of_fid = {b["fid"]: b["seq"] for b in reader.blocks}
    for cid, cont in reader.conts.items():
        at = owner.get(cid)
        if at is not None:
            row_of_fid[cont["fid"]] = at
    #: Each block's whole line: itself, every row above it, every row below it. Computed once, from the parents
    #: alone, and bounded -- a document can put two blocks in each other's line, and this runs before the cycle
    #: among kept rows is broken.
    lineage: dict[int, set[int]] = {}
    for block in reader.blocks:
        at, up = parent_of(block), {block["seq"]}
        while at is not None and at not in up:
            up.add(at)
            at = parent_of(by_block[at])
        lineage[block["seq"]] = up
    related: dict[int, set[int]] = {b["seq"]: set(lineage[b["seq"]]) for b in reader.blocks}
    for seq, up in lineage.items():
        for above in up:
            related[above].add(seq)

    made: dict[int, Note] = {}
    for block in reader.blocks:
        parts = list(block["parts"])
        read_through = {block["fid"]}
        read_ancestors: list[set[int]] = []
        for cont in chains[block["seq"]]:
            ancestors = set(cont["ancestors"])
            # A chain element already inside this block, or inside an earlier element of the same chain, has
            # been collected once through that frame; appending it again would store the paragraph twice.
            if read_through & ancestors:
                continue
            if any(cont["fid"] in anc for anc in read_ancestors):
                # The other way round, and the one the guard above cannot see: this element *encloses* an element
                # of the same chain that was read earlier, so its own text already holds that paragraph and
                # appending it stores the paragraph twice. Stored as the chain reads it, because dropping either
                # piece would lose the rest of this element, and counted -- this row's ``chars`` is long.
                counts["continuation_doubled"] = counts.get("continuation_doubled", 0) + 1
            if _foreign(ancestors - read_through, related[block["seq"]], row_of_fid):
                # The element sits inside a frame belonging to a row off this block's line, so its words are in
                # two unrelated top-level rows and the document does not say which. Kept in both, as the page
                # reads, and counted: the top-level sum is long by that much.
                counts["continuation_overlap"] = counts.get("continuation_overlap", 0) + 1
            if parts and cont["parts"]:
                parts.append("\n")
            parts.extend(cont["parts"])
            read_through.add(cont["fid"])
            read_ancestors.append(ancestors)
        text = text_of(parts, counts)
        if not text:
            counts["empty"] = counts.get("empty", 0) + 1
            continue
        ref = block["context"] or ""
        if ref and ref not in reader.contexts:
            # Implies ``period_end_missing`` as well, and is counted apart because only this one means the
            # document is wrong rather than the period being an instant or open-ended.
            counts["context_missing"] = counts.get("context_missing", 0) + 1
        end = reader.contexts.get(ref)
        if end is None:
            counts["period_end_missing"] = counts.get("period_end_missing", 0) + 1
        concept = str(block["concept"] or "")
        parent = parent_of(block)
        made[block["seq"]] = Note(concept=concept, taxonomy=taxonomy(concept), local_name=local(concept),
                                  kind=kind_of(concept), seq=int(block["seq"]), top_level=parent is None,
                                  parent_seq=parent, context_ref=ref or None, period_end=end, text=text,
                                  chars=len(text))

    # A parent dropped as empty would leave ``parent_seq`` naming a row that does not exist, which #757 would
    # store as a dangling key. Re-point each row at its nearest kept ancestor instead, and if there is none it is
    # top-level -- which is true: no kept block holds its words.
    notes: list[Note] = []
    by_seq = {b["seq"]: b for b in reader.blocks}
    for seq in sorted(made):
        note, parent = made[seq], made[seq].parent_seq
        # ``parent == seq`` is reachable: a block claiming the continuation it sits inside makes the owner map
        # name the block itself. So the walk also leaves on that, and a walk that comes back to something it has
        # already asked about leaves with no parent -- which is the honest answer, not a guess.
        seen: set[int] = {seq}
        while parent is not None and (parent == seq or parent not in made):
            if parent in seen:
                parent = None
                break
            seen.add(parent)
            parent = parent_of(by_seq[parent])
        if parent != note.parent_seq:
            counts["parent_dropped_as_empty"] = counts.get("parent_dropped_as_empty", 0) + 1
            note = replace(note, parent_seq=parent, top_level=parent is None)
        notes.append(note)

    # Two kept blocks can each be the other's parent -- each one tagged inside the continuation the other claims
    # -- and then neither is top-level and the whole filing's text counts as nothing. Broken at the lowest ``seq``
    # of the cycle, which is made top-level, and counted: a sum of zero on a filing with notes in it is the kind
    # of number that gets read as "this filer tags nothing".
    final = {n.seq: n.parent_seq for n in notes}
    for i, note in enumerate(notes):
        at, seen = final[note.seq], {note.seq}
        while at is not None and at not in seen:
            seen.add(at)
            at = final.get(at)
        if at == note.seq:
            # ``at is not None`` is a different question: it is true for a row that merely *leads into* a cycle,
            # and cutting that row loose leaves the cycle standing and counts its text twice.
            counts["parent_cycle"] = counts.get("parent_cycle", 0) + 1
            notes[i] = replace(note, parent_seq=None, top_level=True)
            final[note.seq] = None

    # A child's own chain can reach outside the row that holds it, so part of its text is in no top-level row at
    # all and the acceptance sum is short by that much. Which is the claim, so it is the thing measured: the
    # child's words against the **top-level** row above it, not against its immediate parent -- a grandchild
    # whose chain leaves its parent but stays inside the grandparent costs the sum nothing.
    #
    # Asked one line at a time, and not of the child's text as one string. The child's words can be in the row
    # above it without being contiguous there -- the parent has its own paragraph between the two halves of a
    # sub-block that continues after a page break, and that is the ordinary shape of a long note. A line is the
    # unit the filer's own markup gives, and a line that is nowhere in the row above is text the sum has lost.
    rows = {n.seq: n for n in notes}
    for note in notes:
        at, seen = note.parent_seq, {note.seq}
        while at is not None and at not in seen and not rows[at].top_level:
            seen.add(at)
            at = rows[at].parent_seq
        if at is None or at in seen:
            continue
        above = rows[at].text
        if any(line and line not in above for line in note.text.split("\n")):
            counts["child_text_outside_parent"] = counts.get("child_text_outside_parent", 0) + 1
    return notes, counts


def top_level_chars(notes: list[Note]) -> int:
    """The document's note text in characters, counted once: the census's ``block chars`` column."""
    return sum(n.chars for n in notes if n.top_level)

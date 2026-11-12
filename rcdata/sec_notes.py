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

import re
from dataclasses import dataclass
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
#: A tag-shaped run, for the rare block whose markup arrived **escaped** (``&lt;p&gt;``) and so reached us as
#: text. Deliberately narrow, because prose keeps its own angle brackets: no space is allowed between the ``<``
#: and the name, and the name must start with a letter. So ``<p>``, ``</p>``, ``<br/>`` and ``<span class="x">``
#: match, while "customers below < 5% of revenue", "margins > 30%" and "a < b and b > c" do not. An earlier
#: version of this module ran ``sec_sections.strip_html`` over every block and deleted everything between a prose
#: ``<`` and the next ``>``, which took two paragraphs out of a debt note and counted nothing.
_ESCAPED_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(?:\s[^<>]*?)?/?>")


def local(qname: str | None) -> str:
    """``us-gaap:DebtDisclosureTextBlock`` -> ``DebtDisclosureTextBlock``; also ``ix11:nonNumeric`` -> ``nonNumeric``."""
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

    The parser has already taken the real tags out, so what arrives here is prose. The one exception is a filer
    that **escaped** its own markup, which reaches us as the text ``<p>``; a tag-shaped run is turned into a line
    break or dropped, and counted as ``escaped_markup``. Nothing else that looks like an angle bracket is touched:
    a note may say "customers below < 5% of revenue", and that is not markup.

    A block that was escaped was escaped whole, so its entities were escaped twice (``&amp;nbsp;``) and the parser
    undid one pass. The second pass runs here, and **only** where a tag-shaped run proves the block really was
    escaped -- unescaping every block twice would turn a note that writes ``&amp;lt;b&amp;gt;`` on purpose into
    one that appears to contain a tag.
    """
    raw = "".join(parts)
    if "<" in raw:
        def _tag(m: re.Match) -> str:
            return "\n" if m.group(2).lower() in _BREAK_TAGS else ""
        raw, found = _ESCAPED_TAG.subn(_tag, raw)
        if found:
            import html as _html
            raw = _html.unescape(raw)
            if counts is not None:
                counts["escaped_markup"] = counts.get("escaped_markup", 0) + 1
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
    period_end: date | None
    text: str
    chars: int

    def row(self, cik: int | str, accession: str, document: str) -> dict:
        """The stored row. The key is ``(cik, accession, document, concept, seq)``.

        ``document`` is in the key because ``seq`` counts within one file: an accession can hold more than one
        inline-XBRL document, each starting again at 0, and without it the second one's rows would collide with
        the first's (#754 parses one document at a time and passes its name).
        """
        return {"cik": int(cik), "accession": str(accession), "document": str(document), "concept": self.concept,
                "taxonomy": self.taxonomy, "local_name": self.local_name, "kind": self.kind, "seq": self.seq,
                "top_level": self.top_level, "parent_seq": self.parent_seq, "period_end": self.period_end,
                "text": self.text, "chars": self.chars, "rules_version": NOTES_VERSION}


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
        """What the document left open. An unclosed block swallows every later block as its child, so the whole
        filing's top-level sum is wrong -- a truncated stored file does this, and it must not be silent."""
        self.close()
        if self._stack:
            self._count("unclosed", len([f for f in self._stack if f["what"] != "skip"]))


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


def parse(html: str, counts: dict | None = None) -> tuple[list[Note], dict]:
    """``(notes, counts)`` -- the note blocks of one inline-XBRL document, and what was left out and why.

    ``counts`` is the build report's half of the answer, and every key is a thing a reader could otherwise lose
    silently: ``not_a_block`` (an ``ix:nonNumeric`` that is a cover fact), ``nil``, ``empty`` (a kept block whose
    text is blank once stitched), ``excluded`` (an ``ix:exclude``, the spec's page header), ``escaped_markup``,
    ``unclosed``, ``context_missing`` (a ``contextRef`` naming no context we read), ``period_end_missing``, and
    five chain faults: ``continuation_missing``, ``continuation_loop``, ``continuation_duplicate_id``,
    ``continuation_no_id``, ``continuation_shared`` (two blocks claiming one continuation, so its text is in both)
    and ``continuation_orphan`` (a continuation no chain reaches, so its text is in none).
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
    for block in reader.blocks:
        chain = _chain(block, reader.conts, counts)
        chains[block["seq"]] = chain
        for cont in chain:
            claimed.add(cont["fid"])
            if cont["id"] is not None:
                if cont["id"] in owner:
                    counts["continuation_shared"] = counts.get("continuation_shared", 0) + 1
                owner.setdefault(cont["id"], block["seq"])
    orphans = [c for c in reader.conts.values() if c["fid"] not in claimed]
    if orphans:
        counts["continuation_orphan"] = counts.get("continuation_orphan", 0) + len(orphans)

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

    notes: list[Note] = []
    for block in reader.blocks:
        parts = list(block["parts"])
        read_through = {block["fid"]}
        for cont in chains[block["seq"]]:
            # A chain element already inside this block, or inside an earlier element of the same chain, has
            # been collected once through that frame; appending it again would store the paragraph twice.
            if read_through & set(cont["ancestors"]):
                continue
            if parts and cont["parts"]:
                parts.append("\n")
            parts.extend(cont["parts"])
            read_through.add(cont["fid"])
        text = text_of(parts, counts)
        if not text:
            counts["empty"] = counts.get("empty", 0) + 1
            continue
        ref = block["context"] or ""
        if ref and ref not in reader.contexts:
            counts["context_missing"] = counts.get("context_missing", 0) + 1
        end = reader.contexts.get(ref)
        if end is None:
            counts["period_end_missing"] = counts.get("period_end_missing", 0) + 1
        concept = str(block["concept"] or "")
        notes.append(Note(concept=concept, taxonomy=taxonomy(concept), local_name=local(concept),
                          kind=kind_of(concept), seq=int(block["seq"]), top_level=parent_of(block) is None,
                          parent_seq=parent_of(block), period_end=end, text=text, chars=len(text)))
    return notes, counts


def top_level_chars(notes: list[Note]) -> int:
    """The document's note text in characters, counted once: the census's ``block chars`` column."""
    return sum(n.chars for n in notes if n.top_level)

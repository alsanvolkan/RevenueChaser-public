"""Relatedness profile v3 (#400, Relatedness v2 plan Step 1): one structured JSON per company.

The writer reads the whole Item 1 (10-K) / Item 4 (20-F), with no ``MAX_INPUT_CHARS``, plus the filing's segment note,
the SIC, sector and industry, and the company's ``chain_mentions`` names (Value Chain's keyword scan of the same
filing) as candidates, and returns one JSON object:

``summary`` (one or two sentences), ``segments`` (``name``, ``share``, ``source``, ``products``, ``end_markets``),
``customers`` (``name``, ``type``, ``share``, ``quote``, ``parent``, ``public``), ``suppliers`` (the same without
``share``), ``named_competitors``, ``business_model``, ``channel``, ``geography``, ``stage`` and ``keywords``
(at most 15).

``quote`` (the sentence a customer or supplier is named in) is read by Value Chain (#410): one extraction for both
boards. ``parent`` and ``public`` (v3c, #717) are hints for that board's name resolver, never an answer on their
own: a ``parent`` counts only when it matches a spine name, and ``public`` only narrows a search. 83.8% of the
party names a profile writes down have no row in the equity universe (measured 11 Nov 2026), and a subsidiary
named instead of its listed parent is the part of that a filing can itself resolve.

The object is stored whole in ``relatedness_profiles.profile_json`` (schema 13, #401); the board's filters
keep reading the attribute rows ``to_attributes`` derives from it (``config.PROFILE_FIELDS``).

Every function here is pure: no store, no network.
"""
from __future__ import annotations

import json
import re

PROMPT_VERSION = "v3c"
MAX_KEYWORDS = 15
MAX_LIST = 30
# A customer's or supplier's quote: one sentence, kept short so a reply of 30 parties fits ``token_budget``.
MAX_QUOTE = 300

# The closed sets the prompt offers for the enum fields. A reply outside its set keeps its own short label
# (``_label``), so a new kind of company is not forced into a wrong one; the board filters on the value as written.
BUSINESS_MODELS = ("product", "service", "subscription", "platform", "licensing", "financial", "resource", "mixed")
CHANNELS = ("direct", "indirect", "mixed")
GEOGRAPHIES = ("domestic", "regional", "global")
STAGES = ("shell", "development", "growth", "mature", "declining")
ENUM_FIELDS = ("business_model", "channel", "geography", "stage")
SEGMENT_SOURCES = ("table", "text")

SYSTEM = (
    "You are a financial analyst. You read a company's annual-report business description and return ONE JSON "
    "object describing what the company actually does. Use only what the text states; never invent a name, a "
    "number or a segment. Tags are short lowercase noun phrases (1-3 words).\n"
    "Schema:\n"
    "{\"summary\": \"one or two plain sentences\",\n"
    " \"segments\": [{\"name\": str, \"share\": number or null, \"source\": \"table\" | \"text\", "
    "\"products\": [tag], \"end_markets\": [tag]}],\n"
    " \"customers\": [{\"name\": str, \"type\": tag, \"share\": number or null, \"quote\": str, "
    "\"parent\": str or null, \"public\": true | false | null}],\n"
    " \"suppliers\": [{\"name\": str, \"type\": tag, \"quote\": str, \"parent\": str or null, "
    "\"public\": true | false | null}],\n"
    " \"named_competitors\": [str],\n"
    f" \"business_model\": one of {list(BUSINESS_MODELS)},\n"
    f" \"channel\": one of {list(CHANNELS)},\n"
    f" \"geography\": one of {list(GEOGRAPHIES)},\n"
    f" \"stage\": one of {list(STAGES)},\n"
    f" \"keywords\": [tag] (at most {MAX_KEYWORDS})}}\n"
    "Rules: a segment's share is its fraction of revenue (0 to 1) when the text or a table states it, else null; "
    "source is \"table\" only when the share comes from a segment table or note. A customer's or supplier's name is "
    "a named company, never a category; its type says what it is (\"distributor\", \"oem\", \"government\"); its "
    "share is the stated fraction of sales or purchases, else null; quote is the one sentence that names it, "
    f"copied as written (at most {MAX_QUOTE} characters). parent is the larger company the text says this party "
    "belongs to (\"Amazon Web Services, a subsidiary of Amazon.com\" -> \"Amazon.com\"), null when the text says "
    "nothing; public is true only when the text presents the party as a publicly traded company, false when it "
    "calls it private, and null when the text does not say. Never guess parent or public from what you know of the "
    f"company: null is the right answer when this filing is silent. Candidates, when given, are company names a keyword scan "
    "of this filing found: list one only when the text names it as a customer, supplier or competitor. A blank-check company or one with no operations has stage \"shell\"."
)


def system_prompt(version: str | None = None) -> str:
    """``SYSTEM`` with the prompt version appended, so a version bump misses the LLM cache (as v2's)."""
    return f"{SYSTEM}\n\nPrompt version: {version or PROMPT_VERSION}."


def _context_lines(context: dict | None) -> list[str]:
    c = context or {}
    out = []
    for key, label in (("sic", "SIC"), ("sector", "Sector"), ("industry", "Industry")):
        if c.get(key):
            out.append(f"{label}: {c[key]}")
    if c.get("segment_note"):
        out.append(f"Segment note (same filing):\n{c['segment_note']}")
    if c.get("candidates"):
        out.append("Candidates (names a keyword scan of this filing found): " + "; ".join(c["candidates"]))
    if c.get("filing_sentences"):
        out.append("Other sentences of the same filing that name customers, suppliers or concentration:\n"
                   + "\n".join(f"- {x}" for x in c["filing_sentences"]))
    if c.get("xbrl_customers"):
        out.append("Major customers (the filing's XBRL):\n" + "\n".join(f"- {x}" for x in c["xbrl_customers"]))
    return out


def build_user(name: str, text: str, context: dict | None = None, part: tuple[int, int] | None = None) -> str:
    """The user prompt of one call: the company, its context and the text (one chunk of it when ``part``)."""
    head = [f"Company: {name}", *_context_lines(context)]
    label = "Business description (10-K Item 1 / 20-F Item 4)"
    if part and part[1] > 1:
        label += f", part {part[0] + 1} of {part[1]}"
    return "\n".join(head) + f"\n\n{label}:\n{text or ''}"


# ── parse ───────────────────────────────────────────────────────────────────

def _tag(value) -> str:
    s = re.sub(r"\s+", " ", str(value or "").strip().lower()).strip(" .,;:-")
    return s if s and len(s) <= 60 else ""


def _tags(values, cap: int = MAX_LIST) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,;\n]", values)
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        s = _tag(v)
        if s and s not in out:
            out.append(s)
        if len(out) >= cap:
            break
    return out


def _name(value) -> str:
    s = re.sub(r"\s+", " ", str(value or "").strip()).strip(" ,;:")
    return s if s and len(s) <= 120 else ""


def _text(value, cap: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:cap]


def _flag(value) -> bool | None:
    """A party's ``public`` (v3c): the reply's boolean, or None for anything else, "unknown" included."""
    return value if isinstance(value, bool) else None


def _share(value) -> float | None:
    """A fraction 0..1; a percent (1 < x <= 100) is read as one. Anything else is None."""
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x or x < 0:
        return None
    if x > 1:
        x = x / 100.0 if x <= 100 else None
    return None if x is None else round(x, 4)


def _label(value) -> str:
    return _tag(value) if isinstance(value, str) else ""      # a number or a list is no enum value


def _segments(values) -> list[dict]:
    out, seen = [], set()
    for s in values if isinstance(values, list) else []:
        if not isinstance(s, dict):
            continue
        name = _name(s.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        src = _tag(s.get("source"))
        out.append({"name": name, "share": _share(s.get("share")),
                    "source": src if src in SEGMENT_SOURCES else "text",
                    "products": _tags(s.get("products")), "end_markets": _tags(s.get("end_markets"))})
    return out[:MAX_LIST]


def _parties(values, with_share: bool) -> list[dict]:
    out, seen = [], set()
    for p in values if isinstance(values, list) else []:
        if isinstance(p, str):
            p = {"name": p}
        if not isinstance(p, dict):
            continue
        name = _name(p.get("name"))
        key = resolve_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        row = {"name": name, "type": _tag(p.get("type")), "quote": _text(p.get("quote"), MAX_QUOTE),
               "parent": _name(p.get("parent")), "public": _flag(p.get("public"))}
        if with_share:
            row["share"] = _share(p.get("share"))
        out.append(row)
    return out[:MAX_LIST]


def empty() -> dict:
    return {"summary": "", "segments": [], "customers": [], "suppliers": [], "named_competitors": [],
            "business_model": "", "channel": "", "geography": "", "stage": "", "keywords": []}


def parse(raw) -> dict | None:
    """The model's reply as a normalised v3 profile, or ``None`` when it is not one (never raises).

    ``None`` means unusable: not JSON, not an object, or an object that carries nothing at all.
    """
    if isinstance(raw, str):
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    prof = {
        "summary": _text(data.get("summary"), 600),
        "segments": _segments(data.get("segments")),
        "customers": _parties(data.get("customers"), with_share=True),
        "suppliers": _parties(data.get("suppliers"), with_share=False),
        "named_competitors": _named(data.get("named_competitors")),
        **{f: _label(data.get(f)) for f in ENUM_FIELDS},
        "keywords": _tags(data.get("keywords"), MAX_KEYWORDS),
    }
    return prof if usable(prof) else None


def _named(values) -> list[str]:
    out, seen = [], set()
    for v in values if isinstance(values, list) else []:
        if isinstance(v, dict):
            v = v.get("name")
        name = _name(v)
        key = resolve_key(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out[:MAX_LIST]


def usable(prof: dict | None) -> bool:
    """True when the profile says something: a shell's (stage only) counts, an all-empty one does not."""
    return bool(prof) and any(prof.get(k) for k in empty())


def is_shell(prof: dict | None, sic=None) -> bool:
    """A blank check (SIC 6770) or a profile whose stage is ``shell``: no profile, no peers, no theme (#400)."""
    return str(sic or "").strip() == "6770" or (prof or {}).get("stage") == "shell"


# A company suffix dropped before two names are compared ("Apple Inc." and "Apple" are one counterparty).
_SUFFIX = re.compile(r"[\s,]+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|l\.p|ag|sa|s\.a|"
                     r"nv|n\.v|se|gmbh|holdings?|group)\.?$", re.I)


def resolve_key(name: str) -> str:
    """The key two spellings of one company share: lower case, punctuation folded, legal suffixes dropped.

    It only decides "the same name" inside one profile (the chunk merge's union). Resolving a name to a spine issuer
    is #410's resolver."""
    s = re.sub(r"\s+", " ", str(name or "").strip().lower())
    s = s.replace("&", " and ")
    prev = None
    while prev != s:
        prev = s
        s = _SUFFIX.sub("", s).strip(" .,")
    s = re.sub(r"[^\w ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── the board's attribute rows ──────────────────────────────────────────────

def to_attributes(prof: dict | None) -> dict[str, list[str]]:
    """The ``config.PROFILE_FIELDS`` lists the board filters and the current similarity read (until Step 2).

    products / end_markets: the union over segments; business_model: the enum; customer_types: the customers' types;
    value_chain_role: the channel; keywords: the keywords."""
    p = prof or {}

    def union(key):
        out = []
        for s in p.get("segments") or []:
            for v in s.get(key) or []:
                if v not in out:
                    out.append(v)
        return out[:MAX_LIST]

    def one(v):
        return [v] if v else []

    types = []
    for c in p.get("customers") or []:
        if c.get("type") and c["type"] not in types:
            types.append(c["type"])
    return {"products": union("products"), "end_markets": union("end_markets"),
            "business_model": one(p.get("business_model")), "customer_types": types,
            "value_chain_role": one(p.get("channel")), "keywords": list(p.get("keywords") or [])}


# ── chunks ──────────────────────────────────────────────────────────────────

# A text up to CHUNK_MAX characters is one call. A longer one is cut at its headings into chunks of at most CHUNK_MAX;
# a section that is itself longer (an overrun with no heading) is cut into fixed WINDOW-character windows.
CHUNK_MAX = 60_000
WINDOW = 40_000

_SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with", "&", "-"}


def _is_heading(line: str) -> bool:
    """A line that reads as a heading: short, no closing punctuation, every word capitalised but the small ones.

    The same family of headers the Value Chain splitter cuts Item 1 on (``ITEM 1. BUSINESS``), widened to the
    sub-headings inside Item 1 (``Building Technologies``)."""
    s = (line or "").strip()
    if not (3 <= len(s) <= 100) or s[-1] in ".;,:?!" or not re.search(r"[A-Za-z]", s):
        return False
    words = s.split()
    if len(words) > 12 or not words[0][:1].isupper() and not words[0][:1].isdigit():
        return False
    for w in words:
        core = w.strip("()\"'.,—–-")
        if not core or core.lower() in _SMALL or not core[0].isalpha():
            continue
        if not core[0].isupper():
            return False
    return True


def _sections(text: str) -> list[str]:
    cuts, pos = [], 0
    for line in text.splitlines(keepends=True):
        if pos and _is_heading(line):
            cuts.append(pos)
        pos += len(line)
    bounds = [0, *cuts, len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:]) if b > a]


def split_chunks(text: str) -> list[str]:
    """``text`` as the chunks the writer reads, in order; joined they are ``text`` again."""
    text = text or ""
    if len(text) <= CHUNK_MAX:
        return [text]
    out, cur = [], ""
    for sec in _sections(text):
        if len(sec) > CHUNK_MAX:
            if cur:
                out.append(cur)
                cur = ""
            out += [sec[i:i + WINDOW] for i in range(0, len(sec), WINDOW)]
        elif len(cur) + len(sec) > CHUNK_MAX:
            out.append(cur)
            cur = sec
        else:
            cur += sec
    if cur:
        out.append(cur)
    return out


# ── merge (avo's amendment 1) ───────────────────────────────────────────────

def _union_parties(profs, key: str, with_share: bool) -> list[dict]:
    by: dict[str, dict] = {}
    for _i, p, _n in profs:
        for c in p.get(key) or []:
            k = resolve_key(c.get("name"))
            if not k:
                continue
            row = by.setdefault(k, {"name": c["name"], "type": "", "quote": "", "parent": "", "public": None,
                                    **({"share": None} if with_share else {})})
            row["type"] = row["type"] or c.get("type") or ""
            row["quote"] = row["quote"] or c.get("quote") or ""
            row["parent"] = row["parent"] or c.get("parent") or ""
            if row["public"] is None:           # False is an answer; only None means no chunk said anything
                row["public"] = c.get("public") if isinstance(c.get("public"), bool) else None
            if with_share and c.get("share") is not None:
                row["share"] = c["share"] if row["share"] is None else max(row["share"], c["share"])
    return list(by.values())[:MAX_LIST]


def _union_segments(profs) -> list[dict]:
    by: dict[str, dict] = {}
    weight: dict[str, float] = {}
    for _i, p, n in profs:
        for s in p.get("segments") or []:
            k = resolve_key(s.get("name")) or s.get("name", "").lower()
            row = by.setdefault(k, {"name": s["name"], "share": None, "source": "text", "products": [],
                                    "end_markets": []})
            for f in ("products", "end_markets"):
                row[f] += [v for v in s.get(f) or [] if v not in row[f]]
            if s.get("share") is not None:
                weight[k] = weight.get(k, 0.0) + float(s["share"]) * n
    total = sum(weight.values())
    for k, row in by.items():
        if k in weight and total > 0:
            row["share"] = round(weight[k] / total, 4)
    return list(by.values())[:MAX_LIST]


def merge_chunks(chunks: list[tuple[dict | None, int]]) -> tuple[dict | None, dict]:
    """One profile from the chunks' ``(profile, chunk length)``, and per field where its value came from.

    summary: the first chunk's. segments: the first chunk with a segment whose ``source`` is ``table``, else the union
    by name with each share weighted by its chunk's length and renormalised to 1. customers / suppliers /
    named_competitors: the union by resolved name. Enum fields: the longest chunk that has a value. keywords: the
    union, the most frequent first (then first seen), capped at 15. An unusable chunk is left out; none usable is
    ``(None, {})``.
    """
    profs = [(i, p, int(n)) for i, (p, n) in enumerate(chunks) if usable(p)]
    if not profs:
        return None, {}
    if len(profs) == 1:
        i, p, _n = profs[0]
        return dict(p), {f: f"chunk:{i}" for f in empty() if p.get(f)}

    out, labels = empty(), {}
    for i, p, _n in profs:
        if p.get("summary"):
            out["summary"], labels["summary"] = p["summary"], f"chunk:{i}"
            break
    table = next(((i, p) for i, p, _n in profs if any(s.get("source") == "table" for s in p.get("segments") or [])),
                 None)
    if table is not None:
        out["segments"], labels["segments"] = list(table[1]["segments"]), f"table:chunk:{table[0]}"
    else:
        out["segments"], labels["segments"] = _union_segments(profs), "union"
    out["customers"] = _union_parties(profs, "customers", with_share=True)
    out["suppliers"] = _union_parties(profs, "suppliers", with_share=False)
    named: dict[str, str] = {}
    for _i, p, _n in profs:
        for name in p.get("named_competitors") or []:
            named.setdefault(resolve_key(name), name)
    out["named_competitors"] = [v for k, v in named.items() if k][:MAX_LIST]
    for f in ("customers", "suppliers", "named_competitors", "keywords"):
        labels[f] = "union"
    longest = sorted(profs, key=lambda x: (-x[2], x[0]))
    for f in ENUM_FIELDS:
        for i, p, _n in longest:
            if p.get(f):
                out[f], labels[f] = p[f], f"chunk:{i}"
                break
    count: dict[str, int] = {}
    first: dict[str, int] = {}
    for _i, p, _n in profs:
        for k in p.get("keywords") or []:
            count[k] = count.get(k, 0) + 1
            first.setdefault(k, len(first))
    out["keywords"] = sorted(count, key=lambda k: (-count[k], first[k]))[:MAX_KEYWORDS]
    return out, labels

"""The inline XBRL of an annual report (10-K, 20-F): revenue by segment, product and country, and major customers.

The facts are the document's own ``ix:nonFraction`` elements and their contexts (``ix:resources``); no taxonomy or
label file is read, so a member's label is its name split into words (``msft:IntelligentCloudMember`` is
"Intelligent Cloud"). Read here:

- **Segment revenue.** A revenue fact (``REVENUE_CONCEPTS``, in that order of preference per member) in USD whose
  context has exactly one dimension, on one of ``AXES`` (business segment, product or service, geography), for a
  year-long period; only the newest such year of the document (an annual report repeats the years before).
- **Major customers.** ``ConcentrationRiskPercentage1`` whose context names the customer on ``MajorCustomersAxis``
  (and, when it says, a customer concentration on ``ConcentrationRiskByTypeAxis``); the benchmark (revenue, accounts
  receivable) from ``ConcentrationRiskByBenchmarkAxis``; the share as the fraction the fact holds (``scale`` -2 on a
  displayed 12 is 0.12); the newest period only. A share above 1 is left out (counted), never guessed at.

Numbers as displayed (``number``): the transformation formats of the dot-decimal and comma-decimal kinds and the
zero dash; another format is left out (counted). A fact marked nil is left out.
"""
from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser

REVENUE_CONCEPTS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "Revenue")
AXES = {"StatementBusinessSegmentsAxis": "business", "ProductOrServiceAxis": "product",
        "StatementGeographicalAxis": "geographic"}
SHARE_CONCEPT = "ConcentrationRiskPercentage1"
CUSTOMER_AXIS = "MajorCustomersAxis"
TYPE_AXIS = "ConcentrationRiskByTypeAxis"
BENCHMARK_AXIS = "ConcentrationRiskByBenchmarkAxis"
YEAR_DAYS = (300, 400)
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")


def local(qname: str | None) -> str:
    return str(qname or "").rsplit(":", 1)[-1]


def label(member: str) -> str:
    """``msft:IntelligentCloudMember`` -> "Intelligent Cloud"; ``country:US`` -> "US"."""
    name = local(member)
    name = name[:-len("Member")] if name.endswith("Member") and len(name) > len("Member") else name
    return " ".join(_WORDS.findall(name)) or name


def number(text: str, fmt: str | None, scale: str | None, sign: str | None) -> float | None:
    """A displayed number as its value, or None for a format this reader does not know."""
    kind = local(fmt).replace("-", "").lower()
    raw = (text or "").strip()
    if kind in ("fixedzero", "zerodash") or (raw in ("-", "—", "–") and kind in ("", "numdotdecimal")):
        value = 0.0
    else:
        digits = raw.strip("()").replace(" ", "").replace(" ", "")
        if kind in ("numdotdecimal", "numcommadecimal", ""):
            if kind == "numcommadecimal":
                digits = digits.replace(".", "").replace(",", ".")
            else:
                digits = digits.replace(",", "")
        else:
            return None
        try:
            value = float(digits)
        except ValueError:
            return None
    try:
        value *= 10 ** int(scale or 0)
    except ValueError:
        return None
    return -value if (sign or "").strip() == "-" else value


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


class _Reader(HTMLParser):
    """The contexts, units and numeric facts of one document (tag names arrive lower-cased)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts: dict[str, dict] = {}
        self.units: dict[str, str] = {}
        self.facts: list[dict] = []
        self._ctx: dict | None = None
        self._unit: str | None = None
        self._field: str | None = None
        self._dim: str | None = None
        self._fact: dict | None = None
        self._depth = 0
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        name, a = local(tag), {k: v for k, v in attrs}
        if tag == "ix:nonfraction":
            if self._fact is not None:
                self._depth += 1
                return
            self._fact = {"name": a.get("name"), "context": a.get("contextref"), "unit": a.get("unitref"),
                          "scale": a.get("scale"), "sign": a.get("sign"), "format": a.get("format"),
                          "nil": str(a.get("xsi:nil") or "").lower() == "true"}
            self._depth, self._text = 1, []
        elif name == "context":
            self._ctx = {"id": a.get("id"), "dims": {}, "start": None, "end": None}
        elif name == "explicitmember" and self._ctx is not None:
            self._dim, self._text = a.get("dimension"), []
        elif name in ("startdate", "enddate", "instant") and self._ctx is not None:
            self._field, self._text = name, []
        elif name == "unit":
            self._unit = a.get("id")
        elif name == "measure" and self._unit is not None:
            self._field, self._text = "measure", []

    def handle_data(self, data):
        if self._fact is not None or self._dim is not None or self._field is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        name, text = local(tag), "".join(self._text).strip()
        if tag == "ix:nonfraction" and self._fact is not None:
            self._depth -= 1
            if self._depth == 0:
                self.facts.append({**self._fact, "text": text})
                self._fact = None
        elif name == "explicitmember" and self._dim is not None and self._ctx is not None:
            self._ctx["dims"][self._dim] = text
            self._dim = None
        elif name in ("startdate", "enddate", "instant") and self._field == name and self._ctx is not None:
            self._ctx["start" if name == "startdate" else "end"] = _day(text)
            self._field = None
        elif name == "context" and self._ctx is not None:
            if self._ctx["id"]:
                self.contexts[self._ctx["id"]] = self._ctx
            self._ctx = None
        elif name == "measure" and self._field == "measure" and self._unit is not None:
            self.units.setdefault(self._unit, local(text))
            self._field = None
        elif name == "unit":
            self._unit = None


def _year_long(ctx: dict) -> bool:
    start, end = ctx.get("start"), ctx.get("end")
    return bool(start and end and YEAR_DAYS[0] <= (end - start).days <= YEAR_DAYS[1])


def parse(html: str, counts: dict | None = None) -> tuple[list[dict], list[dict]]:
    """``(segments, customers)`` of one annual report's inline XBRL (empty when it has none)."""
    counts = counts if counts is not None else {}
    reader = _Reader()
    reader.feed(html or "")
    reader.close()
    found: list[tuple[dict, dict, float]] = []
    for f in reader.facts:
        ctx = reader.contexts.get(f["context"] or "")
        if f["nil"] or ctx is None:
            continue
        value = number(f["text"], f["format"], f["scale"], f["sign"])
        if value is None:
            counts["format_unknown"] = counts.get("format_unknown", 0) + 1
            continue
        found.append((f, ctx, value))
    return _segments(found, reader.units), _customers(found, counts)


def _segments(found, units) -> list[dict]:
    best: dict[tuple[str, str], tuple[int, dict]] = {}
    rows = []
    for f, ctx, value in found:
        concept = local(f["name"])
        if concept not in REVENUE_CONCEPTS or units.get(f["unit"] or "") != "USD" or len(ctx["dims"]) != 1:
            continue
        (axis, member), = ctx["dims"].items()
        if local(axis) not in AXES or not _year_long(ctx):
            continue
        rows.append((ctx["end"], REVENUE_CONCEPTS.index(concept), AXES[local(axis)], member, concept, value, ctx))
    if not rows:
        return []
    newest = max(r[0] for r in rows)
    for end, rank, axis, member, concept, value, ctx in rows:
        if end != newest:
            continue
        key = (axis, member)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, {"axis": axis, "member": member, "label": label(member), "concept": concept,
                                "value": value, "unit": "USD", "period_start": ctx["start"], "period_end": end})
    return [row for _, row in sorted(best.values(), key=lambda x: (x[1]["axis"], x[1]["member"]))]


def _benchmark(member: str | None) -> str | None:
    name = label(member or "") if member else None
    if not name:
        return None
    low = name.lower()
    return "revenue" if ("revenue" in low or "sales" in low) else \
        "accounts_receivable" if "receivable" in low else name


def _customers(found, counts) -> list[dict]:
    rows = []
    for f, ctx, value in found:
        if local(f["name"]) != SHARE_CONCEPT:
            continue
        dims = {local(a): m for a, m in ctx["dims"].items()}
        customer = dims.get(CUSTOMER_AXIS)
        kind = dims.get(TYPE_AXIS)
        if not customer or (kind and local(kind) != "CustomerConcentrationRiskMember") or ctx["end"] is None:
            continue
        if not 0 <= value <= 1:
            counts["share_over_one"] = counts.get("share_over_one", 0) + 1
            continue
        rows.append({"customer": label(customer), "member": customer, "benchmark": _benchmark(dims.get(BENCHMARK_AXIS)),
                     "share": value, "period_start": ctx["start"], "period_end": ctx["end"]})
    if not rows:
        return []
    newest = max(r["period_end"] for r in rows)
    out: dict[tuple, dict] = {}
    for r in rows:
        if r["period_end"] == newest:
            out.setdefault((r["member"], r["benchmark"]), r)
    return sorted(out.values(), key=lambda r: (r["customer"], r["benchmark"] or ""))

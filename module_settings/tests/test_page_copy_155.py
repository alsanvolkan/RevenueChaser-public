"""#155 + #141: the Data Health page speaks plain words and cites issues that exist.

avo's rule (25 Oct 2026): our INTERNAL short forms never reach the page. Market terms a reader
meets outside this project (SEC, ETF, 13F, CUSIP, CIK, FIGI, ISIN, SPX, LEAPS) stay allowed.

A bare ``#N`` means issue N of this repository (CLAUDE.md, "Where work happens"), so a number from
the private tracker is written ``alsanvolkan/RevenueChaser#N``. The test keeps it simple: every
citation in the dataset texts is qualified, or is an open issue ``docs/open_issues.json`` lists.

Store only: the seeded store is a temp DuckDB per store file, no provider is asked.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from RevenueChaser.core import datasets
from RevenueChaser.module_settings import boards, inventory, report
from RevenueChaser.module_settings.tests.test_identity_coverage_124 import FIXED, _seed

BANNED = ("OIPD", "OHLC", "D01", "COT", "NMF", "UW", "LLM", "SIC", "ATR", "RS")
_BANNED_RE = re.compile(r"\b(" + "|".join(BANNED) + r")\b")
_CITATION_RE = re.compile(r"(?<![\w/&])((?:[\w.-]+/[\w.-]+)?#(\d+))\b")
_OPEN_ISSUES = Path(__file__).resolve().parents[2] / "docs" / "open_issues.json"
_REGISTRY_TEXTS = ("label", "writer_note", "freshness_note", "coverage_note", "next_step")


def _visible(html: str) -> str:
    """The page's words: tags and their attributes dropped (an element id is not copy), text and
    the drill's script strings kept."""
    return re.sub(r"<[^>]+>", " ", html)


def _hits(name: str, text: str) -> list[str]:
    return [f"{name}: {m.group(1)} in ...{text[max(0, m.start() - 50):m.end() + 30]!r}"
            for m in _BANNED_RE.finditer(text)]


def _registry_texts():
    for spec in datasets.REGISTRY:
        for field in _REGISTRY_TEXTS:
            value = getattr(spec, field)
            if value:
                yield f"{spec.key}.{field}", value


def test_the_rendered_page_uses_no_internal_short_form(rawstore_tmp_db):
    _seed()
    page = _visible(report.render_settings_dashboard(inventory.collect(now=FIXED)))
    drill = json.dumps(inventory.ticker_detail("AAPL"))

    assert _hits("page", page) + _hits("drill", drill) == []


def test_the_texts_a_seeded_page_may_not_reach_use_no_internal_short_form_either():
    """The registry and the board cards are read directly: a dataset or a note the seed does not
    render still lands on the live page."""
    texts = list(_registry_texts())
    texts += [(f"{e['key']}.label", e["label"]) for e in boards.BOARD_INPUTS]
    texts += [(f"{e['key']}.price_basis", e["price_basis"]) for e in boards.BOARD_INPUTS]
    texts += [(f"drill.{t}", label) for t, label in inventory._DRILL_TABLE_LABELS.items()]
    texts += [(f"vendor.{k}", v) for k, v in inventory._VENDOR_SOURCE_LABELS.items()]
    texts += [(f"holdings.{k}", v) for k, v in inventory._HOLDINGS_PROVIDERS]

    assert [hit for name, text in texts for hit in _hits(name, text)] == []


def test_the_reviewed_labels_read_in_plain_words(rawstore_tmp_db):
    assert datasets.by_key("oipd").label == "Implied Probability chain"
    assert datasets.by_key("ohlc").label == "Legacy daily open/high/low/close cache (SPY fallback)"
    _seed()
    labels = {r["key"]: r["label"] for r in inventory.collect(now=FIXED)["rows"]}
    assert labels["oipd"] == "Implied Probability chain"
    assert labels["ohlc"] == "Legacy daily open/high/low/close cache (SPY fallback)"


def test_every_issue_citation_in_the_dataset_texts_is_qualified_or_an_open_issue(rawstore_tmp_db):
    """#141: ``#87`` here is SPX, not the private tracker's Identity R4 issue the text meant."""
    known = {int(i["number"]) for i in json.loads(_OPEN_ISSUES.read_text(encoding="utf-8"))["issues"]}
    _seed()
    texts = list(_registry_texts())
    texts += [(f"row.{r['key']}.note", r.get("note") or "") for r in inventory.collect(now=FIXED)["rows"]]

    bad = [f"{name}: {m.group(1)}" for name, text in texts for m in _CITATION_RE.finditer(text)
           if "/" not in m.group(1) and int(m.group(2)) not in known]
    assert bad == []
    assert any("alsanvolkan/RevenueChaser#87" in text for _, text in texts)

"""#394 (review finding VC-3): bullets end a sentence, and a sentence that says
"compete"/"competitor"/"rival" never yields a supplier or customer edge. No network, no store."""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import mentions, sentences

# NVDA 10-K Item 1: a competitor bullet runs into the next bullet, whose first word is "suppliers".
NVDA_BULLETS = ("• large cloud services companies with internal teams designing hardware and software that incorporate "
                "accelerated or AI computing functionality as part of their internal solutions or platforms, such as "
                "Alibaba Group, Alphabet Inc., Amazon, Inc., or Amazon, Baidu, Inc., Huawei, and Microsoft Corporation, "
                "or Microsoft; • suppliers of Arm-based CPUs and companies that incorporate hardware and software for CPUs")
# MRAM 10-K Item 1: "suppliers" names the competitors' product class, not a supplier.
MRAM_COMPETE = ("Our products compete with DRAM and NVSRAM suppliers such as Hynix, Micron, Winbond, Samsung, and "
                "several other smaller companies.")
ONTO_COMPETITORS = "Our principal competitors for advanced packaging inspection are KLA and Camtek Ltd."


def _pairs(text):
    return {(m["dst_ticker"], m["relation"]) for m in mentions.find_mentions(text)}


def _sents(text):
    return [text[a:b].strip() for a, b in sentences.spans(text)]


# ── bullets end a sentence ────────────────────────────────────────────────
def test_bullet_after_semicolon_ends_the_sentence():
    sents = _sents(NVDA_BULLETS)
    assert len(sents) == 2 and sents[1].startswith("suppliers of Arm-based")


@pytest.mark.parametrize("text", [
    "We buy wafers from TSMC ● Our customers include Dell ● We compete with Intel",
    "We buy wafers from TSMC\n- Our customers include Dell\n* We compete with Intel",
])
def test_bullet_markers_split(text):
    assert len(_sents(text)) == 3
    assert _pairs(text) == {("TSM", "supplier"), ("DELL", "customer"), ("INTC", "competitor")}


def test_hyphenated_word_at_line_start_is_not_a_bullet():
    assert len(_sents("suppliers of Arm\n-based CPUs and other parts.")) == 1


def test_nvda_hyperscalers_are_not_suppliers():
    pairs = _pairs(NVDA_BULLETS)
    assert not {p for p in pairs if p[1] == "supplier"}
    assert ("MSFT", "supplier") not in pairs and ("AMZN", "supplier") not in pairs


# ── a competitor sentence never gives a supplier / customer ──────────────
@pytest.mark.parametrize("text, ticker", [
    (MRAM_COMPETE, "MU"),
    (ONTO_COMPETITORS, "KLAC"),
    ("Our rivals, including Dell, purchase from the same foundries.", "DELL"),
    ("We are competing for the same customers as Intel.", "INTC"),
    ("Intel competes with us for the largest customers.", "INTC"),
])
def test_compete_sentence_is_competitor_only(text, ticker):
    pairs = _pairs(text)
    assert (ticker, "competitor") in pairs and {r for _, r in pairs} == {"competitor"}


def test_supplier_in_the_next_sentence_is_still_a_supplier():
    text = MRAM_COMPETE + " We purchase DRAM from Micron."
    assert _pairs(text) == {("MU", "competitor"), ("SSNLF", "competitor"), ("MU", "supplier")}

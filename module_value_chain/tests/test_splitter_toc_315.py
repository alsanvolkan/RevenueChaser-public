"""#315: a body heading with a stray space inside the word ("Item 1. Bus iness.", as the same filer writes
"TAB LE OF CONTENTS") is an Item 1 start, so the contents entry is not the only match; a contents list with no
body heading at all stays ``short``. Shapes from the issue's sec.gov reproductions (AAP, ALGM, ARCB, BRK-B, CPRT)."""
from __future__ import annotations

import pytest

from RevenueChaser.module_value_chain import config, splitter

BODY = "Unless the context otherwise requires, we are a retailer of automotive parts. " + "We sell parts. " * 150
RISK = "Our business is subject to many risks. " + "Risk text. " * 20
TOC = "Table of Contents \n PART I \n Item 1. \n Business \n 2 \n Item 1A. \n Risk Factors \n 7 \n Item 2. \n Properties \n 20 \n"


def _filing(heading: str, stop: str = "Item 1A. Risk Factors") -> str:
    return f"{TOC} Forward-looking statements. \n {heading} \n {BODY} \n {stop} \n {RISK}"


@pytest.mark.parametrize("heading", [
    "Item 1. Bus iness.",            # AAP
    "Item 1. Bu siness.",            # ALGM
    "ITEM 1. BUSINES S",             # ARCB
    "Item 1. Busines s Description",  # BRK-B
    "Item 1. Business",              # the clean heading: the rule the contents entry never beat
])
def test_body_heading_with_a_stray_space_is_the_start(heading):
    item, quality = splitter.slice_item(_filing(heading))
    assert quality == "ok" and len(item) >= config.MIN_ITEM_CHARS
    assert "Unless the context" in item[:40] and "Risk text" not in item   # a trailing "." or word of the heading may lead


@pytest.mark.parametrize("stop", ["Item 1A. Risk Fac tors", "ITEM 1A. RIS K FACTORS", "Item 1B. Unre solved Staff Comments"])
def test_stop_heading_with_a_stray_space_ends_the_slice(stop):
    item, quality = splitter.slice_item(_filing("Item 1. Bus iness.", stop))
    assert quality == "ok" and "Risk text" not in item and item.endswith("We sell parts.")


def test_contents_list_without_a_body_heading_stays_short():
    """CPRT: the only match is the contents entry, 402 characters before the next stop; nothing is faked."""
    toc = "TABLE OF CONTENTS \n Page \n PART I \n 1 \n Item 1 \n Business \n 2 \n Industry Overview \n 4 \n" \
          + "Operating and Growth Strategies \n 6 \n" * 12 + "Item 1A. \n Risk Factors \n 15 \n"
    item, quality = splitter.slice_item(f"{toc} Overview \n {BODY} \n Risk Factors \n {RISK}")
    assert quality == "short" and len(item) < config.MIN_ITEM_CHARS and item.startswith("2")

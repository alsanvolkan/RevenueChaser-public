"""#400 Step 1: chunking above 60k characters and the chunk merge rule (avo's amendment 1), on three fixture filings.

The fixtures are built here (deterministic) in the shape of the three filings the issue names: a conglomerate (five
segment headings, ~150k characters), a two-segment company (~105k) and a single-segment one whose Item 1 overran into
~130k characters with no heading at all.
"""
from __future__ import annotations

import random

from RevenueChaser.module_relatedness import profile_v3 as v3

_WORDS = ("the company designs manufactures sells products services customers markets revenue segment operations "
          "through our we its and of in for to with").split()


def _para(rng, n):
    out, size = [], 0
    while size < n:
        s = " ".join(rng.choice(_WORDS) for _ in range(18)).capitalize() + "."
        out.append(s)
        size += len(s) + 1
    return " ".join(out)


def _section(rng, title, n):
    return f"{title}\n" + "\n".join(_para(rng, 900) for _ in range(max(1, n // 900))) + "\n"


def conglomerate():
    rng = random.Random(1)
    return "".join(_section(rng, t, 30_000) for t in ("Aerospace Systems", "Building Technologies",
                                                      "Performance Materials", "Safety and Productivity Solutions",
                                                      "Corporate and Other"))


def two_segment():
    rng = random.Random(2)
    return "".join(_section(rng, t, 34_000) for t in ("Overview", "Consumer Segment", "Enterprise Segment"))


def single_overrun():
    rng = random.Random(3)
    return "Our Business\n" + " ".join(_para(rng, 900) for _ in range(140)) + "\n"


# ── chunking ────────────────────────────────────────────────────────────────

def test_a_text_up_to_60k_is_one_chunk():
    assert v3.split_chunks("x" * 60_000) == ["x" * 60_000]
    assert v3.split_chunks("") == [""]


def test_the_conglomerate_is_cut_at_its_headings():
    text = conglomerate()
    assert len(text) > 140_000
    chunks = v3.split_chunks(text)
    assert "".join(chunks) == text                         # nothing lost, nothing repeated
    assert len(chunks) >= 3 and all(len(c) <= v3.CHUNK_MAX for c in chunks)
    heads = ("Aerospace Systems", "Building Technologies", "Performance Materials",
             "Safety and Productivity Solutions", "Corporate and Other")
    assert all(c.startswith(heads) for c in chunks)        # every chunk opens on a heading


def test_the_two_segment_filing_keeps_each_section_whole():
    """Three ~35k sections: two together pass 60k, so each is its own chunk, cut at its heading."""
    text = two_segment()
    chunks = v3.split_chunks(text)
    assert "".join(chunks) == text
    assert [c.split("\n", 1)[0] for c in chunks] == ["Overview", "Consumer Segment", "Enterprise Segment"]


def test_an_overrun_with_no_heading_falls_back_to_40k_windows():
    text = single_overrun()
    assert len(text) > 120_000
    chunks = v3.split_chunks(text)
    assert "".join(chunks) == text
    assert all(len(c) <= v3.WINDOW for c in chunks) and len(chunks) == -(-len(text) // v3.WINDOW)


def test_heading_detection():
    assert v3._is_heading("Building Technologies")
    assert v3._is_heading("ITEM 1. BUSINESS")
    for line in ("The company designs products.", "and of in for to with our", "x" * 120, "", "2024"):
        assert not v3._is_heading(line), line


# ── merge ───────────────────────────────────────────────────────────────────

def _p(**kw):
    return {**v3.empty(), **kw}


def _seg(name, share=None, source="text", products=(), end_markets=()):
    return {"name": name, "share": share, "source": source, "products": list(products),
            "end_markets": list(end_markets)}


def test_one_chunk_is_its_own_profile():
    prof = _p(summary="s", keywords=["a"], stage="mature")
    merged, labels = v3.merge_chunks([(prof, 100)])
    assert merged == prof
    assert labels == {"summary": "chunk:0", "stage": "chunk:0", "keywords": "chunk:0"}   # only fields with a value


def test_summary_comes_from_the_first_chunk():
    merged, labels = v3.merge_chunks([(_p(summary="first"), 10), (_p(summary="second"), 1000)])
    assert merged["summary"] == "first" and labels["summary"] == "chunk:0"
    merged, _ = v3.merge_chunks([(_p(summary=""), 10), (_p(summary="second"), 1000)])
    assert merged["summary"] == "second"          # an empty first summary is not kept over a real one


def test_a_table_segment_chunk_wins():
    a = _p(segments=[_seg("Aero", 0.5), _seg("Build", 0.5)])
    b = _p(segments=[_seg("Aerospace", 0.4, "table", ["engines"]), _seg("Materials", 0.6, "table")])
    merged, labels = v3.merge_chunks([(a, 50_000), (b, 10_000)])
    assert [s["name"] for s in merged["segments"]] == ["Aerospace", "Materials"]
    assert labels["segments"] == "table:chunk:1"


def test_segments_union_by_name_with_length_weighted_renormalised_shares():
    a = _p(segments=[_seg("Consumer", 0.6, products=["phones"]), _seg("Enterprise", 0.4)])
    b = _p(segments=[_seg("consumer", 0.2, end_markets=["retail"]), _seg("Cloud", 0.8)])
    merged, labels = v3.merge_chunks([(a, 30_000), (b, 10_000)])
    seg = {s["name"].lower(): s for s in merged["segments"]}
    assert set(seg) == {"consumer", "enterprise", "cloud"}
    # weights 3:1 -> consumer 0.6*3+0.2*1=2.0, enterprise 1.2, cloud 0.8; total 4.0
    assert seg["consumer"]["share"] == 0.5 and seg["enterprise"]["share"] == 0.3 and seg["cloud"]["share"] == 0.2
    assert seg["consumer"]["products"] == ["phones"] and seg["consumer"]["end_markets"] == ["retail"]
    assert labels["segments"] == "union"
    assert abs(sum(s["share"] for s in merged["segments"]) - 1.0) < 1e-9


def test_segments_without_any_share_stay_unshared():
    merged, _ = v3.merge_chunks([(_p(segments=[_seg("A")]), 10), (_p(segments=[_seg("B")]), 10)])
    assert [s["share"] for s in merged["segments"]] == [None, None]


def test_customers_and_suppliers_union_by_resolved_name():
    a = _p(customers=[{"name": "Apple Inc.", "type": "oem", "share": 0.1, "quote": "Apple Inc. accounted ..."}],
           suppliers=[{"name": "TSMC", "type": "foundry", "quote": "q1"}])
    b = _p(customers=[{"name": "APPLE", "type": "", "share": 0.12, "quote": ""},
                      {"name": "Dell", "type": "oem", "share": None, "quote": "Dell ..."}],
           suppliers=[{"name": "tsmc", "type": "", "quote": ""}])
    merged, labels = v3.merge_chunks([(a, 10), (b, 10)])
    assert [c["name"] for c in merged["customers"]] == ["Apple Inc.", "Dell"]
    apple = merged["customers"][0]
    assert apple["share"] == 0.12 and apple["type"] == "oem" and apple["quote"] == "Apple Inc. accounted ..."
    assert merged["suppliers"] == [{"name": "TSMC", "type": "foundry", "quote": "q1", "parent": "", "public": None}]
    assert labels["customers"] == "union"


def test_parent_and_public_survive_the_chunk_merge():       # v3c, #717
    a = _p(customers=[{"name": "AWS", "type": "", "share": None, "quote": "", "parent": "", "public": False}])
    b = _p(customers=[{"name": "aws", "type": "", "share": None, "quote": "", "parent": "Amazon.com",
                       "public": True}])
    merged, _ = v3.merge_chunks([(a, 10), (b, 10)])
    aws = merged["customers"][0]
    # The first chunk that said anything wins, and ``False`` is something it said.
    assert aws["parent"] == "Amazon.com" and aws["public"] is False


def test_enum_fields_come_from_the_longest_chunk():
    a = _p(business_model="service", channel="direct", stage="growth")
    b = _p(business_model="product", channel="", stage="mature")
    merged, labels = v3.merge_chunks([(a, 10_000), (b, 50_000)])
    assert merged["business_model"] == "product" and merged["stage"] == "mature"
    assert merged["channel"] == "direct"          # the longest chunk said nothing: the next longest that did
    assert labels["business_model"] == "chunk:1" and labels["channel"] == "chunk:0"


def test_keywords_union_is_capped_at_15_by_frequency():
    a = _p(keywords=[f"a{i}" for i in range(12)] + ["common"])
    b = _p(keywords=["common"] + [f"b{i}" for i in range(12)])
    c = _p(keywords=["common", "b0"])
    merged, _ = v3.merge_chunks([(a, 10), (b, 10), (c, 10)])
    kw = merged["keywords"]
    assert len(kw) == 15 and kw[0] == "common" and kw[1] == "b0"     # 3 and 2 mentions first
    assert kw[2:] == [f"a{i}" for i in range(12)] + ["b1"]          # then first appearance


def test_unusable_chunks_are_left_out_of_the_merge():
    merged, labels = v3.merge_chunks([(None, 99_999), (_p(summary="s", stage="mature"), 10)])
    assert merged["summary"] == "s" and merged["stage"] == "mature"
    assert labels["summary"] == "chunk:1"
    assert v3.merge_chunks([(None, 1)]) == (None, {})

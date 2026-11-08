"""#400 Step 1: the v3 profile's prompt, parse and attribute rows (pure, no store, no network)."""
from __future__ import annotations

import json

from RevenueChaser.module_relatedness import config, profile_v3 as v3

REPLY = {
    "summary": "Designs graphics processors.",
    "segments": [{"name": "Compute & Networking", "share": 78, "source": "table",
                  "products": ["GPU", "networking"], "end_markets": ["Data Center"]},
                 {"name": "Graphics", "share": 0.22, "source": "nonsense", "products": ["gpu"],
                  "end_markets": ["gaming"]},
                 {"name": "graphics", "share": 0.5}],
    "customers": [{"name": "Foxconn Inc.", "type": "OEM", "share": "12%", "quote": "Customer A,  Foxconn ..."},
                  {"name": "FOXCONN", "type": "oem"}, "Dell Technologies"],
    "suppliers": [{"name": "TSMC", "type": "foundry", "quote": "We rely on TSMC."}],
    "named_competitors": ["AMD", {"name": "Intel Corporation"}, "Intel"],
    "business_model": "Product", "channel": "mixed", "geography": "global", "stage": "mature",
    "keywords": [f"k{i}" for i in range(20)],
}


def test_parse_normalises_every_field():
    p = v3.parse("```json\n" + json.dumps(REPLY) + "\n```")
    assert p["summary"] == "Designs graphics processors."
    seg = p["segments"]
    assert [s["name"] for s in seg] == ["Compute & Networking", "Graphics"]     # a repeated name is dropped
    assert seg[0]["share"] == 0.78 and seg[0]["source"] == "table"             # a percent reads as a fraction
    assert seg[1]["source"] == "text" and seg[1]["products"] == ["gpu"]       # an unknown source is text
    assert seg[0]["products"] == ["gpu", "networking"] and seg[0]["end_markets"] == ["data center"]
    assert [c["name"] for c in p["customers"]] == ["Foxconn Inc.", "Dell Technologies"]   # Foxconn once
    assert p["customers"][0] == {"name": "Foxconn Inc.", "type": "oem", "quote": "Customer A, Foxconn ...",
                                 "share": 0.12}
    assert p["suppliers"] == [{"name": "TSMC", "type": "foundry", "quote": "We rely on TSMC."}]
    assert p["named_competitors"] == ["AMD", "Intel Corporation"]
    assert (p["business_model"], p["channel"], p["geography"], p["stage"]) == ("product", "mixed", "global", "mature")
    assert len(p["keywords"]) == v3.MAX_KEYWORDS == 15


def test_unusable_replies_are_none():
    for raw in ("not json", "[1, 2]", "{}", json.dumps({"segments": [], "summary": " "}), None):
        assert v3.parse(raw) is None
    assert v3.parse(json.dumps({"stage": "shell"})) == {**v3.empty(), "stage": "shell"}


def test_share_reading():
    assert v3._share(0.3) == 0.3 and v3._share("30%") == 0.3 and v3._share(30) == 0.3
    for bad in (-1, 150, "n/a", None, float("nan")):
        assert v3._share(bad) is None


def test_resolve_key_folds_suffixes_and_punctuation():
    assert v3.resolve_key("Apple Inc.") == v3.resolve_key("APPLE") == "apple"
    assert v3.resolve_key("Procter & Gamble Co.") == v3.resolve_key("Procter and Gamble") == "procter and gamble"
    assert v3.resolve_key("Taiwan Semiconductor Manufacturing Company Limited") == "taiwan semiconductor manufacturing"
    assert v3.resolve_key("") == ""


def test_shell_rule():
    assert v3.is_shell(None, sic="6770") and v3.is_shell({"stage": "shell"})
    assert not v3.is_shell({"stage": "mature"}, sic="3674") and not v3.is_shell(None)


def test_attributes_feed_the_board_fields():
    attrs = v3.to_attributes(v3.parse(json.dumps(REPLY)))
    assert set(attrs) == set(config.PROFILE_FIELDS)
    assert attrs["products"] == ["gpu", "networking"]
    assert attrs["end_markets"] == ["data center", "gaming"]
    assert attrs["business_model"] == ["product"] and attrs["value_chain_role"] == ["mixed"]
    assert attrs["customer_types"] == ["oem"]
    assert len(attrs["keywords"]) == 15
    assert v3.to_attributes(None) == {f: [] for f in config.PROFILE_FIELDS}


def test_prompt_carries_version_context_and_part():
    assert v3.system_prompt().endswith("Prompt version: v3.")
    user = v3.build_user("NVIDIA Corp", "TEXT", {"sic": "3674", "sector": "Technology", "industry": "Semis",
                                                 "segment_note": "Compute 78%", "candidates": ["Dell", "HP"]},
                         part=(1, 3))
    assert user.startswith("Company: NVIDIA Corp\nSIC: 3674\nSector: Technology\nIndustry: Semis\n")
    assert "Segment note (same filing):\nCompute 78%" in user and "Dell; HP" in user
    assert "part 2 of 3:\nTEXT" in user
    assert "part" not in v3.build_user("X", "T", None, part=(0, 1))

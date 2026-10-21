"""Tests for the UW Interaction catalog — structural integrity.

The catalog is data: malformed entries must fail loudly in a unit test, not
surface as a confusing empty board at runtime.
"""
from RevenueChaser.module_uw_interaction.catalog import (
    TOOLS, FAMILY_ORDER, get_tool, path_params, tools_for_family, validate_catalog,
)


class TestCatalogStructure:
    def test_no_families_empty_and_all_ordered(self):
        assert FAMILY_ORDER
        assert all(tools_for_family(f) for f in FAMILY_ORDER)

    def test_every_tool_belongs_to_an_ordered_family(self):
        families = {t["family"] for t in TOOLS}
        assert families <= set(FAMILY_ORDER)

    def test_keys_unique_across_catalog(self):
        keys = [t["key"] for t in TOOLS]
        assert len(keys) == len(set(keys)), f"duplicate keys: {[k for k in keys if keys.count(k) > 1]}"

    def test_validate_catalog_is_clean(self):
        problems = validate_catalog()
        assert problems == [], f"catalog problems: {problems}"

    def test_get_tool_roundtrip(self):
        first = TOOLS[0]
        assert get_tool(first["key"]) == first
        assert get_tool("does_not_exist") is None

    def test_every_path_placeholder_has_a_param(self):
        for t in TOOLS:
            placeholders = path_params(t["path"])
            names = {p["name"] for p in t["params"]}
            assert placeholders <= names, f"{t['key']}: {placeholders - names}"


class TestParamSchema:
    def test_every_param_has_known_kind(self):
        for t in TOOLS:
            for p in t["params"]:
                assert p["kind"] in ("text", "int", "float", "bool", "select"), (
                    f"{t['key']}:{p['name']} bad kind {p['kind']!r}")
                assert "name" in p and p["name"]

    def test_required_params_are_declared(self):
        for t in TOOLS:
            for p in t["params"]:
                if p.get("required"):
                    assert p.get("in_path") or p.get("default"), (
                        f"{t['key']}:{p['name']} required but neither in_path nor default")

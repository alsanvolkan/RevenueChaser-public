"""#752: the item-cutting rule is one versioned module, and a board importing it does not pull in the builder.

What these tests are for, and what they deliberately are not:

- They pin the **seam** first. The rule's behaviour is already covered by the seven
  ``module_value_chain/tests/test_splitter_*.py`` files, which were written against it and keep passing against
  ``module_value_chain.splitter`` unchanged. Re-asserting the same outputs here from the same functions would be
  the function compared with itself, which proves nothing about a move.
- They pin what the move could silently break: the re-exported names are the **same objects**, the board's version
  is pinned rather than defaulted, an unknown version raises instead of falling back, and the rule module stays
  free of the builder's imports.
- The golden check at the end of the file is the exception, and it is not a seam test: it holds ``ours-1``'s output
  against what the pre-move code returned. It is here rather than beside the splitter tests because the version,
  not the board, is what it pins.

Red on the merge-base: ``rcdata.sec_sections`` does not exist there, so every test below fails at import. That is
the honest red-first result for a move, and the PR report says so rather than claiming a behaviour difference.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from RevenueChaser.module_value_chain import config as vc_config, splitter
from RevenueChaser.rcdata import sec_sections

from .sec_sections_cases import CASES, STRIP_HTML_INPUT


# ── the version seam ───────────────────────────────────────────────────────────────────────────────────────────
def test_the_board_is_pinned_to_ours_2_and_not_to_the_default():
    """The board names its version in its own code, so a later default change cannot move its text unseen.

    ``ours-2`` since #847 (plan v31 step 1b). ``DEFAULT_VERSION`` is still ``ours-1``, so this is also the test
    that the board is not riding the default: reading the default would give the other version outright.
    """
    assert splitter.VERSION == sec_sections.OURS_2 == "ours-2"
    assert splitter.VERSION in sec_sections.VERSIONS
    assert splitter.VERSION != sec_sections.DEFAULT_VERSION
    source = Path(splitter.__file__).read_text(encoding="utf-8")
    assert "VERSION = _rule.OURS_2" in source, "the board must pin its version, not inherit DEFAULT_VERSION"


def test_the_board_passes_its_version_and_does_not_ride_the_default():
    """Dropping ``version=VERSION`` from the wrapper must fail here, and nowhere else would notice.

    Written while ``DEFAULT_VERSION`` was the board's own version, when a wrapper that forgot to name its version
    returned exactly the same text and every other test in this file passed. It is kept word for word after #847
    moved the pin, because the default is what moves: the day ``DEFAULT_VERSION`` is bumped to the board's pin
    again, this is the only test that still says the board names it. The sentinel is a version that answers
    differently, so reading the default is visible whatever the default happens to be.
    """
    sentinel = sec_sections.Rule(version="sentinel", slice_item=lambda text, form="10-K": ("SENTINEL", "ok"),
                                 aif_business=lambda text: "SENTINEL", caps=False)
    text = ("Item 1. Business\n" + "We make widgets and sell them to customers worldwide. " * 200
            + "\nItem 1A. Risk Factors\nRisky.\n")
    with mock.patch.dict(sec_sections.RULES, {"sentinel": sentinel}, clear=False), \
            mock.patch.object(sec_sections, "DEFAULT_VERSION", "sentinel"):
        assert sec_sections.cut_item(text, "10-K") == ("SENTINEL", "ok"), "the sentinel has to be reachable"
        item, quality = splitter.slice_item(text, "10-K")
        assert (item, quality) != ("SENTINEL", "ok"), "the board must name its version, not take the default"
        assert quality == "ok" and item is not None and item.startswith("We make widgets")
        # And the board's pin, not ``DEFAULT_VERSION``'s rule either, which the sentinel alone cannot show.
        assert (item, quality) == sec_sections.cut_item(text, "10-K", version=splitter.VERSION)
        assert splitter.aif_business("annual information form\nDESCRIPTION OF THE BUSINESS\n" + "x " * 3000
                                     + "\nRISK FACTORS\n") != "SENTINEL"


def test_ours_1_caps_its_text_and_ours_2_does_not():
    """The cap is a property of the version, and a stored row was cut by the version that wrote it.

    ``ours-1`` caps at :data:`MAX_ITEM_CHARS` and may therefore answer ``capped``; ``ours-2`` caps nowhere
    (``sec_forms`` stores sections uncut, #757). Since #847 the board reads ``ours-2``, so nothing it cuts may
    come back ``capped`` -- see ``test_the_board_s_cut_no_longer_caps_or_says_capped``.
    """
    assert sec_sections.rule("ours-1").caps is True
    assert sec_sections.rule("ours-2").caps is False
    assert sec_sections.rule(splitter.VERSION).caps is False, "the board reads an uncapped cut since #847"


def test_the_board_s_cut_no_longer_caps_or_says_capped():
    """A body far over the cap comes back whole and ``ok``, not cut at 200,000 and ``capped`` (#847).

    This is the one behaviour change the pin move makes to this board's own output, and ``capped`` is not a
    cosmetic label: ``module_relatedness.locator.asks()`` sends a ``capped`` filing to a model to be re-cut, so a
    version that caps nowhere must not keep claiming it.
    """
    body = ("Item 1. Business\n" + "We make widgets and sell them to customers worldwide. " * 200
            + "\nItem 1A. Risk Factors\n" + "risk text\n" * 300)
    cap = 5_000  # under the body and over MIN_ITEM_CHARS: ``ours-1`` answers ``capped`` at this cap
    with mock.patch.object(sec_sections, "MAX_ITEM_CHARS", cap):
        assert sec_sections.cut_item(body, "10-K", version=sec_sections.OURS_1)[1] == "capped"
        item, quality = splitter.slice_item(body, "10-K")
        assert quality == "ok" and item is not None and len(item) > cap
        # The label follows the cut: S8 is ours-1's length-against-the-cap signal, and the same slice would be
        # ``ok;suspect:S8`` if the board's flagger had stayed ours-1's (the ``flag`` re-export still is).
        assert "S8" in splitter.flag(quality, body, item, "10-K")
        assert "S8" not in splitter.cut_flag(quality, body, item, "10-K", version=splitter.VERSION)


def test_an_unknown_version_raises_and_names_what_exists():
    with pytest.raises(ValueError) as e:
        sec_sections.rule("ours-99")
    assert "ours-99" in str(e.value) and "ours-1" in str(e.value)


def test_an_explicit_none_raises_and_is_not_the_default():
    """A ``rules_version`` column read back as NULL arrives here as ``None``.

    Leaving the argument out means :data:`DEFAULT_VERSION`, which is for a caller that only looks at a section.
    Passing ``None`` is a caller that meant to name a version and had nothing to name, which is exactly the case
    that must not quietly cut with whatever today's default is.
    """
    assert sec_sections.rule().version == sec_sections.DEFAULT_VERSION
    for call in (lambda: sec_sections.rule(None),
                 lambda: sec_sections.cut_item("Item 1. Business\nbody", "10-K", version=None),
                 lambda: sec_sections.cut_aif_business("annual information form", version=None)):
        with pytest.raises(ValueError):
            call()


def test_cut_item_and_cut_aif_business_refuse_an_unknown_version_too():
    """Both entry points go through ``rule()``; neither may fall back to a default and mislabel a stored section."""
    with pytest.raises(ValueError):
        sec_sections.cut_item("Item 1. Business\nbody", "10-K", version="ours-99")
    with pytest.raises(ValueError):
        sec_sections.cut_aif_business("annual information form", version="ours-99")


def test_every_declared_version_has_a_rule_and_every_rule_is_declared():
    assert set(sec_sections.VERSIONS) == set(sec_sections.RULES)
    for name, rule in sec_sections.RULES.items():
        assert rule.version == name, f"{name} carries the version string {rule.version!r}"
        assert callable(rule.slice_item) and callable(rule.aif_business)


def test_the_default_version_is_one_that_exists():
    assert sec_sections.DEFAULT_VERSION in sec_sections.VERSIONS


# ── the re-exports are the same objects, so the two paths cannot diverge ───────────────────────────────────────
@pytest.mark.parametrize("name", ["flag", "base", "strip_html"])
def test_the_board_re_exports_the_rule_module_s_own_function(name):
    assert getattr(splitter, name) is getattr(sec_sections, name)


@pytest.mark.parametrize("name", ["_LINE_START", "_LINE_END", "_TOC_NEXT", "_PAGE_NO", "_is_ref", "_line_item",
                                  "_AIF", "_AIF_START", "_AIF_END", "_SECTION", "_TAIL_REF", "_START_REF"])
def test_the_board_re_exports_the_rule_module_s_own_helpers(name):
    """``test_splitter_foreign_400.py`` asserts on ``splitter._LINE_START`` directly; the others are here so that a
    later copy of one into the board is caught instead of quietly forking the rule."""
    assert getattr(splitter, name) is getattr(sec_sections, name)


@pytest.mark.parametrize("name", ["DASH", "MIN_ITEM_CHARS", "MAX_ITEM_CHARS", "START_PATTERNS", "STOP_PATTERNS",
                                  "spaced"])
def test_the_board_config_re_exports_the_rule_module_s_constants(name):
    """Read as ``config.<name>`` by ``rcdata/build/fundamentals/filings.py``, ``module_relatedness/config.py`` and
    ``module_value_chain/history.py``, so they must keep resolving to one definition."""
    assert getattr(vc_config, name) is getattr(sec_sections, name)


def test_the_cap_the_cut_reads_is_the_rules_own_and_not_the_board_alias():
    """The cap is part of ``ours-1``, so that cut reads ``sec_sections.MAX_ITEM_CHARS``.

    ``module_value_chain.config.MAX_ITEM_CHARS`` is that same value re-exported -- read by
    ``rcdata/build/fundamentals/filings.py``, ``module_value_chain/history.py`` and ``module_relatedness`` to record
    or report the limit -- and rebinding the board's alias must not move the cut, because a stored slice was cut at
    the version's cap and nothing in ``filing_item`` says which cap that was. ``test_splitter_capped_260.py`` patches
    the rule's cap for the same reason.

    Asked of ``ours-1`` by name since #847: the board reads ``ours-2``, which caps nowhere, so the board's wrapper
    can no longer be the thing that shows a cap being read from the wrong module.
    """
    body = ("Item 1. Business\n" + "We make widgets and sell them to customers worldwide. " * 200
            + "\nItem 1A. Risk Factors\n" + "risk text\n" * 300)
    cap = 5_000  # under the body and over MIN_ITEM_CHARS, so the cut is capped rather than short
    whole, quality = sec_sections.cut_item(body, "10-K", version=sec_sections.OURS_1)
    assert quality == "ok" and whole is not None and len(whole) > cap

    with mock.patch.object(vc_config, "MAX_ITEM_CHARS", cap):
        item, quality = sec_sections.cut_item(body, "10-K", version=sec_sections.OURS_1)
    assert (item, quality) == (whole, "ok"), "the board's alias must not be the cap the cut reads"

    with mock.patch.object(sec_sections, "MAX_ITEM_CHARS", cap):
        item, quality = sec_sections.cut_item(body, "10-K", version=sec_sections.OURS_1)
    assert quality == "capped" and item is not None and len(item) == cap


def test_the_board_config_keeps_its_private_aliases():
    """Both spellings existed before the move and either may be read somewhere."""
    assert vc_config._DASH is vc_config.DASH
    assert vc_config._spaced is vc_config.spaced


# ── the rule module must not drag the builder into a board import ──────────────────────────────────────────────
_ALLOWED_IMPORTS = {"re", "dataclasses", "html", "__future__"}


def _imported_names(path: Path) -> set[str]:
    """The top-level name of every module ``path`` imports; a relative import keeps its dots, so it cannot pass."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            found.add("." * node.level + root if node.level else root)
    return found


def test_the_rule_module_imports_nothing_but_the_standard_library():
    """A board slicing a filing must not pay for duckdb, httpx and the dataset registry.

    Checked on the source rather than on ``sys.modules``, because another test in the same process may already
    have imported the builder for its own reasons.
    """
    assert _imported_names(Path(sec_sections.__file__)) <= _ALLOWED_IMPORTS, \
        f"{sorted(_imported_names(Path(sec_sections.__file__)) - _ALLOWED_IMPORTS)} would reach a board import"


def test_every_package_init_on_the_way_in_imports_nothing_either():
    """Importing ``RevenueChaser.rcdata.sec_sections`` runs the package inits above it first, so an import added to
    one of them would reach every board through this module. Both are empty today; this test is where that is
    noticed -- ``rcdata/__init__.py`` is a docstring and the repo-root one is empty. The scan reads source, so it cannot see a dynamic ``importlib.import_module`` or a module
    ``__getattr__`` -- a reviewer's point on PR 758, and the reason these files are kept bare rather than watched.
    """
    rcdata_init = Path(sec_sections.__file__).with_name("__init__.py")
    package_init = rcdata_init.parent.parent / "__init__.py"
    for init in (rcdata_init, package_init):
        extra = _imported_names(init) - _ALLOWED_IMPORTS
        assert not extra, f"{init.parent.name}/__init__.py now imports {sorted(extra)}"


# ── the golden check: ours-1 still gives what main's own code gave ─────────────────────────────────────────────
# Captured by running ``origin/main``'s ``module_value_chain/splitter.py`` against the inputs of
# ``sec_sections_cases.py`` on 12 Nov 2026, before the move. So these numbers come from the code being replaced,
# not from the replacement, which is what makes the check worth running. Three of them record a defect rather than
# a success -- ``split_word_item_1a`` is VFF's overrun (#755): the filer splits the word in its stop heading, our
# pattern does not see it, and the slice runs on into Item 1A. ``aif_description_of_business`` is one of the 28
# Annual Information Forms our single heading misses (#756), ``over_the_cap`` is the cap ``sec_forms`` removes
# (#757). The move must keep those too: changing them is a later, versioned change, not a side effect of a move.
#
# What the golden does NOT hold is worth saying, because it bounds what a green run here means: it reads one path
# per case and no stored filing, so a rule change inside a branch no case reaches would pass. PR 758's review found
# four such branches by mutating the rule, and the four cases added for them (the dash class, the two suspect
# signals, the AIF cap) close those; the real accuracy question -- is the slice the right section -- is #763's
# hand-checked set, not this file.
_GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "sec_sections_ours_1_golden.json").read_text(encoding="utf-8"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_golden_file_covers_every_case_and_nothing_else():
    assert set(_GOLDEN["cases"]) == {name for name, _, _ in CASES}
    assert _GOLDEN["rule_version"] == sec_sections.OURS_1


@pytest.mark.parametrize("name,text,form", CASES, ids=[c[0] for c in CASES])
def test_ours_1_matches_what_main_s_own_rule_returned(name, text, form):
    want = _GOLDEN["cases"][name]
    assert _sha(text) == want["input_sha256"], \
        f"the input {name!r} changed since the golden was captured; recapture it or leave it alone"

    item, quality = sec_sections.cut_item(text, form, version=sec_sections.OURS_1)
    assert quality == want["quality"]
    assert sec_sections.flag(quality, text, item, form) == want["flagged_quality"]
    if want["item_chars"] is None:
        assert item is None
    else:
        assert item is not None and len(item) == want["item_chars"]
        assert _sha(item) == want["item_sha256"]
        assert item[:80] == want["item_head"] and item[-80:] == want["item_tail"]

    aif = sec_sections.cut_aif_business(text, version=sec_sections.OURS_1)
    if want["aif_business_chars"] is None:
        assert aif is None
    else:
        assert aif is not None and len(aif) == want["aif_business_chars"]
        assert _sha(aif) == want["aif_business_sha256"]


@pytest.mark.parametrize("name,text,form", CASES, ids=[c[0] for c in CASES])
def test_the_board_s_splitter_gives_the_same_as_the_pinned_rule(name, text, form):
    """The board goes through one wrapper; this is the only thing that wrapper may do."""
    assert splitter.slice_item(text, form) == sec_sections.cut_item(text, form, version=splitter.VERSION)
    assert splitter.aif_business(text) == sec_sections.cut_aif_business(text, version=splitter.VERSION)


def test_strip_html_is_unchanged():
    assert _sha(STRIP_HTML_INPUT) == _GOLDEN["strip_html"]["input_sha256"]
    assert sec_sections.strip_html(STRIP_HTML_INPUT) == _GOLDEN["strip_html"]["output"]
    assert splitter.strip_html(STRIP_HTML_INPUT) == _GOLDEN["strip_html"]["output"]

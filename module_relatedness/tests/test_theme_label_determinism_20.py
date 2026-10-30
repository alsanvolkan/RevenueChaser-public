"""#20: the offline theme label must not depend on the order the tag rows are scanned."""
from __future__ import annotations

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import themes


def _tie_rows(run_id: str, fine_first: str):
    """One medium theme with two equal fine children whose tags tie on every count.

    Every tag is carried by exactly one fine theme, so ``tag_doc_freq`` is 1 for all six and
    ``Counter.most_common()`` is a six-way tie: the label is whichever child's tags come first.
    """
    theme_rows = [
        {"theme_id": f"{run_id}:m1", "level": "medium", "parent_id": None, "label": "", "size": 4},
        {"theme_id": f"{run_id}:f1", "level": "fine", "parent_id": f"{run_id}:m1", "label": "", "size": 2},
        {"theme_id": f"{run_id}:f2", "level": "fine", "parent_id": f"{run_id}:m1", "label": "", "size": 2},
    ]
    tags = {f"{run_id}:f1": ["deposits", "branch", "consumer"],
            f"{run_id}:f2": ["biologics", "clinical", "drugs"]}
    order = [f"{run_id}:f1", f"{run_id}:f2"] if fine_first == "f1" else [f"{run_id}:f2", f"{run_id}:f1"]
    tag_rows = [{"theme_id": tid, "tag": t, "rank": i} for tid in order for i, t in enumerate(tags[tid])]
    return theme_rows, tag_rows


def _medium_label(run_id: str, fine_first: str) -> str:
    theme_rows, tag_rows = _tie_rows(run_id, fine_first)
    rawstore.write_rows("relatedness_themes", theme_rows)
    rawstore.write_rows("relatedness_theme_tags", tag_rows)
    themes.relabel_from_tags(run_id)
    return rawstore.get("relatedness_themes", f"{run_id}:m1")["label"]


def test_medium_label_is_the_same_whichever_child_s_tags_are_scanned_first(rawstore_tmp_db):
    assert _medium_label("tieA", "f1") == _medium_label("tieB", "f2")


def test_the_medium_label_on_a_tie_is_the_lowest_theme_id_child_s_tags(rawstore_tmp_db):
    """The rule itself, not only that it is stable (#20, R1 review MUST 1).

    ``f1``'s tags win over ``f2``'s whichever order the rows are scanned in. The e2e cannot pin this:
    there the fine numbering comes out of NMF, and the #51 fixture's tied singular values leave it to
    the LAPACK build. Here the ``theme_id``s are the test's own, so the rule is pinned where it is
    decided.
    """
    assert _medium_label("pinA", "f1") == "deposits, branch, consumer"
    assert _medium_label("pinB", "f2") == "deposits, branch, consumer"


def test_the_tie_is_broken_on_the_theme_id_as_text_not_as_a_number(rawstore_tmp_db):
    """``f10`` sorts before ``f2`` (#20 NIT): the comparison is ``sorted()``/``ORDER BY`` on the id
    text, and the labels and docstrings say so. Deterministic either way; this is the one that is
    written down."""
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": "txt:m1", "level": "medium", "parent_id": None, "label": "", "size": 4},
        {"theme_id": "txt:f2", "level": "fine", "parent_id": "txt:m1", "label": "", "size": 2},
        {"theme_id": "txt:f10", "level": "fine", "parent_id": "txt:m1", "label": "", "size": 2},
    ])
    rawstore.write_rows("relatedness_theme_tags", [
        {"theme_id": "txt:f2", "tag": t, "rank": i} for i, t in enumerate(("deposits", "branch", "consumer"))]
        + [{"theme_id": "txt:f10", "tag": t, "rank": i}
           for i, t in enumerate(("biologics", "clinical", "drugs"))])
    themes.relabel_from_tags("txt")
    assert rawstore.get("relatedness_themes", "txt:m1")["label"] == "biologics, clinical, drugs"


def test_run_rows_order_does_not_depend_on_the_insertion_order(rawstore_tmp_db):
    """``run_rows`` is the one reader of a run's rows, so the order is its contract (#20)."""
    rawstore.write_rows("relatedness_theme_tags", [
        {"theme_id": "ordB:f2", "tag": "zinc", "rank": 1},
        {"theme_id": "ordB:f1", "tag": "alpha", "rank": 0},
        {"theme_id": "ordB:f2", "tag": "argon", "rank": 0},
    ])
    read = [(r["theme_id"], r["rank"], r["tag"]) for r in themes.run_rows("relatedness_theme_tags", "ordB")]
    assert read == sorted(read)


def _naming_prompt(run_id: str, fine_first: str, members_reversed: bool = False) -> str:
    """The user prompt ``name_themes`` sends for the medium theme, with the theme rows written in
    ``fine_first`` order: ``_run_themes`` reads them back in that order and ``_descendant_fines``
    hands them to ``_aggregate`` in it.

    ``members_reversed`` writes ``relatedness_theme_members`` back to front instead. DuckDB returns
    rows in insertion order when nothing orders them, so that is how a store whose member rows were
    not written by today's ``build_shadow`` looks to the reader.
    """
    medium = {"theme_id": f"{run_id}:m1", "level": "medium", "parent_id": None, "label": "", "size": 4}
    f1 = {"theme_id": f"{run_id}:f1", "level": "fine", "parent_id": f"{run_id}:m1", "label": "banks", "size": 2}
    f2 = {"theme_id": f"{run_id}:f2", "level": "fine", "parent_id": f"{run_id}:m1", "label": "biotech", "size": 2}
    rawstore.write_rows("relatedness_themes", [medium, f1, f2] if fine_first == "f1" else [medium, f2, f1])
    rawstore.write_rows("relatedness_theme_tags", [
        {"theme_id": f"{run_id}:f1", "tag": t, "rank": i} for i, t in enumerate(("deposits", "branch"))]
        + [{"theme_id": f"{run_id}:f2", "tag": t, "rank": i} for i, t in enumerate(("biologics", "clinical"))])
    members = [{"ticker": t, "theme_id": f"{run_id}:f1", "weight": 1.0} for t in ("BAC", "WFC")] \
        + [{"ticker": t, "theme_id": f"{run_id}:f2", "weight": 1.0} for t in ("AMGN", "BIIB")]
    rawstore.write_rows("relatedness_theme_members", list(reversed(members)) if members_reversed else members)
    from RevenueChaser.module_relatedness import naming
    sent = []

    def _complete(system, user, **kw):
        sent.append(user)
        return '{"name": "X"}'

    naming.name_themes(run_id, complete=_complete)
    return next(u for u in sent if u.startswith("Level: medium"))


def test_the_medium_theme_s_naming_prompt_does_not_depend_on_the_scan_order(rawstore_tmp_db):
    """A prompt that differs between machines also misses the LLM cache, so it is a paid call (#20)."""
    assert _naming_prompt("nameA", "f1") == _naming_prompt("nameB", "f2")


def _reverse_for_b(monkeypatch, module):
    """``run_rows`` handing ``module`` a reversed row order for the ``...B`` run only.

    The two runs then see opposite scan orders even though ``run_rows`` itself is ordered, which is
    what a machine-to-machine difference looked like before the ``ORDER BY`` landed.
    """
    plain = themes.run_rows

    def _rows(table, run_id, level=None):
        rows = plain(table, run_id, level)
        return list(reversed(rows)) if run_id.endswith("B") else rows

    monkeypatch.setattr(module, "run_rows", _rows)


def test_the_label_does_not_rely_on_the_sql_order(rawstore_tmp_db, monkeypatch):
    """Belt and braces (#20): even handed opposite scan orders, :func:`themes.relabel_from_tags`
    walks the children in ``theme_id`` order."""
    _reverse_for_b(monkeypatch, themes)
    assert _medium_label("revA", "f1") == _medium_label("revB", "f1")


def _tag_line(prompt: str) -> str:
    return next(l for l in prompt.splitlines() if l.startswith("Representative tags:"))


def test_the_naming_prompt_s_tags_do_not_rely_on_the_sql_order(rawstore_tmp_db, monkeypatch):
    """The same for the prompt ``naming.name_themes`` sends (#20).

    The tag line, because this harness reverses what ``run_rows`` *returns*: it defeats the SQL
    ``ORDER BY`` on purpose, so it can only test the Python side, and only the tag order has one
    (``naming`` sorts ``fine_ids``). The example companies' order is a SQL contract, not a Python
    one -- ``test_the_whole_naming_prompt_does_not_depend_on_the_member_insertion_order`` is the test
    for it.
    """
    from RevenueChaser.module_relatedness import naming
    _reverse_for_b(monkeypatch, naming.themes_mod)
    assert _tag_line(_naming_prompt("nrevA", "f1")) == _tag_line(_naming_prompt("nrevB", "f1"))


def test_the_whole_naming_prompt_does_not_depend_on_the_member_insertion_order(rawstore_tmp_db):
    """The **whole** prompt, not only its tag line (#20, R1 review MUST 2).

    The example companies come from ``relatedness_theme_members``, which ``themes.ROW_ORDER`` now
    reads ``theme_id, ticker``. That reproduces the order today's ``build_shadow`` writes -- its
    tickers come from ``tag_docs``' ``sorted(profiles)``, so within a theme they are already
    alphabetical -- and makes it a contract rather than the scan's choice, so a run whose member
    rows went in in some other order gives the same prompt. A prompt that differs between machines
    also misses the LLM cache (``deepseek.complete`` hashes ``(system, user)``), so the same theme is
    paid for twice.
    """
    assert _naming_prompt("memA", "f1") == _naming_prompt("memB", "f1", members_reversed=True)


def _coarse_label(run_id: str) -> str:
    """One coarse theme with two medium children, each with one fine child whose tags tie.

    The fine themes' parents are crossed (``f1`` under ``m2``), so the order ``med_tags`` is filled
    in -- the fine loop's -- disagrees with ``theme_id`` order, and only sorting the medium loop
    makes the coarse label the same every time.
    """
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{run_id}:c1", "level": "coarse", "parent_id": "", "label": "", "size": 4},
        {"theme_id": f"{run_id}:m1", "level": "medium", "parent_id": f"{run_id}:c1", "label": "", "size": 2},
        {"theme_id": f"{run_id}:m2", "level": "medium", "parent_id": f"{run_id}:c1", "label": "", "size": 2},
        {"theme_id": f"{run_id}:f1", "level": "fine", "parent_id": f"{run_id}:m2", "label": "", "size": 2},
        {"theme_id": f"{run_id}:f2", "level": "fine", "parent_id": f"{run_id}:m1", "label": "", "size": 2},
    ])
    rawstore.write_rows("relatedness_theme_tags", [
        {"theme_id": f"{run_id}:f1", "tag": t, "rank": i} for i, t in enumerate(("deposits", "branch", "consumer"))]
        + [{"theme_id": f"{run_id}:f2", "tag": t, "rank": i}
           for i, t in enumerate(("biologics", "clinical", "drugs"))])
    themes.relabel_from_tags(run_id)
    return rawstore.get("relatedness_themes", f"{run_id}:c1")["label"]


def test_the_coarse_label_walks_its_medium_children_in_theme_id_order(rawstore_tmp_db):
    """The coarse label is the tags of the medium child with the lowest ``theme_id`` as text, not the
    fine loop's order (#20)."""
    assert _coarse_label("crsA").split(", ")[0] == "biologics"

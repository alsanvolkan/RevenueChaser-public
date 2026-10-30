"""NMF nested themes (Phase 8a) — tag x ticker factorisation -> fine/medium/coarse.

Offline (no LLM): TF-IDF over profile tags (``max_df=0.5``), NMF for the fine
level, agglomerative merge for medium/coarse with ``parent_id`` links. The
``theme_runs`` row + ``relatedness_themes``/``_theme_tags``/``_theme_members`` are
written *beside* the old vocabulary (shadow); naming is a separate off-peak step.

See docs/classification_design.md Phase 8a.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import numpy as np

from RevenueChaser.core import rawstore
from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS, UNCLASSIFIED, canonical_sector
from RevenueChaser.core.universe import SECTOR_COARSE_PREFIX

from . import config, store as rel_store

logger = logging.getLogger(__name__)

MIN_SIZE = 20
STABILITY_FLOOR = 0.90    # a k is stable when its mean pairwise ARI across SEEDS runs clears this (choose_k's floor)
SEEDS = 5
MIN_PROFILES = 10          # below this NMF themes mean nothing: report, don't build
NMF_INITS = ("nndsvd", "nndsvda", "nndsvdar", "random")   # MiniBatchNMF inits that need no W/H from the caller


@dataclass(frozen=True)
class NmfFit:
    """How every NMF of one theme run is fitted: the k candidates, the seed-stability runs and the final fine fit.

    The default is the best of the nine #108 S4 tuning variants on a store copy (2026-10-15: nndsvda init, 500
    iterations; fine ARI 0.691 against the unscoped fit, seed stability 0.740; PC ``.cache/research/108/
    s4_tuning.json``). It replaces the nndsvd / 60 fit of every run before S1. Both stay below the pre-registered S4
    bars (ARI 0.80, the live run's 0.761); avo chose to rebuild with it anyway (card "Kuralı gevşet").
    """
    init: str = "nndsvda"
    max_iter: int = 500

    def __post_init__(self):
        if self.init not in NMF_INITS:
            raise ValueError(f"NMF init must be one of {NMF_INITS}, not {self.init!r}")
        if isinstance(self.max_iter, bool) or not isinstance(self.max_iter, int) or self.max_iter < 1:
            raise ValueError(f"NMF max_iter must be a whole number >= 1, not {self.max_iter!r}")


DEFAULT_FIT = NmfFit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tag_docs(profiles: dict[str, dict]) -> tuple[list[str], list[list[str]]]:
    tickers = sorted(profiles)
    docs = []
    for t in tickers:
        tags = []
        for field in config.PROFILE_FIELDS:
            tags.extend(str(x).strip().lower() for x in (profiles[t].get(field) or []))
        docs.append([x for x in tags if x])
    return tickers, docs


def tag_matrix(profiles: dict[str, dict]):
    """TF-IDF matrix (tickers x tags) with ``max_df=0.5``. Returns (X, terms, tickers).

    ``min_df`` adapts to the universe (3 at 60+ names, 1 on a handful) so a small
    run does not trip sklearn's ``max_df < min_df`` check.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    tickers, docs = tag_docs(profiles)
    min_df = min(3, max(1, len(docs) // 20))
    vec = TfidfVectorizer(analyzer=lambda d: d, max_df=0.5, min_df=min_df,
                          max_features=20_000)
    X = vec.fit_transform(docs)
    return X, list(vec.get_feature_names_out()), tickers


def nmf_assign(X, k: int, seed: int = 0, fit: NmfFit = DEFAULT_FIT) -> np.ndarray:
    from sklearn.decomposition import MiniBatchNMF
    W = MiniBatchNMF(n_components=k, init=fit.init, random_state=seed,
                     max_iter=fit.max_iter, batch_size=512).fit_transform(X)
    return W.argmax(axis=1)


def stability(X, k: int, seeds: int = SEEDS, fit: NmfFit = DEFAULT_FIT) -> float:
    """Mean pairwise ARI of the fine assignment across ``seeds`` NMF runs."""
    from sklearn.metrics import adjusted_rand_score
    labels = [nmf_assign(X, k, seed=s, fit=fit) for s in range(seeds)]
    pairs = [(i, j) for i in range(seeds) for j in range(i + 1, seeds)]
    if not pairs:
        return 1.0
    return float(np.mean([adjusted_rand_score(labels[i], labels[j]) for i, j in pairs]))


def _min_size(labels: np.ndarray) -> int:
    counts = np.bincount(labels)
    return int(counts[counts > 0].min()) if counts.size else 0


def _choose(X, candidates: list[int], seeds: int, min_size: int,
            floor: float, fit: NmfFit = DEFAULT_FIT) -> tuple[int, float]:
    """``(k, stability)``: the smallest stable ``k`` with every theme >= ``min_size``.

    Candidates are clamped to ``k < min(n, n_features)`` (the nndsvd inits raise
    beyond that). When none clears both bars, the most
    stable candidate wins (ties: smaller ``k``); when none is usable at all, the
    size-based default ``max(2, n // MIN_SIZE)``. Every NMF here uses ``fit``.
    """
    n = X.shape[0]
    cap = min(n, X.shape[1])
    scored = []
    for k in sorted({int(c) for c in candidates if 2 <= int(c) < cap}):
        lab = nmf_assign(X, k, seed=0, fit=fit)
        scored.append((k, stability(X, k, seeds, fit=fit), _min_size(lab)))
    if not scored:
        k = max(2, min(n // MIN_SIZE, cap - 1))
        return k, stability(X, k, seeds, fit=fit)
    ok = [s for s in scored if s[1] >= floor and s[2] >= min_size]
    if ok:
        best = min(ok, key=lambda s: s[0])
    else:
        best = sorted(scored, key=lambda s: (-s[1], s[0]))[0]
    return best[0], best[1]


def choose_k(X, candidates: list[int], seeds: int = SEEDS,
             min_size: int = MIN_SIZE, floor: float = STABILITY_FLOOR,
             fit: NmfFit = DEFAULT_FIT) -> int:
    """Smallest ``k`` that is stable (ARI >= floor) with every theme >= min_size.

    Falls back to the most stable candidate when none clears both bars.
    """
    return _choose(X, candidates, seeds, min_size, floor, fit)[0]


def nested_parents(X, fine: np.ndarray, k_medium: int, k_coarse: int):
    """Hierarchical agglomerative merge: fine centroids -> medium -> coarse.

    Medium clusters the fine centroids. Coarse clusters the **medium centroids**
    (so coarse is a merge of medium, not a second independent cut of fine) and is
    mapped back to one coarse id per fine theme. Returns ``(ids, med, coarse)``
    where ``med``/``coarse`` are per-fine-theme label arrays.

    Every cut returns at least one child: a level that ends with a single cluster is
    returned as-is (agglomerative clustering needs two samples). The medium cut is
    halved from the number of fine themes present (``max(2, n_fine // 2)``), so most
    medium themes merge several fine children; the naming step reuses a single child's
    name when a medium theme has only one fine child, instead of asking the model (#433).
    """
    from sklearn.cluster import AgglomerativeClustering
    ids = sorted(set(fine.tolist()))
    centroids = np.vstack([np.asarray(X[fine == i].mean(axis=0)).ravel() for i in ids])
    if len(ids) < 2:                      # one fine theme: the parents are that theme
        return ids, np.zeros(len(ids), dtype=int), np.zeros(len(ids), dtype=int)
    # Held below the fine count (#433), the cut merges one pair of fine themes; a floor
    # of 1 keeps a real merge for a caller that asks for more mediums than there are fines.
    med = AgglomerativeClustering(
        n_clusters=max(1, min(k_medium, len(ids) - 1))).fit_predict(centroids)
    med_ids = sorted(set(med.tolist()))
    if len(med_ids) < 2:                  # one medium theme: coarse is that theme
        return ids, med, np.zeros_like(med)
    med_centroids = np.vstack([centroids[med == m].mean(axis=0) for m in med_ids])
    coarse_med = AgglomerativeClustering(
        n_clusters=min(k_coarse, len(med_ids))).fit_predict(med_centroids)
    med_pos = {m: p for p, m in enumerate(med_ids)}
    coarse = np.asarray([coarse_med[med_pos[m]] for m in med])
    return ids, med, coarse


def build_shadow(candidates=(60, 90, 120, 150), k_medium: int | None = None,
                 k_coarse: int = 25, run_id: str | None = None,
                 profiles: dict | None = None, universe=None,
                 fit: NmfFit = DEFAULT_FIT) -> dict:
    """Compute the nested themes and write the shadow tables. No LLM naming.

    The fit covers ``universe`` only (#108 S0): the stored profiles also hold names that fell below the floor, left
    the universe or are excluded types, and those must not shape the themes. With the stored profiles
    (``profiles`` None) ``universe`` defaults to ``core.universe.classification_universe()``; a caller that passes
    ``profiles`` has chosen its names, and ``universe`` narrows them only when given. ``out_of_universe`` in the
    result counts the profiles left out.

    Every NMF of the run uses ``fit`` (``DEFAULT_FIT`` unless given); ``fit`` in the result names it, because
    ``theme_runs`` has no column for it. When ``k_medium`` is not given it is derived as
    ``max(2, n_fine // 2)`` from the realised fine count; an explicit value is honoured but still clamped below
    ``n_fine`` as a safety net, so the effective value is ``min(requested_or_derived, n_fine - 1)`` with a floor of 1.
    """
    if profiles is None:
        profiles = rel_store.load_profiles()
        if universe is None:
            from RevenueChaser.core.universe import classification_universe
            universe = classification_universe()
    profiles = {t: p for t, p in profiles.items() if any(p.values())}
    out_of_universe = 0
    if universe is not None:
        allowed = {str(t).upper() for t in universe}
        scoped = {t: p for t, p in profiles.items() if t.upper() in allowed}
        out_of_universe = len(profiles) - len(scoped)
        profiles = scoped
    if len(profiles) < MIN_PROFILES:
        return {"n": len(profiles), "error": "not enough profiles", "out_of_universe": out_of_universe}

    try:
        X, terms, tickers = tag_matrix(profiles)
    except ValueError as exc:             # e.g. every tag pruned by min_df/max_df
        return {"n": len(profiles), "error": f"no usable tags: {exc}", "out_of_universe": out_of_universe}
    if X.shape[1] < 3:
        return {"n": len(profiles), "error": "not enough distinct tags", "out_of_universe": out_of_universe}
    # A name whose tags are all below min_df has an all-zero row: NMF would park
    # it in theme 0 (argmax of zeros). Leave it unthemed instead.
    keep = np.asarray(X.getnnz(axis=1) > 0)
    unthemed = int((~keep).sum())
    if unthemed:
        X = X[keep]
        tickers = [t for t, k in zip(tickers, keep) if k]
        profiles = {t: profiles[t] for t in tickers}
    if len(tickers) < MIN_PROFILES:
        return {"n": len(tickers), "error": "not enough profiles", "unthemed": unthemed,
                "out_of_universe": out_of_universe}
    k_fine, fine_stability = _choose(X, list(candidates), SEEDS, MIN_SIZE,
                                     STABILITY_FLOOR, fit)
    fine = nmf_assign(X, k_fine, seed=0, fit=fit)
    n_fine = len(set(fine.tolist()))      # an empty NMF component leaves fewer fine themes
    # The medium level is halved from the realised fine count (#433). An explicit
    # k_medium is honoured; when none is given we derive it. In every case the
    # safety-net clamp keeps the effective cut below n_fine with a floor of 1.
    requested = k_medium
    derived = max(2, n_fine // 2) if k_medium is None else k_medium
    clamped = max(1, min(derived, n_fine - 1))
    if clamped != derived:
        logger.info("k_medium requested=%s derived=%d clamped=%d: the run realised %d fine themes",
                    "default" if requested is None else requested, derived, clamped, n_fine)
    k_medium = clamped
    ids, med, coarse = nested_parents(X, fine, k_medium, k_coarse)
    run_id = run_id or f"nmf-{_now()}"

    tag_index: dict[int, set] = {i: set() for i in ids}
    _, docs = tag_docs(profiles)
    for doc, label in zip(docs, fine):
        tag_index.setdefault(int(label), set()).update(doc)

    fine_size: dict[int, int] = {int(i): int((fine == i).sum()) for i in ids}
    med_size: dict[int, int] = {}
    coarse_size: dict[int, int] = {}
    med_parent: dict[int, int] = {}
    for i, med_id, co_id in zip(ids, med, coarse):
        i, med_id, co_id = int(i), int(med_id), int(co_id)
        med_size[med_id] = med_size.get(med_id, 0) + fine_size[i]
        coarse_size[co_id] = coarse_size.get(co_id, 0) + fine_size[i]
        med_parent[med_id] = co_id

    theme_rows, tag_rows, member_rows = [], [], []
    for i, (med_id, _co_id) in zip(ids, zip(med, coarse)):
        i, med_id = int(i), int(med_id)
        fine_id = f"{run_id}:f{i}"
        theme_rows.append({"theme_id": fine_id, "level": "fine",
                           "parent_id": f"{run_id}:m{med_id}", "label": "",
                           "size": fine_size[i]})
        tag_rows += [{"theme_id": fine_id, "tag": tg, "rank": r}
                     for r, tg in enumerate(sorted(tag_index.get(i, set())))]
    for m in sorted(med_size):
        theme_rows.append({"theme_id": f"{run_id}:m{m}", "level": "medium",
                           "parent_id": f"{run_id}:c{med_parent[m]}", "label": "",
                           "size": med_size[m]})
    for c in sorted(coarse_size):
        theme_rows.append({"theme_id": f"{run_id}:c{c}", "level": "coarse",
                           "parent_id": "", "label": "", "size": coarse_size[c]})
    for t, label in zip(tickers, fine):
        member_rows.append({"ticker": t, "theme_id": f"{run_id}:f{int(label)}",
                            "weight": 1.0})

    run_row = {
        "id": run_id, "created_at": _now(), "k_fine": k_fine,
        "k_medium": int(len(set(med.tolist()))), "k_coarse": int(len(set(coarse.tolist()))),
        "jaccard": fine_stability, "is_current": 0}
    existing = rawstore.get("theme_runs", run_id)
    if existing:                      # a rebuild of a live id keeps its pointer
        run_row["is_current"] = existing.get("is_current") or 0

    # One transaction, scoped to this run: older runs' themes/tags/members stay
    # untouched, and a crash leaves either the old state or the whole new run.
    def _write(tx):
        _delete_run_rows(tx, run_id, ("relatedness_themes", "relatedness_theme_tags",
                                      "relatedness_theme_members"))
        tx.write_rows("relatedness_themes", theme_rows)
        tx.write_rows("relatedness_theme_tags", tag_rows)
        tx.write_rows("relatedness_theme_members", member_rows)
        tx.write_rows("theme_runs", [run_row])

    for name in ("relatedness_themes", "relatedness_theme_tags",
                 "relatedness_theme_members", "theme_runs"):
        rawstore.ensure_table(name)
    rawstore.atomic(_write)
    return {"run_id": run_id, "n": len(tickers), "k_fine": k_fine,
            "k_medium": int(len(set(med.tolist()))),
            "k_coarse": int(len(set(coarse.tolist()))),
            "themes": len(theme_rows), "members": len(member_rows),
            "unthemed": unthemed, "out_of_universe": out_of_universe, "fit": asdict(fit)}


def _delete_run_rows(tx, run_id: str, tables) -> None:
    """Delete ``run_id``'s rows (``run_id:...`` theme ids) from ``tables``."""
    for name in tables:
        tx.execute(f"DELETE FROM {name} WHERE theme_id = ? OR starts_with(theme_id, ?)",
                   [run_id, f"{run_id}:"])


def current_run_id() -> str | None:
    """The pinned run's id: the one lookup in ``core.universe.current_theme_run`` (C23). Strict: a failed read
    raises, so the guards that refuse to touch the current run never read it as "no current run"."""
    from RevenueChaser.core.universe import current_theme_run
    return current_theme_run(strict=True)


# The deterministic read order of a run's theme tables (#20). A table absent here is read in the
# scan's order on purpose -- see :func:`run_rows`.
ROW_ORDER = {"relatedness_themes": "theme_id", "relatedness_theme_tags": "theme_id, rank, tag"}


def run_rows(table: str, run_id: str, level: str | None = None) -> list[dict]:
    """Rows of a theme table that belong to ``run_id`` (the run id or ``run:fN``).

    The one reader of a run's rows: a SQL prefix filter (no ``run_id`` column, no
    whole-table scan filtered in Python). ``level`` narrows ``relatedness_themes``.

    **The order is part of the contract for two of the three tables** (#20, ``ROW_ORDER``): without
    it the order was the scan's, and a caller that aggregates one theme's rows into another's -- the
    offline label (:func:`relabel_from_tags`) and the naming prompt (``naming.name_themes``) both do
    -- gave a different answer on two machines from the same rows. Measured: the #51 e2e labelled one
    merged medium theme ``branch, consumer, deposits`` on Windows and ``biologics, clinical, drugs``
    on the hosted Linux CI.

    ``relatedness_theme_members`` is deliberately **not** ordered. Its rows are written in the fit's
    ticker order, which is the universe's (cap-descending), and ``naming._aggregate`` keeps only the
    first ``MAX_TICKERS`` of a theme as the prompt's example companies. Ordering by ``ticker`` would
    make those examples alphabetical instead of the theme's largest names, and ``ORDER BY theme_id``
    alone is no safer, because the sort is not stable. Giving the table a ``rank`` column is the real
    fix and is its own change; the residual is parked.
    """
    sql = (f"SELECT * FROM {table} WHERE (theme_id = ? OR starts_with(theme_id, ?))")
    args: list = [run_id, f"{run_id}:"]
    if level:
        sql += " AND level = ?"
        args.append(level)
    order = ROW_ORDER.get(table)
    return rawstore.query(f"{sql} ORDER BY {order}" if order else sql, args)


def _useful_tag(tag: str) -> bool:
    """False for numeric/spec junk that makes a poor human theme label.

    Drops empty/short tags, tags with no letters, and tags that are mostly digits
    or start with a digit (``1.6t``, ``12-inch``, ``2.5d integration``).
    """
    t = str(tag or "").strip().lower()
    if len(t) < 3 or not any(ch.isalpha() for ch in t):
        return False
    if t[0].isdigit():
        return False
    digits = sum(ch.isdigit() for ch in t)
    return digits < 0.5 * len(t)


def tag_doc_freq(tag_lists) -> dict[str, int]:
    """``{tag: number of themes carrying it}`` over an iterable of tag lists.

    The frequency ``_ordered_tags`` sorts by: a tag half the run shares is less
    distinctive than one a single theme carries.
    """
    freq: dict[str, int] = {}
    for tags in tag_lists:
        for t in {str(x).strip().lower() for x in tags}:
            freq[t] = freq.get(t, 0) + 1
    return freq


def _ordered_tags(tags, doc_freq: dict[str, int] | None = None) -> list[str]:
    """Useful tags, rarest-first (more distinctive), stable on ties."""
    freq = doc_freq or {}
    seen, out = set(), []
    for t in tags:
        t = str(t or "").strip().lower()
        if not _useful_tag(t) or t in seen:
            continue
        seen.add(t)
        out.append(t)
    out.sort(key=lambda t: freq.get(t, 0))   # stable: ties keep caller order
    return out


def _label(tags, top: int = 3, doc_freq: dict[str, int] | None = None) -> str:
    """Join the top useful tags into a label (falls back to raw tags when none)."""
    ordered = _ordered_tags(tags, doc_freq)
    if not ordered:
        ordered = [str(t).strip().lower() for t in tags if str(t).strip()]
    return ", ".join(ordered[:top])


def relabel_from_tags(run_id: str, top: int = 3, only_empty: bool = False) -> int:
    """Deterministic labels from each theme's top tags (LLM naming can refine).

    ``only_empty`` writes only themes whose label is still blank, so a name that
    is already there (LLM or human) is never overwritten.

    Fine themes label from their own tags; medium themes aggregate their child
    fine themes' tags (the reader level is ``medium``); coarse themes aggregate
    their child medium themes. Numeric/spec junk is skipped and rarer (more
    distinctive) tags are preferred -- this is the offline fallback, not a name.

    The aggregation walks the children in ``theme_id`` order (#20), not in the order the tag rows
    were scanned. ``Counter.most_common()`` keeps first-seen order, so on an exact tie -- two merged
    fine themes of equal size whose tags are equally rare -- the label is the lowest-numbered
    child's tags, the same on every machine. Stored labels are not rewritten by this: only a run
    relabelled from here on follows the rule.
    """
    from collections import Counter
    themes_rows = {r["theme_id"]: r for r in run_rows("relatedness_themes", run_id)}
    fine_tags: dict[str, list] = {}
    for r in run_rows("relatedness_theme_tags", run_id):
        fine_tags.setdefault(r["theme_id"], []).append((r.get("rank") or 0, r["tag"]))

    doc_freq = tag_doc_freq([t for _r, t in items] for items in fine_tags.values())

    rows, med_tags, coarse_tags = [], {}, {}
    for tid, items in sorted(fine_tags.items()):
        ordered = [t for _r, t in sorted(items)]
        parent = (themes_rows.get(tid) or {}).get("parent_id")
        if parent:
            med_tags.setdefault(parent, []).extend(ordered)
        if ordered and tid in themes_rows:
            row = dict(themes_rows[tid])
            row["label"] = _label(ordered, top, doc_freq)
            rows.append(row)
    for mid, tags in sorted(med_tags.items()):
        if mid not in themes_rows:
            continue
        row = dict(themes_rows[mid])
        row["label"] = _label([t for t, _n in Counter(tags).most_common()],
                              top, doc_freq)
        rows.append(row)
        cparent = themes_rows[mid].get("parent_id")
        if cparent:
            coarse_tags.setdefault(cparent, []).extend(tags)
    for cid, tags in sorted(coarse_tags.items()):
        if cid not in themes_rows:
            continue
        row = dict(themes_rows[cid])
        row["label"] = _label([t for t, _n in Counter(tags).most_common()],
                              top, doc_freq)
        rows.append(row)
    if only_empty:
        rows = [r for r in rows
                if not (themes_rows.get(r["theme_id"]) or {}).get("label")]
    if rows:
        rawstore.write_rows("relatedness_themes", rows)
    return len(rows)


def themes_for_ticker(ticker: str, run_id: str | None = None) -> list[str]:
    """Display labels for a ticker's medium themes in the current/pinned run."""
    run_id = run_id or current_run_id()
    if not run_id:
        return []
    rows = rawstore.query(
        "SELECT theme, theme_id FROM ticker_themes "
        "WHERE ticker = ? AND theme_run_id = ? AND level = 'medium'",
        [ticker.upper(), run_id])
    out = []
    for r in rows:
        label = r.get("theme") or ""
        if not label:
            th = rawstore.get("relatedness_themes", r["theme_id"])
            label = (th or {}).get("label") or r["theme_id"]
        out.append(label)
    return out


def themes_for_tickers(tickers, run_id: str | None = None) -> dict[str, list[str]]:
    """``{ticker: labels}`` as ``themes_for_ticker`` returns them, for many tickers in a fixed number of queries.

    A ticker with no medium theme is absent.
    """
    tickers = list(dict.fromkeys(t.upper() for t in tickers if t))
    run_id = run_id or current_run_id()
    if not run_id or not tickers:
        return {}
    marks = ", ".join("?" for _ in tickers)
    rows = rawstore.query(
        "SELECT ticker, theme, theme_id FROM ticker_themes "
        f"WHERE theme_run_id = ? AND level = 'medium' AND ticker IN ({marks})", [run_id, *tickers])
    labels = {}
    missing = sorted({r["theme_id"] for r in rows if not r.get("theme")})
    if missing:
        for theme_id, th in rawstore.get_many("relatedness_themes", missing).items():
            labels[theme_id] = (th or {}).get("label") or theme_id
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["ticker"], []).append(r.get("theme") or labels.get(r["theme_id"]) or r["theme_id"])
    return out


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _label_tokens(s: str) -> set[str]:
    import re
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t}


def _label_jaccard(a: str, b: str) -> float:
    return _jaccard(_label_tokens(a), _label_tokens(b))


def clean_theme_label(s: str) -> str:
    """Strip a trailing junk parenthetical; keep title-case industry parens.

    Drops trailing `` (… )`` when the inside does not start with an uppercase
    letter, or when it contains ``&`` / ``+`` / digits (ticker/tag leftovers).
    """
    import re
    text = str(s or "").rstrip()
    if not text:
        return ""
    m = re.search(r"\s+\(([^)]*)\)\s*$", text)
    if not m:
        return text
    inside = m.group(1)
    junk = (not inside or not inside[0].isupper()
            or any(ch in inside for ch in "&+")
            or any(ch.isdigit() for ch in inside))
    if junk:
        return text[:m.start()].rstrip()
    return text


def scrub_run_labels(run_id: str) -> int:
    """Apply ``clean_theme_label`` to every theme label in ``run_id``."""
    rows = []
    for r in run_rows("relatedness_themes", run_id):
        cleaned = clean_theme_label(r.get("label") or "")
        if cleaned != (r.get("label") or ""):
            row = dict(r)
            row["label"] = cleaned
            rows.append(row)
    if rows:
        rawstore.write_rows("relatedness_themes", rows)
    return len(rows)


LEVELS = ("fine", "medium", "coarse")
COARSE_MODES = ("nmf", "sector")     # how finalize_run builds the coarse level (#108, card "Sektörden coarse")


def finalize_run(run_id: str, adopt_from: str | None = None, coarse: str = "nmf") -> dict:
    """The one pre-switch pipeline of ``--rebuild-themes`` and ``--cutover``.

    ``relabel_from_tags`` (blank labels only) -> optional adopt -> scrub ->
    coarse level -> unique labels at every level -> match ETFs
    -> ``cutover_materialize``. Every label writer runs before the switch, so the
    boards never see a run with blank or duplicate labels.

    ``coarse`` picks the coarse level: ``"nmf"`` keeps the agglomerative cut (drop empty coarses -> rebalance);
    ``"sector"`` parents every medium to its modal Morningstar sector (``sector_coarses``). An unknown mode raises
    ``ValueError`` before anything is written.
    """
    if coarse not in COARSE_MODES:
        raise ValueError(f"coarse mode must be one of {COARSE_MODES}, not {coarse!r}")
    out: dict = {"run_id": run_id, "coarse": coarse}
    relabel_from_tags(run_id, only_empty=True)
    if adopt_from:
        out.update(adopt_medium_catalog(run_id, adopt_from))
    scrub_run_labels(run_id)
    if coarse == "sector":
        out["k_coarse"] = sector_coarses(run_id).get("k_coarse")
    else:
        drop_empty_coarses(run_id)
        out["k_coarse"] = rebalance_coarses(run_id).get("k_coarse")
    for level in LEVELS:
        unique_theme_labels(run_id, level)
    etfs = match_theme_etfs(run_id)
    out["themes"] = etfs.get("themes")
    mat = cutover_materialize(run_id)
    out["rows"] = mat.get("rows")
    out["profiled"] = mat.get("profiled")
    return out


def drop_empty_coarses(run_id: str) -> dict:
    """Delete target-run coarse nodes that no medium still parents to."""
    themes_all = run_rows("relatedness_themes", run_id)
    used = {(r.get("parent_id") or "") for r in themes_all
            if r.get("level") == "medium" and r.get("parent_id")}
    empty = [r["theme_id"] for r in themes_all
             if r.get("level") == "coarse" and r["theme_id"] not in used]
    if empty:
        ph = ", ".join("?" * len(empty))
        rawstore.execute(
            f"DELETE FROM relatedness_themes WHERE theme_id IN ({ph})",
            list(empty))
    remaining = len(run_rows("relatedness_themes", run_id, "coarse"))
    tr = rawstore.get("theme_runs", run_id)
    if tr is not None:
        row = dict(tr)
        row["k_coarse"] = remaining
        rawstore.write_rows("theme_runs", [row])
    return {"dropped": len(empty), "k_coarse": remaining}


def unique_theme_labels(run_id: str, level: str = "coarse") -> int:
    """Make labels unique within ``run_id`` + ``level``: ``Label``, ``Label (2)``.

    Deterministic (ordered by ``theme_id``): the first occurrence keeps its
    label, later duplicates get the next free `` (n)`` suffix. Empty labels are
    left alone. Returns the number of rows changed.
    """
    rows = sorted(run_rows("relatedness_themes", run_id, level),
                  key=lambda r: str(r["theme_id"]))
    used: set[str] = set()
    updates: list[dict] = []
    for r in rows:
        label = r.get("label") or ""
        if not label:
            continue
        if label in used:
            n = 2
            candidate = f"{label} ({n})"
            while candidate in used:
                n += 1
                candidate = f"{label} ({n})"
            row = dict(r)
            row["label"] = candidate
            updates.append(row)
            used.add(candidate)
        else:
            used.add(label)
    if updates:
        rawstore.write_rows("relatedness_themes", updates)
    return len(updates)


def _jaccard_distance_matrix(sets: list[set]) -> np.ndarray:
    n = len(sets)
    dist = np.zeros((n, n), dtype=float)
    for i in range(n):
        for j in range(i + 1, n):
            d = 1.0 - _jaccard(sets[i], sets[j])
            dist[i, j] = dist[j, i] = d
    return dist


def _agglomerative_groups(sets: list[set], k: int) -> list[list[int]]:
    """Average-linkage Jaccard clustering into ``min(k, n)`` groups of indices."""
    from sklearn.cluster import AgglomerativeClustering
    n = len(sets)
    if n < 2:
        return [list(range(n))]
    dist = _jaccard_distance_matrix(sets)
    labels = AgglomerativeClustering(
        n_clusters=min(k, n), metric="precomputed",
        linkage="average").fit_predict(dist)
    groups: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        groups.setdefault(int(lab), []).append(i)
    return list(groups.values())


def rebalance_coarses(run_id: str, max_share: float = 0.45) -> dict:
    """Enforce no coarse holds more than ``max_share`` of profiled tickers.

    Agglomerative average-linkage on medium member-set Jaccard distance into
    ``k = min(12, n_mediums)`` coarses, then **recursively split any coarse**
    still over ``max_share`` (reclustering its mediums) until it fits or holds a
    single medium. Relabels from the modal Morningstar sector (>=50%) else the
    largest medium's cleaned label; duplicate run+level labels are then uniqued.
    """
    themes_all = run_rows("relatedness_themes", run_id)
    by_id = {r["theme_id"]: r for r in themes_all}
    meds = [r for r in themes_all if r.get("level") == "medium"]
    coarses = [r for r in themes_all if r.get("level") == "coarse"]
    n_coarse = len(coarses)
    # Duplicate run+level labels must be unique even when no split is needed.
    unique_theme_labels(run_id, "coarse")
    if len(meds) < 2:
        return {"rebalanced": False, "reason": "too_few_mediums",
                "k_coarse": n_coarse, "max_share": 0.0}

    tsets = _theme_ticker_sets(run_id)
    # profiled = union of fine members
    profiled: set[str] = set()
    for r in themes_all:
        if r.get("level") == "fine":
            profiled |= tsets.get(r["theme_id"]) or set()
    if not profiled:
        return {"rebalanced": False, "reason": "no_profiled",
                "k_coarse": n_coarse, "max_share": 0.0}

    coarse_sizes = {c["theme_id"]: len(tsets.get(c["theme_id"]) or ())
                    for c in coarses}
    largest = max(coarse_sizes.values()) if coarse_sizes else 0
    total = len(profiled)
    if largest / total <= max_share:
        return {"rebalanced": False, "reason": "ok_share",
                "k_coarse": n_coarse, "max_share": largest / total}

    med_ids = [m["theme_id"] for m in meds]
    sets = [tsets.get(mid) or set() for mid in med_ids]

    def _union_size(group: list[int]) -> int:
        out: set[str] = set()
        for i in group:
            out |= sets[i]
        return len(out)

    def _split(group: list[int]) -> list[list[int]]:
        """Recursively recluster ``group`` until every piece fits or is a singleton."""
        if len(group) <= 1:
            return [group]
        out: list[list[int]] = []
        for sub in _agglomerative_groups([sets[i] for i in group], 12):
            idxs = [group[i] for i in sub]
            if len(idxs) > 1 and _union_size(idxs) / total > max_share:
                out.extend(_split(idxs))
            else:
                out.append(idxs)
        return out

    groups: list[list[int]] = []
    for g in _agglomerative_groups(sets, 12):
        if len(g) > 1 and _union_size(g) / total > max_share:
            groups.extend(_split(g))
        else:
            groups.append(g)

    n_groups = len(groups)
    new_coarse_ids = [f"{run_id}:c{i}" for i in range(n_groups)]
    sector_by = _sector_by_ticker()

    cluster_members: dict[int, set[str]] = {}
    cluster_meds: dict[int, list[str]] = {}
    med_updates: list[dict] = []
    for gi, group in enumerate(groups):
        members: set[str] = set()
        med_list: list[str] = []
        for i in group:
            members |= sets[i]
            med_list.append(med_ids[i])
            row = dict(by_id[med_ids[i]])
            row["parent_id"] = new_coarse_ids[gi]
            med_updates.append(row)
        cluster_members[gi] = members
        cluster_meds[gi] = med_list

    coarse_rows: list[dict] = []
    for gi in range(n_groups):
        members = cluster_members[gi]
        secs = [sector_by[t] for t in members if sector_by.get(t)]
        label = ""
        if secs:
            sec, cnt = Counter(secs).most_common(1)[0]
            if cnt >= 0.5 * len(members) and sec:
                label = sec
        if not label:
            # largest medium's cleaned label
            best = max(cluster_meds[gi],
                       key=lambda mid: len(tsets.get(mid) or ()),
                       default="")
            label = clean_theme_label((by_id.get(best) or {}).get("label") or "")
        coarse_rows.append({
            "theme_id": new_coarse_ids[gi], "level": "coarse",
            "parent_id": "", "label": label, "size": len(members),
        })

    # drop old coarses not in the new set
    old_ids = [c["theme_id"] for c in coarses]
    keep = set(new_coarse_ids)
    drop = [cid for cid in old_ids if cid not in keep]
    if drop:
        ph = ", ".join("?" * len(drop))
        rawstore.execute(
            f"DELETE FROM relatedness_themes WHERE theme_id IN ({ph})", drop)
    if coarse_rows:
        rawstore.write_rows("relatedness_themes", coarse_rows)
    if med_updates:
        rawstore.write_rows("relatedness_themes", med_updates)
    unique_theme_labels(run_id, "coarse")

    tr = rawstore.get("theme_runs", run_id)
    if tr is not None:
        row = dict(tr)
        row["k_coarse"] = n_groups
        rawstore.write_rows("theme_runs", [row])

    max_after = (max(len(m) for m in cluster_members.values()) / total
                 if cluster_members else 0.0)
    return {"rebalanced": True, "k_coarse": n_groups,
            "max_share": max_after, "max_share_before": largest / total}


def _sector_slug(sector: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", sector.lower()).strip("-")


def sector_coarse_id(run_id: str, sector: str) -> str:
    """The coarse theme id of ``sector`` in ``run_id``: ``<run>:sec-<slug>``, the same suffix in every run."""
    return f"{run_id}:{SECTOR_COARSE_PREFIX}{_sector_slug(sector)}"


def _sector_votes(members, sector_by: dict[str, str]) -> Counter:
    """Morningstar sector -> member count; names with no sector value (or ``Unclassified``) do not vote."""
    votes: Counter = Counter()
    for t in members:
        sec = canonical_sector(sector_by.get(t) or "")
        if sec != UNCLASSIFIED:
            votes[sec] += 1
    return votes


def _modal_sector(votes: Counter) -> str:
    """The most-voted sector; a tie goes to the first in ``MORNINGSTAR_SECTORS``; no vote is ``Unclassified``."""
    if not votes:
        return UNCLASSIFIED
    top = max(votes.values())
    return next(s for s in MORNINGSTAR_SECTORS if votes.get(s) == top)


def sector_coarse_plan(medium_members: dict[str, set], sector_by: dict[str, str]) -> dict[str, str]:
    """``{medium id: Morningstar sector}``: each medium goes to the modal sector of its member names.

    Pure (no store). ``sector_by`` maps ticker -> sector in any spelling ``canonical_sector`` reads; a name with
    no sector value does not vote, and a medium where no member has one is ``Unclassified``.
    """
    return {mid: _modal_sector(_sector_votes(members, sector_by)) for mid, members in medium_members.items()}


def sector_coarse_summary(medium_members: dict[str, set], sector_by: dict[str, str],
                          plan: dict[str, str]) -> dict:
    """What ``plan`` (``sector_coarse_plan``) makes of the coarse level. Pure (no store).

    ``k_coarse``; ``profiled`` (names under the mediums); ``max_share`` (largest coarse / profiled); ``sizes``
    (sector -> names, largest first); ``purity`` (share of names whose own sector, ``Unclassified`` when missing,
    equals their coarse); ``mixed_mediums`` (mediums whose modal sector holds under half of their sector-known
    names); ``unclassified_mediums``.
    """
    members_of: dict[str, set] = {}
    mixed = 0
    for mid, members in medium_members.items():
        sec = plan[mid]
        members_of.setdefault(sec, set()).update(members)
        votes = _sector_votes(members, sector_by)
        if votes and votes[sec] < 0.5 * sum(votes.values()):
            mixed += 1
    profiled = set().union(*members_of.values()) if members_of else set()
    total = len(profiled)
    coarse_of = {t: sec for sec, names in members_of.items() for t in names}
    pure = sum(1 for t in profiled if canonical_sector(sector_by.get(t) or "") == coarse_of.get(t))
    sizes = {sec: len(names) for sec, names in sorted(members_of.items(), key=lambda kv: (-len(kv[1]), kv[0]))}
    return {"k_coarse": len(sizes), "profiled": total,
            "max_share": (max(sizes.values()) / total) if total and sizes else 0.0,
            "sizes": sizes, "purity": (pure / total) if total else 0.0,
            "mixed_mediums": mixed,
            "unclassified_mediums": sum(1 for sec in plan.values() if sec == UNCLASSIFIED)}


def sector_coarses(run_id: str) -> dict:
    """Rebuild ``run_id``'s coarse level from the 11 Morningstar sectors (#108, card "Sektörden coarse").

    Every medium is parented to ``sector_coarse_id(run_id, <its modal sector>)`` (``sector_coarse_plan``); the
    coarse rows are the sectors in use, labelled with the sector name, and every other coarse row of the run is
    deleted. Fine and medium rows keep their labels; only the medium ``parent_id`` changes. One transaction, inside
    the run; re-running it gives the same rows. ``theme_runs.k_coarse`` follows.

    Returns ``sector_coarse_summary`` of the plan it wrote.
    """
    themes_all = run_rows("relatedness_themes", run_id)
    meds = sorted((r for r in themes_all if r.get("level") == "medium"), key=lambda r: str(r["theme_id"]))
    old_coarse = [r["theme_id"] for r in themes_all if r.get("level") == "coarse"]
    tsets = _theme_ticker_sets(run_id)
    med_sets = {m["theme_id"]: set(tsets.get(m["theme_id"]) or ()) for m in meds}
    sector_by = _sector_by_ticker()
    plan = sector_coarse_plan(med_sets, sector_by)

    members_of: dict[str, set] = {}
    med_rows: list[dict] = []
    for m in meds:
        mid = m["theme_id"]
        sec = plan[mid]
        members_of.setdefault(sec, set()).update(med_sets[mid])
        row = dict(m)
        row["parent_id"] = sector_coarse_id(run_id, sec)
        med_rows.append(row)
    coarse_rows = [{"theme_id": sector_coarse_id(run_id, sec), "level": "coarse", "parent_id": "",
                    "label": sec, "size": len(names)} for sec, names in sorted(members_of.items())]
    keep = {r["theme_id"] for r in coarse_rows}
    drop = [cid for cid in old_coarse if cid not in keep]

    def _write(tx):
        if drop:
            tx.execute(f"DELETE FROM relatedness_themes WHERE theme_id IN ({', '.join('?' * len(drop))})", drop)
        if coarse_rows:
            tx.write_rows("relatedness_themes", coarse_rows)
        if med_rows:
            tx.write_rows("relatedness_themes", med_rows)
        if run_row is not None:
            tx.write_rows("theme_runs", [run_row])

    run_row = rawstore.get("theme_runs", run_id)
    if run_row is not None:
        run_row = {**run_row, "k_coarse": len(coarse_rows)}
    rawstore.atomic(_write)
    return sector_coarse_summary(med_sets, sector_by, plan)


def adopt_medium_catalog(target_run: str, source_run: str) -> dict:
    """Replace target-run mediums with the source-run medium catalog.

    Score per fine: ``0.5 * ticker_jaccard + 0.5 * label_jaccard`` (after
    ``clean_theme_label``). Ties: larger source size, then smaller id. Adopted
    medium ids keep the source suffix under ``target_run``. Fines reparented;
    unused target mediums dropped. Coarses and fines kept.
    """
    src_meds = {r["theme_id"]: r
                for r in run_rows("relatedness_themes", source_run, "medium")}
    tgt_rows = {r["theme_id"]: r for r in run_rows("relatedness_themes", target_run)}
    tgt_fines = {tid: r for tid, r in tgt_rows.items() if r.get("level") == "fine"}
    tgt_meds = {tid: r for tid, r in tgt_rows.items() if r.get("level") == "medium"}

    src_members: dict[str, set[str]] = {}
    for r in rawstore.query(
            "SELECT ticker, theme_id FROM ticker_themes "
            "WHERE theme_run_id = ? AND level = 'medium'", [source_run]):
        if r["theme_id"] in src_meds:
            src_members.setdefault(r["theme_id"], set()).add(r["ticker"])

    if not src_members:
        return {"adopted": 0, "fines_reparented": 0, "dropped_mediums": 0}

    fine_members: dict[str, set[str]] = {}
    for r in run_rows("relatedness_theme_members", target_run):
        if r["theme_id"] in tgt_fines:
            fine_members.setdefault(r["theme_id"], set()).add(r["ticker"])

    fine_coarse: dict[str, str] = {}
    for fid, frow in tgt_fines.items():
        old_med = frow.get("parent_id") or ""
        fine_coarse[fid] = (tgt_rows.get(old_med) or {}).get("parent_id") or ""

    # source medium id -> list of target fine ids
    assignment: dict[str, list[str]] = {}
    for fid, members in fine_members.items():
        if not members or not src_members:
            continue
        fine_lab = clean_theme_label((tgt_fines[fid].get("label") or ""))
        ranked = sorted(
            (
                (0.5 * _jaccard(members, stickers)
                 + 0.5 * _label_jaccard(
                     fine_lab,
                     clean_theme_label((src_meds[sid].get("label") or ""))),
                 len(stickers), sid)
                for sid, stickers in src_members.items()
            ),
            key=lambda x: (-x[0], -x[1], x[2]))
        assignment.setdefault(ranked[0][2], []).append(fid)

    adopted_rows: list[dict] = []
    fine_updates: list[dict] = []
    adopted_ids: set[str] = set()
    for sid, fids in assignment.items():
        new_id = f"{target_run}:{str(sid).rsplit(':', 1)[-1]}"
        adopted_ids.add(new_id)
        counts: Counter = Counter()
        union: set[str] = set()
        for fid in fids:
            mem = fine_members.get(fid) or set()
            union |= mem
            c = fine_coarse.get(fid) or ""
            if c:
                counts[c] += len(mem)
            frow = dict(tgt_fines[fid])
            frow["parent_id"] = new_id
            fine_updates.append(frow)
        adopted_rows.append({
            "theme_id": new_id, "level": "medium",
            "parent_id": (counts.most_common(1)[0][0] if counts else ""),
            "label": clean_theme_label(src_meds[sid].get("label") or ""),
            "size": len(union),
        })

    dropped = [tid for tid in tgt_meds if tid not in adopted_ids]
    if dropped:
        ph = ", ".join("?" * len(dropped))
        rawstore.execute(
            f"DELETE FROM relatedness_themes WHERE theme_id IN ({ph})",
            list(dropped))
    if adopted_rows:
        rawstore.write_rows("relatedness_themes", adopted_rows)
    if fine_updates:
        rawstore.write_rows("relatedness_themes", fine_updates)
    return {
        "adopted": len(adopted_ids),
        "fines_reparented": len(fine_updates),
        "dropped_mediums": len(dropped),
    }


# Morningstar sector name (core.gics_map.MORNINGSTAR_SECTORS) -> SPDR sector ETF
_SECTOR_SPDR = {
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}


def _sector_by_ticker() -> dict[str, str]:
    """``ticker -> Morningstar sector name`` for every spine row that has a sector value.

    A store the identity master has not re-applied since #266 still holds the old GICS
    spellings (``Information Technology``); ``canonical_sector`` reads them as their
    Morningstar name, so both spellings count toward one sector.
    """
    return {
        r["ticker"]: canonical_sector(r["sector"])
        for r in rawstore.query("SELECT ticker, sector FROM equity_universe")
        if str(r.get("sector") or "").strip()
    }


def _etf_holding_tickers(entry) -> set[str]:
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except (TypeError, ValueError):
            return set()
    if not isinstance(entry, list):
        return set()
    out = set()
    for h in entry:
        if isinstance(h, dict) and h.get("ticker"):
            out.add(str(h["ticker"]).upper())
    return out


def _theme_ticker_sets(run_id: str) -> dict[str, set[str]]:
    """Ticker set per theme node in ``run_id`` (fines + ancestor unions)."""
    themes_rows = run_rows("relatedness_themes", run_id)
    by_id = {r["theme_id"]: r for r in themes_rows}
    fine_set: dict[str, set[str]] = {
        r["theme_id"]: set() for r in themes_rows if r.get("level") == "fine"}
    for m in run_rows("relatedness_theme_members", run_id):
        if m["theme_id"] in fine_set:
            fine_set[m["theme_id"]].add(m["ticker"])

    children: dict[str, list[str]] = {}
    for r in themes_rows:
        p = r.get("parent_id") or ""
        if p:
            children.setdefault(p, []).append(r["theme_id"])

    def _desc_tickers(tid: str) -> set[str]:
        row = by_id.get(tid) or {}
        if row.get("level") == "fine":
            return set(fine_set.get(tid) or ())
        out: set[str] = set()
        for ch in children.get(tid, []):
            out |= _desc_tickers(ch)
        return out

    return {tid: _desc_tickers(tid) for tid in by_id}


def match_theme_etfs(run_id: str) -> dict:
    """Attach top overlapping ROTATION ETFs to each theme node (store-only).

    Candidates are sector/theme ETFs present in ``etf_holdings``. Overlap is
    Jaccard of holdings vs theme ticker set; keep ``|H∩T| >= 2`` and overlap
    ``>= 0.02``, top 3. Always include the modal Morningstar sector's SPDR as rank 1
    when available (even if it failed the thresholds), bumping others.
    """
    from RevenueChaser.core.rotation_reference import ROTATION_ETFS

    candidates = {
        t: info for t, info in ROTATION_ETFS.items()
        if info.get("group") in ("sector", "theme")
    }
    holdings: dict[str, set[str]] = {}
    for t in candidates:
        row = rawstore.get("etf_holdings", t)
        if not row:
            continue
        ht = _etf_holding_tickers(row.get("entry"))
        if ht:
            holdings[t] = ht

    theme_sets = _theme_ticker_sets(run_id)
    sector_by_ticker = _sector_by_ticker()

    out_rows: list[dict] = []
    themes_touched = 0
    for tid, tset in theme_sets.items():
        if not tset:
            continue
        scored = []
        for etf, hset in holdings.items():
            inter = len(hset & tset)
            if inter < 2:
                continue
            ov = _jaccard(hset, tset)
            if ov < 0.02:
                continue
            info = candidates[etf]
            scored.append((ov, inter, etf, info))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))

        secs = [sector_by_ticker[t] for t in tset if sector_by_ticker.get(t)]
        modal = Counter(secs).most_common(1)[0][0] if secs else ""
        spdr = _SECTOR_SPDR.get(modal) if modal else None

        # top 3 by overlap; force sector SPDR to rank 1 if missing
        top_etfs = [s[2] for s in scored[:3]]
        if spdr and spdr in holdings and spdr not in top_etfs:
            top_etfs = [spdr] + [e for e in top_etfs if e != spdr]
            top_etfs = top_etfs[:3]
        elif spdr and spdr in top_etfs:
            top_etfs = [spdr] + [e for e in top_etfs if e != spdr]

        if not top_etfs:
            continue
        themes_touched += 1
        ov_by = {s[2]: s[0] for s in scored}
        for rank, etf in enumerate(top_etfs, start=1):
            info = candidates.get(etf) or ROTATION_ETFS.get(etf) or {}
            ov = ov_by.get(etf)
            if ov is None and etf in holdings:
                ov = _jaccard(holdings[etf], tset)
            out_rows.append({
                "theme_id": tid, "etf": etf,
                "etf_name": info.get("name") or "",
                "group_name": info.get("group") or "",
                "overlap": float(ov or 0.0), "rank": rank,
            })

    rawstore.ensure_table("relatedness_theme_etfs")
    run_themes = list(theme_sets)
    for i in range(0, len(run_themes), 200):
        chunk = run_themes[i:i + 200]
        ph = ", ".join("?" * len(chunk))
        rawstore.execute(
            f"DELETE FROM relatedness_theme_etfs WHERE theme_id IN ({ph})",
            chunk)
    if out_rows:
        rawstore.write_rows("relatedness_theme_etfs", out_rows)
    return {"themes": themes_touched, "rows": len(out_rows)}


def cutover_materialize(run_id: str) -> dict:
    """Switch the boards to ``run_id`` in ONE transaction; older runs are kept.

    The run must have members, otherwise it is refused (``ValueError``) and
    nothing changes. Inside the transaction: ``ticker_themes`` rows for the
    run's fine/medium/coarse chains are rewritten and ``theme_runs.is_current``
    moves to the run. A crash at any point leaves the previous run live.
    ``legacy-keyword`` themes are not copied. Every older run's rows stay in the
    store (card 10, REL-1): readers pick the current run through ``is_current``.
    Returns ``{run_id, rows, profiled}``.
    """
    rawstore.ensure_table("ticker_themes")
    rawstore.ensure_table("relatedness_themes")
    rawstore.ensure_table("relatedness_theme_members")
    rawstore.ensure_table("equity_universe")

    prefix = f"{run_id}:"
    n_members = rawstore.query(
        "SELECT count(*) AS n FROM relatedness_theme_members "
        "WHERE theme_id = ? OR starts_with(theme_id, ?)", [run_id, prefix])
    if not (n_members and n_members[0]["n"]):
        raise ValueError(f"theme run {run_id!r} has no members; cutover refused")
    if rawstore.get("theme_runs", run_id) is None:
        raise ValueError(f"unknown theme run: {run_id}")
    fetched_at = time.time()
    with rawstore.transaction() as tx:
        tx.execute("DELETE FROM ticker_themes WHERE theme_run_id = ?", [run_id])
        # fine
        tx.execute(
            """
            INSERT INTO ticker_themes (ticker, theme_id, theme, level, theme_run_id, fetched_at)
            SELECT m.ticker, f.theme_id, COALESCE(f.label, ''), 'fine', ?, ?
            FROM relatedness_theme_members m
            JOIN relatedness_themes f
              ON f.theme_id = m.theme_id AND f.level = 'fine'
            WHERE m.theme_id = ? OR starts_with(m.theme_id, ?)
            """,
            [run_id, fetched_at, run_id, prefix])
        # medium parent
        tx.execute(
            """
            INSERT INTO ticker_themes (ticker, theme_id, theme, level, theme_run_id, fetched_at)
            SELECT m.ticker, med.theme_id, COALESCE(med.label, ''), 'medium', ?, ?
            FROM relatedness_theme_members m
            JOIN relatedness_themes f
              ON f.theme_id = m.theme_id AND f.level = 'fine'
            JOIN relatedness_themes med
              ON med.theme_id = f.parent_id AND med.level = 'medium'
            WHERE (m.theme_id = ? OR starts_with(m.theme_id, ?))
              AND f.parent_id IS NOT NULL AND f.parent_id <> ''
            """,
            [run_id, fetched_at, run_id, prefix])
        # coarse grandparent
        tx.execute(
            """
            INSERT INTO ticker_themes (ticker, theme_id, theme, level, theme_run_id, fetched_at)
            SELECT m.ticker, c.theme_id, COALESCE(c.label, ''), 'coarse', ?, ?
            FROM relatedness_theme_members m
            JOIN relatedness_themes f
              ON f.theme_id = m.theme_id AND f.level = 'fine'
            JOIN relatedness_themes med
              ON med.theme_id = f.parent_id AND med.level = 'medium'
            JOIN relatedness_themes c
              ON c.theme_id = med.parent_id AND c.level = 'coarse'
            WHERE (m.theme_id = ? OR starts_with(m.theme_id, ?))
              AND med.parent_id IS NOT NULL AND med.parent_id <> ''
            """,
            [run_id, fetched_at, run_id, prefix])
        tx.execute("UPDATE theme_runs SET is_current = CASE WHEN id = ? THEN 1 ELSE 0 END",
                   [run_id])

    rows = rawstore.query(
        "SELECT count(*) AS n FROM ticker_themes WHERE theme_run_id = ?",
        [run_id])
    profiled = rawstore.query(
        """
        SELECT count(DISTINCT m.ticker) AS n
        FROM relatedness_theme_members m
        JOIN relatedness_themes f
          ON f.theme_id = m.theme_id AND f.level = 'fine'
        WHERE m.theme_id = ? OR starts_with(m.theme_id, ?)
        """,
        [run_id, prefix])
    return {
        "run_id": run_id,
        "rows": int((rows[0]["n"] if rows else 0) or 0),
        "profiled": int((profiled[0]["n"] if profiled else 0) or 0),
    }

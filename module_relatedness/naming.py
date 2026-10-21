"""LLM naming for the NMF themes (Phase 8a) — off-peak, thinking-low.

``build_shadow`` writes unlabeled themes; this module gives them human names with
one DeepSeek call per theme, bottom-up (fine -> medium -> coarse) so a parent can
see its children's labels. Calls go through ``core.providers.deepseek.complete``,
which owns the cache and the off-peak gate: at peak the run stops cleanly and
returns the next off-peak time instead of billing peak. The offline tag fallback
stays in ``themes.relabel_from_tags``.

See docs/classification_design.md Phase 8a.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek, grok

from . import themes as themes_mod

logger = logging.getLogger(__name__)

MAX_TOKENS = 3000
MAX_TAGS = 8
MAX_TICKERS = 8
LEVEL_ORDER = {"fine": 0, "medium": 1, "coarse": 2}

SYSTEM = (
    "You name clusters of companies that share a business. Given a cluster's "
    "level, size, representative tags, example companies and (for a parent) its "
    "already-named child clusters, return a JSON object {\"name\": \"...\"}. The "
    "name is 2-5 words in Title Case -- a concise industry or sector phrase. No "
    "ticker symbols, no comma-separated tag lists, no explanations."
)


def _needs_name(label) -> bool:
    """True when a label is missing or is still a tag dump (contains a comma).

    The offline fallback in ``themes._label`` joins tags with ``", "`` and the
    LLM names never contain a comma, so a comma is a reliable tag-dump marker.
    """
    s = str(label or "").strip()
    return (not s) or ("," in s)


def _clean_name(raw) -> str:
    s = re.sub(r"\s+", " ", str(raw or "")).strip().strip(".,;:-\"'")
    if not s or "<" in s or ">" in s:       # markup is never a theme name
        return ""
    if s.islower() or s.isupper():
        s = s.title()
    return s


def _parse_name(raw) -> str:
    """The model's ``{"name": ...}``; empty string when unusable."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return _clean_name(data.get("name"))


def _run_themes(run_id: str) -> dict[str, dict]:
    return {r["theme_id"]: r for r in themes_mod.run_rows("relatedness_themes", run_id)}


def _fine_tags(run_id: str) -> dict[str, list[str]]:
    """``{fine_theme_id: [tag, ...]}`` in stored rank order."""
    raw: dict[str, list] = {}
    for r in themes_mod.run_rows("relatedness_theme_tags", run_id):
        raw.setdefault(r["theme_id"], []).append((r.get("rank") or 0, r["tag"]))
    return {tid: [t for _r, t in sorted(items)] for tid, items in raw.items()}


def _fine_members(run_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in themes_mod.run_rows("relatedness_theme_members", run_id):
        out.setdefault(r["theme_id"], []).append(r["ticker"])
    return out


def _descendant_fines(theme_id, level, parent, fine_ids):
    """The fine themes under ``theme_id`` (itself when ``level == 'fine'``)."""
    if level == "fine":
        return [theme_id]
    if level == "medium":
        return [f for f in fine_ids if parent.get(f) == theme_id]
    return [f for f in fine_ids if parent.get(parent.get(f)) == theme_id]


def _aggregate(tags_by_fine, members_by_fine, fines):
    """Distinctive tags (numeric junk skipped) + example tickers for a cluster."""
    counts: Counter = Counter()
    for f in fines:
        for t in tags_by_fine.get(f, []):
            counts[t] += 1
    tags = [t for t, _n in counts.most_common()
            if themes_mod._useful_tag(t)][:MAX_TAGS]
    tickers, seen = [], set()
    for f in fines:
        for tk in members_by_fine.get(f, []):
            if tk not in seen:
                seen.add(tk)
                tickers.append(tk)
    return tags, tickers[:MAX_TICKERS]


def _child_labels(theme_id, rows) -> list[str]:
    """Already-written labels of ``theme_id``'s direct children (named first)."""
    return sorted(str(r["label"]) for r in rows.values()
                  if (r.get("parent_id") or "") == theme_id and r.get("label"))


def _build_user(level, size, tags, tickers, child_labels) -> str:
    lines = [f"Level: {level}", f"Size: {size} companies",
             "Representative tags: " + (", ".join(tags) or "(none)"),
             "Example companies: " + (", ".join(tickers) or "(none)")]
    if child_labels:
        lines.append("Already-named child clusters: " + ", ".join(child_labels))
    lines.append('Return JSON only: {"name": "..."}')
    return "\n".join(lines)


def _disambiguate(base: str, tags: list[str], used: set[str],
                  doc_freq: dict[str, int] | None = None) -> str:
    """Make ``base`` unique within the run, prefix-preserving for the parent.

    The suffix is the cluster's most distinctive free tag — ``_ordered_tags``'
    rarest-first order, the order ``themes._label`` uses — so two clusters never
    "disagree" because of the alphabet (#433). When every tag is taken, a
    numbered suffix. Not taken: a plain numbered suffix for every collision, or
    a second model call asking for a distinct name (avo's call is pending).
    """
    if base.lower() not in used:
        return base
    for tag in themes_mod._ordered_tags(tags, doc_freq):
        # Title case so ``clean_theme_label`` keeps the parenthetical; a tag it
        # would scrub as junk (digits, ``&``) cannot disambiguate.
        cand = f"{base} ({str(tag).title()})"
        if cand.lower() not in used and themes_mod.clean_theme_label(cand) == cand:
            return cand
    n = 2
    while f"{base} ({n})".lower() in used:
        n += 1
    return f"{base} ({n})"


def _sync_ticker_themes(run_id: str, rows: dict[str, dict]) -> int:
    """Copy fine, medium and coarse labels onto ``ticker_themes.theme`` for this run."""
    labels = {tid: r["label"] for tid, r in rows.items() if r.get("label")}
    if not labels:
        return 0
    updates = []
    for r in rawstore.query(
            "SELECT ticker, theme_id, theme, level, theme_run_id FROM ticker_themes "
            "WHERE theme_run_id = ?", [run_id]):
        want = labels.get(r.get("theme_id"))
        if want and r.get("theme") != want:
            updates.append({**r, "theme": want})
    if updates:
        rawstore.write_rows("ticker_themes", updates)
    return len(updates)


def name_themes(run_id: str | None = None, *, complete=None,
                max_tokens: int = MAX_TOKENS, force: bool = False) -> dict:
    """Name every still-unlabeled theme in a run, bottom-up (fine->medium->coarse).

    ``complete`` is injectable (defaults to ``deepseek.complete``; the CLI's
    ``--provider grok`` passes ``grok.complete``, #388). At peak the
    first call raises ``PeakHoursError``; already-written names are kept and the
    result reports the pending count and the next off-peak time. A spent SuperGrok
    allowance stops the same way and reports ``stopped_reason`` ``allowance`` and
    ``next_reset``. Idempotent:
    themes whose label already looks human (no comma) are skipped unless
    ``force``. Labels of every level are copied onto ``ticker_themes.theme``.
    """
    run_id = run_id or themes_mod.current_run_id()
    if not run_id:
        return {"run_id": None, "named": 0, "pending": 0, "next_off_peak": None}
    caller = complete or deepseek.complete
    # A Grok reply that names nothing is not cached, so the next run asks again (review B S3); DeepSeek's stays as is.
    vkw = {"validate": lambda c: bool(_parse_name(c))} if caller is grok.complete else {}

    rows = _run_themes(run_id)
    if not rows:
        return {"run_id": run_id, "named": 0, "pending": 0, "next_off_peak": None}

    parent = {tid: (r.get("parent_id") or "") for tid, r in rows.items()}
    fine_ids = [tid for tid, r in rows.items() if r.get("level") == "fine"]
    tags_by_fine = _fine_tags(run_id)
    members_by_fine = _fine_members(run_id)
    doc_freq = themes_mod.tag_doc_freq(tags_by_fine.values())

    used = {str(r.get("label") or "").strip().lower()
            for r in rows.values() if not _needs_name(r.get("label"))}
    todo = [dict(r) for r in rows.values()
            if force or _needs_name(r.get("label"))]
    todo.sort(key=lambda r: (LEVEL_ORDER.get(r.get("level"), 9),
                             str(r.get("theme_id"))))

    named, next_off_peak, error = 0, None, None
    allowance = None                      # a SuperGrok allowance stop (#388): the reset time, or "" when unknown
    for row in todo:
        tid, level = row["theme_id"], row.get("level") or "fine"
        fines = _descendant_fines(tid, level, parent, fine_ids)
        if level == "medium" and len(fines) == 1:
            child = rows.get(fines[0]) or {}
            if not _needs_name(child.get("label")):
                # One fine child: the medium theme *is* that theme, so copy the
                # name instead of asking the model for a duplicate (#433). Not
                # taken: asking the model for a distinct name (avo's call is
                # pending). A child that is still unnamed falls through.
                row = dict(row)
                row["label"] = str(child["label"])
                rawstore.write_rows("relatedness_themes", [row])
                rows[tid] = row
                named += 1
                continue
        tags, tickers = _aggregate(tags_by_fine, members_by_fine, fines)
        user = _build_user(level, row.get("size") or 0, tags, tickers,
                           _child_labels(tid, rows))
        try:
            raw = caller(SYSTEM, user, thinking=True, reasoning_effort="low",
                         json_mode=True, max_tokens=max_tokens, **vkw)
        except deepseek.PeakHoursError as exc:
            next_off_peak = exc.next_off_peak.strftime("%Y-%m-%d %H:%M")
            break
        except grok.GrokAllowanceError as exc:
            allowance = exc.next_reset.strftime("%Y-%m-%d %H:%M") if exc.next_reset else ""
            break
        except (deepseek.DeepSeekTransientError, grok.GrokTransientError) as exc:
            error = str(exc)              # the service is down: stop, keep what we have
            break
        except (deepseek.DeepSeekAuthError, grok.GrokAuthError, grok.GrokRequestError):
            raise                         # a bad key (or a wrong Grok endpoint/model) fails every call: write nothing
        except (deepseek.DeepSeekError, grok.GrokError):
            # thinking-low sometimes spends the whole budget on reasoning and
            # returns empty content. Retry once without thinking.
            try:
                raw = caller(SYSTEM, user, thinking=False, json_mode=True,
                             max_tokens=min(1500, max_tokens), **vkw)
            except deepseek.PeakHoursError as exc:
                next_off_peak = exc.next_off_peak.strftime("%Y-%m-%d %H:%M")
                break
            except grok.GrokAllowanceError as exc:
                allowance = exc.next_reset.strftime("%Y-%m-%d %H:%M") if exc.next_reset else ""
                break
            except (deepseek.DeepSeekTransientError, grok.GrokTransientError) as exc:
                error = str(exc)
                break
            except (deepseek.DeepSeekAuthError, grok.GrokAuthError, grok.GrokRequestError):
                raise
            except (deepseek.DeepSeekError, grok.GrokError):
                raw = ""
        base = _parse_name(raw)
        if not base:
            # No usable name: keep the offline tag label (it holds a comma, so the
            # theme still "needs a name") and retry on the next run.
            continue
        row = dict(row)
        row["label"] = _disambiguate(base, tags, used, doc_freq)
        rawstore.write_rows("relatedness_themes", [row])
        rows[tid] = row
        used.add(row["label"].lower())
        named += 1

    _sync_ticker_themes(run_id, rows)
    out = {"run_id": run_id, "named": named, "pending": len(todo) - named,
           "next_off_peak": next_off_peak}
    if allowance is not None:
        out["stopped_reason"] = "allowance"
        out["next_reset"] = allowance or None
    if error:
        out["error"] = error
    return out


__all__ = ["name_themes", "SYSTEM", "MAX_TOKENS"]

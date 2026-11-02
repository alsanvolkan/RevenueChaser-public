"""Writer comparison (#370, avo 2 Nov 2026): up to 100 names profiled by one provider + model, scored, kept apart.

The question it answers: which writer gives the best peers on the same names (Step 0's numbers, #341), so avo can
pick the board's writer. It runs as a dashboard job (``POST /run/relatedness-compare``), so keys come from the
dashboard's own environment, and it writes **no board table**: not ``relatedness_profiles`` / ``_attributes`` /
``_scores``, not ``theme_runs``. What it leaves: the writer's replies in ``llm_cache`` (a re-run costs nothing) and one
file, ``.cache/research/360/<provider>-<model>.json``, in Step 0's ``--scores`` shape (``tickers`` + ``matrix``) plus
the run's counts. Then ``python -m RevenueChaser.module_relatedness.validation report --scores <file>`` scores it,
offline.
"""
from __future__ import annotations

import json
import re
import time

from RevenueChaser.core import rawstore
from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.core.providers import deepseek, grok

from . import config, engine, profiles, similarity, writers

MAX_NAMES = 100
OUT_DIR = OUTPUT_DIR / "research" / "360"


def out_path(provider: str, model: str):
    return OUT_DIR / f"{provider}-{re.sub(r'[^A-Za-z0-9._-]', '_', model)}.json"


def _llm_rows(model: str) -> int:
    try:
        return int(rawstore.query("SELECT count(*) AS n FROM llm_cache WHERE model = ?", [model])[0]["n"])
    except Exception:  # noqa: BLE001 - no cache table yet
        return 0


def run(tickers: list[str], provider: str, model: str) -> dict:
    """Profile ``tickers`` with ``provider`` / ``model``, score every pair, write the scores file; returns the counts."""
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers or len(tickers) > MAX_NAMES:
        raise ValueError(f"give 1 to at most {MAX_NAMES} names; got {len(tickers)}")
    w = writers.get(provider)
    if model not in w.models:
        raise ValueError(f"model {model!r} is not one of {provider}'s: {', '.join(w.models)}")
    started, cache_before = time.time(), _llm_rows(model)
    budget = profiles.CallBudget(None)
    names = engine._company_names(tickers)
    profs: dict[str, dict] = {}
    no_text, empty, stopped = [], [], None
    for t in tickers:
        _name, text = engine._filing_text(t)
        if not text:
            no_text.append(t)
            continue
        try:
            prof = profiles.profile_for(t, names.get(t) or t, text, thinking=False, effort=None, budget=budget,
                                        provider=provider, model=model)
        except deepseek.PeakHoursError:
            stopped = "peak"
            break
        except (deepseek.DeepSeekAuthError, grok.GrokAuthError, grok.GrokAllowanceError) as exc:
            stopped = "allowance" if isinstance(exc, grok.GrokAllowanceError) else "auth"
            break
        except (deepseek.DeepSeekError, grok.GrokError):
            prof = {f: [] for f in config.PROFILE_FIELDS}
        if any(prof.values()):
            profs[t] = prof
        else:
            empty.append(t)
    matrix = [[None if a == b or a not in profs or b not in profs else round(similarity.score_pair(profs[a], profs[b])[0], 6)
               for b in tickers] for a in tickers]
    out = {"provider": provider, "model": model, "tickers": tickers, "matrix": matrix,
           "usable": len(profs), "empty": empty, "no_text": no_text, "attempts": budget.calls,
           "new_replies": _llm_rows(model) - cache_before, "seconds": round(time.time() - started, 1),
           "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if stopped:
        out["stopped_reason"] = stopped
    path = out_path(provider, model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {k: v for k, v in out.items() if k != "matrix"}

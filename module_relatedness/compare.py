"""Writer comparison (#370, avo 2 Nov 2026): up to 100 names profiled by one provider + model, scored, kept apart.

The question it answers: which writer gives the best peers on the same names (Step 0's numbers, #341), so avo can
pick the board's writer. It runs as a dashboard job (``POST /run/relatedness-compare``), so keys come from the
dashboard's own environment, and it writes **no board table**: not ``relatedness_profiles`` / ``_attributes`` /
``_scores``, not ``theme_runs``. What it leaves: the writer's replies in ``llm_cache`` (a re-run costs nothing) and one
file, ``.cache/research/360/<provider>-<model>.json``, in Step 0's ``--scores`` shape (``tickers`` + ``matrix``) plus
the run's counts.

Since 11 Nov 2026 (plan F4) it measures what the board does: each name gets the board's input (``engine._prepare``:
the layer's business section, the v3b context, the fallback chain and the writer's locator call), and the matrix is
similarity v2 (``similarity.blended_peer_lists`` on the local embeddings; ``method`` says ``embedding``, or ``tags``
when no vector could be made). A Claude run also records the subscription windows before and after (``usage``), so
the share of the weekly window the names used is read off the file. Then ``python -m RevenueChaser.module_relatedness.validation report --scores <file>`` scores it,
offline.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from RevenueChaser.core import rawstore
from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.core import fundamentals_layer, subscription_usage
from RevenueChaser.core.providers import claude_code, deepseek, grok
from RevenueChaser.rcdata import opens

from . import config, embeddings, engine, locator, profile_v3, profiles, similarity, writers

MAX_NAMES = 100
OUT_DIR = OUTPUT_DIR / "research" / "360"


def out_path(provider: str, model: str, partial: bool = False):
    """The result file; a run that stopped early writes ``.partial.json`` so a complete result is never overwritten."""
    name = f"{provider}-{re.sub(r'[^A-Za-z0-9._-]', '_', model)}"
    return OUT_DIR / f"{name}{'.partial' if partial else ''}.json"


def _llm_rows(model: str) -> int:
    try:
        return int(rawstore.query("SELECT count(*) AS n FROM llm_cache WHERE model = ?", [model], db="relatedness")[0]["n"])
    except Exception:  # noqa: BLE001 - no cache table yet
        return 0


def _budget(provider: str) -> "profiles.CallBudget":
    """DeepSeek's USD cap as a call budget, as ``engine.build_profiles`` does (PR 371 review S1); the free writers have
    none (avo, 2 Nov 2026)."""
    if provider != "deepseek":
        return profiles.CallBudget(None)
    cap = config.USD_CAP
    return profiles.CallBudget(max(0, int(cap / config.EST_USD_PER_CALL)) if cap > 0 else 0)


def run(tickers: list[str], provider: str, model: str) -> dict:
    """Profile ``tickers`` with ``provider`` / ``model``, score every pair, write the scores file; returns the counts.

    The engine's stop rules hold (PR 371 review S2): a peak, a cap, an auth or allowance failure, a request error
    (every call would fail alike), ``engine.MAX_TRANSIENT_FAILURES`` transient failures, or ``config.GROK_UNUSABLE_STOP``
    unusable replies before the first usable one each end the run with what it has."""
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
    if not tickers or len(tickers) > MAX_NAMES:
        raise ValueError(f"give 1 to at most {MAX_NAMES} names; got {len(tickers)}")
    w = writers.get(provider)
    if model not in w.models:
        raise ValueError(f"model {model!r} is not one of {provider}'s: {', '.join(w.models)}")
    if provider == "grok":
        grok.reset_gate()                              # the first request reads the allowance
    if provider == "claude":
        claude_code.reset_gate()
    usage_before = _usage(provider)
    started, cache_before = time.time(), _llm_rows(model)
    budget = _budget(provider)
    names = engine._company_names(tickers)
    # Off the layer the CIK would come from SEC's ticker map, a network call the comparison never made; the CIK only
    # adds the layer's context, so off the layer it is left empty.
    ciks = engine._cik_for(tickers) if fundamentals_layer.reads_layer() else {}
    profs: dict[str, dict] = {}
    pjs: dict[str, dict] = {}
    no_text, short_text, empty, shell = [], [], [], []
    todo = []
    for t in tickers:
        inp = engine._prepare(t, names.get(t), ciks.get(t))      # the board's input (plan F4)
        if profile_v3.is_shell(None, inp["context"].get("sic")):
            shell.append(t)
        elif inp["fallback"] not in engine.FALLBACKS and not inp.get("locate"):
            (short_text if inp["fallback"] == "short_text" else no_text).append(t)
        else:
            todo.append((t, inp))
    lock, halt = threading.Lock(), threading.Event()
    state = {"stopped": None, "transient": 0}

    def stop(why: str) -> None:
        with lock:
            state["stopped"] = state["stopped"] or why
        halt.set()

    def one(t: str, inp: dict) -> None:
        if halt.is_set():
            return
        try:
            text = inp["text"]
            if inp.get("locate"):
                try:
                    located = locator.locate(t, inp["name"], (inp.get("source") or {}).get("form"),
                                             inp["locate"]["text"], budget=budget, provider=provider, model=model)
                except (deepseek.PeakHoursError, deepseek.DeepSeekAuthError, deepseek.DeepSeekTransientError,
                        grok.GrokAllowanceError, grok.GrokAuthError, grok.GrokRequestError, grok.GrokTransientError):
                    raise
                except (deepseek.DeepSeekError, grok.GrokError):
                    located = None
                if located is not None:
                    text = located[:engine.MAX_FALLBACK_CHARS]
                elif inp["fallback"] not in engine.FALLBACKS:
                    with lock:
                        no_text.append(t)
                    return
            prof = profiles.profile_for(t, inp["name"], text, budget=budget, provider=provider, model=model,
                                        context=inp["context"])
        except profiles.BudgetExceeded:
            return stop("cap")
        except deepseek.PeakHoursError:
            return stop("peak")
        except grok.GrokAllowanceError:
            return stop("allowance")
        except (deepseek.DeepSeekAuthError, grok.GrokAuthError):
            return stop("auth")
        except grok.GrokRequestError:
            return stop("request")
        except (deepseek.DeepSeekTransientError, grok.GrokTransientError):
            with lock:
                state["transient"] += 1
                many = state["transient"] >= engine.MAX_TRANSIENT_FAILURES
            return stop("transient") if many else None
        except (deepseek.DeepSeekError, grok.GrokError):
            prof = {f: [] for f in config.PROFILE_FIELDS}
        v3 = getattr(prof, "v3", None)
        unusable = False
        with lock:
            if v3 is not None and profile_v3.is_shell(v3):
                shell.append(t)
            elif any(prof.values()):
                profs[t] = prof
                if v3 is not None:
                    pjs[t] = v3
            else:
                empty.append(t)
                unusable = not profs and len(empty) >= config.GROK_UNUSABLE_STOP
        if unusable:
            stop("unusable")                           # a wrong endpoint shape: stop before paying for every name

    # The Claude CLI is slow per call and its gate is shared, so its names go out ``COMPARE_WORKERS`` at a time
    # (avo, 11 Nov 2026: "8li paralel"); the paid writers stay one at a time so a stop costs at most one call.
    workers = config.COMPARE_WORKERS if provider == "claude" else 1
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers, **opens.pool_kwargs()) as ex:
            for f in as_completed([ex.submit(one, t, inp) for t, inp in todo]):
                f.result()
    else:
        for t, inp in todo:
            one(t, inp)
            if halt.is_set():
                break
    stopped = state["stopped"]
    order = {t: i for i, t in enumerate(tickers)}       # threads finish in any order; the lists keep the input's
    for names_ in (no_text, empty, shell):
        names_.sort(key=order.__getitem__)
    matrix, method = _matrix(tickers, profs, pjs)
    out = {"provider": provider, "model": model, "tickers": tickers, "matrix": matrix, "method": method,
           "usable": len(profs), "empty": empty, "no_text": no_text, "short_text": short_text, "shell": shell,
           "attempts": budget.calls,
           "new_replies": _llm_rows(model) - cache_before, "seconds": round(time.time() - started, 1),
           "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if usage_before is not None:
        out["usage"] = {"before": usage_before, "after": _usage(provider)}
    if stopped:
        out["stopped_reason"] = stopped
    path = out_path(provider, model, partial=bool(stopped))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {k: v for k, v in out.items() if k != "matrix"} | {"file": str(path)}


def _usage(provider: str) -> dict | None:
    """``{window: percent}`` of the Claude subscription now (``None`` for another writer or an unreadable login)."""
    if provider != "claude":
        return None
    try:
        return {w.key: w.percent for w in subscription_usage.fetch_claude()}
    except Exception:  # noqa: BLE001 - a missing reading is reported as such, never fails the comparison
        return {}


def _matrix(tickers: list[str], profs: dict[str, dict], pjs: dict[str, dict]) -> tuple[list, str]:
    """The pair matrix in ``tickers`` order: similarity v2 when the embedder gives vectors, else the tag cosine.
    shortcut: no sector in the structure term (the board passes the universe's); the same for every writer, so the
    comparison stays fair; add it when the comparison has to match the board's absolute numbers."""
    vectors = {}
    try:
        vectors = embeddings.company_vectors(pjs, write=False) if pjs else {}
    except Exception:  # noqa: BLE001 - no embedder: the tag cosine, said in ``method``
        vectors = {}
    if vectors and len(profs) > 1:
        _peers, upper = similarity.blended_peer_lists(profs, pjs, vectors, None, top=1)
        order = sorted(profs)
        pair, k = {}, 0
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                pair[(order[i], order[j])] = pair[(order[j], order[i])] = round(float(upper[k]), 6)
                k += 1
        return [[pair.get((a, b)) for b in tickers] for a in tickers], "embedding"
    scale = similarity.idf(profs)                          # the board's scoring before #669
    return [[None if a == b or a not in profs or b not in profs
             else round(similarity.score_pair(profs[a], profs[b], scale)[0], 6)
             for b in tickers] for a in tickers], "tags"

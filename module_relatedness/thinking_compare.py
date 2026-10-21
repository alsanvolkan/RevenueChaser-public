"""Thinking vs non-thinking comparison on 50 pilot tickers.

Runs the same profile prompt three ways — non-thinking, thinking/low and
thinking/high — on 50 companies that already have a non-thinking profile, then
measures how much thinking changes the tags, the pair scores and the peer lists.

    python -m RevenueChaser.module_relatedness.thinking_compare

Writes the report and its JSON sidecar under ``.cache/relatedness/`` (regenerable
artifacts). Profiles are read from the durable store, never a JSON file.
"""
from __future__ import annotations

import itertools
import json
import statistics
import time

from scipy.stats import spearmanr

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek

from . import config, engine, profiles, similarity, store

_REPORT = config.RELATEDNESS_JSON.parent / "validation_thinking_vs_nonthinking_50.md"
_SIDE = config.RELATEDNESS_JSON.parent / "thinking_compare.json"
N = 50
MODES = (("non-thinking", False, None), ("thinking-low", True, "low"),
         ("thinking-high", True, "high"))


def _pick(n: int = N) -> list[str]:
    """50 pilot tickers (after the first 20) that have a usable profile."""
    pilot = engine.select_pilot()
    have = store.load_profiles()
    out = []
    for t in pilot[20:]:
        if t in have and any(have[t].values()):
            out.append(t)
        if len(out) >= n:
            break
    return out


def _tags(profile: dict) -> set[str]:
    return {t for v in (profile or {}).values() for t in (v or [])}


def _pairs(profiles_by_ticker: dict[str, dict]) -> dict[tuple[str, str], float]:
    ts = sorted(profiles_by_ticker)
    out = {}
    for a, b in itertools.combinations(ts, 2):
        out[(a, b)] = similarity.score_pair(profiles_by_ticker[a], profiles_by_ticker[b])[0]
    return out


def _peer_lists(profiles_by_ticker: dict[str, dict], k: int = 5) -> dict[str, list[str]]:
    ts = sorted(profiles_by_ticker)
    out = {}
    for a in ts:
        scored = sorted(((b, similarity.score_pair(profiles_by_ticker[a], profiles_by_ticker[b])[0])
                         for b in ts if b != a), key=lambda x: -x[1])
        out[a] = [b for b, _ in scored[:k]]
    return out


def main() -> int:
    tickers = _pick()
    print(f"comparing {len(tickers)} tickers across {len(MODES)} modes")

    results: dict[str, dict[str, dict]] = {m[0]: {} for m in MODES}
    timing: dict[str, float] = {}
    for label, thinking, effort in MODES:
        t0 = time.time()
        for t in tickers:
            row = rawstore.get("chain_filings", t) or {}
            text = row.get("item_text") or ""
            name = row.get("name") or t
            if not text:
                continue
            try:
                results[label][t] = profiles.profile_for(
                    t, name, text, thinking=thinking, effort=effort)
            except deepseek.PeakHoursError:
                print(f"{label}: peak hours — stopping this mode")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{label} {t} failed: {exc}")
        timing[label] = round(time.time() - t0, 1)
        print(f"  {label}: {len(results[label])} profiles in {timing[label]}s")

    non = results["non-thinking"]
    low = results["thinking-low"]
    high = results["thinking-high"]
    common = sorted(set(non) & set(low) & set(high))

    # richness + per-ticker tag overlap
    def rich(mode): return statistics.mean([len(_tags(mode[t])) for t in common]) if common else 0
    def overlap(a, b):
        vals = []
        for t in common:
            ta, tb = _tags(a[t]), _tags(b[t])
            vals.append(len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0)
        return statistics.mean(vals) if vals else 0.0

    # pair-score impact
    pn, pl, ph = _pairs({t: non[t] for t in common}), _pairs({t: low[t] for t in common}), _pairs({t: high[t] for t in common})
    keys = sorted(pn)
    def deltas(a, b): return [abs(a[k] - b[k]) for k in keys]
    def rho(a, b):
        return spearmanr([a[k] for k in keys], [b[k] for k in keys]).correlation

    # peer-list agreement (top-5) vs non-thinking
    def peer_agree(a, b):
        pa, pb = _peer_lists(a), _peer_lists(b)
        return statistics.mean([len(set(pa[t]) & set(pb[t])) / 5 for t in common]) if common else 0.0
    def top1_change(a):
        pn_, pa_ = _peer_lists(non, 1), _peer_lists(a, 1)
        return sum(1 for t in common if pn_[t] and pa_[t] and pn_[t][0] != pa_[t][0]) / len(common) if common else 0.0

    # token usage from the durable llm_cache
    def usage(thinking, effort):
        from RevenueChaser.core import rawstore
        tot_in = tot_out = tot_reason = n = 0
        for r in rawstore.get_all("llm_cache"):
            if bool(r.get("thinking")) != thinking or (r.get("effort") or None) != effort:
                continue
            resp = r.get("response")
            u = (resp.get("usage") if isinstance(resp, dict) else None) or {}
            n += 1
            tot_in += u.get("prompt_tokens", 0)
            tot_out += u.get("completion_tokens", 0)
            tot_reason += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        return n, tot_in, tot_out, tot_reason

    u_non, u_low, u_high = usage(False, None), usage(True, "low"), usage(True, "high")

    md = f"""# Thinking vs non-thinking — {len(common)} tickers

Same prompt and filing text; only the thinking flag / effort differs.
Modes: non-thinking, thinking (effort low), thinking (effort high).

## Profile richness (mean tags per company)
| Mode | mean tags |
|---|---|
| non-thinking | {rich(non):.1f} |
| thinking-low | {rich(low):.1f} |
| thinking-high | {rich(high):.1f} |

## Tag agreement with non-thinking (mean Jaccard per company)
| Comparison | overlap |
|---|---|
| thinking-low vs non-thinking | {overlap(low, non):.2f} |
| thinking-high vs non-thinking | {overlap(high, non):.2f} |

## Effect on pair scores ({len(keys)} pairs)
| Comparison | mean |delta| | Spearman rho |
|---|---|---|
| low vs non | {statistics.mean(deltas(pl, pn)):.4f} | {rho(pl, pn):.3f} |
| high vs non | {statistics.mean(deltas(ph, pn)):.4f} | {rho(ph, pn):.3f} |
| high vs low | {statistics.mean(deltas(ph, pl)):.4f} | {rho(ph, pl):.3f} |

## Peer lists (top-5) vs non-thinking
| Comparison | top-5 overlap | #1 peer changed |
|---|---|---|
| low vs non | {peer_agree(low, non):.2f} | {top1_change(low):.0%} |
| high vs non | {peer_agree(high, non):.2f} | {top1_change(high):.0%} |

## Token usage (this session's cache)
| Mode | calls | prompt tok | completion tok | reasoning tok |
|---|---|---|---|---|
| non-thinking | {u_non[0]} | {u_non[1]} | {u_non[2]} | {u_non[3]} |
| thinking-low | {u_low[0]} | {u_low[1]} | {u_low[2]} | {u_low[3]} |
| thinking-high | {u_high[0]} | {u_high[1]} | {u_high[2]} | {u_high[3]} |

Wall time this run (includes cache hits): {timing}

## Reading
- Higher tag overlap + high Spearman rho => thinking mostly adds detail without
  changing conclusions.
- Low top-5 overlap or a high #1-change rate => thinking materially changes the
  peers, so the mode choice matters.
"""
    _REPORT.write_text(md, encoding="utf-8")
    _SIDE.write_text(json.dumps({
        "tickers": common, "modes": list(results), "timing": timing,
        "richness": {m: rich(results[m]) for m in results},
        "overlap": {"low": overlap(low, non), "high": overlap(high, non)},
        "pair_delta": {"low": statistics.mean(deltas(pl, pn)),
                       "high": statistics.mean(deltas(ph, pn))},
        "spearman": {"low": rho(pl, pn), "high": rho(ph, pn), "high_low": rho(ph, pl)},
        "peer_top5": {"low": peer_agree(low, non), "high": peer_agree(high, non)},
        "top1_change": {"low": top1_change(low), "high": top1_change(high)},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", _REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

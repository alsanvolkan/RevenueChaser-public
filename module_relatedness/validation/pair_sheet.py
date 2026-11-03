"""The pair check sheet: generator and scorer (Relatedness v2 Step 0, #341). No store, no LLM, no network.

Metric 4 of the method review: 50 random pairs scored in the 90th to 99th percentile and 50 random pairs below the
50th, shuffled; avo marks each related or not. Target: >= 80% agreement. Done once per method change.

The score matrix is an argument (``tickers`` plus an ``n x n`` array); the sheet is built from each unordered pair
once, with the pair's score the mean of its two directed scores where both exist (a symmetric matrix is unchanged).
**A pair scored in neither direction counts as 0** and the percentiles are taken over every pair of names
(review B1, #342): the store keeps only each name's top-25 peers (``relatedness_scores``), so dropping the missing
pairs would draw the "low" band from near-neighbours. On such a matrix most pairs tie at 0, so when fewer than
``n_low`` pairs lie strictly below the median the low band becomes the pairs at or below it (``low_rule`` in the
key says which), and a high pair must also score above the median, since p90 is itself 0 when fewer than 10% of
pairs are scored. Under 1% scored pairs (``MIN_SCORED_SHARE``) no high band is left at all, so ``generate`` refuses
the matrix and asks for a dense one. The random seed is a parameter: the same matrix and seed give the same sheet.

Both files are created, never overwritten, unless ``force`` is passed (review R4): a marked sheet is avo's work.
The scorer reads a sheet saved by Excel too, with a byte-order mark or ``;`` / tab as the delimiter (review R3).

Two files are written: the **sheet** (CSV ``pair_no, a, b, related``) avo fills in, which shows neither the score
nor the band so the marking is blind, and the **key** (JSON) that the scorer reads to know each pair's band.
A mark is ``y/yes/1/true/related`` or ``n/no/0/false/not/unrelated``; an empty mark, and a key pair missing from the
sheet, count as unmarked. ``passes`` is set only when every pair of the key is marked (review S3).
"""
from __future__ import annotations

import csv
import io
import json
import warnings
from pathlib import Path
from typing import Sequence

import numpy as np

from .metrics import PAIR_AGREEMENT_TARGET

N_HIGH = 50
N_LOW = 50
HIGH_BAND = (90.0, 99.0)   # percentiles, inclusive
LOW_BELOW = 50.0           # percentile, exclusive
# Below this share of scored pairs p99 is itself 0 and the high band is empty (review R1, #342: 3,500 names with
# top-25 peers is 0.8%), so the sheet asks for a dense matrix instead. 1,503 names (the pilot) is about 1.8%.
MIN_SCORED_SHARE = 0.01

SHEET_FIELDS = ["pair_no", "a", "b", "related"]
_YES = {"y", "yes", "1", "true", "related", "r"}
_NO = {"n", "no", "0", "false", "not", "unrelated", "not related"}


def _pair_scores(tickers: Sequence[str], scores: np.ndarray):
    s = np.asarray(scores, dtype=float)
    if s.shape != (len(tickers), len(tickers)):
        raise ValueError(f"score matrix is {s.shape}, expected {(len(tickers), len(tickers))}")
    n = len(tickers)
    if n >= 2:
        scored = ~np.isnan(s)
        scored |= scored.T
        np.fill_diagonal(scored, False)
        share = float(scored.sum()) / (n * (n - 1))
        del scored
        if share < MIN_SCORED_SHARE:
            raise ValueError(
                f"only {share:.2%} of pairs are scored (under {MIN_SCORED_SHARE:.0%}), so the 90th-99th percentile "
                f"band is empty: pass a dense score matrix (every pair scored), not the stored top-25 peers")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # a pair scored in neither direction: NaN, then 0 below
        sym = np.nanmean(np.stack([s, s.T]), axis=0)
    iu, ju = np.triu_indices(len(tickers), 1)
    return iu, ju, np.nan_to_num(sym[iu, ju], nan=0.0)


def generate(tickers: Sequence[str], scores: np.ndarray, seed: int,
             n_high: int = N_HIGH, n_low: int = N_LOW) -> list[dict]:
    """``n_high`` pairs from the 90th-99th percentile and ``n_low`` from below the 50th, shuffled.

    Each pair is ``{pair_no, a, b, band, score, low_rule}``; ``pair_no`` runs 1..n in sheet order. Raises
    ``ValueError`` when a band holds fewer pairs than asked for.
    """
    iu, ju, vals = _pair_scores(tickers, scores)
    if not len(vals):
        raise ValueError("the score matrix has no pairs")
    p_lo, p_hi = np.percentile(vals, HIGH_BAND)
    p50 = np.percentile(vals, LOW_BELOW)
    # Above the median too: on a top-k matrix p90 is often 0 itself, and an unscored pair is never "high".
    high = np.flatnonzero((vals >= p_lo) & (vals <= p_hi) & (vals > p50))
    low, low_rule = np.flatnonzero(vals < p50), "< p50"
    if len(low) < n_low:                       # most pairs tie at the median (a top-k matrix: many zeros)
        low, low_rule = np.flatnonzero(vals <= p50), "<= p50"
    if len(high) < n_high:
        raise ValueError(f"only {len(high)} pairs in the high band (p90-p99), {n_high} needed")
    if len(low) < n_low:
        raise ValueError(f"only {len(low)} pairs in the low band (<= p50), {n_low} needed")
    rng = np.random.default_rng(seed)
    picked = [(int(k), "high") for k in rng.choice(high, n_high, replace=False)]
    picked += [(int(k), "low") for k in rng.choice(low, n_low, replace=False)]
    order = rng.permutation(len(picked))
    out = []
    for no, o in enumerate(order, start=1):
        k, band = picked[o]
        out.append({"pair_no": no, "a": tickers[iu[k]], "b": tickers[ju[k]], "band": band,
                    "score": float(vals[k]), "low_rule": low_rule})
    return out


def write_sheet(pairs: list[dict], sheet_path: Path, key_path: Path, seed: int, method: str = "",
                force: bool = False) -> None:
    """Write the blind sheet (CSV) and its key (JSON: seed, method, band and score per pair number).

    Refuses (``FileExistsError``, naming the file) when either file exists, before writing anything, unless
    ``force``: a sheet avo has marked is never overwritten by accident.
    """
    sheet_path, key_path = Path(sheet_path), Path(key_path)
    if not force:
        for path in (sheet_path, key_path):
            if path.exists():
                raise FileExistsError(f"{path} already exists; pass force (--force) to overwrite it")
    mode = "w" if force else "x"
    with sheet_path.open(mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_FIELDS)
        w.writeheader()
        for p in pairs:
            w.writerow({"pair_no": p["pair_no"], "a": p["a"], "b": p["b"], "related": ""})
    key = {"seed": seed, "method": method, "target": PAIR_AGREEMENT_TARGET,
           "low_rule": pairs[0].get("low_rule", "< p50") if pairs else None,
           "pairs": {str(p["pair_no"]): {"a": p["a"], "b": p["b"], "band": p["band"], "score": p["score"]}
                     for p in pairs}}
    with key_path.open(mode, encoding="utf-8") as fh:
        fh.write(json.dumps(key, indent=1))


def _mark(value: str, pair_no: str) -> bool | None:
    v = (value or "").strip().lower()
    if not v:
        return None
    if v in _YES:
        return True
    if v in _NO:
        return False
    raise ValueError(f"pair {pair_no}: cannot read the mark {value!r}; use y/n (related / not)")


def _read_rows(sheet_path: Path) -> list[dict]:
    """The sheet's rows; a byte-order mark is dropped and the delimiter (``,`` ``;`` or tab) is detected."""
    text = sheet_path.read_text(encoding="utf-8-sig")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    header = [h.strip() for h in (reader.fieldnames or [])]
    if "pair_no" not in header:
        raise ValueError(f"{sheet_path} has no header row {', '.join(SHEET_FIELDS)} (found: {header})")
    reader.fieldnames = header
    return list(reader)


def score(sheet_path: Path, key_path: Path, target: float = PAIR_AGREEMENT_TARGET) -> dict:
    """Agreement of avo's marks with the bands: related on a high pair, not related on a low pair.

    ``agreement`` is over the marked rows; ``passes`` is ``None`` until every pair of the key is marked, so a
    cut-down sheet cannot pass. A ``pair_no`` that appears twice is refused.
    """
    key = json.loads(Path(key_path).read_text(encoding="utf-8"))["pairs"]
    rows = _read_rows(Path(sheet_path))
    n_agree = n_marked = n_unmarked = 0
    seen: set[str] = set()
    for r in rows:
        no = str(r["pair_no"]).strip()
        if no not in key:
            raise ValueError(f"pair {no} is not in the key")
        if no in seen:
            raise ValueError(f"pair {no} appears more than once on the sheet")
        seen.add(no)
        k = key[no]
        if (r["a"], r["b"]) != (k["a"], k["b"]):
            raise ValueError(f"pair {no} is {r['a']}/{r['b']} on the sheet but {k['a']}/{k['b']} in the key")
        m = _mark(r.get("related", ""), no)
        if m is None:
            n_unmarked += 1
            continue
        n_marked += 1
        n_agree += int(m == (k["band"] == "high"))
    n_missing = len(key) - len(seen)
    n_unmarked += n_missing
    agreement = n_agree / n_marked if n_marked else None
    complete = n_marked == len(key)
    return {"agreement": agreement, "n_agree": n_agree, "n_marked": n_marked, "n_unmarked": n_unmarked,
            "n_missing": n_missing, "target": target,
            "passes": agreement >= target if complete and agreement is not None else None}

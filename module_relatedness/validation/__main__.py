"""Step 0 validation CLI for Relatedness v2 (#341). Offline: no LLM, no provider, no network, no store write.

    python -m RevenueChaser.module_relatedness.validation report --scores S.json
        [--themes T.json] [--industries I.json] [--seeds SEEDS.json]
        [--etf-holdings H.json | --etf-holdings-from-store] [--out REPORT.json]
    python -m RevenueChaser.module_relatedness.validation pairs-generate --scores S.json --seed N
        --sheet SHEET.csv --key KEY.json [--method NAME] [--force]
    python -m RevenueChaser.module_relatedness.validation pairs-score --sheet SHEET.csv --key KEY.json

Input files (JSON):

- ``--scores``: ``{"tickers": [...], "matrix": [[...], ...]}`` (row = name, column = peer, ``null`` = no score) or
  ``{"pairs": [{"ticker", "peer", "score"}, ...]}``. Its tickers are the universe the peer sets are loaded against.
- ``--themes``: ``{"fine": {id: {"label", "members"}}, "medium": {...}}``.
- ``--industries``: ``{ticker: GICS industry}``.
- ``--seeds``: ``[{ticker: theme label}, ...]``, one hard assignment per seed run.
- ``--etf-holdings``: ``{etf: [tickers]}``. ``--etf-holdings-from-store`` reads ``etf_holdings`` read-only instead;
  on a real store run it with ``RC_STORE_AUTO_ENSURE=0`` (``CLAUDE.md``, Data safety).

The report prints one JSON object (or writes it to ``--out``); ``theme_runs`` in it holds the five column values.
``pairs-generate`` writes the blind sheet avo marks and its key, and refuses to overwrite either without
``--force``; ``pairs-score`` prints the agreement.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pair_sheet, report


def _load(path: str | None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _scores(path: str):
    data = _load(path)
    if "pairs" in data:
        return report.matrix_from_pairs(data["pairs"])
    return report.matrix_from_dense(data["tickers"], data["matrix"])


def _emit(obj, out: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=1, default=float)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)


def _report(args) -> int:
    tickers, m = _scores(args.scores)
    themes = _load(args.themes) or {}
    if args.etf_holdings_from_store:
        from .peer_sets import etf_holdings_from_store
        holdings = etf_holdings_from_store()
    else:
        holdings = _load(args.etf_holdings)
    out = report.build_report(tickers, m, fine=themes.get("fine"), medium=themes.get("medium"),
                              industry_by=_load(args.industries), seeds=_load(args.seeds),
                              etf_holdings=holdings)
    _emit(out, args.out)
    return 0


def _pairs_generate(args) -> int:
    tickers, m = _scores(args.scores)
    pairs = pair_sheet.generate(tickers, m, seed=args.seed)
    pair_sheet.write_sheet(pairs, Path(args.sheet), Path(args.key), seed=args.seed, method=args.method,
                           force=args.force)
    _emit({"sheet": args.sheet, "key": args.key, "pairs": len(pairs), "seed": args.seed}, None)
    return 0


def _pairs_score(args) -> int:
    _emit(pair_sheet.score(Path(args.sheet), Path(args.key)), None)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m RevenueChaser.module_relatedness.validation",
                                 description="Relatedness v2 validation (offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="score a relatedness run on the five metrics")
    r.add_argument("--scores", required=True)
    r.add_argument("--themes")
    r.add_argument("--industries")
    r.add_argument("--seeds")
    src = r.add_mutually_exclusive_group()
    src.add_argument("--etf-holdings")
    src.add_argument("--etf-holdings-from-store", action="store_true")
    r.add_argument("--out")
    r.set_defaults(func=_report)

    g = sub.add_parser("pairs-generate", help="write the 100-pair check sheet and its key")
    g.add_argument("--scores", required=True)
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--sheet", required=True)
    g.add_argument("--key", required=True)
    g.add_argument("--method", default="")
    g.add_argument("--force", action="store_true", help="overwrite an existing sheet and key (a marked sheet is lost)")
    g.set_defaults(func=_pairs_generate)

    s = sub.add_parser("pairs-score", help="agreement of a marked sheet with its key")
    s.add_argument("--sheet", required=True)
    s.add_argument("--key", required=True)
    s.set_defaults(func=_pairs_score)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

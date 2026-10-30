"""Off-peak CLI for the Relatedness board.

    python -m RevenueChaser.module_relatedness --run [--limit N] [--cap USD] [--floor M | --tickers A,B]
    python -m RevenueChaser.module_relatedness --status
    python -m RevenueChaser.module_relatedness --name-themes [--run-id ID]
    python -m RevenueChaser.module_relatedness --rebuild-themes [--run-id ID] [--name | --build-only]
        [--floor M | --tickers A,B] [--nmf-init INIT] [--nmf-max-iter N] [--coarse {nmf,sector}]
    python -m RevenueChaser.module_relatedness --cutover --run-id ID
        [--adopt-medium-from ID] [--coarse {nmf,sector}]

``--coarse sector`` (#108) builds the coarse level from the 11 Morningstar sectors instead of the agglomerative cut;
once the live run is a sector run, every later --rebuild-themes / --cutover must pass it too (the default is nmf).

The run is off-peak gated (``core.offpeak``): at peak it stops cleanly and reports
the next off-peak time; completed profiles are durable and resume next run. Theme
naming (``--name-themes``) is off-peak too and stops the same way, keeping the
names already written. ``--cutover`` is store-only (no LLM) and takes a shadow
run id: the current (live) run is refused, so build a new one with
``--rebuild-themes``. ``--rebuild-themes`` refuses the current run id too.

``--floor`` (USD millions) and ``--tickers`` choose the names (#108 S0): the names ``--run`` profiles, and the
universe ``--rebuild-themes`` fits. Without either, ``--run`` profiles the whole classification universe and the fit
covers it, as before. They go with ``--run`` and ``--rebuild-themes`` only; ``--status``, ``--name-themes`` and
``--cutover`` refuse them. WARNING: ``--run`` with a selection is a full Fetch of exactly those names. It downloads
their missing filings from SEC, calls DeepSeek for every name without a current profile, and replaces the board:
all peer scores, ``relatedness_latest.json`` and a result-history run then cover only the selection until the next
default run. Run it only with the user's typed words, never under the standing permission.
``--rebuild-themes --build-only`` writes the shadow run and stops: no cutover, no naming; name
it with ``--name-themes --run-id ID`` and switch with ``--cutover --run-id ID``.
``--nmf-init`` and ``--nmf-max-iter`` set the NMF fit of a ``--rebuild-themes`` run (default ``themes.DEFAULT_FIT``,
nndsvda / 500 since #108 S1); the summary names the fit used, as ``theme_runs`` has no column for it.
``--k-medium N`` sets that run's medium theme cut; without it the engine derives ``max(2, n_fine // 2)`` from the
realised fine count, which is the live default and what #57 asks to compare. The value is still clamped below
``n_fine``, so the summary names both: ``k_medium_requested`` (``"default"`` when the flag is absent) beside the
realised ``k_fine`` and ``k_medium``. A sweep is therefore three ``--rebuild-themes --build-only --k-medium N``
runs, which make no provider call and no cutover.

Grok writer (#388, avo 2026-10-16 "grok kullan direk"; manual, never scheduled):

    python -m RevenueChaser.module_relatedness --grok-ping
    python -m RevenueChaser.module_relatedness --run --provider grok --limit N [--floor M | --tickers A,B]
    python -m RevenueChaser.module_relatedness --name-themes --provider grok [--run-id ID]
    python -m RevenueChaser.module_relatedness --rebuild-themes --name --provider grok [--run-id ID] ...

``--provider grok`` writes through the SuperGrok subscription (``core/providers/grok.py``): no off-peak gate, the
allowance is read before each batch of calls and a spent allowance stops the run cleanly (exit 0, pending reported).
``--limit N`` then caps the **calls** (it never shortens the universe the board covers) and is required; ``--cap`` is
USD and DeepSeek's only. The summary names the provider, the model, the calls (0 USD each), done, pending and stale.
``--grok-ping`` makes exactly one live call with the profile prompt and a run's parameters, writes nothing, and says
whether the endpoint and model answer a usable profile. The default provider is DeepSeek, so RC-Relatedness-OffPeak
(``--run --cap 8``) is unchanged.
"""
from __future__ import annotations

import argparse
import json

from RevenueChaser.core import offpeak, rawstore, subscription_usage
from RevenueChaser.core.providers import deepseek, grok

from . import config, engine


# Why a run stopped that the next (off-peak) run cannot fix on its own, so the task exits 1: a rejected
# credential and repeated transient failures; with Grok also a rejected request (endpoint, model) and a run whose
# first replies were all unusable. Peak, the SuperGrok allowance, the cost cap and names still pending (no filing text
# yet) are the normal night-to-night state, so they exit 0 and the scheduled task stays green (C-F1).
_FAILED_STOP_REASONS = frozenset({"auth", "transient", "request", "unusable"})
_GROK_FAILURES = (deepseek.DeepSeekAuthError, grok.GrokAuthError, grok.GrokRequestError)
# The ping's company: a made-up, public-domain-style description (no real issuer, no store read).
_PING_TEXT = ("Example Semiconductor Corp designs graphics processors and networking chips sold to cloud data "
              "center operators and PC makers; manufacturing is outsourced to foundries.")


def _last_run() -> dict:
    try:
        row = rawstore.get("run_registry", "relatedness_run")
    except Exception:  # noqa: BLE001 - table may not exist yet
        return {}
    return row.get("value") if row else {}


def _summary(**kwargs) -> str:
    fields = ("provider", "run_id", "named", "copied", "pending", "next_off_peak", "stopped_reason",
              "next_reset", "themes", "rows", "adopted", "fines_reparented", "dropped_mediums", "profiled",
              "unthemed", "out_of_universe", "fit", "k_fine", "k_medium", "k_medium_requested", "coarse",
              "k_coarse", "error")
    return json.dumps({k: kwargs.get(k) for k in fields
                       if k in kwargs}, ensure_ascii=False)


def _grok_ping() -> int:
    """One live SuperGrok call with the profile prompt and a run's parameters; writes nothing (no cache, no store).

    Prints ``{provider, model, endpoint, ok, fields, calls[, error]}``; exit 0 when the reply parses into a profile.
    """
    from . import profiles
    grok.reset_gate()
    before = grok.calls()
    out: dict = {"provider": "grok", "model": grok.MODEL, "endpoint": grok.endpoint()}
    try:
        raw = grok.complete(profiles.system_prompt(), profiles.build_user("Example Semiconductor Corp", _PING_TEXT),
                            thinking=config.THINKING, reasoning_effort=config.REASONING_EFFORT,
                            max_tokens=profiles.token_budget(config.THINKING), json_mode=True, use_cache=False)
        fields = [f for f, v in profiles.parse_profile(raw).items() if v]
        out.update(ok=bool(fields), fields=fields)
        if not fields:
            out["error"] = f"the reply held no usable profile: {raw[:200]}"
    except grok.GrokError as exc:
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    out["calls"] = grok.calls() - before
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


def _grok_run(chosen, limit: int) -> int:
    """``--run --provider grok --limit N``: the summary names the provider, the model and the calls (0 USD each)."""
    before = grok.calls()
    try:
        payload = engine.run(tickers=chosen, provider="grok", max_calls=limit)
    except engine.AlreadyRunning as exc:
        print(json.dumps({"skipped": str(exc)}, ensure_ascii=False))
        return 1
    reason = payload.get("stopped_reason")
    out = {"provider": "grok", "model": grok.MODEL, "calls": grok.calls() - before,
           "cost_usd": grok.COST_USD_PER_CALL * (grok.calls() - before),
           "done": payload.get("n"), "pending": payload.get("pending") or 0,
           "stale": sum(1 for c in payload.get("companies") or [] if c.get("stale"))}
    if reason:
        out["stopped_reason"] = reason
    if payload.get("next_reset"):
        out["next_reset"] = payload["next_reset"]
    if payload.get("error"):
        out["error"] = payload["error"]
    print(json.dumps(out, ensure_ascii=False))
    return 1 if reason in _FAILED_STOP_REASONS else 0


def _floor(value: str) -> float:
    """A market-cap floor in USD millions; above 0 (the universe at 0 would also keep the names with no cap)."""
    floor = float(value)
    if not floor > 0:
        raise argparse.ArgumentTypeError("--floor is USD millions and must be above 0")
    return floor


def _max_iter(value: str) -> int:
    """NMF iterations: a whole number, at least 1."""
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("--nmf-max-iter must be at least 1")
    return n


def _k_medium(value: str) -> int:
    """The medium cut: a whole number, at least 1. The engine still clamps it below the realised fine count."""
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("--k-medium must be at least 1")
    return n


def _selection(args) -> list[str] | None:
    """The names ``--floor`` or ``--tickers`` choose; None keeps the default (the universe, no cap floor)."""
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.floor is not None:
        from RevenueChaser.core.universe import classification_universe
        return classification_universe(args.floor * 1_000_000)
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Relatedness board (off-peak LLM)")
    p.add_argument("--run", action="store_true", help="run the profile job")
    p.add_argument("--limit", type=int, default=None, help="cap tickers")
    p.add_argument("--cap", type=float, default=None, help="USD hard-stop for this run")
    p.add_argument("--status", action="store_true", help="show last run + off-peak state")
    p.add_argument("--name-themes", action="store_true",
                   help="LLM-name the current (or --run-id) theme run only")
    p.add_argument("--rebuild-themes", action="store_true",
                   help="build a new NMF run and switch to it atomically (refused if empty)")
    p.add_argument("--cutover", action="store_true",
                   help="finish a shadow --run-id (labels, ETFs) and switch to it; the "
                        "current run is refused")
    p.add_argument("--adopt-medium-from", default=None, metavar="ID",
                   help="with --cutover/--rebuild-themes: adopt medium catalog from ID")
    p.add_argument("--name", action="store_true",
                   help="with --rebuild-themes, also LLM-name the new run")
    p.add_argument("--run-id", default=None, help="theme run id (default: current)")
    from .themes import COARSE_MODES, DEFAULT_FIT, NMF_INITS, NmfFit
    p.add_argument("--coarse", choices=COARSE_MODES, default="nmf",
                   help="coarse level at --cutover / --rebuild-themes: nmf (agglomerative + rebalance, default) "
                        "or sector (one coarse per Morningstar sector, #108)")
    p.add_argument("--build-only", action="store_true",
                   help="with --rebuild-themes: write the shadow run only (no cutover, no naming)")
    pick = p.add_mutually_exclusive_group()
    warn = (" With --run: a full Fetch of exactly these names (SEC filings, DeepSeek profiles) that replaces "
            "every peer score and relatedness_latest.json until the next default run.")
    pick.add_argument("--floor", type=_floor, default=None, metavar="M",
                      help="operating names with a market cap >= M USD millions (default: no floor, Tiers A and B)."
                           + warn)
    pick.add_argument("--tickers", default=None, metavar="A,B",
                      help="exactly these tickers, comma-separated." + warn)
    p.add_argument("--nmf-init", choices=NMF_INITS, default=None,
                   help=f"with --rebuild-themes: the NMF init (default {DEFAULT_FIT.init})")
    p.add_argument("--k-medium", type=_k_medium, default=None, metavar="N",
                   help="the medium theme cut of a --rebuild-themes run (default: the engine's "
                        "max(2, n_fine // 2)); still clamped below the realised fine count")
    p.add_argument("--nmf-max-iter", type=_max_iter, default=None, metavar="N",
                   help=f"with --rebuild-themes: the NMF iterations (default {DEFAULT_FIT.max_iter})")
    p.add_argument("--provider", choices=("deepseek", "grok"), default="deepseek",
                   help="the LLM that writes profiles (--run) or theme names (--name-themes, --rebuild-themes "
                        "--name). deepseek (default) is off-peak gated and capped by --cap USD; grok (SuperGrok "
                        "subscription, #388) has no off-peak gate, stops at the allowance and needs --limit N, "
                        "which then caps the calls")
    p.add_argument("--grok-ping", action="store_true",
                   help="one live SuperGrok call with the profile prompt; writes nothing; exit 0 when it answers a "
                        "usable profile")
    args = p.parse_args(argv)
    grok_on = args.provider == "grok"
    if grok_on:
        if args.status or args.cutover or not (args.run or args.name_themes or args.rebuild_themes
                                               or args.grok_ping):
            p.error("--provider grok goes with --run, --name-themes or --rebuild-themes --name")
        if args.rebuild_themes and not args.name:
            p.error("--provider grok names themes: add --name to --rebuild-themes")
        if args.run and args.cap is not None:
            p.error("--cap is USD and DeepSeek's only; with --provider grok, --limit N caps the calls")
        if args.run and (args.limit is None or args.limit < 1):
            p.error("--provider grok needs --limit N (N >= 1): the most SuperGrok calls this run makes")
        if args.limit is not None and not args.run:
            p.error("--limit caps the SuperGrok calls of --run only; theme naming has no call cap")
    chosen = _selection(args)
    if chosen == []:      # an empty list must never fall back to a default selection downstream
        p.error("--floor / --tickers selected no names")
    if chosen is not None and (args.status or args.name_themes or args.cutover
                               or not (args.run or args.rebuild_themes)):
        p.error("--floor / --tickers go with --run or --rebuild-themes only")
    if args.build_only and not args.rebuild_themes:
        p.error("--build-only goes with --rebuild-themes")
    if args.coarse != "nmf" and not (args.cutover or (args.rebuild_themes and not args.build_only)):
        p.error("--coarse goes with --cutover or --rebuild-themes (not --build-only)")
    if (args.nmf_init is not None or args.nmf_max_iter is not None) and not args.rebuild_themes:
        p.error("--nmf-init / --nmf-max-iter go with --rebuild-themes only")
    if args.k_medium is not None and not args.rebuild_themes:
        p.error("--k-medium goes with --rebuild-themes only")
    fit = NmfFit(init=args.nmf_init or DEFAULT_FIT.init, max_iter=args.nmf_max_iter or DEFAULT_FIT.max_iter)
    if args.build_only and (args.name or args.adopt_medium_from):
        p.error("--build-only writes the shadow run only: --name and --adopt-medium-from belong to "
                "--name-themes / --cutover")

    if grok_on or args.grok_ping:
        print(f"Grok (SuperGrok subscription, model {grok.MODEL}): no off-peak gate; the allowance is read before "
              f"each batch of {grok.GATE_EVERY} calls.")
    else:
        print(offpeak.describe())
        if not offpeak.is_off_peak() and deepseek.peak_override_active():
            print("Peak override ON (set from the Hermes usage card) — DeepSeek calls "
                  "will run at the 2x peak rate until this window ends.")
    if args.grok_ping:
        return _grok_ping()
    if args.status:
        print("last run:", json.dumps(_last_run(), ensure_ascii=False))
        # Claude / Grok bill by allowance, not by time of day: one line each when
        # a login is present. Read failures are swallowed inside check().
        for _provider, line in subscription_usage.check():
            print(line)
        return 0

    # Theme naming through Grok (#108 S1 with #388): the same naming, SuperGrok's client.
    name_kw = {"complete": grok.complete} if grok_on else {}
    if grok_on:
        # The login and the allowance before any write, SEC request or theme rebuild (review B M1).
        try:
            grok.preflight()
        except grok.GrokAuthError as exc:
            print(json.dumps({"provider": "grok", "model": grok.MODEL, "calls": 0, "cost_usd": 0.0,
                              "stopped_reason": "auth", "error": str(exc)}, ensure_ascii=False))
            return 1
        except grok.GrokAllowanceError as exc:
            out = {"provider": "grok", "model": grok.MODEL, "calls": 0, "cost_usd": 0.0,
                   "stopped_reason": "allowance", "error": str(exc)}
            if exc.next_reset is not None:
                out["next_reset"] = exc.next_reset.strftime("%Y-%m-%d %H:%M")
            print(json.dumps(out, ensure_ascii=False))
            return 0

    if args.name_themes:
        from . import naming
        try:
            named = naming.name_themes(args.run_id, **name_kw)
        except _GROK_FAILURES as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
        print(_summary(**({"provider": "grok", **named} if grok_on else named)))
        return 0

    if args.cutover:
        from . import themes
        run_id = args.run_id or themes.current_run_id()
        if not run_id:
            print(json.dumps({"error": "no run_id / no current theme run"},
                             ensure_ascii=False))
            return 1
        if run_id == themes.current_run_id():
            print(json.dumps({"error": f"theme run {run_id!r} is the current (live) run; "
                              "--cutover would rewrite it in place. Build a new run "
                              "with --rebuild-themes (or pass another --run-id)."},
                             ensure_ascii=False))
            return 1
        try:
            print(_summary(**themes.finalize_run(run_id, args.adopt_medium_from, coarse=args.coarse)))
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
        return 0

    if args.rebuild_themes:
        from . import naming, themes
        if args.run_id and args.run_id == themes.current_run_id():
            print(json.dumps({"error": f"theme run {args.run_id!r} is the current "
                              "(live) run; a rebuild would blank it in place. "
                              "Use a new --run-id."}, ensure_ascii=False))
            return 1
        built = themes.build_shadow(run_id=args.run_id, universe=chosen, fit=fit, k_medium=args.k_medium)
        run_id = built.get("run_id")
        if not run_id:
            print(json.dumps(built, ensure_ascii=False))
            return 0
        cuts = {"k_fine": built.get("k_fine"), "k_medium": built.get("k_medium"),
                "k_medium_requested": (built.get("k_medium_requested")
                                       if built.get("k_medium_requested") is not None else "default")}
        if args.build_only:
            print(_summary(run_id=run_id, themes=built.get("themes"), unthemed=built.get("unthemed"),
                           out_of_universe=built.get("out_of_universe"), fit=built.get("fit"), **cuts))
            return 0
        try:
            cut = themes.finalize_run(run_id, args.adopt_medium_from, coarse=args.coarse)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
        out = {"run_id": run_id, "themes": built.get("themes"), **cuts,
               "rows": cut.get("rows"), "named": 0, "pending": 0,
               "next_off_peak": None, "unthemed": built.get("unthemed"),
               "out_of_universe": built.get("out_of_universe"), "fit": built.get("fit"),
               "coarse": cut.get("coarse"), "k_coarse": cut.get("k_coarse")}
        if args.adopt_medium_from:
            out.update(adopted=cut.get("adopted"),
                       fines_reparented=cut.get("fines_reparented"),
                       dropped_mediums=cut.get("dropped_mediums"))
        if args.name:
            if grok_on:
                out["provider"] = "grok"
            try:
                out.update(naming.name_themes(run_id, **name_kw))
            except _GROK_FAILURES as exc:
                out["error"] = str(exc)
                print(_summary(**out))
                return 1
        print(_summary(**out))
        return 0

    if not args.run:
        print("use --run [--limit N] [--cap USD], --status, --name-themes, "
              "--rebuild-themes, --cutover, or --grok-ping / --provider grok")
        return 0

    if grok_on:
        return _grok_run(chosen, args.limit)

    try:
        payload = engine.run(tickers=chosen, limit=args.limit, usd_cap=args.cap)
    except engine.AlreadyRunning as exc:
        # Not green: nothing ran. (An off-peak deferral is different: it exits 0 below.)
        print(json.dumps({"skipped": str(exc)}, ensure_ascii=False))
        return 1
    done = payload.get("n")
    pending = payload.get("pending") or 0
    reason = payload.get("stopped_reason")
    if reason or pending > 0:
        # Stopped (peak, cap) or work left: one JSON object naming what is done, what is pending and why.
        # Exit 1 only for a stop the next run cannot fix on its own (a bad credential or repeated transient
        # failures); peak, the cost cap and pending names exit 0 (C-F1) — they are the normal night-to-night state.
        out = {"done": done, "pending": pending,
               "next_off_peak": payload.get("next_off_peak")}
        if reason:
            out["stopped_reason"] = reason
        if payload.get("error"):
            out["error"] = payload["error"]
        print(json.dumps(out, ensure_ascii=False))
        return 1 if reason in _FAILED_STOP_REASONS else 0
    print(json.dumps({"n": done, "pending": pending,
                      "next_off_peak": payload.get("next_off_peak")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

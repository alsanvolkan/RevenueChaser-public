"""Relatedness engine — pilot selection, profiles, similarity.

Profiles are produced off-peak by the LLM and stored durably in
``relatedness_profiles`` / ``relatedness_attributes`` (never a ``.cache`` JSON
file). ``no_fetch`` never calls the LLM; it re-renders from the stored profiles,
so a dashboard Refresh is zero-network.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone

from RevenueChaser.core import job_lock, ledger, rawstore
from RevenueChaser.core.reporting_common import write_artifact
from RevenueChaser.core.providers import deepseek, grok
from RevenueChaser.module_value_chain import engine as vc_engine

from . import config
from . import profiles as profiles_mod
from . import similarity

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3        # an empty LLM answer is retried in at most 3 runs
MAX_TRANSIENT_FAILURES = 5   # stop a run after this many non-auth LLM failures

# The canonical spine is `equity_universe`; the rename lands in docs/plan_rest.md.
_UNIVERSE = "equity_universe" if "equity_universe" in rawstore.MANIFEST else "equity_universe"


class RunStopped(RuntimeError):
    """The profile run stopped early: ``peak`` (DeepSeek), ``allowance`` (SuperGrok, #388), ``auth``, ``transient``
    (repeated), ``request`` (Grok answered a 4xx: endpoint, model or parameter wrong) or ``unusable`` (Grok's first
    replies of a run were all unusable).

    Written profiles stay durable; the caller reloads them from the store.
    """

    def __init__(self, reason: str, next_off_peak=None, message: str | None = None):
        self.reason = reason
        self.next_off_peak = next_off_peak
        self.message = message
        super().__init__(message or reason)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def select_pilot(limit: int | None = None) -> list[str]:
    """The shared classification universe (no market-cap floor), cap-descending.

    One selector for every classification board (core/universe.py). ``limit``
    caps the set for tests/demos.
    """
    from RevenueChaser.core.universe import classification_universe
    tickers = classification_universe()
    return tickers[:limit] if limit else tickers


def _save(path, data: dict) -> None:
    """Write the payload atomically (REL-11): a crash mid-write leaves the previous file whole."""
    write_artifact(path, json.dumps(data, ensure_ascii=False))


def _layer_documents(tickers: list[str]) -> dict[str, dict] | None:
    """``{ticker: filing_item document row}``: the newest annual report text the fundamentals layer serves per
    ticker (``sec/filings``, the same splitter and cap as Value Chain's corpus, so the same filing gives the same
    text), or None off the layer (build plan PR 20, G6)."""
    from RevenueChaser.core import fundamentals_layer
    if not fundamentals_layer.reads_layer():
        return None
    from RevenueChaser.rcdata import read
    cik_of = {str(c["ticker"]).upper(): str(c["cik"]) for c in read.fundamental_companies(tickers) if c.get("cik")}
    docs = {str(d["cik"]): d for d in read.filing_texts(set(cik_of.values()))}
    return {t: docs[c] for t, c in cik_of.items() if c in docs}


def _filing_text(ticker: str) -> tuple[str, str]:
    docs = _layer_documents([ticker])
    if docs is not None:
        return ticker, (docs.get(ticker.upper()) or {}).get("text") or ""
    row = rawstore.get("chain_filings", ticker) or {}
    return row.get("name") or ticker, row.get("item_text") or ""


def _filing_source(ticker: str) -> dict | None:
    """The filing ``_filing_text`` reads for ``ticker``: ``{cik, accession, form, filed}`` (build plan PR 20: a profile
    carries the filing it was made from), or None without one."""
    docs = _layer_documents([ticker])
    if docs is not None:
        d = docs.get(ticker.upper())
        return {"cik": d["cik"], "accession": d["accession"], "form": d.get("form"),
                "filed": d.get("filing_date")} if d else None
    row = rawstore.get("chain_filings", ticker)
    if not row or not row.get("accession"):
        return None
    return {"cik": row.get("cik") or None, "accession": row["accession"], "form": row.get("form"),
            "filed": row.get("filed")}


def _company_names(tickers: list[str]) -> dict[str, str]:
    """``{ticker: name}`` from the shared universe (``equity_universe.name``), for the prompt (#388 D5).

    ``chain_filings`` has no name column, so ``_filing_text``'s name is the ticker; the prompt used to say
    ``Company: AAPL``."""
    if not tickers:
        return {}
    rows = rawstore.query(f"SELECT ticker, name FROM {_UNIVERSE} WHERE ticker IN (SELECT unnest(?))",
                          [list(tickers)])
    return {r["ticker"]: str(r["name"]) for r in rows if r.get("name")}


# How many tickers' filing texts ``_input_hashes`` holds at once. Since #317 the hash is taken over the whole
# text, so one is up to the splitter's ``MAX_ITEM_CHARS`` (200 KB) and a chunk up to ~40 MB, transient.
_HASH_CHUNK = 200


def _input_hashes(groups: dict[str, list[str]], names: dict[str, str]) -> dict[str, str]:
    """``{group key: input hash}``: the hash of the prompt a group would be profiled from today (#388 D5).

    The same choice ``_work`` makes: the first ticker with filing text speaks for its CIK group, under its universe
    name; the hash is ``store.prompt_hash`` of that exact prompt, as in the profile's ``cache_key``. A group with no
    filing text gets none (its kept profile is not redone). Reads the column ``_filing_text`` reads
    (``chain_filings.item_text``), in chunks with one query each, store-only.
    """
    from . import store

    out: dict[str, str] = {}
    items = list(groups.items())
    for i in range(0, len(items), _HASH_CHUNK):
        chunk = items[i:i + _HASH_CHUNK]
        wanted = [t for _k, ts in chunk for t in ts]
        docs = _layer_documents(wanted)
        rows = ({t: {"item_text": d.get("text")} for t, d in docs.items()} if docs is not None else
                rawstore.get_many("chain_filings", wanted, columns=["ticker", "item_text"]))
        for key, ts in chunk:
            rep = next((t for t in ts if (rows.get(t) or {}).get("item_text")), None)
            if rep is None:
                continue
            user = profiles_mod.build_user(names.get(rep) or rep, rows[rep]["item_text"])
            out[key] = store.prompt_hash(profiles_mod.SYSTEM, user)
    return out


def _cik_for(tickers: list[str]) -> dict[str, str]:
    """``{ticker: cik}`` (empty string when unknown): the fundamentals layer's company (G6) when the boards read it,
    else the SEC ticker map."""
    from RevenueChaser.core import fundamentals_layer
    if fundamentals_layer.reads_layer():
        from RevenueChaser.rcdata import read
        got = {str(c["ticker"]).upper(): str(c.get("cik") or "") for c in read.fundamental_companies(tickers)}
        return {t: got.get(t.upper(), "") for t in tickers}
    from RevenueChaser.core.providers import sec_edgar
    cmap = sec_edgar._cik_map()
    return {t: str((cmap.get(t.upper()) or {}).get("cik") or "") for t in tickers}


def _writer(provider: str) -> tuple[str, bool, str]:
    """``(model, thinking, effort)`` a profile written by ``provider`` records in its cache key.

    DeepSeek's are the profile mode's, as before. Grok is sent neither DeepSeek knob (``core/providers/grok.py``), so
    its key says so; its staleness does not read them (``store.is_stale``, ``ALT_WRITERS``)."""
    if provider == "grok":
        return grok.MODEL, False, ""
    return deepseek.MODEL, config.THINKING, config.REASONING_EFFORT


def build_profiles(tickers: list[str], use_llm: bool = True,
                   thinking_tickers: set[str] | None = None,
                   workers: int | None = None,
                   usd_cap: float | None = None,
                   provider: str = "deepseek",
                   max_calls: int | None = None) -> dict[str, dict]:
    """Ensure a durable profile per ticker (cache-first, CIK-grained).

    Profiles live in ``relatedness_profiles`` / ``relatedness_attributes`` (not
    ``.cache``). The mode stamp (model/thinking/effort/prompt_version) in
    ``run_registry`` records the current mode; profiles made under another mode stay
    visible (stale) until re-profiled. Dual-class
    shares (same CIK) are profiled once and copied to every ticker. ``use_llm=False``
    never calls the LLM. ``usd_cap`` caps the number of LLM calls this run (the rest
    stay pending for the next run).

    ``provider`` is the writer (#388): ``deepseek`` (default; off-peak gated, ``usd_cap``) or ``grok`` (SuperGrok;
    allowance gated, no off-peak gate; ``max_calls`` caps its calls, ``usd_cap`` is not read, ``None`` is no cap).
    Which profiles need the LLM is the same for both: a current-prompt profile of either writer is current
    (``store.is_stale``). A Grok run stops (``unusable``) when its first ``config.GROK_UNUSABLE_STOP`` replies are all
    unusable, and saves none of the unusable replies it got before its first usable one, so a wrong endpoint shape
    cannot count an attempt on every group.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from . import store

    profiles_mod.llm(provider)                 # an unknown writer fails here, before anything is written
    grok_run = provider == "grok"
    w_model, w_thinking, w_effort = _writer(provider)
    mode = store.mode_stamp()
    if use_llm and store.get_mode() != mode:
        # Never delete on a mode change (#114 C2): profiles made under the old
        # mode stay visible, flagged stale, until each is re-profiled below.
        store.set_mode(mode)

    if not use_llm:
        return store.load_profiles(tickers)

    ciks = _cik_for(tickers)
    groups: dict[str, list[str]] = {}
    for t in tickers:
        groups.setdefault(ciks.get(t) or t, []).append(t)

    have = store.load_profiles(tickers)
    meta = store.profile_meta(tickers)
    names = _company_names(tickers)

    def _needs_llm(t: str, current: str | None = None) -> bool:
        """Not done: no row, stale, made from another input, or empty/no_text/short_text and not out of retries.

        ``current`` is the hash of the prompt the group would be profiled from today; a stored profile made from
        another one (a new filing text or company name) is redone like a stale one (#388 D5).
        """
        m = meta.get(t)
        if m is None:
            return True
        stale = store.is_stale(m, mode) or (
            current is not None and store.input_hash(m.get("cache_key")) != current)
        if m["status"] == "ok" and any((have.get(t) or {}).values()):
            return stale and m["attempts"] < MAX_ATTEMPTS
        if m["status"] == "empty":
            return stale or m["attempts"] < MAX_ATTEMPTS
        return True                        # no_text / short_text / unknown: re-check (no LLM cost)

    # The input check reads filing texts, so it runs only for the groups nothing else already sends to the LLM.
    current = _input_hashes({k: ts for k, ts in groups.items() if not any(_needs_llm(t) for t in ts)}, names)
    todo = {k: ts for k, ts in groups.items() if any(_needs_llm(t, current.get(k)) for t in ts)}

    # ``cap`` is a spend ceiling: ``0`` means no LLM call at all. It counts calls
    # (the thinking retry included), so the call budget is threaded into
    # ``profile_for`` and the group slice below only bounds how many groups start.
    if grok_run:
        # SuperGrok costs 0 USD a call: the cap is a call count (``--limit N``), the allowance gate the ceiling.
        max_calls = None if max_calls is None else max(0, int(max_calls))
        if max_calls == 0:
            todo = {}
        elif max_calls is not None and len(todo) > max_calls:
            logger.warning("relatedness: grok call cap %d -> %d/%d groups this run", max_calls, max_calls, len(todo))
            todo = dict(list(todo.items())[:max_calls])
        grok.reset_gate()                      # the first request of the run reads the allowance
    else:
        cap = usd_cap if usd_cap is not None else config.USD_CAP
        max_calls = None
        if cap is not None:
            max_calls = max(0, int(cap / config.EST_USD_PER_CALL)) if cap > 0 else 0
            if max_calls <= 0:
                todo = {}
            elif len(todo) > max_calls:
                logger.warning("relatedness: $ cap %.2f -> %d/%d groups this run",
                               cap, max_calls, len(todo))
                todo = dict(list(todo.items())[:max_calls])
    budget = profiles_mod.CallBudget(max_calls)

    stop = threading.Event()
    state_lock = threading.Lock()
    reason = {"why": None, "next": None, "message": None}
    transient = {"n": 0}
    replies = {"usable": 0, "unusable": 0}     # a Grok run's replies so far (the ``unusable`` stop)

    def _fail(kind: str, exc) -> None:
        """Record the first run-stopping failure and set the stop event."""
        with state_lock:
            if reason["why"]:
                return
            if kind == "transient":
                transient["n"] += 1
                if transient["n"] < MAX_TRANSIENT_FAILURES:
                    return
                reason["why"], reason["message"] = "transient", str(exc)
            elif kind == "peak":
                reason["why"] = "peak"
                reason["next"] = getattr(exc, "next_off_peak", None)
            elif kind == "allowance":
                reason["why"], reason["message"] = "allowance", str(exc)
                reason["next"] = getattr(exc, "next_reset", None)
            elif kind in ("request", "unusable"):
                reason["why"], reason["message"] = kind, str(exc)
            else:
                reason["why"], reason["message"] = "auth", str(exc)
            stop.set()

    def _unusable_before_any_usable(usable: bool) -> bool:
        """A Grok run: count the reply; True when it is unusable and no usable reply came yet in this run (it is
        then not saved), and stop the run once ``GROK_UNUSABLE_STOP`` such replies came. DeepSeek: always False."""
        if not grok_run:
            return False
        with state_lock:
            replies["usable" if usable else "unusable"] += 1
            first = not usable and not replies["usable"]
            hit = first and replies["unusable"] >= config.GROK_UNUSABLE_STOP
        if hit:
            _fail("unusable", RuntimeError(
                f"the first {config.GROK_UNUSABLE_STOP} Grok replies of this run held no usable profile; check the "
                f"endpoint and model with --grok-ping (model {grok.MODEL})"))
        return first

    def _work(item):
        if stop.is_set():
            return
        _key, ts = item
        rep = next((t for t in ts if _filing_text(t)[1]), ts[0])
        name, text = _filing_text(rep)
        name = names.get(rep) or name             # the company, not the ticker (#388 D5)
        unusable = profiles_mod.text_status(text)
        if unusable:
            # No filing text (``no_text``) or one under Value Chain's MIN_ITEM_CHARS (``short_text``, #313):
            # the writer would answer from the company name alone, so nothing is sent and nothing is spent.
            for t in ts:
                if any((have.get(t) or {}).values()):
                    continue               # keep a usable (stale) profile
                store.save_profile(t, ciks.get(t),
                                   {f: [] for f in config.PROFILE_FIELDS},
                                   "", w_model, status=unusable)
            return
        user = profiles_mod.build_user(name, text)
        # The profile's own cache key (structured JSON, the mode stamp checked by
        # ``store.is_stale``) is deliberately not the LLM cache key
        # (``deepseek._cache_key``, a sha256 of the prompt bytes): the first says
        # whether a profile matches the current mode, the second whether the raw
        # reply is already stored. A prompt_version bump moves both (the version
        # is in the prompt; ``profiles.system_prompt``). A Grok key names its
        # provider (#388); a DeepSeek key is byte-identical to before.
        ck = store.cache_key(w_model, w_thinking, w_effort, store.PROMPT_VERSION, rep,
                             store.prompt_hash(profiles_mod.SYSTEM, user), provider=provider,
                             source=_filing_source(rep))
        if stop.is_set():
            return
        try:
            prof = profiles_mod.profile_for(rep, name, text, thinking=config.THINKING,
                                            effort=config.REASONING_EFFORT,
                                            budget=budget, provider=provider)
        except profiles_mod.BudgetExceeded:
            return                     # out of LLM budget: this group stays pending
        except deepseek.PeakHoursError as exc:
            _fail("peak", exc)
            raise
        except grok.GrokAllowanceError as exc:
            _fail("allowance", exc)
            raise
        except (deepseek.DeepSeekAuthError, grok.GrokAuthError) as exc:
            _fail("auth", exc)
            raise
        except grok.GrokRequestError as exc:
            _fail("request", exc)
            raise
        except (deepseek.DeepSeekTransientError, grok.GrokTransientError) as exc:
            _fail("transient", exc)
            raise
        except (deepseek.DeepSeekError, grok.GrokError) as exc:
            # Empty content / unusable reply: count the attempt (D2 stops after 3
            # runs) instead of re-billing it on every run.
            logger.warning("relatedness profile %s unusable: %s", rep, exc)
            prof = {f: [] for f in config.PROFILE_FIELDS}
        usable = any(prof.values())
        if _unusable_before_any_usable(usable):
            return                     # Grok: nothing usable yet this run, so this reply counts no attempt
        for t in ts:
            m = meta.get(t)
            if usable:
                store.save_profile(t, ciks.get(t), prof, ck, w_model)
                continue
            # Unusable answer: counted, never saved as done, and never allowed
            # to overwrite a profile that still has content.
            tries = 1 if m is None or store.is_stale(m, mode) and m["status"] != "ok" \
                else m["attempts"] + 1
            if any((have.get(t) or {}).values()):
                row = rawstore.get("relatedness_profiles", t) or {}
                old_ck = row.get("cache_key") or ""
                if isinstance(old_ck, dict):
                    old_ck = json.dumps(old_ck, sort_keys=True, separators=(",", ":"))
                store.save_profile(t, ciks.get(t), have[t], old_ck,
                                   row.get("model") or w_model,
                                   prompt_version=row.get("prompt_version")
                                   or store.PROMPT_VERSION,
                                   status="ok", attempts=tries)
            else:
                store.save_profile(t, ciks.get(t), prof, ck, w_model,
                                   status="empty", attempts=tries)

    if todo:
        # LLM workers are a separate pool from the SEC pool (sec_edgar._throttle).
        with ThreadPoolExecutor(max_workers=workers or config.LLM_WORKERS) as ex:
            futs = [ex.submit(_work, it) for it in todo.items()]
            for f in as_completed(futs):
                try:
                    f.result()
                except (deepseek.PeakHoursError, deepseek.DeepSeekAuthError,
                        deepseek.DeepSeekTransientError, grok.GrokAllowanceError, grok.GrokAuthError,
                        grok.GrokRequestError, grok.GrokTransientError):
                    pass               # the worker recorded why; stop below
                except Exception as exc:  # noqa: BLE001 - per-group LLM failure
                    logger.warning("profile group failed: %s", exc)
                if reason["why"]:
                    # Stop submitting: cancel what has not started and drain.
                    for g in futs:
                        g.cancel()
                    break
    if reason["why"]:
        raise RunStopped(reason["why"], reason["next"], reason["message"])
    return store.load_profiles(tickers)


# The shared coarse / medium / fine order. It names theme levels directly and
# maps co-movement's three cuts (broad -> tight, docs/dashboard_ui.md §8) so the
# first cut is Coarse and the last is Fine.
_LEVELS = ("coarse", "medium", "fine")


def _theme_levels() -> dict[str, dict]:
    """``{ticker: {coarse/medium/fine: label}}`` for the current theme run.

    The level comes from the ticker's ``ticker_themes`` rows (the current run
    only) but the **label** comes from ``core.universe.theme_catalog`` — the one
    Theme vocabulary — so Relatedness, Co-movement and Sector Rotation read the
    same coarse label. Store-only: a missing table, an unpinned run, or a query
    failure yields an empty map so a company simply carries empty theme strings.
    """
    try:
        from RevenueChaser.core.universe import theme_catalog
        from . import themes as rel_themes
        run_id = rel_themes.current_run_id()
        if not run_id:
            return {}
        rows = rawstore.query(
            "SELECT ticker, level, theme_id, theme FROM ticker_themes "
            "WHERE theme_run_id = ?", [run_id])
        catalog = theme_catalog()
    except Exception:  # noqa: BLE001 - optional enrichment, never fatal
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        level = r.get("level")
        if level not in _LEVELS:
            continue
        label = (catalog.get(r.get("theme_id")) or {}).get("label")
        if label is None:
            label = r.get("theme") or ""      # no catalog row: fall back to the copy
        out.setdefault(r["ticker"], {})[level] = label
    return out


def _cluster_levels() -> dict[str, dict]:
    """``{ticker: {level: cluster_id}}`` from the latest default run.

    Reads the co-movement board's own default ``(horizon, benchmark)`` — a
    hardcoded ``horizon = 5`` went stale the moment that default moved to 1d
    (S7). Empty when no matching run exists, so the peer row omits the chips.

    Among those runs: the newest prices first, then the board's default window
    (a Fetch also stores other windows, #95, which must not displace it), then
    the latest.
    """
    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_comovement import settings as comov_settings

    rows = rawstore.query(
        "SELECT as_of FROM comov_runs WHERE horizon = ? AND benchmark = ? "
        "ORDER BY prices_through DESC NULLS LAST, (window_days = ?) DESC, "
        "as_of DESC LIMIT 1",
        [comov_config.DEFAULT_HORIZON, comov_config.DEFAULT_BENCHMARK,
         comov_settings.effective().default_window])
    if not rows:
        return {}
    as_of = rows[0]["as_of"]
    try:
        cut_rows = rawstore.query(
            "SELECT DISTINCT cut FROM comov_nodes WHERE as_of = ?", [as_of])
        member_rows = rawstore.query(
            "SELECT cut, cluster_id, ticker FROM comov_members WHERE as_of = ?",
            [as_of])
    except Exception:  # noqa: BLE001 - optional enrichment, never fatal
        return {}
    cuts = sorted(r["cut"] for r in cut_rows)
    level_by_cut = {c: _LEVELS[i] for i, c in enumerate(cuts)
                    if i < len(_LEVELS)}
    out: dict[str, dict] = {}
    for r in member_rows:
        level = level_by_cut.get(r["cut"])
        if level:
            out.setdefault(r["ticker"], {})[level] = r["cluster_id"]
    return out


def _same_clusters(cluster_map: dict, focal: str, peer: str) -> dict | None:
    """Per level, whether ``focal`` and ``peer`` share a cluster id."""
    if not cluster_map or not focal or not peer:
        return None
    a = cluster_map.get(focal) or {}
    b = cluster_map.get(peer) or {}
    out = {lvl: (a.get(lvl) == b.get(lvl))
           for lvl in _LEVELS if a.get(lvl) is not None and b.get(lvl) is not None}
    return out or None


def _attach_peer_market(peers: dict) -> None:
    """Attach ``move`` / ``beyond_spy`` / ``cluster`` to every focal's peers (store-only).

    Loads closes for the focals, their peers and SPY only, and correlates just
    the unique (focal, peer) pairs - never an N-by-N matrix, so it covers every
    focal. The values equal the matrix entries (same pairwise-complete rule and
    ``min_periods``). Horizons with too little overlap are omitted rather than
    filled with a fake ``0.0``.
    """
    if not peers:
        return
    from RevenueChaser.module_comovement import config as comov_config
    from RevenueChaser.module_comovement import ohlc as comov_ohlc
    from RevenueChaser.module_comovement import returns as comov_returns
    from RevenueChaser.module_comovement import settings as comov_settings

    focals = set(peers)
    peer_tickers = {p.get("peer") for entries in peers.values() for p in entries}
    price_tickers = sorted((focals | peer_tickers) - {None} | {"SPY"})
    try:
        # ``log_returns`` uses the last ``window + horizon`` sessions only, so read just those (#388): the same
        # rows, not every stored bar.
        need = comov_settings.effective().window + max(comov_config.HORIZONS)
        prices = comov_ohlc.price_frame(price_tickers, start=comov_ohlc.window_start(price_tickers, need))
    except Exception:  # noqa: BLE001 - missing/empty store must not break a run
        return
    if prices is None or getattr(prices, "empty", True):
        return

    pairs = list(dict.fromkeys(
        (focal, p["peer"]) for focal, entries in peers.items()
        for p in entries if p.get("peer") is not None))

    move_by_pair: dict[tuple[str, str], dict[int, float]] = {}
    default_ret = None
    for n in comov_config.HORIZONS:
        ret, _dropped = comov_returns.log_returns(prices, horizon=n)
        if n == comov_config.DEFAULT_HORIZON:
            default_ret = ret          # reused below, not recomputed
        if ret.empty:
            continue
        for pair, value in zip(pairs, comov_returns.pair_correlation(ret, pairs)):
            if not math.isnan(value):
                move_by_pair.setdefault(pair, {})[int(n)] = round(float(value), 4)

    spy_by_pair: dict[tuple[str, str], float] = {}
    if default_ret is None:
        default_ret, _dropped = comov_returns.log_returns(
            prices, horizon=comov_config.DEFAULT_HORIZON)
    if not default_ret.empty and "SPY" in default_ret.columns:
        cols = [c for c in default_ret.columns if c != "SPY"]
        if cols:
            resid = comov_returns.residualize(default_ret[cols], default_ret["SPY"])
            for pair, value in zip(pairs, comov_returns.pair_correlation(resid, pairs)):
                if not math.isnan(value):
                    spy_by_pair[pair] = round(float(value), 4)

    cluster_map = _cluster_levels()
    for focal, entries in peers.items():
        for p in entries:
            other = p.get("peer")
            move = move_by_pair.get((focal, other))
            if move:
                p["move"] = dict(move)
            spy = spy_by_pair.get((focal, other))
            if spy is not None:
                p["beyond_spy"] = spy
            same = _same_clusters(cluster_map, focal, other)
            if same:
                p["cluster"] = same


def _payload(profiles: dict[str, dict], thinking: dict[str, dict],
             stale: set[str] | None = None) -> dict:
    from RevenueChaser.core.gics_map import canonical_sector, sector_path
    from RevenueChaser.core.universe import classification_universe, filter_fields, theme_catalog
    # One read of the company fields (#49): ``filter_fields`` carries name/sector/industry/market_cap already,
    # so the second ``equity_universe`` query this function used to run only risked disagreeing with it.
    extra = filter_fields(list(profiles))
    themes = _theme_levels()
    from . import store
    sources = {t: store.profile_source(m.get("cache_key")) for t, m in store.profile_meta(list(profiles)).items()}
    companies = []
    for t in sorted(profiles):
        x = extra.get(t) or {}
        tl = themes.get(t) or {}
        companies.append({
            "ticker": t,
            "name": x.get("name") or t,
            # An old GICS spelling still in the store reads as its Morningstar name (filter_fields normalises it,
            # and ``canonical_sector`` is idempotent on the names it returns).
            "sector": canonical_sector(x.get("sector")),
            "industry": x.get("industry") or "",
            "market_cap": x.get("market_cap"),
            "earnings_date": x.get("earnings_date"),
            "index": x.get("index") or [],
            # The shared filter row (core.universe.filter_fields, #383 C1b): the sector path and every level of
            # the pinned theme run the Theme facet filters on.
            "gics": x.get("gics") or sector_path(canonical_sector(x.get("sector")),
                                                 x.get("industry") or ""),
            "theme_ids": x.get("theme_ids") or [],
            "theme_coarse": tl.get("coarse") or "",
            "theme_medium": tl.get("medium") or "",
            "theme_fine": tl.get("fine") or "",
            "stale": t in (stale or ()),
            # Build plan PR 20 (G6): the filing the profile was read from (CIK, accession, form, filed).
            "source": sources.get(t),
            "weights": similarity.term_weights(profiles[t]),
            "profile": profiles[t],
        })

    weights = {c["ticker"]: c["weights"] for c in companies}
    tickers = [c["ticker"] for c in companies]
    names = {c["ticker"]: c["name"] for c in companies}
    import numpy as np
    peers, raw_scores = similarity.peer_lists(profiles, config.TOP_PEERS)
    arr = (np.sort(np.asarray(raw_scores, dtype="float64"))
           if raw_scores else np.zeros(0))
    # Pairs scored is every unordered pair of profiled tickers; the omitted
    # zero-score pairs are added back analytically (never materialised).
    n_total = len(profiles) * (len(profiles) - 1) // 2
    n_zero = max(0, n_total - len(arr))
    n_pairs = n_total
    for a in peers:
        for p in peers[a]:
            p["name"] = names.get(p["peer"]) or p["peer"]
            # Compare the unrounded score, and count the zero pairs analytically.
            raw = p.pop("score_raw", p["score"])
            p["percentile"] = (
                round(100.0 * (n_zero + float(np.searchsorted(arr, raw, side="left")))
                      / n_total, 1)
                if n_total else 0.0)

    # Market stats for every focal's peers, computed pair-only (never N x N).
    _attach_peer_market(peers)

    # thinking-vs-nonthinking comparison on the sample
    cmp_scores = []
    for t in sorted(thinking):
        if t in weights and thinking[t]:
            for other in tickers:
                if other == t:
                    continue
                s1, _ = similarity.score_pair(profiles[t], profiles[other])
                s2, _ = similarity.score_pair(thinking[t], profiles[other])
                cmp_scores.append(abs(s1 - s2))
    thinking_cmp = {
        "n": len(thinking),
        "mean_abs_diff": round(sum(cmp_scores) / len(cmp_scores), 4) if cmp_scores else None,
    }

    return {
        "as_of": _now(),
        "model": deepseek.MODEL,
        "n": len(companies),
        "companies": companies,
        "peers": peers,
        "n_pairs": n_pairs,
        "thinking_compare": thinking_cmp,
        "fields": list(config.PROFILE_FIELDS),
        "universe_total": len(classification_universe()),
        "theme_catalog": theme_catalog(),
    }


def _stored_provider_health() -> dict | None:
    """The last Fetch's stored health, for a Refresh (no live provider call). None when nothing is recorded."""
    from RevenueChaser.core import history

    from . import history as rel_history

    try:
        row = history.last_run(rel_history.BOARD)
    except Exception:  # noqa: BLE001 - a locked history file never fails the render
        return None
    return (row or {}).get("provider_health")


def _stop_health(provider: str, reason: str | None, message: str | None) -> dict | None:
    """The banner's health when this run stopped on a provider failure (#80).

    A peak or allowance stop is a schedule, not a failed call, so it leaves the banner empty. ``auth`` carries
    HTTP 401 so the banner names a rejected key."""
    if reason not in ("auth", "transient", "request", "unusable"):
        return None
    name = provider or "deepseek"
    return {"provider": name, "calls": 1, "failed": 1,
            "auth_error": {"status": 401} if reason == "auth" else None,
            "failures": [{"endpoint": name, "error": message or reason}],
            "breaker_open": False, "skipped_calls": 0}


class AlreadyRunning(RuntimeError):
    """A Fetch run holds the ``relatedness`` job lock."""


def run(no_fetch: bool = False, tickers: list[str] | None = None,
        limit: int | None = None, usd_cap: float | None = None, run_id: str | None = None,
        provider: str = "deepseek", max_calls: int | None = None) -> dict:
    """A Fetch records its scores and payload in result history (#388) under ``run_id`` (the dashboard job's id; the
    CLI gets ``ledger.new_run_id()``, a scheduled task's ``job_runs`` id).

    ``provider="grok"`` (#388, the CLI's ``--run --provider grok --limit N``) writes the missing and stale profiles
    through SuperGrok; ``max_calls`` caps its calls while the run still covers every name (``limit`` slices the names
    and is meant for DeepSeek demos). The dashboard Fetch and RC-Relatedness-OffPeak stay on DeepSeek."""
    if no_fetch:
        return _run(True, tickers, limit, usd_cap)
    # Scheduled (CLI) and dashboard Fetch runs must not overlap. Refresh writes
    # nothing, so it never needs the lock.
    with job_lock.acquire("relatedness", wait=0) as got:
        if not got:
            raise AlreadyRunning("Another Relatedness run is already in progress.")
        if provider == "deepseek":
            return _run(False, tickers, limit, usd_cap, run_id)
        return _run(False, tickers, limit, usd_cap, run_id, provider=provider, max_calls=max_calls)


def _record_history(payload: dict, requested: int, run_id: str | None, run_at: float) -> None:
    """Result history of a Fetch (#388): after the latest table. A failure is a job note, never a failed run: the
    latest table and the board do not depend on it."""
    from . import history
    try:
        history.record(payload, requested=requested, run_id=run_id or ledger.new_run_id(), run_at=run_at)
    except Exception as exc:
        logger.exception("Relatedness result history failed")
        ledger.add_note(f"Relatedness result history was not recorded: {type(exc).__name__}: {exc}")


def _last_run_as_of() -> str | None:
    """The ``as_of`` of the last Fetch (``run_registry`` ``relatedness_run``), or None before the first one."""
    try:
        row = rawstore.get("run_registry", "relatedness_run")
    except Exception:  # noqa: BLE001 - the table may not exist yet
        return None
    value = (row or {}).get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value.get("as_of") if isinstance(value, dict) else None


def _run(no_fetch: bool, tickers: list[str] | None, limit: int | None,
         usd_cap: float | None, run_id: str | None = None, provider: str = "deepseek",
         max_calls: int | None = None) -> dict:
    from . import store
    run_at = time.time()
    tickers = select_pilot() if tickers is None else list(tickers)
    if limit:
        tickers = tickers[:limit]
    # A Grok run asks SEC nothing (review B M1): it profiles the filings already stored; the DeepSeek nightly and
    # the Value Chain sync fetch the rest.
    vc_engine.fetch_corpus(tickers, no_fetch=no_fetch or provider != "deepseek")

    next_off_peak = None
    stopped_reason = None
    stopped_message = None
    try:
        if provider == "deepseek":
            profiles = build_profiles(tickers, use_llm=not no_fetch, usd_cap=usd_cap)
        else:
            profiles = build_profiles(tickers, use_llm=not no_fetch, provider=provider, max_calls=max_calls)
    except RunStopped as exc:
        # Boundary reached or a hard failure: render from the durable profiles
        # already written; the rest stay pending for the next off-peak run.
        stopped_reason, next_off_peak, stopped_message = (
            exc.reason, exc.next_off_peak, exc.message)
        profiles = store.load_profiles(tickers)
    except deepseek.PeakHoursError as exc:
        stopped_reason = "peak"
        next_off_peak = getattr(exc, "next_off_peak", None)
        profiles = store.load_profiles(tickers)
    # A SuperGrok allowance stop names when the allowance resets, not an off-peak time (#388).
    next_reset = next_off_peak if stopped_reason == "allowance" else None
    if next_reset is not None:
        next_off_peak = None

    # "Profiled" counts usable profiles only: empty / no_text / short_text rows are not done.
    profiles = {t: p for t, p in profiles.items() if any(p.values())}
    payload = _payload(profiles, {}, stale=store.stale_tickers(list(profiles)))
    # Pending is what a next run can still profile (#388 D4): a name whose filing has no text, or one too short to
    # profile from (#313), waits for a better filing, not for DeepSeek, so both are counted apart.
    meta_now = store.profile_meta(tickers)
    no_text = {t for t, m in meta_now.items() if m["status"] == "no_text" and t not in profiles}
    short_text = {t for t, m in meta_now.items() if m["status"] == "short_text" and t not in profiles}
    payload["no_text"] = len(no_text)
    payload["short_text"] = len(short_text)
    payload["pending"] = max(0, len(tickers) - len(profiles) - len(no_text) - len(short_text))
    if no_fetch:
        # A Refresh changes no profile or score, so it shows when the last Fetch ran (#388 REL-10).
        payload["as_of"] = _last_run_as_of() or payload["as_of"]
    if next_off_peak is not None:
        payload["next_off_peak"] = next_off_peak.strftime("%Y-%m-%d %H:%M")
    if next_reset is not None:
        payload["next_reset"] = next_reset.strftime("%Y-%m-%d %H:%M")
    if provider != "deepseek" and not no_fetch:
        # Which writer this Fetch ran with (#388): the board's ``model`` stays the profile mode's (DeepSeek).
        payload["writer"] = {"provider": provider, "model": _writer(provider)[0]}
    if stopped_reason:
        payload["stopped_reason"] = stopped_reason
    if stopped_message:
        payload["error"] = stopped_message
    # The banner reads this key (#80). A Refresh has no live provider call: the last Fetch's stored health.
    health = _stop_health(provider, stopped_reason, stopped_message)
    if health:
        payload["provider_health"] = health
    elif no_fetch:
        payload["provider_health"] = _stored_provider_health()
    if not no_fetch:
        # Refresh is pure: relatedness_scores / run_registry change only on Fetch.
        store.save_scores(payload["peers"])
        rawstore.write_rows("run_registry", [{
            "key": "relatedness_run",
            "value": json.dumps({"as_of": payload["as_of"], "n": payload["n"],
                                 "pending": payload["pending"], "no_text": payload["no_text"],
                                 "short_text": payload["short_text"],
                                 "next_off_peak": payload.get("next_off_peak")})}])
        _record_history(payload, len(tickers), run_id, run_at)
    _save(config.RELATEDNESS_JSON, payload)
    return payload


def load_payload() -> dict:
    if config.RELATEDNESS_JSON.exists():
        try:
            return json.loads(config.RELATEDNESS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"as_of": _now(), "model": deepseek.MODEL, "n": 0, "companies": [],
            "peers": {}, "n_pairs": 0, "thinking_compare": {}, "fields": list(config.PROFILE_FIELDS)}

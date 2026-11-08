"""Where each example column comes from, and the selection funnel, for the control board (issue #492, PR (i)).

Both are worked out from the code's own records and from what ``newdata.collect`` already read; nothing here opens
a store or calls a provider. A column whose source the code does not name is ``mix``, and a count the model cannot
read is None: neither is ever guessed.

- :func:`columns_by_source`: one key of :data:`PV_LABELS` per column of an example table (``s["pv"]``);
- :func:`funnel`: the stock and ETF selection steps, their rule words read from ``my_universe_rules.json`` at
  render, their counts from the spine counts (``view["universe"]["funnel"]``).
"""
from __future__ import annotations

from pathlib import Path

from RevenueChaser.rcdata.needs import screen

PV_LABELS: dict[str, str] = {
    "build": "the build", "rule": "our rule", "sec": "SEC", "wrds": "WRDS", "figi": "OpenFIGI", "alpaca": "Alpaca",
    "uw": "Unusual Whales", "yf": "yfinance", "finra": "FINRA", "ibkr": "IBKR", "exch": "exchanges",
    "av": "Alpha Vantage", "issuer": "fund issuers", "macro": "FRED · BLS · CFTC · Treasury",
    "mix": "several sources: the code does not say which column comes from which yet",
}

#: Columns the build writes about a row rather than from a source.
BOOKKEEPING = frozenset({"source", "source_as_of", "fetched_at", "run_id", "observed", "revised_at", "revised_by",
                         "valid_from", "valid_to", "source_ref", "source_id", "source_symbol", "spine_as_of"})
#: Columns our own rules decide.
RULE_COLUMNS = frozenset({"tier", "tier_rule", "rule", "reason", "kind", "change"})
#: ``<x>_source`` columns whose columns do not share their prefix.
SIBLINGS: dict[str, tuple[str, ...]] = {
    "type_source": ("security_type",),
    "link_source": ("gvkey", "iid"),
    "gics_source": ("gsector", "ggroup", "gind", "gsubind", "gics_from"),
    "figi_source": ("figi", "composite_figi", "share_class_figi", "exch_code", "figi_ticker", "security_description"),
    "shares_source": ("shares", "shares_as_of"),
    "map_source": ("ticker", "candidate_ticker"),
}
#: A source word's key, tried in this order on the lower-cased word.
_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("openfigi",), "figi"),
    (("sec", "edgar"), "sec"),
    (("wrds", "compustat", "crsp"), "wrds"),
    (("alpaca",), "alpaca"),
    (("unusual", "uw"), "uw"),
    (("yfinance",), "yf"),
    (("finra",), "finra"),
    (("ibkr",), "ibkr"),
    (("alphavantage",), "av"),
    (("nasdaq", "nyse", "cboe"), "exch"),
    (("ssga", "ishares", "vanguard", "invesco", "ark", "funds"), "issuer"),
    (("fred", "bls", "cftc", "treasury", "fed"), "macro"),
    (("rule",), "rule"),
)


def _tokens(word: str) -> list[str]:
    import re
    return [t for t in re.split(r"[^a-z0-9]+", word) if t]


def source_key(value) -> str | None:
    """The :data:`PV_LABELS` key a source word names (``sec/cover_shares`` -> ``sec``), or None when it names none.

    The short words (``sec``, ``uw``, ``fed``, ``ark``, ...) match a whole token, so ``secstat`` is not SEC; a word
    holding ``rule`` anywhere (``length_rule_v1``, ``venue_rule(Cboe)``) is our rule, before any provider in it."""
    word = str(value or "").strip().lower()
    if not word:
        return None
    if "rule" in word:
        return "rule"
    tokens = _tokens(word)
    for needles, key in _WORDS:
        for n in needles:
            if (n in tokens) if len(n) <= 4 else (n in word):
                return key
    return None


def _distinct_key(values) -> str | None:
    """One key named by every value: the key; several: ``mix``; none named: None."""
    keys = {source_key(v) for v in values if v not in (None, "")}
    keys.discard(None)
    if not keys:
        return None
    return keys.pop() if len(keys) == 1 else "mix"


def _table_key(table: str, datasets: dict) -> str | None:
    """The key of the step that serves ``table`` first: a ``first`` or ``only source`` card before the others, then
    the one writing the fewest tables. Several writers and no first source: ``mix``. No writer at all: None."""
    writers = [(name, ds) for name, ds in datasets.items() if table in tuple(getattr(ds, "tables", ()) or ())]
    if not writers:
        return None

    def role(name):
        card = screen.DATASETS.get(name)
        return card.role if card else None

    firsts = [(n, ds) for n, ds in writers if role(n) in ("first", "only source")]
    if not firsts and len(writers) > 1:
        return "mix"
    pick = min(firsts or writers, key=lambda nd: (len(tuple(nd[1].tables)), nd[0]))
    keys = {source_key(p) for p in (getattr(pick[1], "providers", ()) or ())}
    if None in keys or len(keys) != 1:
        return "mix"
    return keys.pop()


def columns_by_source(sample: dict, datasets: dict) -> list[str]:
    """One :data:`PV_LABELS` key per column of ``sample`` (``{"table", "columns", "rows"}``), in column order.

    In this order: bookkeeping is ``build``; a rule column is ``rule``; a column with a ``<x>_source`` sibling (its
    own prefix, or :data:`SIBLINGS`), and the sibling itself, take what the sibling's values in the rows name; every
    other column the table's first step (:func:`_table_key`). A sibling whose values name no source leaves its
    columns to the table's step; a table no step is known to write gives ``mix``, never a guess."""
    names = [str(c.get("name")) for c in sample.get("columns") or []]
    rows = sample.get("rows") or []
    index = {n: i for i, n in enumerate(names)}
    sibling_of: dict[str, str] = {}
    for n in names:
        if n.endswith("_source") and n != "source":
            sibling_of[n] = n
            prefix = n[: -len("_source")]
            if prefix in index:
                sibling_of.setdefault(prefix, n)
            for col in SIBLINGS.get(n, ()):
                if col in index:
                    sibling_of[col] = n
    from_sibling = {s: _distinct_key(r[index[s]] for r in rows if len(r) > index[s])
                    for s in set(sibling_of.values())}
    table_key = None
    table_done = False
    out: list[str] = []
    for n in names:
        if n in BOOKKEEPING:
            out.append("build")
        elif n in RULE_COLUMNS:
            out.append("rule")
        elif n in sibling_of and from_sibling[sibling_of[n]] is not None:
            out.append(from_sibling[sibling_of[n]])
        else:
            if not table_done:
                table_key, table_done = _table_key(str(sample.get("table") or ""), datasets), True
            out.append(table_key or "mix")
    return out


# ── The selection funnel ──────────────────────────────────────────────────────────────────────────────────────

def _usd(value: float) -> str:
    return f"{value / 1e6:,.0f}M USD"


def _n(value) -> int | None:
    return None if value is None else int(value)


def funnel(nd: dict, *, rules_path: Path | None = None) -> dict:
    """``{"stocks": [{step, n, drops}], "etfs": [...], "added": int | None}``.

    The words of each rule are read from ``my_universe_rules.json`` (or ``rules_path``) now; the counts are the
    Identity card's steps and the Our universe card's counts, which ``newdata.collect`` already read. A step those
    do not count (an ETN or CEF is not counted anywhere the model reads) is None."""
    from RevenueChaser.rcdata.build.spine import my_universe as mine
    rules = mine.load_rules(rules_path)
    steps = {str(s.get("key")): s.get("count") for s in ((nd.get("identity") or {}).get("steps") or [])}
    mu = nd.get("my_universe") or {}
    built = bool(mu.get("built"))
    kinds = mu.get("kinds") or {}
    by_rule = mu.get("rules") or {}

    def counted(mapping, *keys):
        if not built:
            return None
        return sum(int(mapping.get(k) or 0) for k in keys)

    fund_rows = ((nd.get("tables") or {}).get("fund") or {}).get("rows")
    tiers = " or ".join(rules.stock_tiers)
    types = ", ".join(rules.stock_types)
    excluded = [w for w, on in (("ETN", rules.exclude_etn), ("CEF", rules.exclude_cef)) if on]
    not_words = ("not an " + " or ".join(excluded)) if excluded else "ETNs and CEFs are kept"
    etf_rule = (f"AUM at or over {_usd(rules.etf_aum_floor)}, or no AUM and a 20-session average daily dollar "
                f"volume at or over {_usd(rules.etf_adv_floor)}")
    stocks = [
        {"step": "in the SEC ticker file", "n": _n(steps.get("sec")), "drops": ""},
        {"step": "listed on Nasdaq, NYSE or Cboe", "n": _n(steps.get("listed")),
         "drops": "over the counter, or no exchange in the file"},
        {"step": f"tier {tiers}; {types}", "n": counted(by_rule, "stock"),
         "drops": f"a tier other than {tiers}, or a type other than {types}"},
        {"step": "on the list", "n": counted(kinds, "stock"), "drops": "removed by hand; added by hand join here"},
    ]
    etfs = [
        {"step": "in the fund file", "n": _n(fund_rows), "drops": ""},
        {"step": not_words, "n": None, "drops": " and ".join(excluded) if excluded else ""},
        {"step": etf_rule, "n": counted(by_rule, "etf_aum", "etf_adv"),
         "drops": f"a known AUM under {_usd(rules.etf_aum_floor)}, or no AUM and a volume under "
                  f"{_usd(rules.etf_adv_floor)}"},
        {"step": "on the list, plus the names added by rule", "n": counted(kinds, "etf"),
         "drops": "removed by hand; added by hand join here"},
    ]
    return {"stocks": stocks, "etfs": etfs, "added": _n(by_rule.get("manual")) if built else None}


def attach_pv(file_views: list[dict], datasets: dict) -> None:
    """Put ``s["pv"]`` on every example table of every dataset row. Each sample is copied first, so the dict
    ``newdata.collect`` handed over is left as it was."""
    for f in file_views:
        for row in f.get("datasets") or []:
            row["samples"] = [{**s, "pv": columns_by_source(s, datasets)} for s in row.get("samples") or []]

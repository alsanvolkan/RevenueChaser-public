"""Shared funnel format — how many names each of a board's own rules removed.

The *format* is shared; the *steps* belong to each board. A funnel is a list of
step dicts: one ``start``, any number of ``drop`` steps, one ``result``, with
``start - sum(drops) == result`` so no name is lost off the books.

A ``note`` step may sit between the start and the result. It is shown but never
counted: it is outside the arithmetic (it says something about names that are
still in the funnel, e.g. "kept, but untyped"). A step may carry a ``link``
(``{"tab": <dashboard tab key>, "anchor": ..., "label": ...}``) that a board
renders as a pointer to where the cause is shown. Pure module: no store, no
network. ``validate`` returns problems (empty list == valid).
"""
from __future__ import annotations

MAX_TICKERS = 50
KINDS = ("start", "drop", "note", "result")


def step(key: str, label: str, count: int, kind: str, *, by_design: bool | None = None,
         fix: dict | None = None, tickers: list[str] | None = None,
         link: dict | None = None) -> dict:
    """Build one funnel step. ``by_design`` is only meaningful on a drop."""
    out: dict = {"key": key, "label": label, "count": count, "kind": kind}
    if kind == "drop":
        out["by_design"] = bool(by_design)
    out["fix"] = dict(fix) if fix else None
    if tickers is not None:
        out["tickers"] = list(tickers)
    if link:
        out["link"] = dict(link)
    return out


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate(steps) -> list[str]:
    """Problems with ``steps``; ``[]`` when it is a valid funnel."""
    if not isinstance(steps, list) or not steps:
        return ["funnel is empty: needs a start step and a result step"]
    problems: list[str] = []
    bad = [s for s in steps if not isinstance(s, dict)]
    if bad:
        return ["funnel steps must be dicts"]
    kinds = [s.get("kind") for s in steps]
    if kinds.count("start") != 1 or kinds[0] != "start":
        problems.append(f"funnel needs exactly one start step, first (kinds: {kinds})")
    if kinds.count("result") != 1 or kinds[-1] != "result":
        problems.append(f"funnel needs exactly one result step, last (kinds: {kinds})")
    for s in steps:
        key = s.get("key")
        if s.get("kind") not in KINDS:
            problems.append(f"step {key!r}: unknown kind {s.get('kind')!r}")
        if not _is_count(s.get("count")):
            problems.append(
                f"step {key!r}: count must be a non-negative integer, got {s.get('count')!r}")
        tickers = s.get("tickers")
        if tickers is not None and len(tickers) > MAX_TICKERS:
            problems.append(
                f"step {key!r}: {len(tickers)} tickers listed, at most {MAX_TICKERS}")
        fix = s.get("fix")
        if fix:
            route = str(fix.get("route") or "")
            if not route.startswith("/run/"):
                problems.append(f"step {key!r}: fix route {route!r} must start with /run/")
        link = s.get("link")
        if link is not None:
            tab = link.get("tab") if isinstance(link, dict) else None
            if not (isinstance(tab, str) and tab):
                problems.append(
                    f"step {key!r}: link needs a non-empty string tab, got {link!r}")
    if not problems and all(_is_count(s.get("count")) for s in steps):
        start = steps[0]["count"]
        drops = sum(s["count"] for s in steps if s.get("kind") == "drop")
        result = steps[-1]["count"]
        if start - drops != result:
            problems.append(
                f"funnel does not add up: start {start} - drops {drops} = "
                f"{start - drops}, but result is {result}")
    return problems


def to_dict(steps: list[dict]) -> list[dict]:
    """JSON-safe copy of ``steps``."""
    return [dict(s) for s in steps]


def from_dict(data) -> list[dict]:
    """Steps from a decoded payload field; anything that is not a list gives ``[]``."""
    if not isinstance(data, list):
        return []
    return [dict(s) for s in data if isinstance(s, dict)]


def funnel_panel_html(steps, *, panel_id: str = "funnel-panel",
                      summary: str = "Universe to scored: where names drop out") -> str:
    """The collapsible panel listing every step of a funnel (#383 C16).

    ``""`` for an empty or invalid funnel (nothing truthful to show); otherwise one row per step, with a
    ``drop``'s listed names and an "and N more" tail. The panel id and summary are configurable, and every
    string is escaped (``core.html_safe.esc``), so a hostile label or ticker cannot reach the markup.
    """
    from .html_safe import esc

    if validate(steps):
        return ""
    rows = []
    for st in steps:
        names = ""
        if st.get("tickers"):
            more = st["count"] - len(st["tickers"])
            names = (f'<div class="note">{esc(", ".join(st["tickers"]))}'
                     f'{esc(f" and {more} more" if more > 0 else "")}</div>')
        # Only a ``drop`` subtracts. A ``note`` counts names that are still in the result, so a minus sign there
        # would read as a removal. #41 is this panel's first note ROW, and a positive number is already the
        # convention for the kind: ``module_comovement`` renders its own note as "Shown, never counted".
        sign = "&minus;" if st["kind"] == "drop" else ""
        design = "" if st["kind"] != "drop" else (" by design" if st.get("by_design") else " needs a look")
        rows.append(f'<tr><td>{esc(st["label"])}{names}</td><td>{sign}{esc(st["count"])}</td>'
                    f'<td>{esc(design.strip())}</td></tr>')
    return (f'<details class="rules" id="{esc(panel_id)}"><summary>{esc(summary)}</summary>'
            '<div class="rules-body"><table><tr><th>Step</th><th>Names</th><th></th></tr>'
            + "".join(rows) + '</table></div></details>')

"""The Settings / Data control board (design v28, avo 30 Oct 2026): what the page shows, worked out from what
``newdata.collect`` read and from the code's own records.

This module only arranges. Every number comes from ``collect`` (``ops.sqlite``, the serving files, the raw and Parquet
folders); every name, step, role, cadence and word comes from ``rcdata.api.screen``; every column from
``rcdata.scaffold.SCHEMA_OF_KIND``; and which board reads which table from the boards' declared reads (or their
planned reads, drawn dashed) and the need catalog. Nothing here opens a store or calls a provider, so a render is
pure and a test builds the whole board from a dictionary.

The page itself is ``control_render``; this is its model:

- ``health``: the twelve tiles of "Health right now";
- ``universe``: the tier sets the next fetches and fills use, as applied (``core.board_settings``);
- ``files``: the serving files in build order, each with its datasets (the Fetch section) and its tables, columns,
  readers and example rows (the Data section);
- ``providers``: the day caps, today's use, our pace and the provider's rule;
- ``flow``: the flow diagram's data (``RC_FLOW``), drawn by the page's script;
- ``calendars``: what each Backfill calendar shows;
- ``cross_check`` and ``queue``: the last two sections.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from RevenueChaser.rcdata import caps, scaffold
from RevenueChaser.rcdata.api import boards as api_boards
from RevenueChaser.rcdata.api import catalog
from RevenueChaser.rcdata.api import screen

#: The page shows avo's clock (CLAUDE.md: Europe/Amsterdam).
LOCAL = ZoneInfo("Europe/Amsterdam")

#: The dashboard's menu, as the readers' tree and the flow diagram's right column group the boards. It is
#: ``app.dashboard.TABS`` in short; importing the dashboard from a render would pull in the whole app, so the copy is
#: held equal to it by ``test_the_board_menu_is_the_dashboards``.
BOARD_MENU: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Research", (("module_earnings_radar", "Earnings Radar"), ("module_spx_gamma", "SPX Gamma"),
                  ("module_oipd", "Implied Probability"), ("module_financials", "Financials"),
                  ("module_uw_interaction", "UW Interaction (lab)"))),
    ("Classification", (("module_comovement", "Co-movement"), ("module_value_chain", "Value Chain"),
                        ("module_relatedness", "Relatedness"))),
    ("Screeners", (("module_sector_rotation", "Sector Rotation"), ("module_asymmetric_growth", "Asymmetric Growth"),
                   ("module_swing_potential", "Swing Potential"), ("module_fundamentals", "Fundamentals"))),
    ("Ownership", (("module_sec_13f", "SEC 13F"), ("module_leaps", "LEAPS"))),
    ("Ops", (("module_settings", "Settings / Data health"), ("logs", "Logs"), ("runs", "Runs"))),
)

#: One hue per serving file (the design's colour key); a file's tables are darker shades of it.
FILE_COLORS = {"spine": "#26a34a", "prices": "#d07a09", "fundamentals": "#765ed4", "ownership": "#d65c8d",
               "options": "#14a195", "market": "#2c94ea"}
SOURCE_COLORS = ("#6f7b32", "#646e2d", "#586228", "#4d5523", "#41481e")
BOARD_COLOR = "#6d7585"
#: The source boxes of the flow diagram, by step: where a provider's datasets sit in the plan.
SOURCE_GROUPS = (("Identity", (1, 2, 3, 4, 5, 7)), ("Index & fund holdings", (6,)), ("Prices", (8,)),
                 ("Fundamentals & earnings", (9,)), ("Ownership & short side", (10,)),
                 ("Options & market", (11,)))

STATUS_PILL = {"fresh": ("p-ok", "fresh"), "unchanged": ("p-ok", "unchanged"), "late": ("p-warn", "late"),
               "stale": ("p-bad", "stale"), "never": ("p-bad", "never ran")}
#: A Health tile filters the Fetch rows by this (an unchanged dataset counts as fresh: it ran and found nothing new).
STATUS_FILTER = {"fresh": "fresh", "unchanged": "fresh", "late": "late", "stale": "stale", "never": "never"}
#: The builder's own alarm: no run started for this long (the old section's ``ND_WORKER_SILENT_MINUTES``).
BUILDER_SILENT_MINUTES = 70
#: A provider's share of its day cap: amber from half, red past this.
CAP_WARN, CAP_BAD = 0.5, 0.7
CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 31, "quarterly": 92}
ROLE_WORDS = {"first": "the first source: its value is served", "only source": "the only source of this value",
              "fills": "fills what the first source leaves open", "cross-check": "compared, not served",
              "build": "writes the served tables from the others' Parquet"}

# Source icons: Tabler Icons (MIT licence, see THIRD-PARTY-NOTICES.md), the outline paths the design drew.
ICONS: dict[str, str] = {
    "sec": "M14 3v4a1 1 0 0 0 1 1h4 M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2z M9 9l1 0 "
           "M9 13l6 0 M9 17l6 0",
    "wrds": "M12 6m-8 0a8 3 0 1 0 16 0a8 3 0 1 0 -16 0 M4 6v6a8 3 0 0 0 16 0v-6 M4 12v6a8 3 0 0 0 16 0v-6",
    "openfigi": "M18.9 7a8 8 0 0 1 1.1 5v1a6 6 0 0 0 .8 3 M8 11a4 4 0 0 1 8 0v1a10 10 0 0 0 2 6 M12 11v2a14 14 0 0 0 "
                "2.5 8 M8 15a18 18 0 0 0 1.8 6 M4.9 19a22 22 0 0 1 -.9 -7v-1a8 8 0 0 1 12 -6.95",
    "ishares": "M10 3.2a9 9 0 1 0 10.8 10.8a1 1 0 0 0 -1 -1h-6.8a2 2 0 0 1 -2 -2v-7a.9 .9 0 0 0 -1 -.8 M15 3.5a9 9 0 "
               "0 1 5.5 5.5h-4.5a1 1 0 0 1 -1 -1v-4.5",
    "ssga": "M10 3.2a9 9 0 1 0 10.8 10.8a1 1 0 0 0 -1 -1h-3.8a4.1 4.1 0 1 1 -5 -5v-4a.9 .9 0 0 0 -1 -.8 M15 3.5a9 9 0 0 "
            "1 5.5 5.5h-4.5a9 9 0 0 0 -1 -1v-4.5",
    "vanguard": "M2 20a2.4 2.4 0 0 0 2 1a2.4 2.4 0 0 0 2 -1a2.4 2.4 0 0 1 2 -1a2.4 2.4 0 0 1 2 1a2.4 2.4 0 0 0 2 1a2.4 "
                "2.4 0 0 0 2 -1a2.4 2.4 0 0 1 2 -1a2.4 2.4 0 0 1 2 1a2.4 2.4 0 0 0 2 1a2.4 2.4 0 0 0 2 -1 M4 18l-1 "
                "-3h18l-1 3 M11 12h7l-7 -9v9 M8 7l-2 5",
    "nasdaq": "M13 5h8 M13 9h5 M13 15h8 M13 19h5 M3 4m0 1a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-4a1 1 0 0 1 "
              "-1 -1z M3 14m0 1a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1z",
    "invesco": "M12 4l-8 4l8 4l8 -4l-8 -4 M4 12l8 4l8 -4 M4 16l8 4l8 -4",
    "alpaca": "M4 6m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v3a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z M6 4l0 2 M6 11l0 9 M10 "
              "14m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v3a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z M12 4l0 10 M12 19l0 1 M16 "
              "5m0 1a1 1 0 0 1 1 -1h2a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-2a1 1 0 0 1 -1 -1z M18 4l0 1 M18 11l0 9",
    "yfinance": "M4 19l16 0 M4 15l4 -6l4 2l4 -5l4 4",
    "uw": "M16.69 7.44a6.973 6.973 0 0 0 -1.69 4.56c0 1.747 .64 3.345 1.699 4.571 M2 9.504c7.715 8.647 14.75 10.265 "
          "20 2.498c-5.25 -7.761 -12.285 -6.142 -20 2.504 M18 11v.01 M11.5 10.5c-.667 1 -.667 2 0 3",
    "alphavantage": "M4 5m0 2a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2z M16 3l0 4 M8 3l0 4 "
                    "M4 11l16 0 M8 15h2v2h-2z",
    "fred": "M4 19l4 -6l4 2l4 -5l4 4l0 5l-16 0",
    "finra": "M11.46 20.846a12 12 0 0 1 -7.96 -14.846a12 12 0 0 0 8.5 -3a12 12 0 0 0 8.5 3a12 12 0 0 1 -.09 7.06 "
             "M15 19l2 2l4 -4",
    "nasdaqtrader": "M3.5 5.5l1.5 1.5l2.5 -2.5 M3.5 11.5l1.5 1.5l2.5 -2.5 M3.5 17.5l1.5 1.5l2.5 -2.5 M11 6l9 0 M11 "
                    "12l9 0 M11 18l9 0",
    "nyse": "M10 5a2 2 0 0 1 4 0a7 7 0 0 1 4 6v3a4 4 0 0 0 2 3h-16a4 4 0 0 0 2 -3v-3a7 7 0 0 1 4 -6 M9 17v1a3 3 0 0 0 "
            "6 0v-1",
    "ibkr": "M7 12l5 5l-1.5 1.5a3.536 3.536 0 1 1 -5 -5l1.5 -1.5 M17 12l-5 -5l1.5 -1.5a3.536 3.536 0 1 1 5 5l-1.5 "
            "1.5 M3 21l2.5 -2.5 M18.5 5.5l2.5 -2.5 M10 11l-2 2 M13 14l-2 2",
    "treasury": "M3 21l18 0 M3 10l18 0 M5 6l7 -3l7 3 M4 10l0 11 M20 10l0 11 M8 14l0 3 M12 14l0 3 M16 14l0 3",
    "cboe": "M21 12h-2c-.894 0 -1.662 -.857 -1.761 -2c-.296 -3.45 -.749 -6 -2.749 -6s-2.5 3.582 -2.5 8s-.5 8 -2.5 "
            "8s-2.452 -2.547 -2.749 -6c-.1 -1.147 -.867 -2 -1.763 -2h-2",
    "cftc": "M7 20l10 0 M6 6l6 -1l6 1 M12 3l0 17 M9 12l-3 -6l-3 6a3 3 0 0 0 6 0 M21 12l-3 -6l-3 6a3 3 0 0 0 6 0",
    "fed": "M3 21l18 0 M3 10l18 0 M5 6l7 -3l7 3 M4 10l0 11 M20 10l0 11 M8 14l0 3 M12 14l0 3 M16 14l0 3",
    "bls": "M3 7m0 2a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v9a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2z M8 7v-2a2 2 0 0 1 2 -2h4a2 "
           "2 0 0 1 2 2v2 M12 12l0 .01 M3 13a20 20 0 0 0 18 0",
}


# ── Small helpers ─────────────────────────────────────────────────────────────────────────────────────────────

def _local(now: datetime) -> datetime:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(LOCAL)


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def day_words(value) -> str:
    """``28 Oct`` (``28 Oct 2025`` outside this year is not needed on a page read today, so the year is left out)."""
    d = _day(value)
    return f"{d.day} {d.strftime('%b')}" if d else "—"


def count(value) -> str:
    return "—" if value is None else f"{int(value):,}"


def _shade(hex_color: str, i: int, n: int) -> str:
    """The ``i``-th of ``n`` darker shades of ``hex_color`` (a file's tables, a group's sources)."""
    r, g, b = (int(hex_color[k:k + 2], 16) for k in (1, 3, 5))
    f = 1 - 0.55 * (i + 1) / max(n, 1)
    return "#" + "".join(f"{max(0, min(255, int(c * f))):02x}" for c in (r, g, b))


def _board_labels() -> dict[str, str]:
    return {key: label for _, tabs in BOARD_MENU for key, label in tabs}


def _status_of(card: dict) -> str:
    return str(card.get("status") or "never")


# ── The readers: which board reads which table ────────────────────────────────────────────────────────────────

def readers() -> dict[str, dict]:
    """``{board key: {"declared": bool, "needs": (...), "tables": {table: [needs]}}}`` for every board in the menu.

    A board that declares ``READS`` is measured; one not migrated yet is drawn from ``boards.PLANNED_READS``, which
    the page marks as planned (a dashed line), so the diagram never claims more than the code holds."""
    out = {}
    for _, tabs in BOARD_MENU:
        for key, _label in tabs:
            reads = api_boards.reads_of(key)
            tables: dict[str, list[str]] = {}
            for need in reads["needs"]:
                n = catalog.NEEDS.get(need)
                if n is not None:
                    tables.setdefault(n.table, []).append(need)
            out[key] = {"declared": reads["declared"], "needs": tuple(reads["needs"]), "tables": tables}
    return out


def _table_kind() -> dict[str, str]:
    return {t: kind for kind, tables in scaffold.SCHEMA_OF_KIND.items() for t in tables}


# ── Health right now ──────────────────────────────────────────────────────────────────────────────────────────

def _providers(nd: dict) -> list[dict]:
    """One row per provider: today's calls against our day cap, our pace and the provider's rule."""
    today = {str(p.get("provider")): p for p in (nd.get("spend") or {}).get("providers") or []}
    rows = []
    for pid, words in screen.PROVIDERS.items():
        p = today.get(pid) or {}
        cap = caps.DAY_CAPS.get(pid)
        used = int(p.get("today") or 0)
        share = (used / cap) if cap else None
        cls = "" if share is None or share < CAP_WARN else ("warn" if share <= CAP_BAD else "bad")
        rows.append({"id": pid, "name": words.name, "what": words.what, "pace": words.pace, "rule": words.rule,
                     "cap": cap, "used": used, "share": share, "cls": cls, "icon": ICONS.get(pid, ""),
                     "failures": p.get("failures_today"), "http_429": p.get("http_429_today")})
    rows.sort(key=lambda r: (r["share"] is None, -(r["share"] or 0), r["name"].lower()))
    return rows


def _tile(key, cls, n, label, f=None) -> dict:
    return {"key": key, "cls": cls, "n": n, "label": label, "filter": f}


def health(nd: dict, providers: list[dict], universe: dict, ibkr: dict | None) -> dict:
    cards = nd.get("datasets") or []
    by = {k: 0 for k in ("fresh", "late", "stale", "never")}
    for c in cards:
        by[STATUS_FILTER.get(_status_of(c), "never")] += 1
    problems = sum(int(c.get("issues_open") or 0) for c in cards)
    v = nd.get("verified") or {}
    run = v.get("builder_last_run") or {}
    silent = v.get("silent_minutes")
    sessions = nd.get("sessions") or {}
    newest = max(sessions) if sessions else None
    bars = next((c for c in cards if c.get("dataset") == "alpaca/bars"), None)
    bars_ok = bars is not None and _status_of(bars) in ("fresh", "unchanged")
    queue = (nd.get("queue") or {}).get("active") or []
    near = next((p for p in providers if p["share"] is not None), None)
    others = [p for p in providers if p["share"] is not None and p is not near][:2]
    backups = [k for k in ((nd.get("backups") or {}).get("kinds") or []) if k.get("newest")]
    newest_backup = max(backups, key=lambda k: str(k["newest"]), default=None)
    tiles = [
        _tile("fresh", "ok", count(by["fresh"]), "fresh datasets", "fresh"),
        _tile("late", "warn" if by["late"] else "ok", count(by["late"]), "late", "late"),
        _tile("stale", "bad" if by["stale"] else "ok", count(by["stale"]), "stale", "stale"),
        _tile("never", "none", count(by["never"]), "never ran", "never"),
        _tile("problems", "neu", count(problems), "open problems in total · build_issues, one per row", "problems"),
        _tile("session", "ok" if bars_ok else "warn", day_words(newest) if newest else "none",
              (f"newest session in prices · {count(sessions[newest])} names have a bar on it" if newest
               else "no session in prices yet: price_bar_daily holds no bar"), "session"),
        _tile("queue", "ops", count(len(queue)), "waiting or running in the queue", "queue"),
        _tile("builder", "none" if silent is None else ("bad" if silent > BUILDER_SILENT_MINUTES else "ok"),
              "never" if silent is None else f"{silent} min",
              ("the builder has no run on record" if silent is None else
               f"since the builder's last run, {_clock(run.get('started_at'))} · "
               + ("past" if silent > BUILDER_SILENT_MINUTES else "on time, within")
               + f" the {BUILDER_SILENT_MINUTES}-minute threshold"), "builder"),
        _tile("cap", "none" if near is None else ("bad" if near["share"] > CAP_BAD else
                                                  "warn" if near["share"] >= CAP_WARN else "ok"),
              "—" if near is None else f"{near['share']:.0%}",
              "no provider has a day cap" if near is None else
              (f"nearest its day cap: {near['name']}, {count(near['used'])} / {count(near['cap'])} calls"
               + ("" if not others else " · then " + ", ".join(f"{p['name']} {p['share']:.0%}" for p in others))),
              "cap"),
        _tile("universe", "none", count(universe.get("filled")) if universe.get("filled") is not None else "—",
              f"of {count(universe.get('size'))} names in the universe have a bar on the newest session"
              + ("" if universe.get("fill_enabled") else " · the Fill buttons are off"), "universe"),
        _tile("gateway", *_gateway(ibkr), "gateway"),
        _tile("backup", "none" if newest_backup is None else ("warn" if newest_backup.get("warn") else "ok"),
              "none" if newest_backup is None else day_words(newest_backup["newest"]),
              "no backup recorded" if newest_backup is None else
              f"newest backup, {newest_backup.get('kind')} · {newest_backup.get('age_days')} days old"),
    ]
    summary = [f"{by['fresh']} fresh", f"{by['late']} late", f"{by['stale']} stale", f"{by['never']} never ran",
               f"{problems} open problems"]
    if near is not None:
        summary.append(f"{near['name']} nearest its cap ({near['share']:.0%})")
    return {"tiles": tiles, "summary": summary, "counts": by, "problems": problems}


def _clock(value) -> str:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return "—"
    return _local(when).strftime("%H:%M")


def _gateway(ibkr: dict | None) -> tuple[str, str, str]:
    if not ibkr:
        return "none", "unknown", "IBKR gateway · its login check has no record on this page"
    state = str(ibkr.get("state") or "unknown")
    if state == "up":
        return "ok", "open", "IBKR gateway · logged in"
    return "warn", "closed" if state == "down" else state, ("IBKR gateway · the IV step skips until a login · "
                                                          + str(ibkr.get("text") or ""))


# ── The universe ──────────────────────────────────────────────────────────────────────────────────────────────

def universe(nd: dict, applied: dict | None) -> dict:
    """The universe bar: each tier set with its count and whether the applied universe holds it, the named groups
    a narrowed request adds, and how many names of it have a bar on the newest session."""
    from RevenueChaser.core import board_settings
    u = nd.get("universe") or {}
    counts = u.get("counts") or {}
    applied = applied or {}
    every_set = sum(board_settings.UNIVERSE_SET_BITS.values())
    every_group = sum(board_settings.UNIVERSE_INCLUDE_BITS.values())
    set_bits = int(applied.get("UNIVERSE_SETS", every_set))
    inc_bits = int(applied.get("UNIVERSE_INCLUDE", every_group))
    sets = []
    for s in screen.UNIVERSE_SETS:
        bit = board_settings.UNIVERSE_SET_BITS.get(s.key)
        sets.append({"key": s.key, "label": s.label, "tier": s.tier, "words": s.words, "offered": s.offered,
                     "n": counts.get(s.key), "on": bool(s.offered and bit and set_bits & bit)})
    include = [{"key": k, "words": screen.INCLUDE_WORDS[k],
                "on": bool(inc_bits & board_settings.UNIVERSE_INCLUDE_BITS[k])}
               for k in board_settings.UNIVERSE_INCLUDE_BITS]
    size = sum(int(s["n"] or 0) for s in sets if s["on"])
    sessions = nd.get("sessions") or {}
    newest = max(sessions) if sessions else None
    default = set_bits == every_set and inc_bits == every_group
    return {"sets": sets, "include": include, "size": size, "total": u.get("total"), "default": default,
            "filled": sessions.get(newest) if newest else None, "tickers": u.get("tickers") or {},
            "as_of": ((nd.get("identity") or {}).get("as_of")), "set_bits": set_bits, "include_bits": inc_bits,
            "fill_enabled": screen.fill_enabled()}


def scope(universe_view: dict) -> dict | None:
    """What a Fetch on a dataset that honours a scope asks for under the applied universe: None for the default
    universe (the daily run takes it all, Karar 82 A), else its tiers, the names of a part of B, and its groups."""
    if universe_view["default"]:
        return None
    on = {s["key"] for s in universe_view["sets"] if s["on"]}
    tiers = sorted({s["tier"] for s in universe_view["sets"] if s["on"] and s["key"] not in ("E", "R")})
    names: list[str] = []
    if {"E", "R"} <= on:
        tiers = sorted(set(tiers) | {"B"})
    else:
        for key in ("E", "R"):
            if key in on:
                names += list(universe_view["tickers"].get(key) or [])
    include = [g["key"] for g in universe_view["include"] if g["on"]]
    return {"tiers": tiers, "names": names, "include": include}


# ── The files and their datasets ──────────────────────────────────────────────────────────────────────────────

def _age(card: dict, now: datetime) -> dict:
    """Time since the last check against the cadence: ``{share, text, cls}``."""
    cadence = str(card.get("cadence") or "daily")
    period = CADENCE_DAYS.get(cadence, 1)
    checked = _day(card.get("checked_on"))
    if checked is None:
        return {"share": None, "cls": "s-none", "text": f"never fetched · {cadence}"}
    days = (_local(now).date() - checked).days
    share = days / period
    cls = "s-ok" if share <= 1 else ("s-warn" if share <= 2 else "s-bad")
    return {"share": min(share, 1.0), "cls": cls,
            "text": f"last check {day_words(checked)} · {cadence} · {days} of {period} day{'s' if period > 1 else ''}"}


def _coverage(card: dict) -> dict | None:
    cov, target = card.get("coverage"), card.get("coverage_target")
    if cov is None and target is None:
        return None
    if cov is None:
        return {"share": None, "target": target, "cls": "s-none", "text": f"not measured yet · target {target:.0%}"}
    cls = "s-ok" if target is None or cov >= target else "s-warn"
    return {"share": float(cov), "target": target, "cls": cls,
            "text": f"{cov:.1%}" + (f" · target {target:.0%}" if target is not None else "")}


def _fill(ds_name: str, ds, kind: str, universe_view: dict, card: dict) -> dict:
    """The "Fill by tier set" row: cells for the offered sets when the dataset honours a scope, else the words why
    it has none. A cell's press is off unless ``RC_FILL_ENABLED`` is set (avo's word, "başla")."""
    from RevenueChaser.rcdata.build import base
    if card.get("attended"):
        return {"cells": [], "why": screen.NO_TIER_ATTENDED}
    if not base.honours_scope(ds):
        return {"cells": [], "why": screen.NO_TIER_DATASET_WORDS.get(ds_name) or screen.NO_TIER_WORDS[kind]}
    cells = [{"key": s["key"], "label": s["label"], "n": s["n"], "picked": s["on"]}
             for s in universe_view["sets"] if s["offered"]]
    return {"cells": cells, "why": None, "calendar": ds_name in CALENDARS}


def _dataset_row(card: dict, ds, kind: str, universe_view: dict, now: datetime) -> dict:
    name = str(card["dataset"])
    meta = screen.DATASETS.get(name)
    from RevenueChaser.rcdata.build import base
    status = _status_of(card)
    pill, words = STATUS_PILL.get(status, ("p-bad", status))
    providers = tuple(getattr(ds, "providers", ()) or ())
    attended = bool(card.get("attended"))
    build = bool(meta and meta.role == "build")
    tags = []
    if name == "alpaca/bars" and status not in ("fresh", "unchanged"):
        tags.append("session")
    if any(p in providers for p in ("ibkr",)):
        tags.append("gateway")
    cost = card.get("cost") or {}
    return {
        "dataset": name, "label": card.get("label") or name, "status": status, "pill": pill,
        "status_words": words + (f" · {card['status_reason']}" if card.get("status_reason") else ""),
        "filter": STATUS_FILTER.get(status, "never"), "tags": tags, "step": meta.step if meta else 0,
        "role": meta.role if meta else "first", "cadence": card.get("cadence"), "providers": providers,
        "attended": attended, "command": card.get("attended_command"), "build": build,
        "rows": card.get("table_rows") if card.get("table_rows") is not None else card.get("rows"),
        "added": card.get("added"), "removed": card.get("removed"), "calls": card.get("calls"),
        "checked_on": card.get("checked_on"), "source_as_of": card.get("source_as_of"),
        "coverage": _coverage(card), "age": _age(card, now),
        "fetch": {"self": name != "spine/security", "honours_scope": base.honours_scope(ds),
                  "provider": cost.get("provider"), "remaining": cost.get("remaining"), "cap": cost.get("cap")},
        "fill": _fill(name, ds, kind, universe_view, card),
        "issues": card.get("issues") or [], "issues_open": int(card.get("issues_open") or 0),
        "paused": bool((card.get("schedule") or {}).get("paused")),
        "backfill": card.get("backfill"), "calendar": name in CALENDARS,
        "recent": card.get("recent") or [], "samples": card.get("samples") or [],
        "cross_check": name in ("wrds/prices", "uw/total_float", "yfinance/float"),
    }


def _health_of(rows: list[dict]) -> dict:
    fresh = sum(1 for r in rows if r["filter"] == "fresh")
    total = len(rows)
    late = sum(1 for r in rows if r["filter"] == "late")
    stale = sum(1 for r in rows if r["filter"] in ("stale",))
    never = sum(1 for r in rows if r["filter"] == "never")
    state = "none" if not total or fresh == 0 and never == total else (
        "ok" if fresh == total else ("bad" if stale else "warn"))
    pills = []
    if late:
        pills.append(("p-warn", f"{late} late"))
    if stale:
        pills.append(("p-bad", f"{stale} stale"))
    if never:
        pills.append(("p-bad", f"{never} never ran"))
    if not pills and total:
        pills.append(("p-ok", "fresh"))
    return {"fresh": fresh, "total": total, "state": state, "pills": pills}


def files(nd: dict, datasets: dict, universe_view: dict, reads: dict, now: datetime) -> list[dict]:
    """The serving files in build order, each with its dataset rows (in step order) and its tables."""
    cards = {str(c.get("dataset")): c for c in nd.get("datasets") or []}
    tables_now = nd.get("tables") or {}
    storage = nd.get("storage") or {}
    labels = _board_labels()
    out = []
    for kind, fc in screen.FILES.items():
        rows = [_dataset_row(cards[n], ds, kind, universe_view, now)
                for n, ds in datasets.items() if ds.kind == kind and n in cards]
        rows.sort(key=lambda r: (r["step"], 0 if r["role"] == "first" else 1, r["dataset"]))
        schema = scaffold.SCHEMA_OF_KIND.get(kind) or {}
        color = FILE_COLORS[kind]
        tables = []
        for i, (t, cols) in enumerate(schema.items()):
            readers_of = [(key, labels[key], reads[key]["declared"], reads[key]["tables"][t])
                          for key in labels if key in reads and t in reads[key]["tables"]]
            writers = [r["dataset"] for r in rows if t in tuple(getattr(datasets[r["dataset"]], "tables", ()))]
            tables.append({"name": t, "color": _shade(color, i, len(schema) + 1),
                           "columns": [(c, str(ty)) for c, ty in cols],
                           "rows": (tables_now.get(t) or {}).get("rows"),
                           "newest": (tables_now.get(t) or {}).get("newest"),
                           "readers": [{"key": k, "label": lb, "declared": d, "needs": n}
                                       for k, lb, d, n in readers_of],
                           "writers": writers})
        live = (storage.get("files") or {}).get(kind) or {}
        out.append({"kind": kind, "name": f"{kind}.duckdb", "steps": fc.steps, "words": fc.words,
                    "runs_on_universe": fc.runs_on_universe, "color": color, "datasets": rows,
                    "health": _health_of(rows), "tables": tables, "path": live.get("path"),
                    "bytes": live.get("bytes"), "storage": storage.get("datasets") or {},
                    "roots": storage.get("roots") or {}})
    return out


# ── The Backfill calendars ────────────────────────────────────────────────────────────────────────────────────

def _cal_bars(nd: dict, universe_view: dict, providers: dict) -> dict:
    from RevenueChaser.rcdata.build.prices import bars
    sessions = nd.get("sessions") or {}
    size = universe_view["size"] or 0
    p = providers.get("alpaca") or {}
    return {"dataset": "alpaca/bars", "provider": "Alpaca", "from": screen.CALENDAR_FIRST["alpaca/bars"],
            "groups": True, "names_per_session": sessions, "universe": size,
            "page": bars.LIMIT, "cap": p.get("cap"), "used": p.get("used", 0), "unit": "sessions",
            "first": (f"Alpaca has daily bars from 2016; the store holds them from {sessions and min(sessions) or 'no day yet'}")}


def _cal_spx(nd: dict, providers: dict) -> dict | None:
    card = next((c for c in nd.get("datasets") or [] if c.get("dataset") == "uw/spot_expiry_strike"), None)
    bf = (card or {}).get("backfill") or {}
    if not bf.get("from_earliest"):
        return None
    p = providers.get("uw") or {}
    return {"dataset": "uw/spot_expiry_strike", "provider": "UW", "from": bf["from_earliest"], "groups": False,
            "missing": bf.get("missing") or [], "calls_per_session": bf.get("calls_per_partition") or 1,
            "cap": p.get("cap"), "used": p.get("used", 0), "unit": "sessions",
            "first": f"the SPX expiry-strike set starts on {day_words(bf['from_earliest'])} {bf['from_earliest'][:4]}"}


CALENDARS = ("alpaca/bars", "uw/spot_expiry_strike")


def calendars(nd: dict, universe_view: dict, provider_rows: list[dict]) -> dict:
    providers = {p["id"]: p for p in provider_rows}
    out = {"alpaca/bars": _cal_bars(nd, universe_view, providers)}
    spx = _cal_spx(nd, providers)
    if spx:
        out["uw/spot_expiry_strike"] = spx
    return out


# ── The flow diagram ──────────────────────────────────────────────────────────────────────────────────────────

def flow(file_views: list[dict], datasets: dict, reads: dict, universe_view: dict) -> dict:
    """``RC_FLOW``: source → plan step → serving file and its tables → board, every line from the code."""
    labels = _board_labels()
    groups, src_step = [], []
    for group, steps in SOURCE_GROUPS:
        items: dict[str, dict] = {}
        for name, ds in datasets.items():
            meta = screen.DATASETS.get(name)
            if not meta or meta.step not in steps:
                continue
            for pid in getattr(ds, "providers", ()) or ():
                it = items.setdefault(pid, {"name": screen.PROVIDERS[pid].name, "steps": [], "plan": False,
                                            "what": [], "icon": ICONS.get(pid, "")})
                if str(meta.step) not in it["steps"]:
                    it["steps"].append(str(meta.step))
                it["what"].append(meta.label)
        if not items:
            continue
        rows = []
        for i, (pid, it) in enumerate(sorted(items.items(), key=lambda kv: kv[1]["name"].lower())):
            it["color"] = SOURCE_COLORS[i % len(SOURCE_COLORS)]
            it["what"] = " · ".join(dict.fromkeys(it["what"]))
            rows.append(it)
            for s in it["steps"]:
                src_step.append([f"{group}|{it['name']}", s])
        groups.append({"group": group, "items": rows})
    counts = {s["key"]: s["n"] for s in universe_view["sets"]}
    step_words = dict(screen.STEPS)
    if universe_view.get("total"):
        step_words[1] = f"Security · {count(universe_view['total'])} tickers in"
        tiers = {"A": counts.get("A"), "B": (counts.get("E") or 0) + (counts.get("R") or 0),
                 "C": counts.get("C"), "D": counts.get("D")}
        step_words[2] = "Tiers · " + " · ".join(f"{k} {count(v)}" for k, v in tiers.items())
    steps = [{"id": str(k), "label": f"{k} · {w}"} for k, w in step_words.items()]
    build_tables = {f["kind"]: [t["name"] for t in f["tables"]] for f in file_views}
    step_tab = []
    for name, ds in datasets.items():
        meta = screen.DATASETS.get(name)
        if not meta:
            continue
        served = build_tables.get(ds.kind, [])
        # A dataset's own tables where they are served; one that stages only (its tables are built by the kind's
        # build step, or are staging tables) feeds all of its file's tables.
        own = [t for t in (getattr(ds, "tables", ()) or ()) if t in served] or served
        for t in own:
            pair = [str(meta.step), t]
            if pair not in step_tab:
                step_tab.append(pair)
    # Step 2 has no dataset of its own: the tiers are a column of the spine's security table.
    step_tab.append(["2", "security"])
    tab_board, board_info = [], {}
    for key, label in labels.items():
        r = reads.get(key) or {"tables": {}, "needs": (), "declared": False}
        for t in r["tables"]:
            tab_board.append([t, label] + ([] if r["declared"] else ["planned"]))
        board_info[label] = {"needs": list(r["needs"]), "declared": r["declared"], "tier": "", "names": "",
                             "note": "" if r["needs"] else "no data need recorded yet"}
    file_board = []
    kind_of = _table_kind()
    for t, b, *_ in tab_board:
        pair = [f"{kind_of.get(t, '?')}.duckdb", b]
        if kind_of.get(t) and pair not in file_board:
            file_board.append(pair)
    file_rows = [{"name": f["name"], "tables": [t["name"] for t in f["tables"]],
                  "health": {"fresh": f["health"]["fresh"], "total": f["health"]["total"],
                             "state": f["health"]["state"]},
                  "line": f["color"], "tables_color": [t["color"] for t in f["tables"]]} for f in file_views]
    board_groups = [{"group": g, "boards": [{"name": lb, "color": _shade(BOARD_COLOR, i, len(tabs) + 6)}
                                            for i, (_, lb) in enumerate(tabs)]} for g, tabs in BOARD_MENU]
    need_reader = {n: f"read.{need.reader_name}" for n, need in catalog.NEEDS.items()}
    table_reads = {}
    for key, label in labels.items():
        for t, needs in (reads.get(key) or {}).get("tables", {}).items():
            table_reads[f"{t}|{label}"] = list(needs)
    return {"sourceGroups": groups, "steps": steps, "files": file_rows, "boardGroups": board_groups,
            "edges": {"srcStep": src_step, "stepTab": step_tab, "tabBoard": tab_board, "fileBoard": file_board},
            "boardInfo": board_info, "tableReads": table_reads, "needReader": need_reader}


# ── The whole board ───────────────────────────────────────────────────────────────────────────────────────────

def build(nd: dict, *, applied: dict | None = None, ibkr: dict | None = None, now: datetime | None = None,
          datasets: dict | None = None) -> dict:
    """The page's model from ``newdata.collect``'s output. ``applied`` is the saved universe
    (``board_settings.values("module_settings")["values"]``), ``ibkr`` the gateway's login state, ``datasets`` the
    builder's datasets (default: ``newdata._datasets()``)."""
    from RevenueChaser.module_settings import newdata
    now = now or datetime.now(timezone.utc)
    if nd.get("error"):
        return {"error": nd["error"], "read_at": _local(now).strftime("%H:%M")}
    datasets = datasets if datasets is not None else newdata._datasets()
    uni = universe(nd, applied)
    provider_rows = _providers(nd)
    reads = readers()
    file_views = files(nd, datasets, uni, reads, now)
    return {
        "error": None,
        "read_at": _local(now).strftime("%H:%M"), "read_at_iso": now.astimezone(timezone.utc).isoformat(),
        "health": health(nd, provider_rows, uni, ibkr),
        "universe": uni, "scope": scope(uni), "files": file_views, "providers": provider_rows,
        "flow": flow(file_views, datasets, reads, uni), "calendars": calendars(nd, uni, provider_rows),
        "cross_check": (nd.get("cross_check") or {}).get("fields") or [],
        "queue": nd.get("queue") or {"active": [], "finished": []},
        "fill_enabled": screen.fill_enabled(), "fill_off_why": screen.FILL_OFF_WHY,
        "datasets_total": len(nd.get("datasets") or []),
    }

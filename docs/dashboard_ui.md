# Dashboard UI — filters and ticker pickers

Source of truth for **equity-universe boards**. Filter widgets are implemented
in [`core/reporting_common.py`](../core/reporting_common.py). Sector names (UW's
11 Morningstar names) come only from [`core/gics_map.py`](../core/gics_map.py), and the
industry is the provider text the identity master stored. Coarse **themes** come from
the pinned NMF run (`relatedness_themes` where `level='coarse'`), not from a
hard-coded theme list or hand `ROTATION_ETFS` names. Do not fork any of these.

Parent chrome (Regime, last-run) stays in `templates/dashboard.html`. A tab
report is title + subtitle + `Last sync: <friendly date> EOD`.

---

## Chrome order (required)

Top of every equity board, **this order**:

1. **Universal filters** — the shared filter bar (§2b): Index, Market Cap,
   Sector cascade, **Theme cascade** (coarse → medium → fine), Earnings.
2. **Ticker combobox** (only on boards that pick a focal name). Always visible.

Do not put the ticker picker above the filters. Do not put the ticker
combobox inside the filter bar. Board knobs (Co-movement Horizon / Remove
market, OIPD expiry, Rotation group buttons) stay outside the panel.

Theme is **inside** the filter bar, as a cascade next to Sector — not a
third sector level and not an always-on dropdown above the ticker.

Co-movement, Value Chain, Relatedness, **and Implied Probability (OIPD)** use
this order. Sector Rotation stays ETF RRG (Theme focuses dots; no MC/Earnings
stock filters).

Above all of it, in the shell and not in a board report: one collapsed
**Open issues** group (`templates/dashboard.html`, see "Open issues — every
dashboard" below). It is parent chrome alongside the Regime and last-run badges
— a board's own HTML never contains it, so it cannot go stale in a cached
artifact.

---

## 1. Ticker combobox

Any control that **selects a ticker** is a searchable combobox, not a native
`<select>`.

- One type-in field; the listbox opens on focus / input.
- Query matches **ticker and listed name** (case-insensitive).
- Rank (ticker always beats name):
  1. exact ticker
  2. ticker prefix
  3. ticker contains
  4. name prefix
  5. name contains
- Closed state: `TICKER — full name`.
- Empty query: filtered universe (cap the painted list ~100; typing narrows).
- Keyboard: ArrowUp/Down, Enter, Escape.
- Options must already pass the board’s universal filters **and** Theme.
- Opt-in `RCCombobox.mount({..., allowFree: true})` (Implied Probability only):
  when the typed text is ticker-shaped (the server's `_valid_ticker` rule) and not in
  the filtered list (a name only the filters hide counts too), one extra item `Open TYPED (not in the list)` follows the matches, so Enter
  still picks the top match and a name outside the filtered pool can be opened. Off by
  default: other boards keep “No matches”.
- Opt-in `clear: true` wires the × beside the input (`combobox_html`); a board with its own
  handler (Co-movement) leaves it off.

A separate “Search ticker or name” that only filters **cards** does **not**
replace a searchable dropdown.

Shared JS/HTML belongs in `reporting_common`: `combobox_html` + `RCCombobox` (in `FILTERS_JS`). The old
`TICKER_COMBO_*` constants are deleted (C1d); every board's picker uses `combobox_html` + `RCCombobox`.

---

## 2. Universal filters

Required on boards that list the shared equity universe (Co-movement,
Relatedness, Value Chain, **Implied Probability**, Sector Rotation, and any
new classification-style tab). Omit on date-only or non-equity tools
(SPX Gamma, UW Interaction).

| Control | When |
|---|---|
| **Detail level** (Coarse / Medium / Fine) | Only if the board has nested grouping. Co-movement = return cuts. Relatedness = NMF theme depth. |
| **Search ticker or name** + count | Always. `{n} names`, or `{n} names in {k} groups` when grouped. |
| **Market Cap** | Pills + min $M – max $M. Default **All**. |
| **Earnings** | Week pills. |
| **Index** | All / S&P 500 / Dow Jones / S&P SmallCap 600 (once stored) / Nasdaq-listed, from the registry (`filters_meta`). |
| **Sector cascade** | All sectors → All industries. |
| **Theme cascade** | All themes → All mediums → All fines (pinned NMF tree). |

The sector path is UW's two levels, sector → industry (one path per ticker; the
identity master stores it, Yahoo fills where UW has none). Themes are overlapping NMF groups
with **one assigned path per ticker** after cutover (`ticker_themes` at
coarse/medium/fine). Theme is **not** a third sector level — it is a parallel
cascade in the same filter bar.

Use the shared filter kit (§2b) from `reporting_common`; the old `MC_*` / `EWEEK_*` / `INDEX_*` / `SECTOR_*`
constants are deleted (C1d). Copying markup into a module is a bug.

OIPD: no extra “Search ticker or name / {n} names” row — the combobox already
searches. Filters only narrow the combobox; they never slice the fitted PDF.

---

## 2b. Filter kit (as built) + help

The shared filter kit is **`core/static/rc_filters.js` + `rc_filters.css`** (ADR-2), inlined by
[`core/reporting_common.py`](../core/reporting_common.py) as `FILTERS_JS` / `FILTERS_CSS`, so every artifact
stays self-contained. Do **not** fork a second kit in a board `report.py`. Co-movement, Implied Probability, Value
Chain and Relatedness are on it, and Settings uses its combobox (C1b, #383 R2a); Asymmetric Growth, Swing and
Fundamentals joined it (C1c, #383 R2b). LEAPS joined it with Index, Market cap, Sector and Theme (KL, #377, Card E): the kit hides rows in every tier (Watchlist, High, Emerging, Quiet) in the browser, ANDed with the Watchlist label chips (avo 2026-10-16), and a count line says how many names show. A failed filter-field read renders the board with a notice. Earnings Radar joined it the same way (KE, #372, Card E), without an Earnings facet: the kit ANDs with the board's search and five chips and hides every row, watchlist rows and the Excluded-by-Filters section included. A count line ("{shown} of {total} names") shows only while a kit filter is on; each section header reads "{shown}/{total}" while a filter is on. A failed filter-field read still renders the board, with ticker and name only and one notice. SEC 13F joined it with Index, Market cap, Sector and Theme (KF, #375, Card E), without an Earnings facet and without a Watchlist: the kit's Sector replaces the old sector chips, the filter rows ride the payload (not the HTML), the row filter is `RCF.pass(FIELDS[ticker])` ANDed with the board's search, and a count line shows only while a kit filter is on. A ticker off the equity spine is `Unclassified` with no market cap, so any cap filter hides it.

The **constants kit** (`MC_*`, `EWEEK_*`, `INDEX_*`, `SECTOR_*`, `TICKER_COMBO_*`) is **deleted** (C1d): every
board is on the kit above, and no board defines its own filter markup.

### API

| Export | Role |
|---|---|
| `filters_bar_html(el_id)` | The `<section class="rcf-bar">` that `RCFilters.mount` fills: Index → Market cap → Sector → Theme → Earnings, each with an ⓘ explanation. A board may omit a facet (`meta.facets`), never reorder. An omitted facet never filters that board and shows no chip there; its shared state is kept for the other boards ("Reset all" clears only the shown facets). |
| `filters_meta(board, **fields)` | The `meta` a board passes to `RCFilters.mount` (K1). The board's own fields (`floor_mc`, `earnings_mode` `"filter"`/`"mark"`, `themes`, `facets`, `explain`, `now`) pass through; `indexes` and `index_labels` come from the index registry (`index_registry.filter_labels`), so the component carries no literal index list. |
| `control_html(key, label, control, title, explain)` | One board-specific control (Horizon, Detail, ...) with its ⓘ explanation beside the label. |
| `combobox_html(input_id, label_id, placeholder)` | The input + listbox for `RCCombobox.mount` (ARIA 1.2 editable combobox). |
| `help_html(body)` + `HELP_CSS` | Shared **What am I looking at?** box. Every tab injects this under the title. |
| `recorded_sessions_html(board)` | Shared **Recorded sessions** block (#383 C7) on the K2 readers (`core/history`): a session board lists its session snapshots (every Fetch of the session counted, the snapshot's rows kept), a run board its runs (quality, rows, params, notes), newest first. Read when the page renders, waiting only briefly for another process that holds `history.duckdb` (`rawstore.brief_wait`); a read never creates the file, and a failed read is shown, not raised. `panel_id` names a second block on one page (Money Flow's, in its panel on the Rotation tab); `read_note` replaces the sentence on when the block is read (Earnings: its page is written before its Fetch is recorded, so the newest Fetch shows up from the next Fetch). Every board that writes history shows it (`core/tests/test_recorded_sessions_c7.py`); Asymmetric Growth keeps its own panel with points and top names (#380). |
| `provider_health_html(health)` | Amber **Data incomplete** banner (Common K3) from the run's provider health `{provider, calls, failed, auth_error, failures[]}`, read from the payload's `provider_health`. Every data tab renders it under the title, above the help box (#383 C4b; `core/tests/test_provider_banner_every_tab.py`); it is empty for a clean run. Recording the health on each board's Fetch comes with the board waves (the `*_result` call-site switches). |

**Row contract** (one row per name, `core/universe.py::filter_fields`): `{ticker, name, index:[label],
market_cap, gics:[sector, industry], theme_ids:[id], earnings_date}`. `theme_ids` holds every level of the
pinned theme run (`core.universe.current_theme_run`, the one lookup); an empty list is unthemed.

**Index list.** The labels are `index_registry.LABELS`: S&P 500, Dow Jones, S&P SmallCap 600 and Nasdaq
Composite (shown as "Nasdaq-listed"). The S&P SmallCap 600 is listed only once `index_constituents` holds its
rows; its first fill is a weekly-step run the user names (`refresh_lists(first_fill=["sp600"])`), never a board.

State lives in localStorage (`rc.filters.v1`, shared by every board unless the user unlinks it) and in the URL
hash. The pure logic is `RCFilters.core`, unit-tested under node (`core/tests/test_rc_filters.py`).

### Board knobs stay outside the bar

- Ticker combobox (focal boards)
- Co-movement Remove market + Horizon + Detail
- OIPD / Financials expiry or statement chrome
- Search+count that filters **cards** (Asymmetric / Swing / Fundamentals), if kept
- The board's own rule row: Asymmetric Growth **Points**; Swing's three method toggles (Minervini, Weinstein,
  Qullamaggie) replace the **Multiplier** pills and Fundamentals' three rule toggles (Rule of 40, Quality, Momentum)
  replace the **Gate** pills, each in its screeners wave (Screeners cards 5 and 3, 2026-10-10). Until then the
  pills stay as they are.

### Who gets the filter bar

| Board | Filter bar | Notes |
|---|---|---|
| Co-movement | yes (on it) | Knobs (Remove market / Horizon / Detail) stay above the bar. |
| Implied Probability | yes (on it, C1b) | No extra search row. Combobox outside (`allowFree`). |
| Value Chain | yes (on it, C1b) | Search + count row above the bar; combobox outside. |
| Relatedness | yes (on it, C1b) | Search + count row above the bar; combobox outside. Theme = the kit's facet over the pinned run's `theme_ids` (the local coarse-label select is gone). |
| Asymmetric Growth Hunter | yes (on it, C1c) | Keep **Points**. |
| Swing Screener | yes (on it, C1c) | Method toggles (card 5); **Multiplier** until then. |
| Fundamental Analysis | yes (on it, C1c) | Rule toggles (card 3); **Gate** until then. |
| Earnings Radar | yes (on it, KE, #372) | Index, Market cap, Sector, Theme (no Earnings facet). ANDs with the board's search and five chips. Every row, including watchlist and Excluded by Filters. |
| SEC 13F | yes (on it, KF, #375) | Index, Market cap, Sector, Theme (no Earnings facet). Replaces the old sector chips. Filter rows ride the payload. ANDs with the board's search. Off-spine tickers are `Unclassified` with no cap. |
| Financials | yes | Filters only pick the name, they do not slice statements. |
| Sector Rotation | **no** stock bar | ETF RRG. Reuse the Theme widgets only to focus mapped ETF dots (`relatedness_theme_etfs` at the selected level). |
| SPX Gamma, UW Interaction | **no** | Date-only / non-equity. |
| Settings / Data health | **no** | Ops inventory, not an equity board; its ticker drill uses the kit's combobox (C1b). Plan: [`data_health.md`](data_health.md). |
| Logs | **no** | The dashboard's own log files (`app/logview.py`), read-only: no Fetch, no Refresh, no data date. |
| Runs | **no** | The run log over `job_runs` (`app/runview.py`), read-only: no Fetch, no Refresh, no data date. One row per job and per scheduled task, newest first, with the trigger as stored (a dash only on rows from before #83; a `running` row the server lost is closed as `lost`, #82). **Problems only** and the name box filter in SQL, not in the page, so the count beneath the table is of the matching runs in the table and not of the page that was read; the line says so when a read hits its limit. |

### The shell on a phone, in light and dark (#184)

Mock: `docs/mocks/shell-phone.html`. Under 900 px (`NARROW_QUERY` in `templates/dashboard.html`) the sidebar is
a drawer behind the top bar's ☰ button (a tab, the scrim or Escape closes it), the tab's name shows in the top
bar, and the badges and sync times wrap onto a line of their own. Every board frame is then exactly as wide as
its pane (`fitFrameWidth` never grows it past the pane), so the page never scrolls sideways; a board wider than
a phone scrolls inside its own frame until its own PR restacks it. On a narrow screen or a coarse pointer the
shell's buttons, inputs, nav items and nav headers are 44 px; a desktop mouse keeps 32 / 38 / 40 px.

The palette is `core/reporting_common.THEME_TOKENS`, rendered once as `THEME_CSS`. **Dark is the default on every
device** (avo, 25 Oct 2026; the shell's look before #184, value for value): a first visit, and a browser whose
storage is blocked, show dark. The "Theme:" button at the foot of the sidebar (inside the drawer on a phone)
cycles Dark → Light → Auto, where Auto follows the device's `prefers-color-scheme`. Light and Auto are stored in
`localStorage["rc-theme"]` (`THEME_STORAGE_KEY`; dark, the default, is stored as nothing; every access in
try/catch, so a private window still toggles, it just does not remember). `THEME_JS` runs in `<head>` so a light
choice never flashes dark. The shell copies the choice onto
each board iframe as `data-theme` on its `<html>`; a board follows it once it uses the shared tokens (one PR
per board); the Runs tab does with the #131 pull request.

### What am I looking at? — every dashboard

Shared `help_html(body)` under the title, closed by default, same `.help`
chrome. Every data tab has it (#383 C3): every tab but Logs and
Runs (neither is a data board), 15 boards. `core/tests/test_help_every_tab.py` holds
that: each tab renders the one shared box, no board-local `.help` fork, and a body
that says what Fetch and Refresh do there. `HELP_CSS` falls back to the standard
dark tokens, so a board without them (Earnings Radar, SPX Gamma, LEAPS) still
gets the same box. Body copy is per-board (one short paragraph:
what the tab shows, what Fetch vs Refresh does). Do not rename existing
“Methodology / Rules” blocks — those stay as deeper docs; help is the
one-screen intro.

### Open issues — every dashboard

A collapsed group in the shell, directly under the top bar and above the board
iframe, says what is still open on the board you are looking at:

```
▸ Open issues — N on this board, M shared · as of 22 Oct 2026
```

One click opens it. Inside are up to three sub-sections — **This board (N)**,
**Affects every board (M)**, **Repository-wide (R)** — and each item is a nested
`<details>`: the title, `kind`, a repo chip only when the issue is not in
`RevenueChaser-public`, and (when not `open`) a status chip — `parked` and
`deferred` carry their `deferred_until` date; its one-sentence summary, visible
as soon as the group is open; and,
when the item is opened, the labelled fields `What is wrong`, `Evidence`,
`Example`, `What was decided`, `Direction`, `Next step`, and last a `Links` row
(the issue first, then any reference) that opens on GitHub in a new tab.

Those fields carry a little markdown, and the panel renders a fixed subset of it:
inline `` `code` ``, `**bold**`, `- ` bullets and a fenced block. Everything else,
links included, stays text — the escaper runs first and the subset works on its
output, so a field can style itself but never paint an element of its own. The
sync keeps a clipped field's last `` ` `` span whole for the same reason.

The three scopes are what keep a board's own problems at the top:

- **board** — the issue names this board (`boards` in the registry);
- **shared** — it affects every data board, so every board lists it;
- **repo** — repository-wide, listed on **Settings / Data health** only.

`Logs` and `Runs` are not data boards, so neither shows a group.

**Where the data comes from.** One checked-in file, `docs/open_issues.json`,
read by `core/open_issues.py`; `reporting_common.open_issues_payload` turns it
into one JSON block for the shell, which re-renders it in `showTab` (no page
load, no board re-render). The block ships **each entry once**, under `entries`
keyed `"<repo>#<number>"`, and a board keeps only its section keys
(`board` / `shared` / `repo`) plus `generated_at`, `stale_days` and
`dropped_links` (`open_issues.panel_payload`): the shared entries would otherwise
be repeated on every data board (16 when measured, 15 now), which took the block to ~590 KB; the deduped block
is ~107 KB for a ~111 KB registry. **Nothing calls GitHub and nothing reads the store at
render time**, so the group is current on every page load. Every string and link
is escaped in the shell (`html_safe.script_json` for the block, `rcEsc` for the
rendering), and only links under `https://github.com/alsanvolkan/` are shown.

**Keeping it current** is a registry job, not a board job:
`python scripts/open_issues_sync.py --write` reads the open issues of the public
repo from the GitHub API, keeps the curated fields, refreshes title/labels/updated-at,
adds a new issue (pre-filled from its body) and drops a closed one. Run it after
each merge round. A newly added issue arrives `scope="board"` with no `boards`,
which the registry tests reject on purpose: the run is not finished until a human
has named its boards and filled its fields. `--check` fails when the file and GitHub disagree;
`--report-thin` lists the entries whose summary or detail is too thin to resume
from, and every board-scoped entry that names no board, the state `--write` leaves a new issue in (#120). A bad or missing registry renders a message inside the group — never a
failed page.

Do not add the group to a board's `report.py`: a board's HTML is a cached
artifact (`reporting_common.write_board`), so the list would freeze until the
board was re-rendered, and Earnings Radar can only be re-rendered by a Fetch.

### Not this design

- Ticker inside the filter bar.
- Treating Theme as a third sector select, or falling back to a hard-coded theme list.
- A literal index list in a board or in the component: the registry owns it (`filters_meta`).
- A Linear/Vercel visual clone — keep RevenueChaser tokens
  (`--bg #05111F`, `--accent #0EA5E9`, `--neon`).

---

## 3. Theme cascade (coarse → medium → fine)

Same widget shape as the sector cascade, **inside** the filter bar:

```
Theme   [ All themes        ▾ ]  [ All mediums     ▾ ]  [ All fines      ▾ ]
```

- Options come from the **pinned** NMF run (`relatedness_themes` +
  `ticker_themes`), never a hard-coded theme list.
- Each ticker has one path after cutover: coarse parent of its medium,
  medium parent of its fine. Picking a coarse narrows mediums to its
  children (unique `theme_medium` among rows with that coarse); picking a
  medium narrows fines the same way. **All** at a level means “don’t
  constrain that level.”
- Filtering **intersects** MC / earnings / index / sector (AND) via `themeOk`.
- Empty catalog (no pin, no `theme_coarse` on `DATA`) → hide the Theme row.
  Do not fall back to a hard-coded theme list (the keyword taxonomy left in #383 C21).
- Duplicate labels stay uniqued (`label (n)`), already done in
  `unique_theme_labels`.
- Closed filter summary shows the finest selected label (fine, else medium,
  else coarse).

Co-movement **Detail** (Coarse/Medium/Fine cluster cuts) is **not** this
control. Do not merge them.

Sector Rotation still plots ETFs. The cascade focuses dots using
`relatedness_theme_etfs` for the selected node (coarse, medium, or fine).
Unmapped → hide extra dots; Theme=All → today’s full map.

---

## 4. Sector Rotation — keep ETF behaviour

Rotation **does not become a stock screener**. The RRG still plots
`ROTATION_ETFS` (11 sector SPDRs, named like the Morningstar sectors + thematic ETFs + region/bond/macro).

Existing expand rules stay:

- **Sector ETF** (XLK, XLE, …) → sector → industry tree from `equity_universe`
  (cap-weighted, no network).
- **Theme ETF** (SMH, XBI, KRE, …) → that ETF’s top-10 holdings + SPY-relative
  strength (`etf_holdings`).

**New:** the coarse-theme dropdown **selects which ETF(s) on the map are in
focus** (highlight / filter RRG points to the mapped tickers). It does not
replace the 11 sector dots with NMF clusters.

### Coarse theme → ETF map

Prefer `relatedness_theme_etfs` (holdings Jaccard) plus a hand overlay for
gaps. Primary ETF is the one the RRG already knows when possible.

| Coarse (intent, after relabel) | Primary ETF | Also on map | Notes |
|---|---|---|---|
| Regional / community banks | **KRE** | XLF | KRE already in `ROTATION_ETFS` |
| Financial services (ex-banks) | **XLF** | | sector SPDR |
| Technology / software | **XLK** | IGV, SKYY, WCLD, AIQ | theme ETFs already on map |
| Semiconductors | **SMH** | SOXX, XLK | |
| Biotech / healthcare | **XBI** | IBB, XLV | |
| Energy / E&P / oil | **XLE** | XOP, USO | |
| Gold / miners | **GLD** | SLV, XME | |
| Aerospace & defense | **ITA** | XLI | |
| Industrials / infra | **XLI** | PAVE, AIRR, ROBO | |
| Airlines | **JETS** | | |
| Utilities | **XLU** | | |
| Real estate / REITs | **XLRE** | | |
| Consumer cyclical | **XLY** | | |
| Consumer defensive | **XLP** | | |
| Communication | **XLC** | | |
| Shipping / maritime | **SEA** | — | U.S. Global Sea to Sky Cargo (equity). **Not BDRY/BWET** (freight *futures*). Add SEA to `ROTATION_ETFS` `group=theme` only if we want it on the RRG; otherwise map the dropdown to SEA holdings off-map. |
| BDCs / specialty finance | **BIZD** | — | VanEck BDC Income. Same: add to map or holdings-only. |

Do **not** add commodity-pool tickers (BDRY, BWET, USO already special-cased as
oil) as “theme” equity. Do not drop existing theme ETFs from the RRG when a
coarse theme is unselected — default Theme=All shows the full map as today.

Live Jaccard (current pin) already points: IT→XLK/IGV/SKYY, industrials→XLI/ITA/PAVE,
industrial-tech→AIRR, REITs→XLRE, mega-bucket→XLF. Treat that table as a
starting overlay, not the menu labels.

---

## 5. Implied Probability (OIPD)

Same chrome as the other equity boards. Filters do **not** slice the fitted
PDF — they only narrow the ticker combobox. No Detail level (one chain, no
nested grouping). Count is `{n} names`.

- Filters → Theme → **one** combobox. Closed label `TICKER — full name`.
- Retire the parent-header `#oipd-ticker` text box. Fetch still `POST /run/module_oipd`
  with that ticker; the parent reads the iframe’s selected value (do not keep
  a second free-text field in `templates/dashboard.html`).
- The pool is the classification universe (operating companies, `core/universe.py`).
  A ticker outside it (SPY, a fund) is typed into the combobox and opened anyway
  (`allowFree`); Refresh and Fetch accept any valid ticker.
- Default focal: last stored `oipd_meta` ticker, else a name that is actually
  in the filtered universe (not a hard-coded `SPY` if filters exclude it).
- Refresh stays store-only (`run(no_fetch=True)`); Fetch is the only UW call.

---

## 6. Relatedness-specific layout (planned)

- Filters first, then Theme, then **one** focal combobox. No Company B / A-vs-B.
- Panel **Most related to** `{TICKER} — {name}`.
- Each peer: ticker + full name; sector → industry; theme path coarse→medium→fine.
  Color: sector gutter (11 Morningstar names) + three theme hues.

---

## 7. Review / new-board checklist

- [ ] Chrome order: filter bar (incl. Theme cascade) → ticker combobox
- [ ] Ticker pickers are comboboxes with ticker-first ranking
- [ ] Equity boards use `reporting_common` filters, default MC All
- [ ] Theme cascade is pinned NMF coarse → medium → fine inside the filter bar
- [ ] Sector Rotation still plots ETFs; theme pick maps to KRE/SMH/… as above
- [ ] Implied Probability uses in-board filters + combobox; no `#oipd-ticker` header box
- [ ] Universal filters use the shared filter kit (`rc_filters`, `filters_bar_html` + `filters_meta`, `docs/dashboard_ui.md` §2b); no literal index list
- [ ] Count string matches the grouping
- [ ] Open issues group present, with the right counts for this board
- [ ] Registry refreshed (`scripts/open_issues_sync.py --write`) after the merge round
- [ ] Unit tests assert chrome; e2e if fetch/refresh/routes change
- [x] Earnings Radar v2 and ER v2 Calibration are gone (no tab, route, job, package)

---

## 8. Relatedness × Co-movement (investigate, then join — do not merge boards)

Two different signals. Do **not** collapse the tabs.

| | Relatedness | Co-movement |
|---|---|---|
| Question | Same *business*? | Same *price path*? |
| Input | 10-K / profile tags → cosine | daily returns → Pearson + hierarchy |
| Store | `relatedness_scores` (top peers) | `comov_members` / `comov_nodes`; prices in `swing_ohlc` |
| Pairwise number | cosine + percentile | **not stored** today (only cluster mean_corr) |

**Co-movement mechanism (as coded today):**

- Bars stay **daily close** (`interval="1d"`). We do **not** download 3d/5d/10d bars.
- Lookback: last **252 trading sessions** of prices. Fetch cache is 2y; clustering uses the tail.
- Pair score: Pearson of **1-day residual** log returns vs SPY by default (default horizon is 1d).
- Detail Coarse / Medium / Fine = **how many clusters** (20 / 80 / 250). Same corr matrix at every cut.

---

## 8b. Co-movement redesign — residual groups + horizon (planned)

This is a **new board**, not a pill on the old one. Goal: find names that move
together **more than the market**, up or down.

### Engine

1. Daily Yahoo OHLC (`interval=1d`), 2y cache. Fetch must include **SPY, DIA,
   QQQ** even if they are not in the equity universe. Today: SPY 500 bars, **DIA
   0, QQQ 0** — Fetch has to pull those.
2. Horizon n ∈ {1,3,5,10}: overlapping `log(P_t / P_{t-n})`, last **252**
   observations. Not 3d Yahoo bars. Not non-overlapping 10d (~25 points).
3. **Remove market** = one benchmark, never three at once (SPY/DIA/QQQ are
   collinear). OLS: `r_i = a + b r_m + e_i`. Cluster **Pearson of residuals
   e**. Off = today’s raw corr (beta stays in the groups).
4. Ward linkage on √(2(1−ρ)), same cuts (20 / 80 / 250) — average linkage
   chained a quarter to half of the universe into one catch-all (review
   2026-10-01). Persist `comov_runs` keyed by `(horizon, benchmark)`. Switching
   either control reclusters; Refresh is store-only once OHLC exists; Fetch is
   Yahoo for missing bars.
4b. **Significance** = a group's mean residual r beats the q99 of the groups the
   *same pipeline selects* from column-shuffled (pure-noise) returns
   (`noise.selected_group_null`, `NULL_PERMS` re-clusterings, Fetch only, stored
   as `comov_runs.null_json`; a Refresh reuses the latest null with the same
   config hash, else groups stay unjudged and visible). The old random-names
   baseline passed 100% of pure-noise groups. The subtitle's count is
   `n_factors`: eigenvalues above the Marchenko–Pastur edge (≈20 on the live
   universe), replacing the inverted "≈N independent bets".
4c. **Out-of-sample persistence** (`persistence.py`, Fetch-only, `comov_lag`):
   groups are also formed on the window ending `OOS_HOLDOUT` = 63 sessions ago
   and tested on that quarter only (mean r vs random names in the held-out
   matrix — valid there, the quarter chose nothing). Each current group is
   matched to its best lagged group by Jaccard: ≥ `PERSIST_MIN_JACCARD` (0.5)
   → badge "held / did not hold out-of-sample · N% same names as a quarter
   ago", else "new this quarter". A line under the note gives, for the current
   Detail, how many lagged groups held (live data: 19/20, 79/80, 220/250).
   A Refresh re-matches against the latest `comov_lag` with the same config
   hash; with none, no verdict is shown. On a persisting card, names that
   were not in the matched group a quarter ago are dimmed ("joined this
   quarter"). The S9 block-bootstrap "stable %" badge was removed
   (2026-09-30): it measured resampling noise inside one window and cost ~20
   clusterings per Fetch.
5. Default: **Remove market = S&P 500 (SPY)**, **horizon = 1d**.
6. Nasdaq pill = **QQQ** (Nasdaq-100), not Composite.
7. Changing Horizon or Remove market = **silent store-only Refresh in the iframe** (board spinner, no parent job badge). Fetch is only for missing Yahoo bars (DIA/QQQ/universe OHLC).

### UX (Explore surface — knobs, then cards)

```
Remove market   Off | S&P 500* (SPY) | Dow (DIA) | Nasdaq (QQQ)
Horizon         1d | 3d | 5d* | 10d
Detail          Coarse | Medium | Fine
Search …        {n} names in {k} groups
then MC · Earnings · Index · Sector · Theme
```

- Subtitle states residual vs which ETF and which n.
- **Sort cards by residual tightness, not size.** Size-sort put the beta blob
  first; that is the old bug.
- Card: label, n, bar of residual r, `together beyond SPY · 0.72 residual r (5d)`,
  then **direction of the last n days** (`▼ last 5d −3.4% vs SPY · same
  direction`). Green/red is not good/bad — it is together-up / together-down
  versus the benchmark.
- Off: keep the beta-group warning. On: hide the leftover blob (mean residual r
  ~0.10); do not show a 184-name “market” card.
- Do not reuse STRONG r>0.60 across n.

Mockup: `hermes/cache/scratch/comovement-redesign.html`.

### Relatedness peer row

Same as before, plus residual at the selected n:

```
move  1d 0.67   3d 0.74   5d 0.76   10d 0.75     beyond SPY 0.72 (5d)
cluster  ● Coarse  ● Medium  ● Fine               at (n, benchmark)
```

### Window control and editable inputs (issue #40)

- **Window** sits in the board settings row beside Remove market / Horizon: **1m · 2m
  · 6m · 9m · 1y · 1.5y** (21 / 42 / 126 / 189 / 252 / 378 sessions, shown in the
  tooltip). A click reloads `?refresh=1&window=…&horizon=…&benchmark=…`, a store-only
  recompute (no network). Horizon and Remove market carry the current window, so changing
  one control never resets another.
- **Precomputed windows (#95):** after a Fetch writes the board, the 6m and 1y windows
  (`config.PRECOMPUTE_WINDOWS`) are computed and stored as runs too (a window whose
  stored run is still current is skipped; a failure is logged and never fails the Fetch).
  A store-only switch serves the latest stored run with the same `config_hash`, the same
  `prices_through`, the same clustered names and no newer price write, instead of
  re-clustering; anything else recomputes as before. A served run keeps the labels it was
  computed with until the next Fetch; the fetch report, drop lists and funnel are live.
- **Under 6m** the Horizon buttons other than 1d are disabled with a visible reason, a
  "short window: noisy, exploratory" warning is always shown, and the out-of-sample
  persistence line is replaced by `payload.oos_note`.
- **Every number the board prints** (window, MIN_OBS, holdout) comes from
  `payload.settings`, never from `config`.
- **"Custom settings" badge + Reset to defaults** appear in the board settings row (not
  the top bar, section 6) only when a Settings override exists. Reset POSTs
  `{reset: true}` (header `X-RC-Panel: board-settings`) to `/api/settings/board/module_comovement` and polls the job with the
  shared `settingsJobVerdict` (`reporting_common.JOB_JS`).
- **Settings → Board inputs → Co-movement** edits the default window, `MIN_OBS` and the
  OOS holdout, with `WINSOR_K`, `MIN_PERIODS` and benchmark lag under a collapsed
  "Fine tuning". Each field shows its allowed range; the server's 400 messages appear
  next to the fields without a reload; a 202 shows Queued / Running and reloads when the
  job is done.

### Not this design

- Multi-factor SPY+DIA+QQQ in one regression.
- Hourly or weekly Yahoo interval.
- Merging Co-movement into Relatedness.
- A third “combined” tab.

---

**Worked example (pair residuals, 252 overlapping obs through 2026-08-31):**

| n | DELL–HPE | DELL–IBM | DELL–SPY | HPE residual vs SPY | IBM residual vs SPY |
|---|---|---|---|---|---|
| 1d | 0.67 | 0.18 | 0.40 | **0.59** | 0.10 |
| 3d | 0.74 | 0.39 | 0.42 | 0.69 | 0.30 |
| 5d | **0.76** | 0.43 | 0.43 | **0.72** | 0.35 |
| 10d | 0.75 | 0.44 | 0.31 | 0.72 | 0.39 |

HPE stays with DELL at every n, including after removing SPY. IBM’s 1d 0.18 is mostly market (residual 0.10).

---

## 9. Implementation order (planned — not started)

1. **Delete Earnings Radar v2 and ER v2 Calibration.** Remove both tabs, jobs, `/view` and `/run` routes, `module_earnings_radar_v2/` (package, tests, calibration logs, data), e2e `test_board_earnings_radar_v2.py`, provenance, runners, cache paths, `.gitignore` entry, and docs (README, `docs/rule_calibration.md` if it only covers v2, data architecture/retrieval rows). Leave **Earnings Radar** (v1) untouched.
2. Relabel / rebalance coarse NMF themes so the Theme dropdown is usable (current pin has one ~2579-name coarse).
3. Shared `reporting_common` combobox + filter chrome.
4. **Co-movement redesign:** Fetch SPY/DIA/QQQ; Remove market + Horizon 1/3/5/10d; cluster residuals; sort by tightness; new card UX (§8b).
5. Value Chain: filters → Theme → ticker combobox.
6. Relatedness: same chrome; one “Most related to” panel; peer row = cosine + **1/3/5/10 move + beyond SPY + cluster chips at (n, benchmark)**.
7. Implied Probability: same chrome; retire `#oipd-ticker`.
8. Sector Rotation: Theme pick maps to existing ETFs; RRG behaviour unchanged.

/* RevenueChaser shared filter bar (ADR-2, plan 2026-09-30_plan_filters_ux.md).
 *
 * One component for every board: Index -> Market cap -> Sector (2 levels: sector, industry)
 * -> Theme (10-K, 3 levels) -> Earnings. Order is fixed; a board may omit a
 * facet, never reorder; an omitted facet never filters that board (its shared
 * state is kept for the others). State lives in localStorage ("rc.filters.v1", shared by
 * every board unless the user unlinks it) and in the URL hash, so it survives
 * reloads (Co-movement's Horizon / Remove market) and syncs live across the
 * same-origin board iframes.
 *
 * Row contract (core/universe.py::filter_fields):
 *   {ticker, name, index:[label], market_cap, gics:[sector, industry],
 *    theme_ids:[id], earnings_date (Unix s | ISO | null)}
 * meta (core/reporting_common.py::filters_meta): {board, floor_mc, earnings_mode:"filter"|"mark",
 *        themes:{id:{label,level,parent}}, indexes:[label], index_labels:{label: short text},
 *        now (Unix s, optional), facets (optional subset), explain:{facet: html} (optional)}
 *
 * No globals besides RCFilters / RCInfo / RCRadios / RCCombobox, and it escapes with
 * ``rcEsc`` (core/html_safe.ESC_JS), which reporting_common.FILTERS_JS prepends. Pure logic is
 * exposed as RCFilters.core so it can be unit-tested without a DOM.
 */
(function (root) {
  "use strict";

  var LS_KEY = "rc.filters.v1", LS_LINK = "rc.filters.linked";
  var FACETS = ["index", "mc", "gics", "theme", "earnings"];
  var MC_BUCKETS = [
    {key: "micro", label: "Micro", range: "< $300M", min: 0, max: 300e6},
    {key: "small", label: "Small", range: "$300M – 2B", min: 300e6, max: 2e9},
    {key: "mid", label: "Mid", range: "$2B – 10B", min: 2e9, max: 10e9},
    {key: "large", label: "Large", range: "$10B – 200B", min: 10e9, max: 200e9},
    {key: "mega", label: "Mega", range: "> $200B", min: 200e9, max: Infinity}];
  var MC_PRESETS = [{label: "$50M–$5B", min: 50e6, max: 5e9}];
  var GICS_LEVELS = ["sector", "industry"];
  var UNCLASSIFIED = "Unclassified";   // the sector unknown bucket (core/gics_map.UNCLASSIFIED)
  var THEME_LEVELS = ["coarse", "medium", "fine"];
  var EARN_KEYS = ["any", "0", "1", "2", "3", "4", "unknown"];

  function defaults() {
    return {v: 1, index: "all", mc: {b: [], min: null, max: null}, gics: null,
            theme: null, earn: "any", earnFilter: false};
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function fmt(n) { return Number(n).toLocaleString("en-US"); }
  function fmtCap(v) {
    if (v == null) return "n/a";
    if (v >= 1e12) return "$" + (v / 1e12).toFixed(2) + "T";
    if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
    return "$" + Math.round(v / 1e6) + "M";
  }

  /* ── dates / earnings weeks ─────────────────────────────────────────────── */
  function toDate(v) {
    if (v == null || v === "") return null;
    if (v instanceof Date) return v;
    if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
    var d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  function monday(d) {
    var x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
    return x;
  }
  function weekDiff(d, now) {
    if (!d) return null;
    return Math.round((monday(d) - monday(now)) / (7 * 86400000));
  }
  function earnKey(row, now) {
    var w = weekDiff(toDate(row.earnings_date), now);
    if (w === null) return "unknown";
    if (w < 0) return "past";
    return w >= 4 ? "4" : String(w);
  }
  // ISO-8601 week number: wraps at 52/53 (no "W54" at year end).
  function isoWeek(d) {
    var t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    var day = t.getUTCDay() || 7;
    t.setUTCDate(t.getUTCDate() + 4 - day);
    var y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return Math.ceil((((t - y0) / 86400000) + 1) / 7);
  }
  function weekLabel(k, now) {
    if (k === "any") return "Any date";
    if (k === "unknown") return "Date unknown";
    var d = new Date(now); d.setDate(d.getDate() + 7 * (+k));
    var m = monday(d);
    var md = m.toLocaleDateString("en-US", {month: "short", day: "numeric"});
    var head = k === "0" ? "This week" : k === "4" ? "In 4+ weeks" : "In " + k + " wk";
    return head + " · W" + isoWeek(d) + (k === "4" ? "+" : "") + " (from " + md + ")";
  }

  /* ── tree keys (sector path, themes by id) ─────────────────────────────── */
  function gicsKeys(row) {
    var p = row.gics || [row.sector || UNCLASSIFIED, row.industry || ""];
    var out = [], acc = [];
    for (var i = 0; i < p.length; i++) {
      if (!p[i]) break;
      acc.push(p[i]);
      out.push(acc.join("›"));
    }
    return out.length ? out : [UNCLASSIFIED];
  }
  // Order for a tree level: the sector unknown bucket ("Unclassified", and the legacy "Other") and the
  // theme catch-all ("Other") sort after every real label; everything else alphabetically.
  function labelCmp(a, b) {
    var ao = /^(other|unclassified)/i.test(a), bo = /^(other|unclassified)/i.test(b);
    if (ao !== bo) return ao ? 1 : -1;
    return a.localeCompare(b);
  }
  // A filter saved before R3 (#86) keyed the sector unknown bucket "Other"; it is "Unclassified" now.
  function legacyGicsKey(key) {
    return String(key).split("›").map(function (seg) { return seg === "Other" ? UNCLASSIFIED : seg; }).join("›");
  }
  function themeKeys(row) { return row.theme_ids || []; }

  /* ── predicates ────────────────────────────────────────────────────────── */
  function makeCore(meta) {
    meta = meta || {};
    var now = meta.now ? toDate(meta.now) : new Date();
    var mode = meta.earnings_mode === "mark" ? "mark" : "filter";
    var pred = {
      index: function (r, F) { return F.index === "all" || (r.index || []).indexOf(F.index) >= 0; },
      mc: function (r, F) {
        var c = r.market_cap, m = F.mc || {};
        if (m.min != null || m.max != null) {
          if (c == null) return false;
          if (m.min != null && c < m.min) return false;
          if (m.max != null && c >= m.max) return false;
        }
        if (m.b && m.b.length) {
          return c != null && MC_BUCKETS.some(function (b) {
            return m.b.indexOf(b.key) >= 0 && c >= b.min && c < b.max;
          });
        }
        return true;
      },
      gics: function (r, F) { return !F.gics || gicsKeys(r).indexOf(F.gics.key) >= 0; },
      theme: function (r, F) {
        if (!F.theme) return true;
        if (F.theme.key === "__none__") return themeKeys(r).length === 0;
        return themeKeys(r).indexOf(F.theme.key) >= 0;
      },
      earnings: function (r, F) {
        if (F.earn === "any") return true;
        if (mode === "mark" && !F.earnFilter) return true;   // marks, never removes
        return earnKey(r, now) === F.earn;
      }
    };
    // A facet the board omits (meta.facets) never filters here: its state is shared across boards and kept for
    // them, but this page shows no control for it (KL review, #377: an Earnings week from Co-movement hid LEAPS rows).
    function on(k) { return !meta.facets || meta.facets.indexOf(k) >= 0; }
    function pass(r, F, skip) {
      for (var k in pred) { if (k !== skip && on(k) && !pred[k](r, F)) return false; }
      return true;
    }
    function mark(r, F) { return on("earnings") && F.earn !== "any" && earnKey(r, now) === F.earn; }
    function active(F) {
      return (on("index") && F.index !== "all") ||
        (on("mc") && ((F.mc.b || []).length || F.mc.min != null || F.mc.max != null)) ||
        (on("gics") && !!F.gics) || (on("theme") && !!F.theme) ||
        (on("earnings") && F.earn !== "any" && (mode === "filter" || F.earnFilter));
    }
    function uniq(rows) {
      var s = {};
      rows.forEach(function (r) { s[r.ticker] = 1; });
      return Object.keys(s).length;
    }
    return {pass: pass, mark: mark, active: active, uniq: uniq, now: now, mode: mode, on: on,
            earnKey: function (r) { return earnKey(r, now); }};
  }

  // Loaded state is data from another board or an older version: keep only what
  // still makes sense here (Postel), and say what was dropped.
  function sanitize(s, meta, rows) {
    var F = defaults(), notes = [];
    if (!s || typeof s !== "object") return {state: F, notes: notes};
    if (typeof s.index === "string") F.index = s.index;
    if (s.mc && typeof s.mc === "object") {
      F.mc.b = (s.mc.b || []).filter(function (k) { return MC_BUCKETS.some(function (b) { return b.key === k; }); });
      F.mc.min = typeof s.mc.min === "number" ? s.mc.min : null;
      F.mc.max = typeof s.mc.max === "number" ? s.mc.max : null;
    }
    if (s.gics && s.gics.key) {
      var gk = legacyGicsKey(s.gics.key), gl = gk !== s.gics.key ? gk.split("›").pop() : s.gics.label;
      var ok = (rows || []).some(function (r) { return gicsKeys(r).indexOf(gk) >= 0; });
      if (ok) F.gics = {key: gk, label: gl, level: s.gics.level};
      else notes.push("Saved sector “" + (s.gics.label || s.gics.key) + "” has no names on this board, so it was cleared.");
    }
    if (s.theme && s.theme.key) {
      if (s.theme.key === "__none__" || (meta.themes || {})[s.theme.key]) F.theme = {key: s.theme.key, label: s.theme.label, level: s.theme.level};
      else notes.push("Saved theme “" + (s.theme.label || s.theme.key) + "” no longer exists, so it was cleared.");
    }
    if (EARN_KEYS.indexOf(s.earn) >= 0) F.earn = s.earn;
    F.earnFilter = !!s.earnFilter;
    return {state: F, notes: notes};
  }

  /* ── one explanation popover open at a time (RCInfo) ───────────────────── */
  var OPEN = null, wired = false;
  function closePop(restore) {
    if (!OPEN) return;
    var p = OPEN; OPEN = null;
    p.pop.hidden = true;
    p.btn.setAttribute("aria-expanded", "false");
    if (restore) p.btn.focus();
  }
  function openPop(btn) {
    var pop = document.getElementById(btn.getAttribute("aria-controls"));
    if (!pop) return;
    if (OPEN && OPEN.btn === btn) { closePop(false); return; }
    closePop(false);
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    OPEN = {btn: btn, pop: pop};
    pop.style.left = "";
    var r = pop.getBoundingClientRect();
    if (r.right > window.innerWidth - 12) pop.style.left = (window.innerWidth - 12 - r.right) + "px";
    if (pop.classList.contains("rcf-picker")) {
      var f = pop.querySelector("input,button:not([disabled]),[tabindex='0']");
      if (f) f.focus();
    }
  }
  function wireInfo() {
    if (wired || typeof document === "undefined") return;
    wired = true;
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".rcf-info,.rcf-trigger");
      if (b && b.getAttribute("aria-controls")) { openPop(b); return; }
      // A click that re-rendered part of the popover (a tree toggle) leaves its
      // target detached; that is still a click inside, never "outside".
      if (e.target.isConnected === false) return;
      if (OPEN && !OPEN.pop.contains(e.target)) closePop(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && OPEN) { e.preventDefault(); closePop(true); }
    });
  }

  /* ── radiogroups (APG pattern: roving tabindex, arrows) ────────────────── */
  function wireRadios(group, onPick) {
    function radios() { return Array.prototype.slice.call(group.querySelectorAll("[role=radio]")); }
    function pick(r, focus) {
      radios().forEach(function (x) {
        var on = x === r;
        x.setAttribute("aria-checked", on ? "true" : "false");
        x.tabIndex = on ? 0 : -1;
      });
      if (focus) r.focus();
      if (onPick) onPick(r.getAttribute("data-v"));
    }
    group.addEventListener("click", function (e) {
      var r = e.target.closest("[role=radio]");
      if (!r || r.getAttribute("aria-disabled") === "true") return;
      pick(r);
    });
    group.addEventListener("keydown", function (e) {
      var list = radios().filter(function (x) { return x.getAttribute("aria-disabled") !== "true"; });
      var i = list.indexOf(document.activeElement);
      if (i < 0) return;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); pick(list[(i + 1) % list.length], true); }
      if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); pick(list[(i - 1 + list.length) % list.length], true); }
    });
  }

  /* ── default explanations (a board may override via meta.explain) ─────── */
  function explain(meta) {
    var floor = meta.floor_mc || 0;
    var d = {
      index: "<b>S&amp;P 500</b>, <b>Dow Jones</b> and <b>S&amp;P SmallCap 600</b> are index constituents; <b>Nasdaq-listed</b> means listed on the Nasdaq exchange (not the Nasdaq-100). Source: Unusual Whales (SmallCap 600: the S&amp;P 1500 list), cached in <code>index_constituents</code>; an index shows here once it is stored." +
        "<div class='rcf-now'>Counts are names <b>on this board</b> that pass your other filters, not the size of the index.</div>",
      mc: "Latest market value from <code>equity_universe</code>. Pick one or more size buckets, or type a custom range in $ millions (min ≤ cap &lt; max)." +
        (floor ? "<div class='rcf-now'>This board only covers names <b>≥ " + rcEsc(fmtCap(floor)) + "</b>, so smaller buckets are empty here.</div>" : ""),
      gics: "Two levels: <b>sector → industry</b>, the 11 Morningstar sector names plus an industry text. Every stock has one path, taken from the identity master: the sector comes from the S&amp;P 1500 list for the names on it, then Unusual Whales, then Yahoo, then the SEC industry code; the industry is Unusual Whales' text (Yahoo's where it has none). Pick at either level: picking a sector keeps every industry under it. Names the identity master cannot place show as <b>Unclassified</b>.",
      theme: "Built by <b>Relatedness</b> from each company's 10-K business description, grouped at three levels (<b>coarse → medium → fine</b>). A theme follows what a company says it sells and to whom, which can cut across sectors." +
        "<div class='rcf-now'>Only profiled companies have themes; the rest are listed as <b>Unthemed</b> so they are never silently dropped.</div>",
      earnings: "Next reported earnings date, bucketed by calendar week (Mon–Sun)." +
        (meta.earnings_mode === "mark"
          ? "<div class='rcf-now'>On this board it <b>marks</b> matching names with an amber dot instead of removing them, so a group's size and numbers stay honest. Tick <b>Filter</b> in the picker to remove the others.</div>"
          : "")
    };
    var o = meta.explain || {};
    Object.keys(o).forEach(function (k) { d[k] = o[k]; });
    return d;
  }

  /* ── the bar ───────────────────────────────────────────────────────────── */
  function mount(opt) {
    wireInfo();
    var host = typeof opt.root === "string" ? document.querySelector(opt.root) : opt.root;
    var rows = opt.rows || [], meta = opt.meta || {};
    var facets = FACETS.filter(function (f) { return !meta.facets || meta.facets.indexOf(f) >= 0; });
    var core = makeCore(meta), board = meta.board || "board";
    var linked = true, F = defaults(), notes = [];
    var themes = meta.themes || {};
    var uid = "rcf-" + board.replace(/[^a-z0-9]/gi, "");
    var ex = explain(meta);

    function lsKey() { return linked ? LS_KEY : LS_KEY + "." + board; }
    function save() {
      var s = JSON.stringify(F);
      try { localStorage.setItem(LS_LINK, linked ? "1" : "0"); } catch (e) {}
      try { localStorage.setItem(lsKey(), s); } catch (e) {}
      try { history.replaceState(null, "", location.pathname + location.search + "#f=" + encodeURIComponent(s)); } catch (e) {}
    }
    function load() {
      try { linked = localStorage.getItem(LS_LINK) !== "0"; } catch (e) {}
      var raw = null;
      try { var m = location.hash.match(/#f=(.*)$/); if (m) raw = JSON.parse(decodeURIComponent(m[1])); } catch (e) {}
      if (!raw) { try { var s = localStorage.getItem(lsKey()); if (s) raw = JSON.parse(s); } catch (e) {} }
      var r = sanitize(raw, meta, rows);
      F = r.state; notes = r.notes;
    }

    function info(key, label) {
      return '<button class="rcf-info" type="button" aria-expanded="false" aria-controls="' + uid + '-i-' + key + '" aria-label="About ' + rcEsc(label) + '">i</button>';
    }
    function pop(key, title) {
      return '<div class="rcf-pop" id="' + uid + '-i-' + key + '" role="region" aria-label="' + rcEsc(title) + ' explained" hidden><h4>' + rcEsc(title) + '</h4>' + (ex[key] || "") + '</div>';
    }
    function trigger(key, label, pickerHtml, extraCls) {
      return '<div class="rcf-ctrl"><div class="rcf-lbl"><span id="' + uid + '-l-' + key + '">' + rcEsc(label) + '</span>' + info(key, label) + '</div>' +
        '<button class="rcf-trigger' + (extraCls || "") + '" type="button" id="' + uid + '-t-' + key + '" aria-haspopup="dialog" aria-expanded="false" aria-controls="' + uid + '-p-' + key + '" aria-labelledby="' + uid + '-l-' + key + ' ' + uid + '-v-' + key + '"><span class="rcf-val" id="' + uid + '-v-' + key + '"></span><span class="rcf-caret" aria-hidden="true">▾</span></button>' +
        '<div class="rcf-pop rcf-picker" id="' + uid + '-p-' + key + '" role="dialog" aria-label="' + rcEsc(label) + '" hidden>' + pickerHtml + '</div>' + pop(key, label) + '</div>';
    }

    var html = '<h2 class="rcf-title" id="' + uid + '-title">Filters · shared across boards <span class="rcf-rule"></span></h2><div class="rcf-ctrls">';
    if (facets.indexOf("index") >= 0) {
      html += '<div class="rcf-ctrl"><div class="rcf-lbl"><span id="' + uid + '-l-index">Index</span>' + info("index", "Index") + '</div>' +
        '<div class="rcf-seg" role="radiogroup" aria-labelledby="' + uid + '-l-index" id="' + uid + '-idx"></div>' + pop("index", "Index membership") + '</div>';
    }
    if (facets.indexOf("mc") >= 0) {
      html += trigger("mc", "Market cap",
        '<fieldset class="rcf-checks" id="' + uid + '-mcb"><legend class="rcf-sr">Size buckets</legend></fieldset>' +
        '<div class="rcf-range"><label class="rcf-sr" for="' + uid + '-mcmin">Minimum, $ millions</label><input id="' + uid + '-mcmin" type="number" min="0" step="1" placeholder="min $M" inputmode="numeric">' +
        '<span aria-hidden="true">–</span><label class="rcf-sr" for="' + uid + '-mcmax">Maximum, $ millions</label><input id="' + uid + '-mcmax" type="number" min="0" step="1" placeholder="max $M" inputmode="numeric">' +
        '<button class="rcf-btn rcf-primary" type="button" data-act="mc-apply">Apply</button></div>' +
        '<div class="rcf-foot"><span>Preset: ' + MC_PRESETS.map(function (p, i) { return '<button class="rcf-link" type="button" data-act="mc-preset" data-i="' + i + '">' + rcEsc(p.label) + '</button>'; }).join(" ") + '</span>' +
        '<button class="rcf-link" type="button" data-act="mc-clear">All sizes</button></div>');
    }
    if (facets.indexOf("gics") >= 0) {
      html += trigger("gics", "Sector",
        '<input class="rcf-search" type="search" placeholder="Search sectors and industries" aria-label="Search sectors">' +
        '<ul class="rcf-tree" role="tree" aria-label="Sector"></ul>' +
        '<div class="rcf-foot"><span class="rcf-meta">Sector › industry</span><button class="rcf-link" type="button" data-clear>All sectors</button></div>');
    }
    if (facets.indexOf("theme") >= 0) {
      html += trigger("theme", "Theme (10-K)",
        '<input class="rcf-search" type="search" placeholder="Search themes" aria-label="Search themes">' +
        '<ul class="rcf-tree" role="tree" aria-label="Themes"></ul>' +
        '<div class="rcf-foot"><span class="rcf-meta">Coarse › medium › fine</span><button class="rcf-link" type="button" data-clear>All themes</button></div>');
    }
    if (facets.indexOf("earnings") >= 0) {
      html += trigger("earnings", "Earnings",
        '<fieldset class="rcf-checks" id="' + uid + '-ew"><legend class="rcf-sr">Earnings week</legend></fieldset>' +
        (core.mode === "mark" ? '<label class="rcf-toggle rcf-ewf"><input type="checkbox" data-act="earn-filter"> Filter (remove names outside the week)</label>' : ''),
        " rcf-narrow");
    }
    html += '</div><div class="rcf-notes" role="status"></div><div class="rcf-active" aria-live="polite"></div>';
    host.classList.add("rcf-bar");
    host.setAttribute("aria-labelledby", uid + "-title");
    host.innerHTML = html;

    function $(sel) { return host.querySelector(sel); }
    function base(skip) { return rows.filter(function (r) { return core.pass(r, F, skip); }); }

    /* index */
    var idx = $("#" + uid + "-idx");
    var indexes = meta.indexes || [];          // from the registry: reporting_common.filters_meta (K1)
    var indexLabel = meta.index_labels || {};
    function buildIndex() {
      if (!idx) return;
      var b = base("index");
      idx.innerHTML = [["all", "All"]].concat(indexes.map(function (l) { return [l, indexLabel[l] || l]; })).map(function (d) {
        var n = d[0] === "all" ? core.uniq(b) : core.uniq(b.filter(function (r) { return (r.index || []).indexOf(d[0]) >= 0; }));
        var on = F.index === d[0];
        return '<button role="radio" type="button" data-v="' + rcEsc(d[0]) + '" aria-checked="' + on + '" tabindex="' + (on ? 0 : -1) + '"' +
          (n === 0 && !on ? ' aria-disabled="true"' : '') + '>' + rcEsc(d[1]) + '<span class="rcf-hint">' + fmt(n) + '</span></button>';
      }).join("");
    }
    if (idx) wireRadios(idx, function (v) { F.index = v; changed(); });

    /* market cap */
    function mcText() {
      var parts = F.mc.b.map(function (k) { return MC_BUCKETS.filter(function (b) { return b.key === k; })[0].label; });
      if (F.mc.min != null || F.mc.max != null) parts.push((F.mc.min != null ? fmtCap(F.mc.min) : "$0") + "–" + (F.mc.max != null ? fmtCap(F.mc.max) : "∞"));
      return parts.join(" + ");
    }
    function buildMc() {
      var box = $("#" + uid + "-mcb");
      if (!box) return;
      var b = base("mc"), floor = meta.floor_mc || 0;
      box.innerHTML = '<legend class="rcf-sr">Size buckets</legend>' + MC_BUCKETS.map(function (bk) {
        var n = core.uniq(b.filter(function (r) { return r.market_cap != null && r.market_cap >= bk.min && r.market_cap < bk.max; }));
        var below = bk.max <= floor, on = F.mc.b.indexOf(bk.key) >= 0;
        return '<label class="' + (below ? "rcf-dis" : "") + '"><span class="rcf-l"><input type="checkbox" value="' + bk.key + '"' + (on ? " checked" : "") + (below && !on ? " disabled" : "") +
          '> <b>' + bk.label + '</b> <span class="rcf-meta">' + bk.range + '</span></span>' +
          (below ? '<span class="rcf-why">below this board\'s ' + rcEsc(fmtCap(floor)) + ' floor</span>' : '<span class="rcf-cnt">' + fmt(n) + '</span>') + '</label>';
      }).join("");
      $("#" + uid + "-mcmin").value = F.mc.min != null ? F.mc.min / 1e6 : "";
      $("#" + uid + "-mcmax").value = F.mc.max != null ? F.mc.max / 1e6 : "";
      var t = mcText();
      $("#" + uid + "-v-mc").textContent = t || "All sizes";
      $("#" + uid + "-t-mc").classList.toggle("rcf-on", !!t);
    }

    /* tree pickers: GICS (path keys) and Themes (catalog ids) */
    function TreePicker(key, levels, nodesFn, keysOf, noneRow) {
      var popEl = $("#" + uid + "-p-" + key);
      if (!popEl) return null;
      var tree = popEl.querySelector(".rcf-tree"), search = popEl.querySelector(".rcf-search");
      var expanded = {};
      function counts() {
        var c = {};
        base(key).forEach(function (r) { keysOf(r).forEach(function (k) { (c[k] = c[k] || {})[r.ticker] = 1; }); });
        return c;
      }
      function render() {
        var q = (search.value || "").trim().toLowerCase(), sel = F[key], nodes = nodesFn(), cnt = counts();
        var kids = {};
        Object.keys(nodes).forEach(function (k) { var p = nodes[k].parent || ""; (kids[p] = kids[p] || []).push(k); });
        function sorted(list) {
          return (list || []).slice().sort(function (a, b) {
            return labelCmp(nodes[a].label, nodes[b].label);
          });
        }
        function match(k) {
          if (!q) return true;
          if (nodes[k].label.toLowerCase().indexOf(q) >= 0) return true;
          return (kids[k] || []).some(match);
        }
        function li(k) {
          if (!match(k)) return "";
          var n = nodes[k], ch = sorted(kids[k]), has = ch.length > 0, open = q ? true : !!expanded[k];
          var c = Object.keys(cnt[k] || {}).length, cur = !!(sel && sel.key === k);
          var h = '<li role="treeitem" aria-level="' + n.level + '"' + (has ? ' aria-expanded="' + open + '"' : '') + ' aria-selected="' + cur + '"><div class="rcf-tn">' +
            '<button class="rcf-tog" type="button" tabindex="-1" data-key="' + rcEsc(k) + '"' + (has ? ' aria-label="' + (open ? "Collapse " : "Expand ") + rcEsc(n.label) + '"' : ' aria-hidden="true"') + '>' + (has ? (open ? "▾" : "▸") : "") + '</button>' +
            '<button class="rcf-row" type="button" data-key="' + rcEsc(k) + '"' + (cur ? ' aria-current="true"' : '') + (c === 0 && !cur ? ' aria-disabled="true"' : '') + '>' +
            '<span>' + rcEsc(n.label) + '<span class="rcf-lvl">' + rcEsc(levels[n.level - 1] || "") + '</span></span><span class="rcf-cnt">' + fmt(c) + '</span></button></div>';
          if (has && open) h += '<ul role="group">' + ch.map(li).join("") + '</ul>';
          return h + "</li>";
        }
        tree.innerHTML = sorted(kids[""]).map(li).join("") + (noneRow ? noneRow() : "");
        $("#" + uid + "-v-" + key).textContent = sel ? (sel.key === "__none__" ? "Unthemed" : sel.label + " · " + (levels[sel.level - 1] || "")) : (key === "gics" ? "All sectors" : "All themes");
        $("#" + uid + "-t-" + key).classList.toggle("rcf-on", !!sel);
      }
      tree.addEventListener("click", function (e) {
        var t = e.target.closest(".rcf-tog");
        if (t && t.getAttribute("aria-hidden") !== "true") {
          // Expanding re-renders the tree; keep the picker open and focused.
          e.stopPropagation();
          var k = t.getAttribute("data-key"); expanded[k] = !expanded[k]; render(); refocus(k); return;
        }
        var r = e.target.closest(".rcf-row");
        if (!r || r.getAttribute("aria-disabled") === "true") return;
        var k2 = r.getAttribute("data-key"), n = nodesFn()[k2];
        F[key] = k2 === "__none__" ? {key: "__none__", label: "Unthemed", level: 1} : {key: k2, label: n.label, level: n.level};
        closePop(true); changed();
      });
      tree.addEventListener("keydown", function (e) {
        var list = Array.prototype.slice.call(tree.querySelectorAll(".rcf-row")), i = list.indexOf(document.activeElement);
        if (i < 0) return;
        var k = list[i].getAttribute("data-key");
        if (e.key === "ArrowDown") { e.preventDefault(); (list[i + 1] || list[i]).focus(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); if (i === 0) search.focus(); else list[i - 1].focus(); }
        else if (e.key === "ArrowRight") { e.preventDefault(); expanded[k] = true; render(); refocus(k); }
        else if (e.key === "ArrowLeft") {
          e.preventDefault();
          if (expanded[k]) { expanded[k] = false; render(); refocus(k); }
          else { var p = (nodesFn()[k] || {}).parent; if (p) refocus(p); }
        }
      });
      function refocus(k) {
        var all = tree.querySelectorAll(".rcf-row");
        for (var j = 0; j < all.length; j++) { if (all[j].getAttribute("data-key") === k) { all[j].focus(); return; } }
      }
      search.addEventListener("input", render);
      search.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") { e.preventDefault(); var f = tree.querySelector(".rcf-row:not([aria-disabled=true])"); if (f) f.focus(); }
      });
      popEl.querySelector("[data-clear]").addEventListener("click", function () { F[key] = null; closePop(true); changed(); });
      return {render: render};
    }
    function gicsNodes() {
      var nodes = {};
      rows.forEach(function (r) {
        var ks = gicsKeys(r);
        ks.forEach(function (k, i) {
          if (!nodes[k]) nodes[k] = {label: k.split("›").pop(), level: i + 1, parent: i ? ks[i - 1] : ""};
        });
      });
      return nodes;
    }
    function themeNodes() {
      var nodes = {}, lv = {coarse: 1, medium: 2, fine: 3};
      Object.keys(themes).forEach(function (id) {
        var t = themes[id];
        nodes[id] = {label: t.label || id, level: lv[t.level] || 1, parent: themes[t.parent] ? t.parent : ""};
      });
      return nodes;
    }
    var gicsPick = TreePicker("gics", GICS_LEVELS, gicsNodes, gicsKeys, null);
    var themePick = TreePicker("theme", THEME_LEVELS, themeNodes, themeKeys, function () {
      var n = core.uniq(base("theme").filter(function (r) { return themeKeys(r).length === 0; }));
      var cur = !!(F.theme && F.theme.key === "__none__");
      return '<li role="treeitem" aria-level="1" aria-selected="' + cur + '"><div class="rcf-tn"><button class="rcf-tog" type="button" tabindex="-1" aria-hidden="true"></button>' +
        '<button class="rcf-row" type="button" data-key="__none__"' + (cur ? ' aria-current="true"' : '') + (n === 0 && !cur ? ' aria-disabled="true"' : '') + '>' +
        '<span class="rcf-meta">Unthemed (no 10-K profile yet)</span><span class="rcf-cnt">' + fmt(n) + '</span></button></div></li>';
    });

    /* earnings */
    function buildEarn() {
      var box = $("#" + uid + "-ew");
      if (!box) return;
      var b = base("earnings");
      box.innerHTML = '<legend class="rcf-sr">Earnings week</legend>' + EARN_KEYS.map(function (k) {
        var n = k === "any" ? core.uniq(b) : core.uniq(b.filter(function (r) { return core.earnKey(r) === k; }));
        return '<label><span class="rcf-l"><input type="radio" name="' + uid + '-ew" value="' + k + '"' + (F.earn === k ? " checked" : "") + '> ' + rcEsc(weekLabel(k, core.now)) + '</span><span class="rcf-cnt">' + fmt(n) + '</span></label>';
      }).join("");
      var f = $("[data-act=earn-filter]");
      if (f) f.checked = !!F.earnFilter;
      $("#" + uid + "-v-earnings").textContent = F.earn === "any" ? "Any date" : weekLabel(F.earn, core.now).split(" · ")[0] + (core.mode === "mark" && !F.earnFilter ? " (marked)" : "");
      $("#" + uid + "-t-earnings").classList.toggle("rcf-on", F.earn !== "any");
    }

    /* active chips + link toggle */
    function buildActive() {
      var chips = [];   // only the facets this board shows: an omitted one's state is kept, never shown or removed here
      if (core.on("index") && F.index !== "all") chips.push(["Index: " + (indexLabel[F.index] || F.index), function () { F.index = "all"; }]);
      var mt = mcText();
      if (core.on("mc") && mt) chips.push(["Cap: " + mt, function () { F.mc = {b: [], min: null, max: null}; }]);
      if (core.on("gics") && F.gics) chips.push(["Sector: " + F.gics.label + " · " + GICS_LEVELS[F.gics.level - 1], function () { F.gics = null; }]);
      if (core.on("theme") && F.theme) chips.push(["Theme: " + (F.theme.key === "__none__" ? "Unthemed" : F.theme.label + " · " + THEME_LEVELS[F.theme.level - 1]), function () { F.theme = null; }]);
      if (core.on("earnings") && F.earn !== "any") chips.push(["Earnings: " + weekLabel(F.earn, core.now).split(" · ")[0] + (core.mode === "mark" && !F.earnFilter ? " (marked)" : ""), function () { F.earn = "any"; }]);
      var row = $(".rcf-active");
      row.innerHTML = (chips.length ? "<span>Active:</span>" : "<span>No filters.</span>") +
        chips.map(function (c, i) { return '<span class="rcf-chip">' + rcEsc(c[0]) + '<button type="button" data-i="' + i + '" aria-label="Remove ' + rcEsc(c[0]) + '">×</button></span>'; }).join("") +
        (chips.length ? '<button class="rcf-link" type="button" data-act="reset">Reset all</button>' : "") +
        '<label class="rcf-toggle rcf-linked"><input type="checkbox" data-act="linked"' + (linked ? " checked" : "") + "> Same filters on every board</label>";
      Array.prototype.forEach.call(row.querySelectorAll(".rcf-chip button"), function (b) {
        b.onclick = function () { chips[+b.getAttribute("data-i")][1](); changed(); };
      });
      var n = $(".rcf-notes");
      n.textContent = notes.join(" ");
      n.hidden = !notes.length;
    }

    host.addEventListener("change", function (e) {
      var t = e.target;
      if (t.closest("#" + uid + "-mcb")) {
        var k = t.value, i = F.mc.b.indexOf(k);
        if (t.checked && i < 0) F.mc.b.push(k);
        if (!t.checked && i >= 0) F.mc.b.splice(i, 1);
        changed(true); return;
      }
      if (t.name === uid + "-ew") { F.earn = t.value; changed(true); return; }
      if (t.getAttribute("data-act") === "earn-filter") { F.earnFilter = t.checked; changed(true); return; }
      if (t.getAttribute("data-act") === "linked") {
        linked = t.checked;
        save();
      }
    });
    host.addEventListener("click", function (e) {
      var a = e.target.closest("[data-act]");
      if (!a) return;
      var act = a.getAttribute("data-act");
      if (act === "mc-apply") {
        var mn = parseFloat($("#" + uid + "-mcmin").value), mx = parseFloat($("#" + uid + "-mcmax").value);
        F.mc.min = isNaN(mn) ? null : mn * 1e6; F.mc.max = isNaN(mx) ? null : mx * 1e6; changed(true);
      } else if (act === "mc-preset") {
        var p = MC_PRESETS[+a.getAttribute("data-i")];
        F.mc = {b: [], min: p.min, max: p.max}; changed(true);
      } else if (act === "mc-clear") {
        F.mc = {b: [], min: null, max: null}; changed(true);
      } else if (act === "reset") {
        F = resetShown(F); notes = []; changed();
      }
    });
    if (typeof window !== "undefined") {
      window.addEventListener("storage", function (e) {
        if (!linked || e.key !== LS_KEY || !e.newValue) return;
        try { var r = sanitize(JSON.parse(e.newValue), meta, rows); F = r.state; notes = r.notes; renderBar(); fire(); } catch (err) {}
      });
    }

    function renderBar() {
      buildIndex(); buildMc();
      if (gicsPick) gicsPick.render();
      if (themePick) themePick.render();
      buildEarn(); buildActive();
    }
    function fire() { if (opt.onChange) opt.onChange(api); }
    // "Reset all" clears the facets this board shows; an omitted facet's shared state stays for the other boards.
    function resetShown(cur) {
      var d = defaults(), keys = {index: ["index"], mc: ["mc"], gics: ["gics"], theme: ["theme"],
                                  earnings: ["earn", "earnFilter"]};
      FACETS.forEach(function (f) { if (!core.on(f)) keys[f].forEach(function (k) { d[k] = cur[k]; }); });
      return d;
    }
    function changed(keepOpen) {
      save();
      var o = OPEN;
      renderBar();
      if (!keepOpen && o && o.pop.classList.contains("rcf-picker")) closePop(false);
      fire();
    }

    var api = {
      pass: function (r) { return core.pass(r, F); },
      mark: function (r) { return core.mark(r, F); },
      active: function () { return !!core.active(F); },
      state: function () { return clone(F); },
      set: function (patch) { Object.keys(patch).forEach(function (k) { F[k] = patch[k]; }); changed(); },
      setGics: function (row, level) {
        var ks = gicsKeys(row), k = ks[Math.min(level, ks.length) - 1];
        F.gics = {key: k, label: k.split("›").pop(), level: Math.min(level, ks.length)};
        changed();
      },
      reset: function () { F = resetShown(F); changed(); },
      earnLabel: function (r) {
        var k = core.earnKey(r);
        return k === "unknown" ? "date unknown" : k === "past" ? "reported recently" : weekLabel(k, core.now).split(" · ")[0].toLowerCase();
      },
      uniq: core.uniq
    };
    load();
    renderBar();
    return api;
  }

  /* ── type-ahead combobox (ARIA 1.2 editable combobox, list autocomplete) ─ */
  function rank(r, q) {
    var t = (r.ticker || "").toLowerCase(), n = (r.name || "").toLowerCase();
    if (t === q) return 0;
    if (t.indexOf(q) === 0) return 1;
    if (t.indexOf(q) > 0) return 2;
    if (n.indexOf(q) === 0) return 3;
    if (n.indexOf(" " + q) >= 0) return 4;
    if (n.indexOf(q) >= 0) return 5;
    return 9;
  }
  function hl(s, q) {
    if (!q) return rcEsc(s);
    var i = String(s).toLowerCase().indexOf(q);
    if (i < 0) return rcEsc(s);
    return rcEsc(s.slice(0, i)) + "<mark>" + rcEsc(s.slice(i, i + q.length)) + "</mark>" + rcEsc(s.slice(i + q.length));
  }
  // Opt-in ``allowFree`` (Implied Probability): typed text shaped like a ticker (the server's ``_valid_ticker``
  // rule, on the text stripped and upper-cased) that no row of the filtered pool holds becomes one extra item
  // after the matches, "Open TYPED (not in the list)", so a name outside the pool, or held only by a row the
  // filters hide, can still be opened.
  var FREE_RE = /^[A-Z0-9][A-Z0-9.\-]{0,9}$/;
  function freeItem(text, rows) {
    var t = String(text || "").trim().toUpperCase();
    if (!FREE_RE.test(t)) return null;
    for (var i = 0; i < rows.length; i++) { if (String(rows[i].ticker).toUpperCase() === t) return null; }
    return {ticker: t, name: "", free: true};
  }
  function combobox(opt) {
    var input = typeof opt.input === "string" ? document.querySelector(opt.input) : opt.input;
    var list = typeof opt.list === "string" ? document.querySelector(opt.list) : opt.list;
    var lid = list.id || "rcc";
    var state = {items: [], active: -1};
    function all() { return typeof opt.rows === "function" ? opt.rows() : opt.rows; }
    function uniqRows(rows) {
      var seen = {}, out = [];
      rows.forEach(function (r) { if (!seen[r.ticker]) { seen[r.ticker] = 1; out.push(r); } });
      return out;
    }
    function render() {
      var q = input.value.trim().toLowerCase(), rows = uniqRows(all()), keep = opt.filter || function () { return true; };
      var pool = rows.filter(keep);
      var hits = pool.map(function (r) { return {r: r, k: q ? rank(r, q) : 0}; }).filter(function (x) { return x.k < 9; })
        .sort(function (a, b) { return a.k - b.k || a.r.ticker.localeCompare(b.r.ticker); });
      var hidden = q ? rows.filter(function (r) { return !keep(r) && rank(r, q) < 9; }).length : 0;
      state.items = hits.slice(0, 100).map(function (x) { return x.r; });
      var free = opt.allowFree ? freeItem(input.value, pool) : null;
      if (free) state.items.push(free);
      state.active = state.items.length ? 0 : -1;
      list.innerHTML = (state.items.length ? state.items.map(function (r, i) {
        var g = opt.groupLabel && !r.free ? opt.groupLabel(r) : "";
        var body = r.free ? '<span class="rcf-n">Open </span><span class="rcf-t">' + rcEsc(r.ticker) + '</span><span class="rcf-n"> (not in the list)</span>'
          : '<span class="rcf-t">' + hl(r.ticker, q) + '</span> <span class="rcf-n">' + hl(r.name || "", q) + '</span>';
        return '<li role="option" id="' + lid + '-o' + i + '" aria-selected="' + (i === 0) + '" data-i="' + i + '"><span>' + body + '</span>' + (g ? '<span class="rcf-g">' + rcEsc(g) + '</span>' : '') + '</li>';
      }).join("") : '<li class="rcf-cfoot" role="presentation">No matches' + (hidden ? "" : " — try fewer letters") + "</li>") +
        (hits.length > 100 ? '<li class="rcf-cfoot" role="presentation">Showing 100 of ' + fmt(hits.length) + " — keep typing to narrow</li>" : "") +
        (hidden ? '<li class="rcf-cfoot" role="presentation">' + fmt(hidden) + " more match but are hidden by your filters</li>" : "");
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      if (state.active >= 0) input.setAttribute("aria-activedescendant", lid + "-o0");
      else input.removeAttribute("aria-activedescendant");
    }
    function move(d) {
      if (!state.items.length) return;
      var i = (state.active + d + state.items.length) % state.items.length;
      state.active = i;
      Array.prototype.forEach.call(list.querySelectorAll("[role=option]"), function (li, k) { li.setAttribute("aria-selected", String(k === i)); });
      input.setAttribute("aria-activedescendant", lid + "-o" + i);
      var el = document.getElementById(lid + "-o" + i);
      if (el && el.scrollIntoView) el.scrollIntoView({block: "nearest"});
    }
    function close() { list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }
    function pick(r) { if (!r) return; input.value = r.free ? r.ticker : r.ticker + " — " + (r.name || r.ticker); close(); if (opt.onPick) opt.onPick(r); }
    function clear() { input.value = ""; close(); if (opt.onClear) opt.onClear(); }
    input.addEventListener("focus", render);
    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); if (list.hidden) render(); else move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); pick(state.items[state.active]); }
      else if (e.key === "Escape") {
        if (!list.hidden) { e.preventDefault(); e.stopPropagation(); close(); }
        else if (input.value) clear();
      } else if (e.key === "Tab") close();
    });
    list.addEventListener("mousedown", function (e) {
      var li = e.target.closest("[role=option]");
      if (!li) return;
      e.preventDefault();
      pick(state.items[+li.getAttribute("data-i")]);
    });
    document.addEventListener("click", function (e) { if (e.target !== input && !list.contains(e.target)) close(); });
    // Opt-in ``clear``: wire the × that combobox_html puts beside the input (a board with its own handler, like
    // Co-movement, leaves it off).
    if (opt.clear) {
      var x = document.querySelector('[data-clear-for="' + input.id + '"]');
      if (x) x.addEventListener("click", function () { clear(); input.focus(); });
    }
    return {render: render, close: close, clear: clear};
  }

  root.RCFilters = {
    mount: mount,
    core: {makeCore: makeCore, sanitize: sanitize, defaults: defaults, earnKey: earnKey, isoWeek: isoWeek, labelCmp: labelCmp,
           weekLabel: weekLabel, gicsKeys: gicsKeys, rank: rank, fmtCap: fmtCap, freeItem: freeItem, FREE_RE: FREE_RE,
           MC_BUCKETS: MC_BUCKETS, FACETS: FACETS, LS_KEY: LS_KEY}
  };
  root.RCInfo = {wire: wireInfo, close: closePop};
  root.RCRadios = {wire: wireRadios};
  root.RCCombobox = {mount: combobox};
})(typeof window !== "undefined" ? window : globalThis);

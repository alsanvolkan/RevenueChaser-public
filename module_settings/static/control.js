/* The Settings / Data control board (design v28). The page's data is window.CTL (routes, the universe, the
   calendars, the fill switch) and window.RC_FLOW (the flow diagram). A press does one of four things: Fetch and
   Pause put one request in the builder's queue; Apply saves the universe; Refresh the example rows reads five
   rows of the live file. Fill and "Fill this range" are off unless the server says otherwise (CTL.fill_enabled).
   No timer repeats: the page checks its own age when it is shown again, and a press follows its request with a
   short chain of timeouts that ends. Every string put into markup goes through rcEsc. */
(function () {
  var C = window.CTL;
  if (!C || !document.querySelector) return;
  var root = document.getElementById('ctl');
  if (!root || !root.addEventListener) return;
  window.CTLX = {};
  function fmt(n) { return Number(n || 0).toLocaleString('en-US'); }
  function say(msg) {
    var old = document.getElementById('toast');
    if (old && old.remove) old.remove();
    var t = document.createElement('div');
    t.id = 'toast'; t.setAttribute('role', 'status'); t.textContent = msg;
    if (document.body && document.body.appendChild) document.body.appendChild(t);
    setTimeout(function () { if (t.remove) t.remove(); }, 4200);
  }
  function send(method, url, body) {
    var opt = { method: method, headers: { 'X-RC-Panel': 'settings' } };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j || {} }; },
                           function () { return { status: r.status, body: {} }; });
    });
  }
  window.CTLX.say = say; window.CTLX.send = send; window.CTLX.fmt = fmt;

  /* the page's age: past CTL.stale_ms the strip on top says so (checked when the page is shown again) */
  function age() {
    var read = Date.parse(C.read_at), bar = document.getElementById('ctl-stale');
    if (bar && !isNaN(read)) bar.hidden = Date.now() - read < C.stale_ms;
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) age(); });
  window.addEventListener('focus', age);

  /* the universe a press runs on: the applied tier sets, the groups, and the names typed by hand */
  function applied() {
    var on = {}, inc = [];
    [].forEach.call(root.querySelectorAll('#universe .useg:not(.uoff)'), function (b) {
      on[b.getAttribute('data-tier')] = b.getAttribute('data-on') === '1';
    });
    [].forEach.call(root.querySelectorAll('#universe .uinc'), function (b) {
      if (b.getAttribute('data-on') === '1') inc.push(b.getAttribute('data-inc'));
    });
    return { on: on, include: inc, hand: (window.CTLX.hand || []).slice() };
  }
  function isDefault(a) {
    var all = C.universe.sets.every(function (s) { return a.on[s.k]; });
    return all && a.include.length === C.universe.include.length && !a.hand.length;
  }
  /* a request's scope from a set of picks: tiers, names (a part of B is asked by its names), the groups */
  function scopeOf(on, a) {
    var tiers = [], names = a.hand.slice();
    if (on.A) tiers.push('A');
    if (on.E && on.R) tiers.push('B');
    else ['E', 'R'].forEach(function (k) { if (on[k]) names = names.concat(C.universe.tickers[k] || []); });
    return { tiers: tiers, names: names, include: a.include };
  }
  window.CTLX.applied = applied; window.CTLX.scopeOf = scopeOf; window.CTLX.isDefault = isDefault;

  /* what a press leaves behind: its request's state, read from the queue a few times, then left as it stands */
  function nth(n) { return n + (n === 1 ? 'st' : n === 2 ? 'nd' : n === 3 ? 'rd' : 'th'); }
  function pill(btn, cls, text) {
    var id = btn.getAttribute('data-jid'), old = id && root.querySelector('.jst[data-for="' + id + '"]');
    if (old && old.remove) old.remove();
    if (!id) { id = 'j' + Math.random().toString(36).slice(2); btn.setAttribute('data-jid', id); }
    var s = document.createElement('span');
    s.className = 'jst ' + cls; s.setAttribute('role', 'status'); s.setAttribute('data-for', id);
    s.textContent = text;
    btn.parentNode.insertBefore(s, btn);
  }
  function follow(btn, rid, left) {
    if (left <= 0) return;
    setTimeout(function () {
      send('GET', C.routes.queue).then(function (r) {
        var q = r.body || {}, active = q.active || [], done = q.finished || [], row = null, pos = 0;
        active.forEach(function (x, i) { if (x.id === rid) { row = x; pos = i + 1; } });
        if (!row) done.forEach(function (x) { if (x.id === rid) row = x; });
        if (!row) { pill(btn, 'st-q', 'queued · the queue no longer lists it'); return; }
        if (row.status === 'queued') { pill(btn, 'st-q', 'queued · ' + nth(pos) + ' in line'); follow(btn, rid, left - 1); }
        else if (row.status === 'running') { pill(btn, 'st-r', 'running'); follow(btn, rid, left - 1); }
        else if (row.status === 'done') { pill(btn, 'st-d', 'done · ' + (row.note || 'see Queue and last runs')); btn.hidden = false; }
        else { pill(btn, 'st-f', row.status + ' · ' + (row.note || 'see Open problems')); btn.hidden = false; }
      }, function () { follow(btn, rid, left - 1); });
    }, 15000);
  }
  function queue(btn, dataset, body) {
    btn.disabled = true;
    send('POST', C.routes.request + dataset, body).then(function (r) {
      btn.disabled = false;
      if (r.status === 202) {
        pill(btn, 'st-q', 'queued · request ' + r.body.id);
        follow(btn, r.body.id, 20);
      } else {
        pill(btn, 'st-f', (r.body.error || ('refused · HTTP ' + r.status)));
      }
    }, function () { btn.disabled = false; pill(btn, 'st-f', 'could not reach the dashboard'); });
  }
  window.CTLX.queue = queue;
  function fetchBody(btn) {
    if (btn.getAttribute('data-scope') !== '1') return undefined;
    var a = applied();
    if (isDefault(a)) return undefined;     /* the default universe is the daily run: no scope, it takes them all */
    return scopeOf(a.on, a);
  }
  function pause(btn) {
    var ds = btn.getAttribute('data-dataset'), paused = btn.getAttribute('aria-pressed') === 'true';
    btn.disabled = true;
    send('POST', C.routes.schedule + ds + '/' + (paused ? 'resume' : 'pause')).then(function (r) {
      btn.disabled = false;
      if (r.status >= 200 && r.status < 300) {
        btn.setAttribute('aria-pressed', paused ? 'false' : 'true');
        btn.textContent = paused ? 'Pause' : 'Resume';
        if (!paused) pill(btn, 'st-p', 'paused · the builder skips it');
        else { var p = root.querySelector('.jst[data-for="' + btn.getAttribute('data-jid') + '"]'); if (p && p.remove) p.remove(); }
      } else pill(btn, 'st-f', r.body.error || ('refused · HTTP ' + r.status));
    }, function () { btn.disabled = false; pill(btn, 'st-f', 'could not reach the dashboard'); });
  }
  function fill(btn) {
    if (!C.fill_enabled) { say(C.fill_off_why); return; }
    var a = applied(), on = {}; on[btn.getAttribute('data-g')] = true;
    queue(btn, btn.getAttribute('data-dataset'), scopeOf(on, a));
  }
  function copy(btn) {
    var cmd = btn.getAttribute('data-cmd') || '';
    try {
      navigator.clipboard.writeText(cmd).then(function () { say('Command copied: ' + cmd); },
                                              function () { say('Run on the server: ' + cmd); });
    } catch (e) { say('Run on the server: ' + cmd); }
  }
  function sample(btn) {
    var t = btn.getAttribute('data-sample-refresh'), sec = btn.closest('.dsec'), note = btn.parentNode.querySelector('.dim');
    btn.disabled = true;
    send('GET', C.routes.sample + encodeURIComponent(t)).then(function (r) {
      btn.disabled = false;
      var s = r.body || {};
      if (r.status !== 200 || !s.ok) { if (note) note.textContent = s.error || ('could not read · HTTP ' + r.status); return; }
      var body = sec && sec.querySelector('tbody');
      if (body) body.innerHTML = (s.rows || []).map(function (row) {
        return '<tr>' + row.map(function (v) { return '<td class="pv-build">' + rcEsc(v == null ? '' : v) + '</td>'; }).join('') + '</tr>';
      }).join('');
      if (note) note.textContent = 'read again at ' + new Date().toTimeString().slice(0, 5) + ' · ' + (s.rows || []).length + ' rows · no provider call';
    }, function () { btn.disabled = false; if (note) note.textContent = 'could not reach the dashboard'; });
  }
  function scrollTo(el) {
    var rm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'start', behavior: rm });
  }
  window.CTLX.scrollTo = scrollTo;
  root.addEventListener('click', function (e) {
    var w = e.target.closest('.wiali');
    if (w) {
      var box = document.getElementById('wial'), open = w.getAttribute('aria-expanded') === 'true';
      w.setAttribute('aria-expanded', open ? 'false' : 'true'); if (box) box.hidden = open; return;
    }
    if (e.target.closest('[data-reload]')) { location.reload(); return; }
    var g = e.target.closest('[data-goto]');
    if (g) {
      var to = g.getAttribute('data-goto');
      if (to === 'universe') { var u = document.getElementById('universe'); scrollTo(u); return; }
      var sec = document.getElementById('sec-' + to);
      if (sec) { sec.open = true; scrollTo(sec); }
      return;
    }
    var s = e.target.closest('[data-sample-refresh]');
    if (s) { sample(s); return; }
    var b = e.target.closest('[data-job]');
    if (!b || b.disabled) return;
    var j = b.getAttribute('data-job');
    if (j === 'fetch') queue(b, b.getAttribute('data-dataset'), fetchBody(b));
    else if (j === 'pause') pause(b);
    else if (j === 'fill') fill(b);
    else if (j === 'copy') copy(b);
  });

  /* open and close every file of the Data section */
  function all(v) { [].forEach.call(root.querySelectorAll('#sec-data details.group'), function (d) { d.open = v; }); }
  var oa = document.getElementById('openall'), ca = document.getElementById('shutall');
  if (oa && oa.addEventListener) oa.addEventListener('click', function () { all(true); });
  if (ca && ca.addEventListener) ca.addEventListener('click', function () { all(false); });
})();

/* The universe control (Karar 86 A). A press on a tier set or a group makes a draft; Apply saves it (the board
   settings of module_settings) and makes it the universe of the next presses. Names typed by hand join the same
   draft; they stay in this browser and ride with the next press, they are not saved on the server.
   shortcut: hand-typed names live in localStorage only; upgrade to a saved list when avo wants them kept. */
(function () {
  var C = window.CTL, X = window.CTLX;
  if (!C || !X) return;
  var segs = [].slice.call(document.querySelectorAll('#universe .useg:not(.uoff)')),
      incs = [].slice.call(document.querySelectorAll('#universe .uinc'));
  if (!segs.length) return;
  var input = document.getElementById('uni-names'), add = document.getElementById('uni-add'),
      handn = document.getElementById('uni-handn'), chips = document.getElementById('uni-hchips'),
      box = document.getElementById('uni-draft'), KEY = 'rc-ctl-hand';
  var hand = [], dhand = [];
  try { hand = JSON.parse(localStorage.getItem(KEY) || '[]') || []; } catch (e) { hand = []; }
  if (!Array.isArray(hand)) hand = [];
  hand = hand.filter(function (x) { return typeof x === 'string' && /^[A-Z0-9.\-\/]{1,12}$/.test(x); });
  dhand = hand.slice(); X.hand = hand.slice();
  function pressed(b) { return b.getAttribute('aria-pressed') === 'true'; }
  function on(b) { return b.getAttribute('data-on') === '1'; }
  function sum(list, h) {
    var n = list.reduce(function (a, b) { return a + (+b.getAttribute('data-n')); }, 0);
    var t = list.length ? list.map(function (b) { return b.getAttribute('data-label'); }).join(' + ') : 'no tier set';
    return { n: n, s: t + ' · ' + X.fmt(n) + ' names' + (h.length ? ' + ' + h.length + ' by hand' : '') };
  }
  function same() {
    return segs.concat(incs).every(function (b) { return pressed(b) === on(b); }) && hand.join(',') === dhand.join(',');
  }
  function paintApplied() {
    var a = sum(segs.filter(on), hand);
    var el = document.getElementById('uni-sum'); if (el) el.textContent = a.s;
    [].forEach.call(document.querySelectorAll('#ctl .runs-on'), function (e) { e.textContent = a.s; });
    [].forEach.call(document.querySelectorAll('#ctl .uni-n b'), function (e) { e.textContent = X.fmt(a.n + hand.length); });
    X.hand = hand.slice();
  }
  function paintDraft() {
    segs.concat(incs).forEach(function (b) { b.classList.toggle('udiff', pressed(b) !== on(b)); });
    chips.innerHTML = dhand.map(function (x) {
      return '<span class="hchip">' + rcEsc(x) + '<button type="button" data-rm="' + rcEsc(x) + '" aria-label="Remove ' + rcEsc(x) + '">×</button></span>';
    }).join('') + (dhand.length ? '<button type="button" class="lnk" id="uni-hclear">clear</button>' : '');
    handn.textContent = dhand.length ? '' : 'no names added';
    if (same()) { box.hidden = true; box.innerHTML = ''; return; }
    var d = sum(segs.filter(pressed), dhand), nn = d.n + dhand.length, empty = !nn;
    box.hidden = false; box.className = 'udraft' + (empty ? ' empty' : '');
    box.innerHTML = '<p>' + (empty
      ? '<b>No names in this draft.</b> A universe needs at least one tier set or one name; Apply stays off.'
      : '<b>Draft:</b> ' + rcEsc(d.s) + '. <b>Apply</b> makes it the universe: the next fetches and fills use ' +
        (nn === 1 ? 'this ' : 'these ') + X.fmt(nn) + (nn === 1 ? ' name' : ' names') +
        '. Nothing stored is deleted; names already filled stay. The daily run is not changed by it.') + '</p>' +
      '<button type="button" class="btn go" id="uni-apply"' + (empty || !segs.some(pressed) ? ' disabled' : '') + '>Apply</button>' +
      '<button type="button" class="btn" id="uni-undo">Undo</button>' +
      (segs.some(pressed) ? '' : '<span class="dim">at least one tier set stays in the universe</span>');
  }
  segs.concat(incs).forEach(function (b) {
    b.addEventListener('click', function () { b.setAttribute('aria-pressed', pressed(b) ? 'false' : 'true'); paintDraft(); });
  });
  function addNames() {
    (input.value || '').split(/[\s,;]+/).forEach(function (x) {
      x = x.trim().toUpperCase();
      if (x && /^[A-Z0-9.\-\/]{1,12}$/.test(x) && dhand.indexOf(x) < 0) dhand.push(x);
    });
    input.value = ''; paintDraft();
  }
  add.addEventListener('click', addNames);
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); addNames(); } });
  function bits(list, map, key) {
    return list.filter(pressed).reduce(function (n, b) { return n + (map[b.getAttribute(key)] || 0); }, 0);
  }
  function apply(btn) {
    btn.disabled = true;
    var changes = { UNIVERSE_SETS: bits(segs, C.universe.bits, 'data-tier'),
                    UNIVERSE_INCLUDE: bits(incs, C.universe.inc_bits, 'data-inc') };
    X.send('POST', C.routes.settings, { changes: changes }).then(function (r) {
      if (r.status !== 202 && r.status !== 200) {
        btn.disabled = false;
        X.say('Not applied: ' + ((r.body.messages || [r.body.error || ('HTTP ' + r.status)]).join('; ')));
        return;
      }
      segs.concat(incs).forEach(function (b) { b.setAttribute('data-on', pressed(b) ? '1' : '0'); });
      hand = dhand.slice();
      try { localStorage.setItem(KEY, JSON.stringify(hand)); } catch (e) { /* a private window: kept for this page */ }
      paintApplied(); paintDraft();
      X.say('Applied: the next presses run on ' + document.getElementById('uni-sum').textContent);
    }, function () { btn.disabled = false; X.say('Not applied: could not reach the dashboard'); });
  }
  document.getElementById('universe').addEventListener('click', function (e) {
    var t = e.target.closest('button'); if (!t) return;
    if (t.getAttribute('data-rm')) { var x = t.getAttribute('data-rm'); dhand = dhand.filter(function (y) { return y !== x; }); paintDraft(); input.focus(); return; }
    if (t.id === 'uni-hclear') { dhand = []; paintDraft(); input.focus(); return; }
    if (t.id === 'uni-apply') { apply(t); return; }
    if (t.id === 'uni-undo') {
      segs.concat(incs).forEach(function (b) { b.setAttribute('aria-pressed', on(b) ? 'true' : 'false'); });
      dhand = hand.slice(); paintDraft();
    }
  });
  paintApplied(); paintDraft();
})();

/* The tier tips (hover on a desktop, press and hold on a phone) and the Backfill calendar. The calendar picks a
   range and, where a dataset fills by tier set, the sets; it says sessions, calls and nights before anything
   starts, from the stored sessions (CTL.calendars). "Fill this range" is off unless CTL.fill_enabled. */
(function () {
  var C = window.CTL, X = window.CTLX;
  if (!C || !X) return;
  var timer = null, held = null;
  [].forEach.call(document.querySelectorAll('#universe .useg'), function (b) {
    b.addEventListener('touchstart', function () { timer = setTimeout(function () { held = b; b.classList.add('tip'); }, 450); }, { passive: true });
    b.addEventListener('touchend', function () { clearTimeout(timer); if (held === b) setTimeout(function () { b.classList.remove('tip'); }, 1600); });
    b.addEventListener('touchmove', function () { clearTimeout(timer); }, { passive: true });
    b.addEventListener('click', function (e) { if (held === b) { held = null; e.stopImmediatePropagation(); e.preventDefault(); } }, true);
  });
  var TODAY = C.today, MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var MONL = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  function key(d) { return d.toISOString().slice(0, 10); }
  function D(s) { return new Date(s + 'T00:00:00Z'); }
  function hd(s) { var d = D(s); return d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear(); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function lastDay(y, m) { return new Date(Date.UTC(y, m + 1, 0)).getUTCDate(); }
  function pct(a, b) { return b ? Math.round(a * 1000 / b) / 10 : 0; }
  function yesterday() { return key(new Date(D(TODAY).getTime() - 864e5)); }
  function uniN() { var n = 0; [].forEach.call(document.querySelectorAll('#universe .useg[data-on="1"]'), function (b) { n += +b.getAttribute('data-n'); }); return n; }
  function dstate(c, k, dow) {
    if (k >= TODAY) return 'future';
    if (dow === 0 || dow === 6) return 'nosess';
    if (k < c.from) return 'notoff';
    if (c.names_per_session) {
      var n = c.names_per_session[k];
      if (!n) return 'missing';
      return n >= 0.98 * (c.universe || n) ? 'filled' : 'partly';
    }
    return (c.missing || []).indexOf(k) >= 0 ? 'missing' : 'filled';
  }
  function count(c, a, b) {
    var n = { filled: 0, partly: 0, missing: 0, notoff: 0 }, d = D(a), e = D(b);
    for (; d <= e; d = new Date(d.getTime() + 864e5)) { var s = dstate(c, key(d), d.getUTCDay()); if (s in n) n[s]++; }
    n.offered = n.filled + n.partly + n.missing; return n;
  }
  function pstate(n) {
    if (!n.offered) return n.notoff ? 'notoff' : 'future';
    if (n.filled === n.offered) return 'filled';
    if (!n.filled && !n.partly) return 'missing';
    return 'partly';
  }
  function est(c, sessions, names) {
    if (c.groups) return { calls: Math.ceil(names * sessions / (c.page || 10000)), how: X.fmt(names) + ' names × ' + X.fmt(sessions) + ' sessions ÷ ' + X.fmt(c.page) + ' bars a page (the code\'s page size)' };
    return { calls: sessions * (c.calls_per_session || 1), how: X.fmt(sessions) + ' sessions × ' + (c.calls_per_session || 1) + ' calls a session (the code\'s estimate)' };
  }
  function nights(c, calls) {
    if (!c.cap) return 'no day cap';
    var room = Math.max(0, c.cap - (c.used || 0));
    var n = calls <= room ? 1 : 1 + Math.ceil((calls - room) / c.cap);
    return n + ' night' + (n > 1 ? 's' : '') + ' under ' + c.provider + '\'s cap of ' + X.fmt(c.cap) + ' a day (' + X.fmt(c.used) + ' used today)';
  }
  var RANGE = {};
  function shared(k, c) {
    if (!RANGE[k]) {
      var g = {};
      (c.sets || []).forEach(function (s) {
        var b = document.querySelector('#universe .useg[data-tier="' + s.k + '"]');
        g[s.k] = !!b && b.getAttribute('data-on') === '1';
      });
      RANGE[k] = { a: null, b: null, aEnd: null, groups: g };
    }
    return RANGE[k];
  }
  function Cal(box, c) {
    this.box = box; this.c = c; this.level = 'y'; this.y = null; this.m = null; this.k = box.getAttribute('data-bf');
    this.R = shared(this.k, c); this.y0 = +c.from.slice(0, 4); this.y1 = +TODAY.slice(0, 4);
    var self = this; box.addEventListener('click', function (e) { self.click(e); }); this.draw();
  }
  Cal.prototype.cells = function () {
    var out = [], y, m, d;
    if (this.level === 'y') for (y = this.y0; y <= this.y1; y++) out.push({ a: y + '-01-01', b: y + '-12-31', lab: '' + y, kind: 'y', y: y });
    else if (this.level === 'm') for (m = 0; m < 12; m++) out.push({ a: this.y + '-' + pad(m + 1) + '-01', b: this.y + '-' + pad(m + 1) + '-' + lastDay(this.y, m), lab: MON[m], kind: 'm', m: m });
    else {
      var lead = (new Date(Date.UTC(this.y, this.m, 1)).getUTCDay() + 6) % 7;
      for (d = 0; d < lead; d++) out.push({ blank: true });
      for (d = 1; d <= lastDay(this.y, this.m); d++) { var k = this.y + '-' + pad(this.m + 1) + '-' + pad(d); out.push({ a: k, b: k, lab: '' + d, kind: 'd' }); }
    }
    return out;
  };
  Cal.prototype.range = function () {
    var yest = yesterday(), c = this.c;
    if (!this.R.a) return { lo: c.from, hi: yest, picked: false };
    var end = this.R.b || this.R.aEnd;
    return { lo: this.R.a < c.from ? c.from : this.R.a, hi: end > yest ? yest : end, picked: true, done: !!this.R.b };
  };
  Cal.prototype.draw = function () {
    var c = this.c, self = this, R = this.R;
    var crumbs = '<button type="button" class="bfcr" data-go="y"' + (this.level === 'y' ? ' aria-current="true"' : '') + '>All years</button>';
    if (this.level !== 'y') crumbs += '<span>›</span><button type="button" class="bfcr" data-go="m"' + (this.level === 'm' ? ' aria-current="true"' : '') + '>' + this.y + '</button>';
    if (this.level === 'd') crumbs += '<span>›</span><button type="button" class="bfcr" data-go="d" aria-current="true">' + MONL[this.m] + '</button>';
    var head = this.level === 'd' ? ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(function (w) { return '<span class="wd">' + w + '</span>'; }).join('') : '';
    var words = { filled: 'filled', partly: 'partly filled', missing: 'missing', notoff: 'not offered by the provider', nosess: 'no session', future: 'not yet' };
    var grid = this.cells().map(function (x) {
      if (x.blank) return '<span class="bfx blank"></span>';
      var n = null, st, sub = '';
      if (x.kind === 'd') st = dstate(c, x.a, D(x.a).getUTCDay());
      else {
        n = count(c, x.a, x.b); st = pstate(n);
        if (n.offered) sub = '<span class="bfp">' + Math.floor(pct(n.filled, n.offered)) + '%</span>';
        else if (st === 'notoff') sub = '<span class="bfp">not offered</span>';
      }
      var end = R.b || R.aEnd, inr = R.a && x.b >= R.a && x.a <= end;
      var cls = 'bfx s-' + st + (inr ? ' inr' : '') + (R.a && x.a <= R.a && x.b >= R.a ? ' rs' : '') + (end && x.a <= end && x.b >= end ? ' re' : '');
      var mb = n && n.offered ? '<span class="mb"><i class="f" style="width:' + pct(n.filled, n.offered) + '%"></i><i class="p" style="width:' + pct(n.partly, n.offered) + '%"></i></span>' : '';
      var aria = x.lab + (x.kind === 'd' ? ' ' + MON[self.m] + ' ' + self.y : '') + ': ' + (n && n.offered ? Math.floor(pct(n.filled, n.offered)) + '% filled' : words[st]);
      var dis = x.kind === 'd' && (st === 'nosess' || st === 'future') ? ' disabled' : '';
      var btn = '<button type="button" class="' + cls + '" data-a="' + x.a + '" data-b="' + x.b + '" data-kind="' + x.kind + '"' + (x.y ? ' data-y="' + x.y + '"' : '') +
        (x.m != null ? ' data-m="' + x.m + '"' : '') + ' aria-label="' + rcEsc(aria) + '"' + dis + '><span class="bfl">' + x.lab + '</span>' + sub + mb + '</button>';
      if (x.kind === 'd') return btn;
      return '<span class="bfcell">' + btn + '<button type="button" class="bfdn" data-dn="' + x.kind + '"' + (x.y ? ' data-y="' + x.y + '"' : '') + (x.m != null ? ' data-m="' + x.m + '"' : '') +
        ' aria-label="Open ' + x.lab + ': its ' + (x.kind === 'y' ? 'months' : 'days') + '" title="Open its ' + (x.kind === 'y' ? 'months' : 'days') + '">▸</button></span>';
    }).join('');
    var yest = yesterday(), T = count(c, c.from, yest);
    var target = '<div class="bftarget"><div class="bfth"><b>Target</b> ' + hd(c.from) + ' → ' + hd(yest) + ' · ' + X.fmt(T.offered) + ' weekdays · ' +
      '<span class="k f">' + pct(T.filled, T.offered) + '% filled</span> · <span class="k p">' + pct(T.partly, T.offered) + '% partly</span> · ' +
      '<span class="k m">' + pct(T.missing, T.offered) + '% missing</span></div><div class="bfbar" role="img" aria-label="' + pct(T.filled, T.offered) + '% of the target filled">' +
      '<i class="f" style="width:' + pct(T.filled, T.offered) + '%"></i><i class="p" style="width:' + pct(T.partly, T.offered) + '%"></i></div></div>';
    var rg = this.range(), P = rg.lo <= rg.hi ? count(c, rg.lo, rg.hi) : { offered: 0, filled: 0, partly: 0, missing: 0 };
    var todo = P.missing + P.partly, sets = (c.sets || []).filter(function (s) { return R.groups[s.k]; });
    var grp = '', plan;
    if (c.groups) {
      grp = '<div class="bfgrp" role="group" aria-label="Which names to fill"><span class="bfgl">Which names</span>' + (c.sets || []).map(function (s) {
        return '<button type="button" class="chipb bfg" data-g="' + rcEsc(s.k) + '" aria-pressed="' + (R.groups[s.k] ? 'true' : 'false') + '">' + rcEsc(s.label) + ' <span class="bfgn">' + X.fmt(s.n) + '</span></button>';
      }).join('') + '<span class="dim">C and D are not offered · the universe\'s sets are ticked at first</span></div>';
      var rows = sets.map(function (s) {
        var e = est(c, P.offered, s.n);
        return '<li><b>' + rcEsc(s.label) + '</b> · ' + X.fmt(s.n) + ' names · ≈' + X.fmt(e.calls) + ' ' + rcEsc(c.provider) + ' calls · ' + rcEsc(nights(c, e.calls)) + '</li>';
      }).join('');
      plan = '<div class="bfplan"><b>What a fill would fetch</b> · ' + (rg.picked ? hd(rg.lo) + ' → ' + hd(rg.hi) + (rg.done ? '' : ' · press another cell to set the end') : 'no range picked: the whole history, ' + hd(rg.lo) + ' → ' + hd(rg.hi)) +
        ' · ' + X.fmt(P.offered) + ' weekdays a name, ' + X.fmt(todo) + ' of them not filled' + (rows ? '<ul>' + rows + '</ul>' : '<span class="dim">tick at least one set above</span>') +
        '<span class="dim">an estimate, not a measurement · holidays are not marked</span></div>';
    } else {
      var e2 = est(c, todo, 0);
      plan = '<div class="bfpick">' + (rg.picked ? '<b>Picked</b> ' + hd(rg.lo) + ' → ' + hd(rg.hi) + (rg.done ? '' : ' · press another cell to set the end') + '<br>' : '') +
        (todo ? X.fmt(todo) + ' sessions to fetch · ≈ ' + X.fmt(e2.calls) + ' calls: ' + rcEsc(e2.how) + ' · ' + rcEsc(nights(c, e2.calls)) : 'nothing to fetch in this range') +
        '<span class="dim">an estimate, not a measurement</span></div>';
    }
    var ready = C.fill_enabled && rg.picked && rg.done && todo > 0 && (!c.groups || sets.length);
    this.box.innerHTML = '<div class="bfhd"><span class="bft">Backfill calendar · <code>' + rcEsc(c.dataset) + '</code> · ' + rcEsc(c.provider) + '</span>' +
      '<button type="button" class="btn bfclose">Close</button></div><p class="bff1">' + rcEsc(c.first) + '. Press a cell to pick the start of the range, another to pick its end; ▸ on a cell opens its months or days.</p>' +
      '<div class="bfctl"><nav class="bfcrumb" aria-label="Calendar level">' + crumbs + '</nav>' + (R.a ? '<button type="button" class="chipb bfreset">Clear the range</button>' : '') + '</div>' +
      grp + '<div class="bfgrid l-' + this.level + '">' + head + grid + '</div>' +
      '<div class="bflegend"><span class="lg s-filled">filled</span><span class="lg s-partly">partly filled</span><span class="lg s-missing">missing</span>' +
      '<span class="lg s-notoff">not offered by the provider</span><span class="lg s-nosess">no session</span><span class="dim">weekends are not sessions; holidays are not marked</span></div>' +
      target + plan + '<div class="bfgo"><button type="button" class="btn go bfrun"' + (ready ? '' : ' disabled') + '>Fill this range' + (c.groups ? ' · ' + sets.length + ' of ' + (c.sets || []).length + ' sets' : '') + '</button>' +
      '<span class="dim">' + rcEsc(C.fill_enabled ? 'puts one request in the queue for the range and the sets' : C.fill_off_why) + '</span></div>';
  };
  Cal.prototype.run = function (btn) {
    var c = this.c, rg = this.range(), R = this.R, on = {}, body;
    if (!C.fill_enabled || !rg.picked || !rg.done) return;
    (c.sets || []).forEach(function (s) { if (R.groups[s.k]) on[s.k] = true; });
    body = c.groups ? X.scopeOf(on, X.applied()) : {};
    body.backfill_from = rg.lo; body.backfill_to = rg.hi;
    X.queue(btn, c.dataset, body);
  };
  Cal.prototype.click = function (e) {
    var t = e.target.closest('button'); if (!t || !this.box.contains(t)) return;
    if (t.classList.contains('bfclose')) {
      this.box.hidden = true;
      var b = document.querySelector('.bfbtn[data-bf="' + this.k + '"]'); if (b) { b.setAttribute('aria-expanded', 'false'); b.focus(); }
      return;
    }
    if (t.classList.contains('bfrun')) { this.run(t); return; }
    if (t.classList.contains('bfdn')) {
      if (t.getAttribute('data-dn') === 'y') { this.level = 'm'; this.y = +t.getAttribute('data-y'); } else { this.level = 'd'; this.m = +t.getAttribute('data-m'); }
      this.draw(); return;
    }
    if (t.classList.contains('bfg')) { var g = t.getAttribute('data-g'); this.R.groups[g] = !this.R.groups[g]; this.draw(); return; }
    if (t.classList.contains('bfreset')) { this.R.a = this.R.b = this.R.aEnd = null; this.draw(); return; }
    if (t.getAttribute('data-go')) { this.level = t.getAttribute('data-go'); if (this.level === 'y') this.y = this.m = null; this.draw(); return; }
    if (!t.classList.contains('bfx')) return;
    if (!this.R.a || this.R.b) { this.R.a = t.getAttribute('data-a'); this.R.b = null; this.R.aEnd = t.getAttribute('data-b'); }
    else { var a = this.R.a, b2 = t.getAttribute('data-b'); if (b2 < a) { this.R.b = this.R.aEnd; this.R.a = t.getAttribute('data-a'); } else this.R.b = b2; }
    this.draw();
  };
  var made = {};
  function open(k, g) {
    var box = document.getElementById('bf-' + k), c = C.calendars[k], b = document.querySelector('.bfbtn[aria-controls="bf-' + k + '"]');
    if (!box || !c) return;
    if (g) { var R = shared(k, c); (c.sets || []).forEach(function (s) { R.groups[s.k] = s.k === g; }); }
    box.hidden = false; if (b) b.setAttribute('aria-expanded', 'true');
    if (!made[k]) made[k] = new Cal(box, c); else made[k].draw();
    if (g && box.scrollIntoView) box.scrollIntoView({ block: 'nearest' });
  }
  document.addEventListener('click', function (e) {
    var f = e.target.closest && e.target.closest('#ctl .fopen[data-bf]');
    if (f) { open(f.getAttribute('data-bf'), f.getAttribute('data-g')); return; }
    var b = e.target.closest && e.target.closest('#ctl .bfbtn');
    if (!b) return;
    var k = b.getAttribute('data-bf'), box = document.getElementById('bf-' + k);
    if (!box) return;
    if (box.hidden) open(k, null); else { box.hidden = true; b.setAttribute('aria-expanded', 'false'); }
  });
})();

/* The journey picks: a line, a table or a board in a file's card lights everything tied to it in all four columns;
   a table's ▸ opens its columns (from the code's schema). */
(function () {
  var D = window.RC_FLOW;
  if (!D || !document.querySelector) return;
  var TR = {};
  D.edges.tabBoard.forEach(function (e) { (TR[e[0]] = TR[e[0]] || []).push(e[1]); });
  var KINDS = ['row', 'tab', 'bd'], ST = [];
  function state(j) {
    for (var i = 0; i < ST.length; i++) if (ST[i].j === j) return ST[i].s;
    var s = { row: {}, tab: {}, bd: {} }; ST.push({ j: j, s: s }); return s;
  }
  function model(j) {
    var rows = [].slice.call(j.querySelectorAll('tr.jr')), tabs = {};
    [].forEach.call(j.querySelectorAll('.tch[data-t]'), function (t) { tabs[t.getAttribute('data-t')] = 1; });
    return { rows: rows, tabs: Object.keys(tabs), R: rows.map(function (r) { return (r.getAttribute('data-tabs') || '').split(' ').filter(Boolean); }) };
  }
  function reach(m, kind, id) {
    var L = { row: {}, tab: {}, bd: {} };
    function rowsOf(t) { m.R.forEach(function (ts, i) { if (ts.indexOf(t) >= 0) L.row[i] = 1; }); }
    if (kind === 'row') { L.row[id] = 1; (m.R[id] || []).forEach(function (t) { L.tab[t] = 1; (TR[t] || []).forEach(function (b) { L.bd[b] = 1; }); }); }
    else if (kind === 'tab') { L.tab[id] = 1; (TR[id] || []).forEach(function (b) { L.bd[b] = 1; }); rowsOf(id); }
    else { L.bd[id] = 1; m.tabs.forEach(function (t) { if ((TR[t] || []).indexOf(id) >= 0) { L.tab[t] = 1; rowsOf(t); } }); }
    return L;
  }
  function paint(j) {
    var s = state(j), m = model(j), L = null;
    KINDS.forEach(function (k) {
      var ids = Object.keys(s[k]); if (!ids.length) return;
      var U = { row: {}, tab: {}, bd: {} };
      ids.forEach(function (id) { var r = reach(m, k, k === 'row' ? +id : id); KINDS.forEach(function (c) { for (var x in r[c]) U[c][x] = 1; }); });
      if (!L) L = U; else KINDS.forEach(function (c) { for (var x in L[c]) if (!U[c][x]) delete L[c][x]; });
    });
    j.classList.toggle('picking', !!L);
    m.rows.forEach(function (r, i) {
      var p = !!s.row[i], lit = !!L && !!L.row[i];
      r.classList.toggle('sel', p); r.classList.toggle('lit', lit && !p); r.classList.toggle('fade', !!L && !lit);
      r.setAttribute('aria-expanded', p ? 'true' : 'false');
      var d = document.getElementById(r.getAttribute('aria-controls')); if (d) d.hidden = !p;
    });
    [].forEach.call(j.querySelectorAll('.tch[data-t]'), function (t) {
      var id = t.getAttribute('data-t'), p = !!s.tab[id], lit = !!L && !!L.tab[id];
      t.classList.toggle('picked', p); t.classList.toggle('lit', lit); t.classList.toggle('fade', !!L && !lit);
      t.querySelector('.tpick').setAttribute('aria-pressed', p ? 'true' : 'false');
    });
    [].forEach.call(j.querySelectorAll('.btree li.on'), function (li) {
      var id = li.getAttribute('data-b'), p = !!s.bd[id], lit = !!L && !!L.bd[id];
      li.classList.toggle('picked', p); li.classList.toggle('lit', lit); li.classList.toggle('fade', !!L && !lit);
      li.setAttribute('aria-pressed', p ? 'true' : 'false');
    });
  }
  function toggle(j, kind, id) { var s = state(j); if (s[kind][id]) delete s[kind][id]; else s[kind][id] = 1; paint(j); }
  document.addEventListener('click', function (e) {
    var j = e.target.closest && e.target.closest('#ctl .journey'); if (!j) return;
    var op = e.target.closest('.topen');
    if (op) { var t = op.closest('.tch'), c = t.querySelector('.tcols'), o = c.hidden; c.hidden = !o; t.classList.toggle('open', o); op.setAttribute('aria-expanded', o ? 'true' : 'false'); return; }
    var tp = e.target.closest('.tpick'); if (tp) { toggle(j, 'tab', tp.getAttribute('data-t')); return; }
    var li = e.target.closest('.btree li.on'); if (li) { toggle(j, 'bd', li.getAttribute('data-b')); return; }
    if (e.target.closest('td.ssdc, td.bdc, .jpanel')) return;
    var r = e.target.closest('tr.jr'); if (!r) return;
    var x = e.target.closest('details,button,a'); if (x && r.contains(x)) return;
    toggle(j, 'row', String(model(j).rows.indexOf(r)));
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var t = e.target, j = t.closest && t.closest('#ctl .journey'); if (!j) return;
    if (t.matches('tr.jr')) { e.preventDefault(); toggle(j, 'row', String(model(j).rows.indexOf(t))); }
    else if (t.matches('.btree li.on')) { e.preventDefault(); toggle(j, 'bd', t.getAttribute('data-b')); }
  });
})();

/* The Health tiles filter the Fetch section, or open the place their fact lives (Karar 85 A, 88 A). */
(function () {
  var X = window.CTLX;
  if (!X) return;
  var NAME = { fresh: 'that are fresh', late: 'that are late', stale: 'that are stale', never: 'that never ran', problems: 'with open problems',
               session: 'behind on the newest session', cap: 'nearest their provider\'s day cap', gateway: 'waiting on the IBKR gateway' };
  var bar = document.getElementById('ffilter'), empty = document.getElementById('ffempty');
  if (!bar || !empty) return;
  function match(d, f) {
    if (f === 'problems') return +(d.getAttribute('data-prob') || 0) > 0;
    if (f === 'session' || f === 'gateway') return (' ' + (d.getAttribute('data-tags') || '') + ' ').indexOf(' ' + f + ' ') >= 0;
    return d.getAttribute('data-st') === f;
  }
  function filter(f) {
    var ds = [].slice.call(document.querySelectorAll('#sec-fetch .fgroup > .dset')), n = 0;
    ds.forEach(function (d) { var ok = !f || match(d, f); d.hidden = !ok; if (ok) n++; });
    [].forEach.call(document.querySelectorAll('#sec-fetch .fgroup'), function (g) { g.hidden = !!f && !g.querySelector('.dset:not([hidden])'); });
    var u = document.getElementById('universe'), lg = document.querySelector('#sec-fetch .flegend');
    if (u) u.hidden = !!f; if (lg) lg.hidden = !!f;
    [].forEach.call(document.querySelectorAll('#sec-health .tile[data-f]'), function (t) { if (NAME[t.getAttribute('data-f')]) t.setAttribute('aria-pressed', f && t.getAttribute('data-f') === f ? 'true' : 'false'); });
    if (!f) { bar.hidden = true; empty.hidden = true; return; }
    bar.hidden = false;
    bar.innerHTML = '<span>Fetch shows the datasets <b>' + rcEsc(NAME[f]) + '</b>: ' + n + ' of ' + ds.length + ' rows</span><button type="button" class="lnk" data-ffclear>Show every dataset</button>';
    empty.hidden = !!n;
    if (!n) empty.innerHTML = '<b>Nothing to show:</b> none of the ' + ds.length + ' rows matches. <button type="button" class="lnk" data-ffclear>Show every dataset</button>';
  }
  var cur = null;
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    if (e.target.closest('#ctl [data-ffclear]')) { cur = null; filter(null); return; }
    var t = e.target.closest('#sec-health .tile[data-f]'); if (!t) return;
    var f = t.getAttribute('data-f');
    if (f === 'universe') { if (cur) { cur = null; filter(null); } X.scrollTo(document.getElementById('universe')); return; }
    if (f === 'queue' || f === 'builder') { var q = document.getElementById('sec-queue'); if (q) { q.open = true; X.scrollTo(q); } return; }
    if (f === 'cap') { var p = document.getElementById('sec-providers'); if (p) { p.open = true; X.scrollTo(p); } return; }
    cur = cur === f ? null : f; filter(cur);
    var s = document.getElementById('sec-fetch'); if (cur && s) { s.open = true; X.scrollTo(s); }
  });
  [].forEach.call(document.querySelectorAll('#sec-health .tile[data-f]'), function (t) { if (NAME[t.getAttribute('data-f')]) t.setAttribute('aria-pressed', 'false'); });
})();

/* The flow diagram is the filter (design v28). A press picks a box and a second press drops it; its own lines and
   the boxes they reach light up, the rest fades, and the Data section below follows. Same column = any of them;
   different columns = all must hold. A picked board fills the info box under the diagram. Every box and line comes
   from window.RC_FLOW, which the page builds from the code; a line to a board not migrated yet is dashed. */
(function () {
  var D = window.RC_FLOW;
  if (!D) return;
  var svg = document.getElementById('flow2');
  if (!svg || !svg.parentNode || !document.createElementNS) return;
  var wrap = svg.parentNode, NS = 'http://www.w3.org/2000/svg';
  var H = 24, GAP = 6, ROW = 24, BH = 20, IN0 = 13, GH = 18, TOP = 32, PAD = 6, GMIN = 64, GMAX = 210, BW = 44;
  var BODY = 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif', MONOF = 'ui-monospace,Menlo,Consolas,monospace';
  var canvas = document.createElement('canvas'), cv = canvas.getContext ? canvas.getContext('2d') : null;
  function tw(t, f) {
    if (!cv) return Math.ceil(String(t).length * 7);
    cv.font = f || ('11.5px ' + BODY); return Math.ceil(cv.measureText(t).width);
  }
  function hw(t) {
    if (!cv) return Math.ceil(String(t).length * 8);
    cv.font = '700 9.5px ' + BODY; return Math.ceil(cv.measureText(t.toUpperCase()).width + t.length * 0.76);
  }

  var HUE = {}, fileOf = {}, tablesOf = {}, stateOf = {}, open = {};
  D.files.forEach(function (f) {
    HUE[f.name] = f.line; tablesOf[f.name] = f.tables; stateOf[f.name] = f.health.state;
    f.tables.forEach(function (t) { fileOf[t] = f.name; });
  });
  var OUT = {}, IN = {}, LOG = [];
  function link(a, b) { (OUT[a] = OUT[a] || []).push(b); (IN[b] = IN[b] || []).push(a); LOG.push([a, b]); }
  D.edges.srcStep.forEach(function (e) { link('src:' + e[0], 'step:' + e[1]); });
  D.edges.stepTab.forEach(function (e) { link('step:' + e[0], 'tab:' + e[1]); });
  D.edges.tabBoard.forEach(function (e) { link('tab:' + e[0], 'bd:' + e[1]); });
  (D.edges.fileBoard || []).forEach(function (e) { link('file:' + e[0], 'bd:' + e[1]); });

  var picks = { src: {}, step: {}, file: {}, bd: {}, hs: {} };
  var STATE = { ok: 'every dataset fresh', warn: 'some late', none: 'never ran', old: 'being cut' };
  function colOf(id) { return id.indexOf('tab:') === 0 ? 'file' : id.split(':')[0]; }
  function seedsOf(id) {
    if (id.indexOf('file:') !== 0) return [id];
    return [id].concat((tablesOf[id.slice(5)] || []).map(function (t) { return 'tab:' + t; }));
  }
  function reach(seeds) {
    var R = {};
    seeds.forEach(function (s) { R[s] = 1; });
    [OUT, IN].forEach(function (M) {
      var seen = {}, q = seeds.slice();
      while (q.length) {
        (M[q.pop()] || []).forEach(function (b) { if (!seen[b]) { seen[b] = 1; R[b] = 1; q.push(b); } });
      }
    });
    return R;
  }
  function litSet() {
    var L = null;
    Object.keys(picks).forEach(function (k) {
      var ids = Object.keys(picks[k]);
      if (!ids.length) return;
      var U = {};
      ids.forEach(function (id) {
        var seeds = [];
        if (k === 'hs') D.files.forEach(function (f) { if (f.health.state === id) seeds = seeds.concat(seedsOf('file:' + f.name)); });
        else seeds = seedsOf(id);
        var R = reach(seeds);
        for (var x in R) U[x] = 1;
      });
      if (L === null) L = U;
      else for (var y in L) if (!U[y]) delete L[y];
    });
    if (L) D.files.forEach(function (f) {
      if (f.tables.some(function (t) { return L['tab:' + t]; })) L['file:' + f.name] = 1;
    });
    return L;
  }

  function el(name, attrs, parent) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function text(parent, x, y, s, cls, style) {
    var t = el('text', { x: x, y: y, class: cls || null, style: style || null }, parent);
    t.textContent = s;
    return t;
  }

  function layout(width) {
    var srcW = 0, stepW = 0, fileW = 0, bdW = 0;
    D.sourceGroups.forEach(function (g) {
      srcW = Math.max(srcW, hw(g.group));
      g.items.forEach(function (s) { s._w = tw(s.name + (s.plan ? ' · plan' : ''), '600 12px ' + BODY) + 35; srcW = Math.max(srcW, IN0 + s._w); });
    });
    srcW += 4;
    D.steps.forEach(function (s) { s._w = tw(s.label) + 20; stepW = Math.max(stepW, s._w); });
    D.files.forEach(function (f) {
      fileW = Math.max(fileW, 24 + tw(f.name, '600 11px ' + MONOF) + 12 + BW + 4);
      f.tables.forEach(function (t) { fileW = Math.max(fileW, 18 + 10 + tw(t, '11px ' + MONOF) + 10); });
    });
    D.boardGroups.forEach(function (g) {
      bdW = Math.max(bdW, hw(g.group));
      g.boards.forEach(function (b) { b._w = tw(b.name, '600 12px ' + BODY) + 20; bdW = Math.max(bdW, IN0 + b._w); });
    });
    bdW += 4;
    var gap = Math.max(GMIN, Math.min(GMAX, Math.floor((width - 2 * PAD - srcW - stepW - fileW - bdW) / 3)));
    var X = { src: PAD }; X.step = X.src + srcW + gap; X.file = X.step + stepW + gap; X.bd = X.file + fileW + gap;
    var pos = {}, y = TOP, groups = [];
    D.sourceGroups.forEach(function (g) {
      var gr = { x: X.src, y: y, name: g.group, rows: [] }; y += GH;
      g.items.forEach(function (s) {
        var id = 'src:' + g.group + '|' + s.name;
        pos[id] = { x: X.src, y: y, h: ROW, cy: y + ROW / 2, w: s._w, ox: X.src + IN0 + s._w, item: s, kind: 'src' };
        gr.rows.push(pos[id].cy); y += ROW;
      });
      groups.push(gr); y += 7;
    });
    var bottom = y;
    y = TOP;
    D.steps.forEach(function (s) {
      pos['step:' + s.id] = { x: X.step, y: y, w: s._w, h: H, cy: y + H / 2, ix: X.step, ox: X.step + s._w, kind: 'step' };
      y += H + GAP;
    });
    bottom = Math.max(bottom, y);
    y = TOP;
    D.files.forEach(function (f) {
      pos['file:' + f.name] = { x: X.file, y: y, w: fileW, h: H, cy: y + H / 2, ix: X.file, ox: X.file + fileW, kind: 'file' };
      y += H + GAP;
      if (open[f.name]) {
        f.tables.forEach(function (t) {
          pos['tab:' + t] = { x: X.file + 18, y: y, w: fileW - 18, h: 20, cy: y + 10, ix: X.file + 18, ox: X.file + fileW, kind: 'tab' };
          y += 24;
        });
        y += 4;
      }
    });
    bottom = Math.max(bottom, y);
    y = TOP;
    D.boardGroups.forEach(function (g) {
      var gr = { x: X.bd, y: y, name: g.group, rows: [] }; y += GH;
      g.boards.forEach(function (b) {
        pos['bd:' + b.name] = { x: X.bd, y: y, h: ROW, cy: y + ROW / 2, w: b._w, ix: X.bd + IN0, item: b, kind: 'bd' };
        gr.rows.push(y + ROW / 2); y += ROW;
      });
      groups.push(gr); y += 7;
    });
    bottom = Math.max(bottom, y);
    return { pos: pos, groups: groups, X: X, width: Math.ceil(X.bd + bdW + PAD), height: bottom + 6 };
  }

  function edges() {
    var out = {}, list = [];
    function add(a, b, hue, la, lb, planned) {
      var k = a + '>' + b;
      if (!out[k]) { out[k] = { a: a, b: b, hue: hue, under: [], planned: true }; list.push(out[k]); }
      out[k].under.push([la, lb]);
      if (!planned) out[k].planned = false;
    }
    D.edges.srcStep.forEach(function (e) { add('src:' + e[0], 'step:' + e[1], '#8a9a6a', 'src:' + e[0], 'step:' + e[1]); });
    D.edges.stepTab.forEach(function (e) {
      var f = fileOf[e[1]];
      add('step:' + e[0], open[f] ? 'tab:' + e[1] : 'file:' + f, HUE[f] || '#98a0ad', 'step:' + e[0], 'tab:' + e[1]);
    });
    D.edges.tabBoard.forEach(function (e) {
      var f = fileOf[e[0]];
      add(open[f] ? 'tab:' + e[0] : 'file:' + f, 'bd:' + e[1], HUE[f] || '#98a0ad', 'tab:' + e[0], 'bd:' + e[1],
          e[2] === 'planned');
    });
    (D.edges.fileBoard || []).forEach(function (e) {
      add('file:' + e[0], 'bd:' + e[1], HUE[e[0]] || '#98a0ad', 'file:' + e[0], 'bd:' + e[1]);
    });
    return list;
  }

  var readers = {};
  D.edges.tabBoard.forEach(function (e) { readers[e[1]] = 1; });

  function render() {
    var width = wrap.clientWidth || 1500;
    var L = layout(width), pos = L.pos;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute('viewBox', '0 0 ' + L.width + ' ' + L.height);
    svg.setAttribute('width', L.width);
    svg.setAttribute('height', L.height);
    var title = el('title', { id: 'flow2-title' }, svg);
    title.textContent = 'Where the data comes from, which step builds it, which file holds it, which board reads it';
    [['Source', L.X.src], ['Plan step', L.X.step], ['SSD file · ▸ opens its tables', L.X.file], ['Board that reads it', L.X.bd]]
      .forEach(function (h) { text(svg, h[1], 16, h[0], 'fh'); });

    var gl = el('g', { class: 'tlines' }, svg);
    L.groups.forEach(function (g) {
      text(svg, g.x, g.y + 12, g.name, 'grph');
      var last = g.rows[g.rows.length - 1];
      el('path', { class: 'tline', d: 'M' + (g.x + 5) + ',' + (g.y + 16) + ' V' + last + g.rows.map(function (r) {
        return ' M' + (g.x + 5) + ',' + r + ' H' + (g.x + 11);
      }).join('') }, gl);
    });

    var ge = el('g', { class: 'flow-edges' }, svg);
    edges().forEach(function (e) {
      var A = pos[e.a], B = pos[e.b];
      if (!A || !B) return;
      var x1 = A.ox, y1 = A.cy, x2 = B.ix, y2 = B.cy, mx = (x1 + x2) / 2;
      var p = el('path', {
        class: 'flow-edge' + (e.planned ? ' planned' : ''), stroke: e.hue,
        'stroke-dasharray': e.planned ? '5 4' : null,
        d: 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2
      }, ge);
      p._under = e.under;
    });

    function node(id, label, cls) {
      return el('g', { class: 'flow-node ' + cls, tabindex: '0', role: 'button', 'aria-pressed': 'false', 'data-node': id,
                       'aria-label': label }, svg);
    }
    D.sourceGroups.forEach(function (g) {
      g.items.forEach(function (s) {
        var id = 'src:' + g.group + '|' + s.name, p = pos[id];
        var n = node(id, s.name + ', ' + g.group + (s.what ? ', ' + s.what : '') + (s.plan ? ', in the plan, not built' : '') + ': press to pick',
                     'src' + (s.plan ? ' plan' : ''));
        el('rect', { class: 'sbox', x: p.x + IN0, y: p.cy - BH / 2, width: p.w, height: BH, rx: 5,
                     style: s.plan ? null : 'fill:' + s.color }, n);
        el('path', { class: 'ico', d: s.icon, transform: 'translate(' + (p.x + IN0 + 6) + ',' + (p.cy - 7) + ') scale(.5833)' }, n);
        text(n, p.x + IN0 + 26, p.cy + 4, s.name + (s.plan ? ' · plan' : ''), 'bt');
      });
    });
    D.steps.forEach(function (s) {
      var p = pos['step:' + s.id], n = node('step:' + s.id, 'Step ' + s.label + ': press to pick', 'step');
      el('rect', { x: p.x, y: p.y, width: p.w, height: p.h, rx: 6 }, n);
      text(n, p.x + 10, p.cy + 4, s.label);
    });
    D.files.forEach(function (f) {
      var p = pos['file:' + f.name], h = f.health, st = h.state;
      var n = node('file:' + f.name, f.name + (f.old ? ', the old store, being cut' : '') + ': press to pick',
                   'file' + (open[f.name] ? ' isopen' : '') + (f.old ? ' old' : ''));
      el('rect', { class: 'box', x: p.x, y: p.y, width: p.w, height: p.h, rx: 6, style: 'stroke:' + f.line,
                   'stroke-dasharray': f.old ? '4 3' : null }, n);
      text(n, p.x + 22, p.cy + 4, f.name, 'mono');
      if (f.tables.length) {
        var tg = el('g', { class: 'flow-tog', tabindex: '0', role: 'button', 'aria-expanded': open[f.name] ? 'true' : 'false',
                           'data-file': f.name, 'aria-label': (open[f.name] ? 'Close ' : 'Open ') + 'the ' + f.tables.length + ' tables of ' + f.name }, n);
        el('rect', { class: 'hit', x: p.x + 1, y: p.y + 1, width: 19, height: p.h - 2, rx: 5 }, tg);
        text(tg, p.x + 7, p.cy + 4, open[f.name] ? '▾' : '▸', 'car');
      }
      var fill = st === 'ok' ? '#1a7f37' : st === 'warn' ? '#9a6700' : st === 'old' ? '#b42318' : '#6b7280';
      var hb = el('g', { class: 'flow-hb', tabindex: '0', role: 'button', 'data-state': st,
                         'aria-pressed': picks.hs[st] ? 'true' : 'false',
                         'aria-label': 'Health of ' + f.name + ': ' + (h.label || (h.fresh + ' of ' + h.total + ' fresh')) +
                                       '. Press to show every file whose health is: ' + STATE[st] }, n);
      el('rect', { x: p.x + p.w - BW - 4, y: p.y + 3, width: BW, height: p.h - 6, rx: 5, style: 'fill:' + fill }, hb);
      text(hb, p.x + p.w - BW / 2 - 4, p.cy + 4, h.label || (h.fresh + '/' + h.total), 'hbt').setAttribute('text-anchor', 'middle');
      if (!open[f.name]) return;
      f.tables.forEach(function (t, i) {
        var q = pos['tab:' + t], m = node('tab:' + t, 'Table ' + t + ' in ' + f.name + ': press to pick', 'tab');
        el('rect', { x: q.x, y: q.y, width: q.w, height: q.h, rx: 5, style: 'fill:' + f.tables_color[i] }, m);
        text(m, q.x + 9, q.cy + 4, t, 'mono');
      });
    });
    D.boardGroups.forEach(function (g) {
      g.boards.forEach(function (b) {
        var id = 'bd:' + b.name, p = pos[id], on = !!readers[b.name];
        var n = node(id, b.name + ', ' + g.group + (on ? '' : ', reads none of these files yet') + ': press to pick', 'bd' + (on ? '' : ' off'));
        el('rect', { class: 'bbox', x: p.x + IN0, y: p.cy - BH / 2, width: p.w, height: BH, rx: 5, style: on ? 'fill:' + b.color : null }, n);
        text(n, p.x + IN0 + 10, p.cy + 4, b.name, 'bt');
      });
    });
    paint();
  }

  var CARD = { '.store (old)': '.store' };
  function label(id) {
    var c = colOf(id), v = id.slice(id.indexOf(':') + 1);
    if (c === 'src') return v.split('|')[1];
    if (c === 'step') return 'step ' + v;
    return v;
  }
  var wasPicked = false;
  function RM() { return window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'; }
  function paint() {
    var L = litSet();
    svg.querySelectorAll('.flow-node').forEach(function (n) {
      var id = n.getAttribute('data-node'), lit = !L || !!L[id];
      n.classList.toggle('dim', !lit);
      n.setAttribute('aria-pressed', picks[colOf(id)][id] ? 'true' : 'false');
    });
    svg.querySelectorAll('.flow-hb').forEach(function (b) {
      b.setAttribute('aria-pressed', picks.hs[b.getAttribute('data-state')] ? 'true' : 'false');
    });
    svg.querySelectorAll('.flow-edge').forEach(function (p) {
      var on = L && p._under.some(function (u) { return L[u[0]] && L[u[1]]; });
      p.classList.toggle('flow-on', !!on);
      p.classList.toggle('flow-dim', !!L && !on);
    });
    var cards = [].slice.call(document.querySelectorAll('details.group[data-file]')), shown = 0;
    cards.forEach(function (c) {
      var name = c.getAttribute('data-file'), key = name === '.store' ? 'file:.store (old)' : 'file:' + name;
      var ok = !L || !!L[key];
      c.hidden = !ok;
      if (ok) shown++;
    });
    var names = [];
    Object.keys(picks).forEach(function (k) {
      Object.keys(picks[k]).forEach(function (id) { names.push(k === 'hs' ? 'health: ' + STATE[id] : label(id)); });
    });
    var cs = document.getElementById('cardsum');
    info();
    if (cs) cs.textContent = names.length
      ? 'Filtered by the flow diagram: ' + shown + ' of ' + cards.length + ' files · ' + names.join(' · ')
      : 'every file is closed when the page loads · the flow diagram above filters this list';
    var dsum = document.getElementById('datasum');
    if (dsum) dsum.textContent = names.length
      ? 'filtered by the flow diagram: ' + shown + ' of ' + cards.length + ' files · ' + names.join(' · ')
      : cards.length + ' files · how full each dataset is, the journey of each step, who reads which table · the flow diagram filters this list';
    var bm = document.getElementById('bdmatch'), de = document.getElementById('dataempty');
    if (bm) {
      bm.hidden = !names.length;
      bm.innerHTML = !names.length ? '' : shown
        ? '<b>' + shown + ' of ' + cards.length + ' files match</b> · <button type="button" class="lnk" id="bdopen">open them ↓</button> · <button type="button" class="lnk" data-clearpicks>clear the picks</button>'
        : '<b>No file matches these picks.</b> <button type="button" class="lnk" data-clearpicks>Clear the picks</button>';
    }
    if (de) de.hidden = !names.length || !!shown;
    if (names.length && !wasPicked) {
      var ds = document.getElementById('sec-data');
      if (ds) { ds.open = true; ds.querySelector('summary').scrollIntoView({ block: 'start', behavior: RM() }); }
    }
    wasPicked = names.length > 0;
  }

  /* item 6: a picked board says what it reads · its catalog needs, its tier set and how many names */
  var BI = D.boardInfo || {}, GRP = {}, BC = {};
  D.boardGroups.forEach(function (g) { g.boards.forEach(function (b) { GRP[b.name] = g.group; BC[b.name] = b.color; }); });
  var NEEDF = { identity: 'spine.duckdb', prices: 'prices.duckdb', fundamentals: 'fundamentals.duckdb', ownership: 'ownership.duckdb',
                options: 'options.duckdb', macro: 'market.duckdb', market: 'market.duckdb' };
  function filesOf(name) {
    var fs = [];
    (IN['bd:' + name] || []).forEach(function (x) {
      var f = x.indexOf('tab:') === 0 ? fileOf[x.slice(4)] : x.slice(5);
      if (f && fs.indexOf(f) < 0) fs.push(f);
    });
    return D.files.map(function (f) { return f.name; }).filter(function (f) { return fs.indexOf(f) >= 0; });
  }
  function info() {
    var box = document.getElementById('bdinfo');
    if (!box) return;
    var ids = Object.keys(picks.bd);
    if (!ids.length) {
      box.className = 'bdinfo empty';
      box.innerHTML = '<p>Press a board on the right of the diagram to see what it reads: its data needs and the files they come from.</p>';
      return;
    }
    box.className = 'bdinfo';
    box.innerHTML = ids.map(function (id) {
      var n = id.slice(3), b = BI[n] || {}, fs = filesOf(n), needs = b.needs || [];
      var fch = fs.length ? fs.map(function (f) {
        return '<span class="bfile" style="background:' + (HUE[f] || '#8b9098') + '">' + rcEsc(f) + '</span>';
      }).join('') : '<span class="dim">none of these files</span>';
      var nch = needs.length ? needs.map(function (x) {
        var f = NEEDF[x.split('.')[0]];
        return '<span class="nch' + (f ? '' : ' nf') + '"' + (f ? ' style="background:' + HUE[f] + '"' : ' title="joined from several files"') + '>' + rcEsc(x) + '</span>';
      }).join('') : '<span class="dim">' + rcEsc(b.note || 'no data need') + '</span>';
      var tier = b.declared ? 'declared in the board (READS): measured' : 'planned: the board is not migrated yet';
      return '<div class="bdi"><div class="bdih"><span class="bch" style="background:' + (BC[n] || '#6d7585') + '">' + rcEsc(n) + '</span>' +
        '<span class="dim">' + rcEsc(GRP[n] || '') + '</span></div>' +
        '<div class="bdir"><span class="jk">reads these files</span><span class="bdiv">' + fch + '</span></div>' +
        '<div class="bdir"><span class="jk">needs · from the catalog</span><span class="bdiv">' + nch + '</span></div>' +
        '<div class="bdir"><span class="jk">record</span><span class="bdiv">' + tier + '</span></div></div>';
    }).join('');
  }

  function hit(e) {
    var t = e.target.closest ? e.target.closest('.flow-tog, .flow-hb, .flow-node') : null;
    if (!t) return;
    if (t.classList.contains('flow-tog')) {
      var f = t.getAttribute('data-file');
      open[f] = !open[f];
      if (!open[f]) (tablesOf[f] || []).forEach(function (x) { delete picks.file['tab:' + x]; });
      render();
      var again = svg.querySelector('.flow-tog[data-file="' + f.replace(/"/g, '\\"') + '"]');
      if (again) again.focus();
      return;
    }
    if (t.classList.contains('flow-hb')) {
      var s = t.getAttribute('data-state');
      if (picks.hs[s]) delete picks.hs[s]; else picks.hs[s] = 1;
      paint();
      return;
    }
    var id = t.getAttribute('data-node'), c = colOf(id);
    if (picks[c][id]) delete picks[c][id]; else picks[c][id] = 1;
    paint();
  }
  svg.addEventListener('click', hit);
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-clearpicks]')) {
      Object.keys(picks).forEach(function (k) { picks[k] = {}; });
      paint();
      return;
    }
    if (e.target.closest('#bdopen')) {
      var ds = document.getElementById('sec-data');
      if (!ds) return;
      ds.open = true;
      ds.querySelectorAll('details.group[data-file]').forEach(function (c) { if (!c.hidden) c.open = true; });
      var first = ds.querySelector('details.group[data-file]:not([hidden])');
      (first || ds).scrollIntoView({ block: 'start', behavior: RM() });
    }
  });
  svg.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); hit(e); }
  });
  var sec = svg.closest('details');
  if (sec) sec.addEventListener('toggle', function () { if (sec.open) render(); });
  var rt;
  window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(render, 150); });
  render();
})();

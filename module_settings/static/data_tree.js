/* The Build stage tree (#492, slice C2): an SVG overlay in a 48 px gutter left of each file panel. A line runs from
   every source box (.dset[data-dataset]) to its own step row (tr.jr[data-ds]), in the box's provider colour lightened
   toward white; every build row gets a dashed line from each step of its file, in the file's colour, joined on a
   trunk. Each line has a wide invisible twin that takes the hover. A press goes through the journey code in
   control.js (it sets sel / lit / fade on the rows); the tree only follows those classes. */
(function () {
  'use strict';
  if (!document.querySelector || !document.createElementNS) return;
  var NS = 'http://www.w3.org/2000/svg', GUTTER = 48, TRUNK = 44, LANE0 = 40, LANE = 3.4, LIGHTEN = 0.45;
  var PHONE = 640;

  function host(j) {
    var f = j.closest ? j.closest('section.fpanel') : null;
    return f || j.parentElement;
  }
  function shown(el) { return !!el && el.getClientRects && el.getClientRects().length > 0; }
  function el(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function rgb(c) {
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((c || '').trim());
    if (m) {
      var h = m[1].length === 3 ? m[1].replace(/./g, '$&$&') : m[1];
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    }
    m = /rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/.exec(c || '');
    return m ? [+m[1], +m[2], +m[3]] : null;
  }
  function lighten(c) {
    var v = rgb(c);
    if (!v) return '#9fb3c8';
    return 'rgb(' + v.map(function (x) { return Math.round(x + (255 - x) * LIGHTEN); }).join(',') + ')';
  }
  function boxColour(box) {
    var a = box.getAttribute('data-color');
    if (a) return a;
    var cs = window.getComputedStyle ? getComputedStyle(box) : null;
    if (!cs) return '';
    return (cs.getPropertyValue('--pc') || cs.getPropertyValue('--pvc') || '').trim() || cs.borderLeftColor || cs.color;
  }
  function fileColour(j) {
    var cs = window.getComputedStyle ? getComputedStyle(j) : null;
    return (cs && cs.getPropertyValue('--fh').trim()) || '#8b949e';
  }
  function arrow(x, y, colour) {
    return el('path', { d: 'M' + (x - 6) + ',' + (y - 3.5) + 'L' + x + ',' + y + 'L' + (x - 6) + ',' + (y + 3.5) + 'Z',
                        fill: colour, 'class': 'ah' });
  }

  function line(svg, d, tip, colour, dashed, box, row) {
    var g = el('g', { 'class': 'tl' + (dashed ? ' bl' : '') });
    var vis = el('path', { d: d, stroke: colour, fill: 'none', 'class': 'vis' });
    if (dashed) vis.setAttribute('stroke-dasharray', '4 3');
    var hit = el('path', { d: d, stroke: 'transparent', fill: 'none', 'class': 'hit', 'stroke-width': '9',
                           'pointer-events': 'stroke' });
    g.appendChild(vis); g.appendChild(arrow(tip[0], tip[1], colour)); g.appendChild(hit);
    g._box = box; g._rows = row;
    hit.addEventListener('mouseenter', function () {
      g.classList.add('glow'); svg.appendChild(g);
      if (box) box.classList.add('dtree-ring');
      row.forEach(function (r) { r.classList.add('dtree-lit'); });
    });
    hit.addEventListener('mouseleave', function () {
      g.classList.remove('glow');
      if (box) box.classList.remove('dtree-ring');
      row.forEach(function (r) { r.classList.remove('dtree-lit'); });
    });
    svg.appendChild(g);
    return g;
  }

  function draw(j) {
    var h = host(j); if (!h) return;
    var svg = h.querySelector(':scope > svg.dtree');
    if (svg) while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!shown(j) || (window.innerWidth || 1024) < PHONE) { if (svg) svg.style.display = 'none'; return; }
    if (!svg) { svg = el('svg', { 'class': 'dtree', 'aria-hidden': 'true' }); h.appendChild(svg); }
    svg.style.display = '';
    h.classList.add('dtree-host');
    var hr = h.getBoundingClientRect();
    svg.setAttribute('width', String(hr.width + GUTTER)); svg.setAttribute('height', String(h.scrollHeight || hr.height));
    var sr = svg.getBoundingClientRect(), ox = sr.left, oy = sr.top;
    var rows = [].slice.call(j.querySelectorAll('tr.jr[data-ds]')).filter(shown);
    var edge = GUTTER, steps = [];
    // Box lines: the top box gets the inner lane.
    var pairs = [];
    rows.forEach(function (r) {
      var ds = r.getAttribute('data-ds'), box = null;
      [].forEach.call(document.querySelectorAll('.dset[data-dataset]'), function (b) {
        if (!box && b.getAttribute('data-dataset') === ds && shown(b)) box = b;
      });
      if (box) pairs.push([box, r]);
    });
    pairs.sort(function (a, b) { return a[0].getBoundingClientRect().top - b[0].getBoundingClientRect().top; });
    pairs.forEach(function (p, k) {
      var br = p[0].getBoundingClientRect(), rr = p[1].getBoundingClientRect();
      var lane = Math.max(2, LANE0 - k * LANE), bx = Math.max(lane + 6, Math.min(br.left + 14, br.right - 6) - ox);
      var by = br.bottom - oy, y = rr.top + rr.height / 2 - oy, x = Math.max(edge, rr.left - ox) + 2;
      var d = 'M' + bx + ',' + by + 'V' + (by + 6 + k * LANE) + 'H' + lane + 'V' + y + 'H' + x;
      steps.push(line(svg, d, [x, y], lighten(boxColour(p[0])), false, p[0], [p[1]]));
    });
    // Build lines: every step row of the file into each build row, on a trunk.
    var fh = fileColour(j);
    rows.forEach(function (b) {
      if (!b.querySelector('.tag.r-build')) return;
      var br = b.getBoundingClientRect(), by = br.top + br.height / 2 - oy, bx = Math.max(edge, br.left - ox) + 2;
      rows.forEach(function (r) {
        if (r === b) return;
        var rr = r.getBoundingClientRect(), y = rr.top + rr.height / 2 - oy + 4;
        var d = 'M' + Math.max(edge, rr.left - ox) + ',' + y + 'H' + TRUNK + 'V' + by + 'H' + bx;
        steps.push(line(svg, d, [bx, by], fh, true, null, [r, b]));
      });
    });
    follow(j);
  }

  // Lit lines go to full strength and the others fade, after the journey code has set its classes.
  function follow(j) {
    var h = host(j), svg = h && h.querySelector(':scope > svg.dtree'); if (!svg) return;
    var picking = j.classList.contains('picking');
    [].forEach.call(svg.querySelectorAll('g.tl'), function (g) {
      var lit = (g._rows || []).some(function (r) { return r.classList.contains('sel') || r.classList.contains('lit'); });
      g.classList.toggle('on', picking && lit); g.classList.toggle('off', picking && !lit);
    });
  }

  var queued = false;
  function all() {
    queued = false;
    [].forEach.call(document.querySelectorAll('.ctl .journey'), draw);
  }
  function soon() {
    if (queued) return; queued = true;
    (window.requestAnimationFrame || function (f) { return setTimeout(f, 16); })(all);
  }

  document.addEventListener('click', function (e) {
    var t = e.target, box = t && t.closest && t.closest('.dset[data-dataset]');
    if (box && !t.closest('button,a,input,select,label,summary,textarea')) {
      var ds = box.getAttribute('data-dataset');
      var row = [].filter.call(document.querySelectorAll('.ctl .journey tr.jr[data-ds]'), function (r) {
        return r.getAttribute('data-ds') === ds;
      })[0];
      if (row && row.click) row.click();
    }
    soon();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') soon(); });
  document.addEventListener('change', soon);
  document.addEventListener('toggle', soon, true);
  window.addEventListener('resize', soon);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', soon); else soon();
  window.addEventListener('load', soon);
})();

// SPDX-License-Identifier: Apache-2.0
// Waveform view: signals, protocol decoder rows, cursors and timing limits on one canvas.
// Interface: web/wave/README-interface.md. No libraries.

import { parseVCD } from './vcd.js';
import { PROTOCOLS, runDecoder, Line } from './decoders/index.js';
import { checkTiming, fmtValue, fmtLimit } from './timing.js';

const Z = -1, X = -2;
const RULER_H = 30;
const ROW = { group: 22, sig: 26, dec: 22 };

let cssLoaded = false;
function loadCss() {
  if (cssLoaded || typeof document === 'undefined') return;
  cssLoaded = true;
  const href = new URL('./wave.css', import.meta.url).href;
  if ([...document.styleSheets].some((s) => s.href === href)) return;
  const l = document.createElement('link');
  l.rel = 'stylesheet';
  l.href = href;
  document.head.appendChild(l);
}

class Track {
  constructor() { this.t = new Float64Array(64); this.v = new Float64Array(64); this.n = 0; }
  push(t, v) {
    const n = this.n;
    if (n && this.v[n - 1] === v) return;
    if (n && this.t[n - 1] === t) {
      if (n > 1 && this.v[n - 2] === v) this.n--; else this.v[n - 1] = v;
      return;
    }
    if (n === this.t.length) {
      const t2 = new Float64Array(n * 2), v2 = new Float64Array(n * 2);
      t2.set(this.t); v2.set(this.v);
      this.t = t2; this.v = v2;
    }
    this.t[n] = t; this.v[n] = v; this.n = n + 1;
  }
  idx(at) { // last index with t <= at
    let lo = 0, hi = this.n - 1, r = -1;
    const t = this.t;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (t[m] <= at) { r = m; lo = m + 1; } else hi = m - 1; }
    return r;
  }
  at(at) { const k = this.idx(at); return k < 0 ? X : this.v[k]; }
}

function normValue(v) {
  if (v === 'z' || v === 'Z') return Z;
  if (v === null || v === undefined || v === 'x' || v === 'X' || Number.isNaN(v)) return X;
  return Number(v);
}

const fmtNum = (x, d) => x.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

function fmtNsAxis(ns, step) {
  if (step >= 1e6) return `${+(ns / 1e6).toFixed(Math.max(0, -Math.floor(Math.log10(step / 1e6))))} ms`;
  if (step >= 1e3) return `${+(ns / 1e3).toFixed(Math.max(0, -Math.floor(Math.log10(step / 1e3))))} us`;
  return `${+ns.toFixed(Math.max(0, -Math.floor(Math.log10(step))))} ns`;
}

class View {
  constructor(element, opts = {}) {
    loadCss();
    this.el = element;
    this.opts = opts;
    this.nsPerCycle = 16.667;
    this.mode = 'cycle'; // 'cycle': time unit = cycle (streaming); 'vcd': time unit = VCD timescale
    this.vcdSec = 1e-12;
    this.signals = [];
    this.byName = new Map();
    this.decoders = [];
    this.arrows = [];
    this.collapsed = new Set();
    this.title = '';
    this.last = 0; this.first = 0;
    this.t0 = 0; this.tpp = 1; this.follow = true; this.userZoomed = false;
    this.curA = null; this.curB = null; this.cursorMode = 'A';
    this.sel = null; this.hover = null;
    this.timingName = null;
    this._raf = 0; this._decodeAt = 0;
    this._build();
  }

  // ---------- public interface ----------

  setSignals(list) {
    this.signals = list.map((s, i) => {
      let name = s.name, group = s.group || '';
      if (!s.group && name.includes('.')) { const k = name.lastIndexOf('.'); group = name.slice(0, k); name = name.slice(k + 1); }
      return { name, full: s.name, width: s.width || 1, kind: s.kind || ((s.width || 1) > 1 ? 'bus' : 'bit'), group, track: new Track(), i };
    });
    this.byName = new Map();
    for (const s of this.signals) { this.byName.set(s.full, s); if (!this.byName.has(s.name)) this.byName.set(s.name, s); }
    this.decoders = [];
    this.arrows = [];
    this.last = 0; this.first = 0;
    this.curA = this.curB = null;
    this.follow = true; this.userZoomed = false;
    this.mode = 'cycle';
    this._dirty();
  }

  append(cycle, values) {
    if (!this.signals.length) return;
    const n = Math.min(values.length, this.signals.length);
    for (let i = 0; i < n; i++) this.signals[i].track.push(cycle, normValue(values[i]));
    if (cycle > this.last) this.last = cycle;
    for (const d of this.decoders) d.dirty = true;
    if (this.follow && this.userZoomed) {
      const span = this.tpp * this._plotW();
      if (this.last > this.t0 + span) this.t0 = this.last - span;
    }
    this._sched();
  }

  loadVCD(text, opts = {}) {
    const v = parseVCD(text);
    const list = v.signals.map((s) => ({ name: s.name, width: s.width, kind: s.kind, group: s.scope.split('.').slice(1).join('.') || s.scope }));
    // one top scope for all: no group header for it
    const scopes = new Set(list.map((s) => s.group));
    if (scopes.size === 1) list.forEach((s) => { s.group = ''; });
    this.setSignals(list);
    v.signals.forEach((s, i) => { const tr = this.signals[i].track; for (let k = 0; k < s.times.length; k++) tr.push(s.times[k], s.values[k]); });
    this.mode = 'vcd';
    this.vcdSec = v.timescale;
    this.title = opts.title || '';
    this.comments = v.comments;
    if (opts.nsPerCycle) this.nsPerCycle = opts.nsPerCycle;
    this.first = Math.min(...v.signals.map((s) => (s.times.length ? s.times[0] : Infinity)), v.end);
    if (!Number.isFinite(this.first)) this.first = 0;
    this.last = v.end;
    this._titleEl.textContent = this.title;
    this.fit();
    return this;
  }

  addDecoder(name, cfg) {
    const d = { name, protocol: cfg.protocol, pins: cfg.pins || {}, options: cfg.options || {}, dirty: true, result: null, showMinor: false };
    if (Array.isArray(d.pins)) { // pins as a list in role order
      const roles = (PROTOCOLS[d.protocol] || {}).roles || [];
      const o = {};
      d.pins.forEach((p, k) => { if (p) o[roles[k]] = p; });
      d.pins = o;
    }
    this.decoders.push(d);
    this._fillTimingSelect();
    this._dirty();
    return d;
  }

  setTimebase(ns) {
    this.nsPerCycle = ns;
    if (this.mode === 'cycle') for (const d of this.decoders) d.dirty = true;
    this._dirty();
  }

  setCursor(cycle, which = 'A') {
    const t = this._cycleToT(cycle);
    if (which === 'B') this.curB = t; else this.curA = t;
    if (this.mode === 'cycle' && cycle > this.last) this.last = cycle;
    this._sched();
  }

  setTiming(name) {
    this.timingName = name || null;
    if (this._timingSel) this._timingSel.value = this.timingName || '';
    this._dirty();
  }

  addArrows(list) {
    for (const a of list) this.arrows.push({ ...a });
    this._sched();
  }

  setTheme(theme) {
    if (theme === 'auto' || !theme) delete this.root.dataset.theme; else this.root.dataset.theme = theme;
    this._sched();
  }

  fit() {
    const w = this._plotW();
    const span = Math.max(this.last - this.first, this.mode === 'cycle' ? 10 : 1);
    this.t0 = this.first;
    this.tpp = (span * 1.01) / w;
    this.userZoomed = false;
    this.follow = true;
    this._sched();
  }

  // show the time range [ns0, ns1] (nanoseconds from the start of the data)
  showRange(ns0, ns1) {
    const toT = (ns) => ns * 1e-9 / this._sec();
    this.t0 = toT(ns0);
    this.tpp = Math.max(toT(ns1) - this.t0, 1e-9) / this._plotW();
    this.userZoomed = true; this.follow = false;
    this._clampView();
    this._sched();
  }

  showMinor(on = true) { for (const d of this.decoders) d.showMinor = on; this._sched(); }

  destroy() {
    this._ro.disconnect();
    this.root.remove();
  }

  // decoded records of a decoder (for checks and for the shell)
  decoded(name) {
    const d = this.decoders.find((x) => x.name === name);
    if (!d) return null;
    this._decode(d);
    return d.result;
  }

  // ---------- time ----------

  _sec() { return this.mode === 'cycle' ? this.nsPerCycle * 1e-9 : this.vcdSec; }
  _tToNs(t) { return t * this._sec() * 1e9; }
  _tToCycle(t) { return this._tToNs(t) / this.nsPerCycle; }
  _cycleToT(c) { return (c * this.nsPerCycle * 1e-9) / this._sec(); }

  // ---------- DOM ----------

  _build() {
    const r = document.createElement('div');
    r.className = 'wv';
    r.innerHTML = `
      <div class="wv-bar">
        <span class="wv-title"></span>
        <span class="wv-tools">
          <button type="button" data-a="out" title="Zoom out (-)" aria-label="Zoom out">&minus;</button>
          <button type="button" data-a="in" title="Zoom in (+)" aria-label="Zoom in">+</button>
          <button type="button" data-a="fit" title="Show all (F)">Fit</button>
          <button type="button" data-a="left" title="Pan left (Left arrow)" aria-label="Pan left">&lsaquo;</button>
          <button type="button" data-a="right" title="Pan right (Right arrow)" aria-label="Pan right">&rsaquo;</button>
          <button type="button" data-a="mode" class="wv-mode" title="Which cursor a click or tap places (Shift+click places B)">Cursor A</button>
          <label class="wv-tsel">Timing <select data-a="timing"><option value="">off</option></select></label>
          <button type="button" data-a="theme" title="Light or dark">Theme</button>
        </span>
      </div>
      <div class="wv-read" aria-live="polite"></div>
      <div class="wv-main"><canvas tabindex="0"></canvas><div class="wv-tip" hidden></div></div>
      <div class="wv-timing" hidden></div>
      <div class="wv-help">Wheel: zoom &middot; Shift+wheel or drag: pan &middot; pinch: zoom &middot; click: cursor A &middot; Shift+click: cursor B &middot; keys: arrows pan, + &minus; zoom, F fit, N/P next or previous edge of the selected row, Esc clears B</div>`;
    this.el.appendChild(r);
    this.root = r;
    this.canvas = r.querySelector('canvas');
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', 'Waveform');
    this._titleEl = r.querySelector('.wv-title');
    this._read = r.querySelector('.wv-read');
    this._tip = r.querySelector('.wv-tip');
    this._timingEl = r.querySelector('.wv-timing');
    this._timingSel = r.querySelector('select[data-a=timing]');
    this._modeBtn = r.querySelector('.wv-mode');
    r.querySelector('.wv-bar').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      const a = b.dataset.a;
      const w = this._plotW();
      if (a === 'in') this._zoomAt(this.t0 + (this.tpp * w) / 2, 0.5);
      else if (a === 'out') this._zoomAt(this.t0 + (this.tpp * w) / 2, 2);
      else if (a === 'fit') this.fit();
      else if (a === 'left') this._pan(-0.25 * w);
      else if (a === 'right') this._pan(0.25 * w);
      else if (a === 'mode') { this.cursorMode = this.cursorMode === 'A' ? 'B' : 'A'; b.textContent = `Cursor ${this.cursorMode}`; }
      else if (a === 'theme') {
        const dark = this._isDark();
        this.setTheme(dark ? 'light' : 'dark');
        this.el.dispatchEvent(new CustomEvent('wavetheme', { detail: dark ? 'light' : 'dark', bubbles: true }));
      }
    });
    this._timingSel.addEventListener('change', () => this.setTiming(this._timingSel.value));
    this._timingEl.addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-k]');
      if (!tr || !this._timingRows) return;
      const m = this._timingRows[+tr.dataset.k];
      if (m && m.worst) this._showSpan(m.worst.t0, m.worst.t1);
    });
    this._events();
    this._ro = new ResizeObserver(() => this._sched());
    this._ro.observe(this.el);
  }

  _fillTimingSelect() {
    const s = this._timingSel;
    const keep = s.value;
    s.innerHTML = '<option value="">off</option>' + this.decoders
      .filter((d) => ['uart', 'i2c', 'onewire'].includes(d.protocol))
      .map((d) => `<option value="${d.name}">${d.name}</option>`).join('');
    s.value = keep;
    s.parentElement.hidden = s.options.length < 2;
  }

  _isDark() {
    const t = this.root.dataset.theme;
    if (t) return t === 'dark';
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  _labelW() { return this.el.clientWidth < 600 ? 104 : 172; }
  _plotW() { return Math.max(50, (this.el.clientWidth || 600) - this._labelW() - 2); }

  _dirty() { for (const d of this.decoders) d.dirty = true; this._sched(); }

  _sched() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this._draw(); });
  }

  // ---------- decoding ----------

  _decode(d) {
    if (!d.dirty && d.result) return;
    const lines = {};
    for (const [role, name] of Object.entries(d.pins)) {
      const s = this.byName.get(name);
      if (s) lines[role] = new Line(s.track.t, s.track.v, s.track.n, 1);
    }
    d.result = runDecoder(d.protocol, lines, d.options, { sec: this._sec() });
    d.timing = d.result.error ? [] : checkTiming(d.protocol, d.result, { ...(PROTOCOLS[d.protocol] || {}).options, ...d.options });
    for (const row of d.result.rows) { // running maximum of t1, to find the first item in view by bisection
      const m = new Float64Array(row.items.length);
      let x = -Infinity;
      row.items.forEach((it, k) => { x = Math.max(x, it.t1); m[k] = x; });
      row.maxT1 = m;
    }
    d.dirty = false;
  }

  // ---------- layout ----------

  _layout() {
    const rows = [];
    let y = RULER_H;
    const groups = [];
    const gmap = new Map();
    for (const s of this.signals) {
      if (!gmap.has(s.group)) { gmap.set(s.group, []); groups.push(s.group); }
      gmap.get(s.group).push(s);
    }
    // each decoder goes after the last of its pins in display order
    const after = new Map();
    for (const d of this.decoders) {
      let lastSig = null;
      for (const name of Object.values(d.pins)) {
        const s = this.byName.get(name);
        if (s && (!lastSig || s.i > lastSig.i)) lastSig = s;
      }
      const key = lastSig ? lastSig.group : '\u0000end';
      if (!after.has(key)) after.set(key, []);
      after.get(key).push(d);
    }
    const addDec = (d) => {
      this._decode(d);
      if (d.result.error) { rows.push({ type: 'dec', d, row: { key: 'err', label: d.result.error, items: [] }, y, h: ROW.dec }); y += ROW.dec; return; }
      for (const row of d.result.rows) {
        if (row.minor && !d.showMinor) continue;
        rows.push({ type: 'dec', d, row, y, h: ROW.dec });
        y += ROW.dec;
      }
    };
    for (const g of groups) {
      const collapsed = this.collapsed.has(g);
      if (g) { rows.push({ type: 'group', name: g, collapsed, y, h: ROW.group }); y += ROW.group; }
      if (!collapsed) for (const s of gmap.get(g)) { rows.push({ type: 'sig', s, y, h: ROW.sig }); y += ROW.sig; }
      for (const d of after.get(g) || []) addDec(d);
    }
    for (const d of after.get('\u0000end') || []) addDec(d);
    this.rows = rows;
    return y + 6;
  }

  // ---------- drawing ----------

  _colors() {
    const cs = getComputedStyle(this.root);
    const g = (n, f) => cs.getPropertyValue(n).trim() || f;
    return {
      bg: g('--wave-bg', '#fff'), fg: g('--wave-fg', '#1d2733'), dim: g('--wave-dim', '#6b7785'), grid: g('--wave-grid', '#e8ecf0'),
      line: g('--wave-line', '#1f6fb2'), bus: g('--wave-bus', '#2a7f62'), z: g('--wave-z', '#b08a00'), x: g('--wave-x', '#c0392b'),
      a: g('--wave-cur-a', '#d9480f'), b: g('--wave-cur-b', '#7048e8'), sel: g('--wave-sel', 'rgba(31,111,178,0.08)'),
      label: g('--wave-label-bg', '#f6f8fa'), sep: g('--wave-sep', '#d5dbe1'),
      data: g('--dec-data', '#1f6fb2'), addr: g('--dec-addr', '#7048e8'), ctrl: g('--dec-ctrl', '#5c6b7a'), state: g('--dec-state', '#2a7f62'),
      err: g('--dec-err', '#c92a2a'), warn: g('--dec-warn', '#c77700'), bit: g('--dec-bit', '#8a96a3'), minor: g('--dec-bit', '#8a96a3'),
      ok: g('--wave-ok', '#2b8a3e'), bad: g('--wave-bad', '#c92a2a'), arrow: g('--wave-arrow', '#d9480f'),
    };
  }

  _x(t) { return this._lw + (t - this.t0) / this.tpp; }
  _t(x) { return this.t0 + (x - this._lw) * this.tpp; }

  _draw() {
    const W = Math.max(200, this.el.clientWidth || 600);
    this._lw = this._labelW();
    if (!this.userZoomed && this.follow) { // keep showing all the data until the reader zooms or pans
      const span = Math.max(this.last - this.first, this.mode === 'cycle' ? 10 : 1);
      this.t0 = this.first;
      this.tpp = (span * 1.01) / this._plotW();
    }
    const H = this._layout();
    const dpr = window.devicePixelRatio || 1;
    const cv = this.canvas;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.height = H + 'px';
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const C = this._colors();
    this.C = C;
    g.fillStyle = C.bg;
    g.fillRect(0, 0, W, H);
    g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
    g.textBaseline = 'middle';
    this._cw = g.measureText('0').width;
    const lw = this._lw;
    const tL = this._t(lw), tR = this._t(W);

    // grid and ruler
    const nsPerPx = this._tToNs(this.tpp);
    const stepNs = niceStep(nsPerPx * 90);
    const nsL = this._tToNs(tL), nsR = this._tToNs(tR);
    g.strokeStyle = C.grid; g.lineWidth = 1;
    g.fillStyle = C.dim;
    g.textAlign = 'center';
    for (let ns = Math.ceil(nsL / stepNs) * stepNs; ns <= nsR; ns += stepNs) {
      const x = Math.round(this._x(ns / (this._sec() * 1e9))) + 0.5;
      g.beginPath(); g.moveTo(x, RULER_H - 6); g.lineTo(x, H); g.stroke();
      const lab = fmtNsAxis(ns, stepNs);
      const hw = g.measureText(lab).width / 2;
      if (x - hw > lw + 2 && x + hw < W - 2) g.fillText(lab, x, 9);
    }
    // cycle ticks when a cycle is at least 6 px wide
    const pxPerCycle = this.nsPerCycle / nsPerPx;
    if (pxPerCycle >= 6) {
      g.strokeStyle = C.sep;
      const c0 = Math.ceil(this._tToCycle(tL)), c1 = this._tToCycle(tR);
      const every = pxPerCycle >= 40 ? 1 : pxPerCycle >= 12 ? 5 : 10;
      for (let c = Math.ceil(c0 / every) * every; c <= c1; c += every) {
        const x = Math.round(this._x(this._cycleToT(c))) + 0.5;
        g.beginPath(); g.moveTo(x, RULER_H - 4); g.lineTo(x, RULER_H); g.stroke();
        if (pxPerCycle * every >= 40) { g.fillStyle = C.dim; g.fillText(`c${c}`, x, 21); }
      }
    } else {
      g.fillStyle = C.dim;
      g.textAlign = 'left';
      g.fillText(`cycle ${fmtNum(this._tToCycle(tL), 0)}`, lw + 4, 21);
      g.textAlign = 'right';
      g.fillText(`${fmtNum(this._tToCycle(tR), 0)}`, W - 4, 21);
    }
    g.strokeStyle = C.sep;
    g.beginPath(); g.moveTo(0, RULER_H - 0.5); g.lineTo(W, RULER_H - 0.5); g.stroke();

    // rows
    for (const row of this.rows) {
      if (this.sel && row.type === 'sig' && row.s === this.sel) { g.fillStyle = C.sel; g.fillRect(0, row.y, W, row.h); }
      g.save();
      g.beginPath(); g.rect(lw, row.y, W - lw, row.h); g.clip();
      if (row.type === 'sig') (row.s.kind === 'bus' ? this._drawBus : this._drawBit).call(this, g, row, tL, tR, C);
      else if (row.type === 'dec') this._drawDec(g, row, tL, tR, C);
      g.restore();
      g.strokeStyle = C.grid;
      g.beginPath(); g.moveTo(0, row.y + row.h - 0.5); g.lineTo(W, row.y + row.h - 0.5); g.stroke();
    }

    // timing marks of the selected decoder
    this._marks = [];
    const td = this.decoders.find((d) => d.name === this.timingName);
    if (td && td.result && !td.result.error) this._drawTiming(g, td, tL, tR, C);

    // arrows
    if (this.arrows.length) this._drawArrows(g, C);

    // label column on top
    g.fillStyle = C.label;
    g.fillRect(0, 0, lw, H);
    g.strokeStyle = C.sep;
    g.beginPath(); g.moveTo(lw - 0.5, 0); g.lineTo(lw - 0.5, H); g.stroke();
    g.textAlign = 'left';
    g.fillStyle = C.dim;
    g.fillText(this.mode === 'vcd' ? 'replay' : 'live', 8, 15);
    for (const row of this.rows) this._drawLabel(g, row, lw, C);

    // cursors
    const drawCur = (t, col, name, dash) => {
      if (t === null) return;
      const x = Math.round(this._x(t)) + 0.5;
      if (x < lw || x > W) return;
      g.strokeStyle = col; g.lineWidth = 1.5; g.setLineDash(dash);
      g.beginPath(); g.moveTo(x, RULER_H - 2); g.lineTo(x, H); g.stroke();
      g.setLineDash([]);
      g.fillStyle = col;
      g.beginPath(); g.moveTo(x - 6, RULER_H - 10); g.lineTo(x + 6, RULER_H - 10); g.lineTo(x, RULER_H - 2); g.closePath(); g.fill();
      g.fillStyle = C.bg; g.textAlign = 'center'; g.font = 'bold 9px ui-monospace, Consolas, monospace';
      g.fillText(name, x, RULER_H - 7);
      g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      g.lineWidth = 1;
    };
    drawCur(this.curB, C.b, 'B', [5, 4]);
    drawCur(this.curA, C.a, 'A', []);
    if (this.hover && this.hover.x > lw) {
      g.strokeStyle = C.dim; g.globalAlpha = 0.35;
      g.beginPath(); g.moveTo(this.hover.x + 0.5, RULER_H); g.lineTo(this.hover.x + 0.5, H); g.stroke();
      g.globalAlpha = 1;
    }
    this._readout();
    this._timingTable(td);
  }

  _yLevels(row) { return { hi: row.y + 5, lo: row.y + row.h - 6, mid: row.y + row.h / 2 }; }

  _drawBit(g, row, tL, tR, C) {
    const tr = row.s.track;
    if (!tr.n) return;
    const { hi, lo, mid } = this._yLevels(row);
    const W = this.el.clientWidth;
    const tEnd = Math.max(this.last, tr.t[tr.n - 1]);
    const xMax = Math.min(this._x(tEnd), W + 2);
    let k = Math.max(0, tr.idx(tL));
    let minX = this._lw - 2; // nothing left of this (after a busy column)
    let pen = false;
    g.lineWidth = 1.5;
    g.strokeStyle = C.line;
    g.beginPath();
    const flush = () => { if (pen) { g.stroke(); g.beginPath(); pen = false; } };
    while (k < tr.n) {
      const ts = tr.t[k];
      if (ts > tR) break;
      const te = k + 1 < tr.n ? tr.t[k + 1] : tEnd;
      const x0 = Math.max(this._x(ts), minX);
      const x1 = Math.min(this._x(te), xMax);
      if (k + 1 < tr.n && this._x(te) - this._x(ts) < 1) { // several changes in one pixel column
        const colEnd = Math.floor(x0) + 1;
        if (pen) g.lineTo(x0, hi); else { g.moveTo(x0, hi); pen = true; }
        g.lineTo(x0, lo);
        g.stroke(); g.beginPath(); pen = false;
        k = Math.max(tr.idx(this._t(colEnd)), k + 1);
        minX = colEnd;
        continue;
      }
      const v = tr.v[k];
      if (x1 > x0) {
        if (v === 1 || v === 0) {
          const y = v === 1 ? hi : lo;
          if (pen) g.lineTo(x0, y); else { g.moveTo(x0, y); pen = true; }
          g.lineTo(x1, y);
        } else {
          flush();
          g.save();
          if (v === Z) { g.strokeStyle = C.z; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x0, mid); g.lineTo(x1, mid); g.stroke(); }
          else { g.fillStyle = C.x; g.globalAlpha = 0.28; g.fillRect(x0, hi, x1 - x0, lo - hi); }
          g.restore();
          g.beginPath();
        }
      }
      minX = this._lw - 2;
      k++;
    }
    flush();
    g.lineWidth = 1;
  }

  _busText(s, v) {
    if (v === Z) return 'z';
    if (v === X) return 'x';
    return '0x' + v.toString(16).toUpperCase().padStart(Math.ceil(s.width / 4), '0');
  }

  _drawBus(g, row, tL, tR, C) {
    const tr = row.s.track;
    if (!tr.n) return;
    const { hi, lo, mid } = this._yLevels(row);
    let k = Math.max(0, tr.idx(tL));
    const xEnd = this._x(Math.min(tR, Math.max(this.last, tr.t[tr.n - 1])));
    g.lineWidth = 1.2;
    while (k < tr.n) {
      const x0 = this._x(tr.t[k]);
      const tn = k + 1 < tr.n ? tr.t[k + 1] : Infinity;
      let x1 = tn === Infinity ? xEnd : this._x(tn);
      const v = tr.v[k];
      if (x1 - x0 < 2 && tn !== Infinity) { // dense: one filled column
        const kk = tr.idx(this._t(Math.floor(x0) + 2));
        g.fillStyle = C.bus; g.fillRect(x0, hi, 2, lo - hi);
        k = Math.max(kk, k + 1);
        continue;
      }
      x1 = Math.min(x1, xEnd);
      const s = Math.min(3, (x1 - x0) / 2);
      if (v === Z) {
        g.strokeStyle = C.z; g.setLineDash([3, 3]);
        g.beginPath(); g.moveTo(x0, mid); g.lineTo(x1, mid); g.stroke(); g.setLineDash([]);
      } else {
        g.strokeStyle = v === X ? C.x : C.bus;
        g.fillStyle = v === X ? C.x : C.bus;
        g.beginPath();
        g.moveTo(x0, mid); g.lineTo(x0 + s, hi); g.lineTo(x1 - s, hi); g.lineTo(x1, mid); g.lineTo(x1 - s, lo); g.lineTo(x0 + s, lo); g.closePath();
        g.globalAlpha = 0.12; g.fill(); g.globalAlpha = 1; g.stroke();
        const text = this._busText(row.s, v);
        const vis0 = Math.max(x0, this._lw), vis1 = Math.min(x1, this.el.clientWidth);
        if (vis1 - vis0 > text.length * this._cw + 8) {
          g.fillStyle = C.fg; g.textAlign = 'center';
          g.fillText(text, (vis0 + vis1) / 2, mid + 0.5);
        }
      }
      if (tn === Infinity || x1 >= this._x(tR)) break;
      k++;
    }
    g.lineWidth = 1;
  }

  _firstItem(row, tL) {
    const m = row.maxT1;
    let lo = 0, hi = row.items.length - 1, r = row.items.length;
    while (lo <= hi) { const mm = (lo + hi) >> 1; if (m[mm] >= tL) { r = mm; hi = mm - 1; } else lo = mm + 1; }
    return r;
  }

  _drawDec(g, row, tL, tR, C) {
    const items = row.row.items;
    if (!items.length) return;
    const y0 = row.y + 3, h = row.h - 6, mid = row.y + row.h / 2;
    let lastX = -1;
    for (let k = this._firstItem(row.row, tL); k < items.length; k++) {
      const it = items[k];
      if (it.t0 > tR) break;
      const x0 = this._x(it.t0);
      let x1 = this._x(it.t1);
      const col = C[it.cls] || C.data;
      if (it.point || x1 - x0 < 1.5) { // a point event (START, STOP) or a tiny item
        if (x0 - lastX < 1 && !it.point) continue;
        g.strokeStyle = col; g.fillStyle = col;
        g.beginPath(); g.moveTo(x0, y0); g.lineTo(x0, y0 + h); g.stroke();
        if (it.point) {
          g.beginPath(); g.moveTo(x0 - 4, y0); g.lineTo(x0 + 4, y0); g.lineTo(x0, y0 + 5); g.closePath(); g.fill();
          const t = it.short || it.text;
          if (t.length * this._cw + 6 < 40) { g.textAlign = 'left'; g.fillText(t, x0 + 3, mid + 3); }
        }
        lastX = x0;
        continue;
      }
      x1 = Math.max(x1, x0 + 2);
      g.fillStyle = col; g.strokeStyle = col;
      g.globalAlpha = it.cls === 'bit' || it.cls === 'minor' ? 0.08 : 0.16;
      g.fillRect(x0, y0, x1 - x0, h);
      g.globalAlpha = 1;
      g.strokeRect(Math.round(x0) + 0.5, y0 + 0.5, Math.max(1, Math.round(x1 - x0) - 1), h - 1);
      const vis0 = Math.max(x0, this._lw), vis1 = Math.min(x1, this.el.clientWidth);
      const room = vis1 - vis0 - 6;
      let t = it.text;
      if (t.length * this._cw > room) t = it.short || '';
      if (t && t.length * this._cw > room) t = '';
      if (t) { g.fillStyle = it.cls === 'bit' || it.cls === 'minor' ? C.dim : C.fg; g.textAlign = 'center'; g.fillText(t, (vis0 + vis1) / 2, mid + 0.5); }
      lastX = x1;
    }
  }

  _rowOfSignal(name) {
    const s = this.byName.get(name);
    return this.rows.find((r) => r.type === 'sig' && r.s === s);
  }

  _drawTiming(g, d, tL, tR, C) {
    const W = this.el.clientWidth;
    const placed = new Map(); // row y -> label spans already drawn
    const free = (y, x0, x1) => !(placed.get(y) || []).some(([a, b]) => x0 < b + 6 && x1 > a - 6);
    const insts = [];
    for (const m of d.timing) {
      for (const inst of (d.result.timing || [])) if (inst.param === m.param && inst.limit) insts.push([inst, inst === m.worst]);
    }
    insts.sort((p, q) => (q[1] - p[1])); // worst cases first: they get the label space
    const font11 = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
    const vis = [];
    for (const [inst, isWorst] of insts) {
      if (inst.t1 < tL || inst.t0 > tR) continue;
      const row = this._rowOfSignal(d.pins[inst.role]);
      if (!row) continue;
      const x0 = this._x(inst.t0), x1 = this._x(inst.t1);
      if (x1 - x0 < 12 && !isWorst) continue;
      vis.push([inst, isWorst, row, x0, x1]);
    }
    for (const [inst, isWorst, row, x0, x1] of [...vis].reverse()) { // brackets first, the worst cases on top
      const y = row.y + 3;
      g.strokeStyle = inst.ok ? C.ok : C.bad; g.lineWidth = isWorst ? 1.6 : 1;
      g.globalAlpha = isWorst ? 1 : 0.5;
      g.beginPath(); g.moveTo(x0, y + 6); g.lineTo(x0, y); g.lineTo(x1, y); g.lineTo(x1, y + 6); g.stroke();
      g.globalAlpha = 1; g.lineWidth = 1;
      this._marks.push({ x0: Math.min(x0, x1), x1: Math.max(x0, x1), y0: y - 2, y1: y + 8, inst });
    }
    for (const [inst, isWorst, row, x0, x1] of vis) { // then the labels, the worst cases first
      const col = inst.ok ? C.ok : C.bad;
      const y = row.y + 3;
      g.font = font11;
      const full = `${inst.param} ${fmtValue(inst.value, inst.limit.unit)} (limit ${fmtLimit(inst.limit)})`;
      const short = `${inst.param} ${fmtValue(inst.value, inst.limit.unit)}`;
      let t = null;
      for (const cand of isWorst ? [full, short, inst.param] : [short]) {
        const tw = Math.ceil(g.measureText(cand).width) + 8;
        if (!isWorst && tw > x1 - x0) continue; // other instances: only inside their own bracket
        let tx = (x0 + x1) / 2 - tw / 2;
        tx = Math.max(this._lw + 2, Math.min(W - tw - 2, tx));
        if (free(y, tx, tx + tw)) { t = [cand, tx, tw]; break; }
      }
      g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      if (!t) continue;
      const [text, tx, tw] = t;
      if (!placed.has(y)) placed.set(y, []);
      placed.get(y).push([tx, tx + tw]);
      g.fillStyle = C.bg; g.globalAlpha = 0.94; g.fillRect(tx, y - 1, tw, 14); g.globalAlpha = 1;
      g.strokeStyle = col; g.strokeRect(tx + 0.5, y - 0.5, tw - 1, 13);
      g.fillStyle = col; g.textAlign = 'left'; g.font = font11;
      g.fillText(text, tx + 4, y + 6);
      g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      this._marks.push({ x0: tx, x1: tx + tw, y0: y - 1, y1: y + 13, inst });
    }
  }

  _drawArrows(g, C) {
    for (const a of this.arrows) {
      const row = this._rowOfSignal(a.signal);
      if (!row) continue;
      const xf = this._x(this._cycleToT(a.fromCycle)), xt = this._x(this._cycleToT(a.toCycle));
      if (Math.max(xf, xt) < this._lw || Math.min(xf, xt) > this.el.clientWidth) continue;
      const yf = row.y + 4, yt = row.y + row.h / 2;
      g.strokeStyle = C.arrow; g.fillStyle = C.arrow; g.lineWidth = 1.3;
      g.beginPath(); g.arc(xf, yf, 2.5, 0, 2 * Math.PI); g.fill();
      g.beginPath(); g.moveTo(xf, yf);
      g.bezierCurveTo(xf + (xt - xf) * 0.5, yf - 8, xt - 10, yt - 10, xt, yt); g.stroke();
      g.beginPath(); g.moveTo(xt, yt); g.lineTo(xt - 7, yt - 5); g.lineTo(xt - 3, yt - 8); g.closePath(); g.fill();
      if (a.label && Math.abs(xt - xf) > a.label.length * this._cw) { g.textAlign = 'left'; g.font = '11px ui-monospace, Consolas, monospace'; g.fillText(a.label, xf + 4, yf - 1); g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'; }
      g.lineWidth = 1;
    }
  }

  _drawLabel(g, row, lw, C) {
    const mid = row.y + row.h / 2;
    const max = Math.floor((lw - 14) / this._cw);
    const clip = (s, n) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + '…' : s);
    g.textAlign = 'left';
    if (row.type === 'group') {
      g.fillStyle = C.fg; g.font = 'bold 12px ui-sans-serif, system-ui, Segoe UI, sans-serif';
      g.fillText(`${row.collapsed ? '▸' : '▾'} ${clip(row.name, max)}`, 6, mid);
      g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      return;
    }
    if (row.type === 'sig') {
      const s = row.s;
      let val = '';
      if (this.curA !== null) { const v = s.track.at(this.curA); val = s.kind === 'bus' ? this._busText(s, v) : v === Z ? 'z' : v === X ? 'x' : String(v); }
      const room = max - (val ? val.length + 1 : 0);
      g.fillStyle = C.fg;
      g.fillText(clip(s.name + (s.kind === 'bus' ? `[${s.width - 1}:0]` : ''), room), 14, mid);
      if (val) { g.fillStyle = C.a; g.textAlign = 'right'; g.fillText(val, lw - 6, mid); }
      return;
    }
    const d = row.d;
    const res = d.result;
    const isMain = res.rows && res.rows[0] === row.row;
    g.fillStyle = res.error ? C.err : C.dim;
    g.font = '11px ui-sans-serif, system-ui, Segoe UI, sans-serif';
    const hasMinor = res.rows && res.rows.some((r) => r.minor);
    const generic = ['frames', 'events', 'transfers', 'bytes', 'TAP state'].includes(row.row.label);
    const head = isMain ? `${hasMinor ? (d.showMinor ? '▾ ' : '▸ ') : ''}${d.name}${generic ? '' : ' ' + row.row.label}` : `   ${row.row.label}`;
    g.fillText(clip(res.error ? `${d.name}: ${res.error}` : head, max + 2), 6, mid);
    if (isMain && res.errors) {
      const t = String(res.errors);
      g.fillStyle = C.err; g.beginPath();
      g.arc(lw - 12, mid, 8, 0, 2 * Math.PI); g.fill();
      g.fillStyle = C.bg; g.textAlign = 'center'; g.fillText(t, lw - 12, mid + 0.5);
    }
    g.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
  }

  _fmtT(t) {
    const ns = this._tToNs(t), c = this._tToCycle(t);
    const ci = Math.abs(c - Math.round(c)) < 0.005 ? fmtNum(Math.round(c), 0) : fmtNum(c, 2);
    return `${fmtNum(ns, 3)} ns <span class="wv-c">cycle ${ci}</span>`;
  }

  _readout() {
    const parts = [];
    if (this.curA !== null) parts.push(`<span class="wv-a">A</span> ${this._fmtT(this.curA)}`);
    if (this.curB !== null) parts.push(`<span class="wv-b">B</span> ${this._fmtT(this.curB)}`);
    if (this.curA !== null && this.curB !== null) {
      const d = this.curB - this.curA;
      const ns = this._tToNs(d), c = this._tToCycle(d);
      const f = ns !== 0 ? 1e3 / Math.abs(ns) : 0;
      parts.push(`B&minus;A ${fmtNum(ns, 3)} ns = ${fmtNum(c, 2)} cycles${f ? ` (${f >= 1 ? fmtNum(f, 3) + ' MHz' : fmtNum(f * 1e3, 3) + ' kHz'})` : ''}`);
    }
    if (!parts.length) parts.push('<span class="wv-dim">Click or tap the waveform to place cursor A; Shift+click (or the Cursor button) places B.</span>');
    const html = parts.join(' <span class="wv-dim">|</span> ');
    if (html !== this._lastRead) { this._read.innerHTML = html; this._lastRead = html; }
  }

  _timingTable(d) {
    const el = this._timingEl;
    if (!d || !d.timing || d.result.error) { el.hidden = true; this._timingRows = null; return; }
    const key = d.name + '|' + d.timing.map((m) => m.param + m.n + (m.worst && m.worst.value)).join();
    el.hidden = false;
    if (key === this._timingKey) return;
    this._timingKey = key;
    this._timingRows = d.timing;
    const src = [...new Set(d.timing.map((m) => m.limit.source))];
    if (!d.timing.length) { el.innerHTML = `<p class="wv-dim">${d.name}: no timing values to check in this data.</p>`; return; }
    el.innerHTML = `<table><thead><tr><th>${d.name}</th><th>worst measured</th><th>limit</th><th>count</th><th>result</th></tr></thead><tbody>${
      d.timing.map((m, k) => `<tr data-k="${k}" title="${m.limit.name}. Source: ${m.limit.source.replace(/"/g, '&quot;')}"><td><b>${m.param}</b> <span class="wv-dim">${m.limit.name}</span></td><td>${fmtValue(m.worst.value, m.limit.unit)}</td><td>${fmtLimit(m.limit)}</td><td>${m.n}</td><td class="${m.fails ? 'wv-bad' : 'wv-ok'}">${m.fails ? `${m.fails} outside` : 'within'}</td></tr>`).join('')
    }</tbody></table><p class="wv-src">Source: ${src.map((x) => x.replace(/\.$/, '')).join(' / ')}. Click a row to show its worst case.</p>`;
  }

  // ---------- navigation ----------

  _clampView() {
    const w = this._plotW();
    const range = Math.max(this.last - this.first, 1);
    const minTpp = this.mode === 'cycle' ? 1 / 400 : this._cycleToT(1) / 400;
    this.tpp = Math.min(Math.max(this.tpp, minTpp), (range * 4) / w);
    const span = this.tpp * w;
    this.t0 = Math.min(Math.max(this.t0, this.first - span * 0.9), this.last - span * 0.1);
  }

  _zoomAt(t, f) {
    const x = this._x(t);
    this.tpp *= f;
    this.userZoomed = true;
    this._clampView();
    this.t0 = t - (x - this._lw) * this.tpp;
    this._clampView();
    this.follow = this.t0 + this.tpp * this._plotW() >= this.last;
    this._sched();
  }

  _pan(px) {
    this.t0 += px * this.tpp;
    this.userZoomed = true;
    this._clampView();
    this.follow = this.t0 + this.tpp * this._plotW() >= this.last;
    this._sched();
  }

  _showSpan(t0, t1) {
    const w = this._plotW();
    const span = Math.max((t1 - t0) * 6, this._cycleToT(20));
    this.tpp = span / w;
    this.t0 = (t0 + t1) / 2 - span / 2;
    this.userZoomed = true; this.follow = false;
    this._clampView();
    this.curA = t0; this.curB = t1;
    this._sched();
  }

  _rowAt(y) { return (this.rows || []).find((r) => y >= r.y && y < r.y + r.h); }

  _snap(t, row, x) {
    if (!row || row.type !== 'sig') return t;
    const tr = row.s.track;
    const k = tr.idx(t);
    let best = t, bd = 7;
    for (const j of [k, k + 1]) {
      if (j < 0 || j >= tr.n) continue;
      const d = Math.abs(this._x(tr.t[j]) - x);
      if (d < bd) { bd = d; best = tr.t[j]; }
    }
    return best;
  }

  _edge(dir, which) {
    const s = this.sel || (this.rows.find((r) => r.type === 'sig') || {}).s;
    if (!s) return;
    const cur = which === 'B' ? this.curB : this.curA;
    const t = cur === null ? this._t(this._lw) : cur;
    const tr = s.track;
    let k = tr.idx(t);
    let nt = null;
    if (dir > 0) { if (k + 1 < tr.n) nt = tr.t[k + 1]; } else { if (k >= 0 && tr.t[k] < t) nt = tr.t[k]; else if (k > 0) nt = tr.t[k - 1]; }
    if (nt === null) return;
    if (which === 'B') this.curB = nt; else this.curA = nt;
    const w = this._plotW();
    if (nt < this.t0 || nt > this.t0 + this.tpp * w) { this.t0 = nt - (this.tpp * w) / 2; this.userZoomed = true; this.follow = false; }
    this._sched();
  }

  _events() {
    const cv = this.canvas;
    const pts = new Map();
    let drag = null, pinch = null;
    const pos = (e) => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    cv.addEventListener('pointerdown', (e) => {
      const p = pos(e);
      cv.focus({ preventScroll: true });
      if (p.x < this._lw) { // label column
        const row = this._rowAt(p.y);
        if (!row) return;
        if (row.type === 'group') { if (this.collapsed.has(row.name)) this.collapsed.delete(row.name); else this.collapsed.add(row.name); }
        else if (row.type === 'sig') this.sel = this.sel === row.s ? null : row.s;
        else if (row.type === 'dec') row.d.showMinor = !row.d.showMinor;
        this._sched();
        return;
      }
      try { cv.setPointerCapture(e.pointerId); } catch (err) { /* pointer not capturable: keep working without capture */ }
      pts.set(e.pointerId, p);
      if (pts.size === 1) drag = { x: p.x, y: p.y, t0: this.t0, moved: false, shift: e.shiftKey };
      else if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        pinch = { d: Math.abs(a.x - b.x) || 1, tpp: this.tpp, tm: this._t((a.x + b.x) / 2) };
        drag = null;
      }
    });
    cv.addEventListener('pointermove', (e) => {
      const p = pos(e);
      if (pts.has(e.pointerId)) pts.set(e.pointerId, p);
      if (pinch && pts.size === 2) {
        const [a, b] = [...pts.values()];
        const d = Math.abs(a.x - b.x) || 1;
        this.tpp = (pinch.tpp * pinch.d) / d;
        this.userZoomed = true; this.follow = false;
        this._clampView();
        this.t0 = pinch.tm - ((a.x + b.x) / 2 - this._lw) * this.tpp;
        this._clampView();
        this._sched();
        return;
      }
      if (drag) {
        const dx = p.x - drag.x;
        if (Math.abs(dx) > 4 || Math.abs(p.y - drag.y) > 6) drag.moved = true;
        if (drag.moved) { this.t0 = drag.t0 - dx * this.tpp; this.userZoomed = true; this.follow = false; this._clampView(); this._sched(); }
        return;
      }
      this.hover = { x: p.x, y: p.y };
      this._tooltip(p);
      this._sched();
    });
    const end = (e) => {
      const p = pos(e);
      if (drag && !drag.moved && pts.size === 1) {
        const row = this._rowAt(p.y);
        const t = this._snap(this._t(p.x), row, p.x);
        if (drag.shift || this.cursorMode === 'B') this.curB = t; else this.curA = t;
        this._sched();
      }
      pts.delete(e.pointerId);
      if (pts.size < 2) pinch = null;
      if (pts.size === 0) drag = null;
    };
    cv.addEventListener('pointerup', end);
    cv.addEventListener('pointercancel', end);
    cv.addEventListener('pointerleave', () => { this.hover = null; this._tip.hidden = true; this._sched(); });
    cv.addEventListener('wheel', (e) => {
      const p = pos(e);
      if (p.x < this._lw) return; // let the page scroll over the labels
      e.preventDefault();
      const horiz = Math.abs(e.deltaX) > Math.abs(e.deltaY);
      if (e.shiftKey || horiz) this._pan((horiz ? e.deltaX : e.deltaY) * 0.8);
      else this._zoomAt(this._t(p.x), Math.exp(e.deltaY * 0.0015));
    }, { passive: false });
    cv.addEventListener('keydown', (e) => {
      const w = this._plotW();
      const k = e.key;
      let used = true;
      if (k === 'ArrowLeft') this._pan(-(e.shiftKey ? 0.9 : 0.15) * w);
      else if (k === 'ArrowRight') this._pan((e.shiftKey ? 0.9 : 0.15) * w);
      else if (k === '+' || k === '=') this._zoomAt(this.curA !== null && this._x(this.curA) > this._lw ? this.curA : this.t0 + (this.tpp * w) / 2, 0.5);
      else if (k === '-' || k === '_') this._zoomAt(this.curA !== null ? this.curA : this.t0 + (this.tpp * w) / 2, 2);
      else if (k === 'f' || k === 'F') this.fit();
      else if (k === 'Home') { this.t0 = this.first; this.userZoomed = true; this._clampView(); this._sched(); }
      else if (k === 'End') { this.t0 = this.last - this.tpp * w; this.userZoomed = true; this._clampView(); this._sched(); }
      else if (k === 'n' || k === 'N') this._edge(1, e.shiftKey ? 'B' : 'A');
      else if (k === 'p' || k === 'P') this._edge(-1, e.shiftKey ? 'B' : 'A');
      else if (k === 'ArrowDown' || k === 'ArrowUp') {
        const sigs = this.rows.filter((r) => r.type === 'sig').map((r) => r.s);
        if (sigs.length) {
          const i = sigs.indexOf(this.sel);
          this.sel = sigs[Math.max(0, Math.min(sigs.length - 1, i + (k === 'ArrowDown' ? 1 : -1)))];
          this._sched();
        }
      } else if (k === 'Escape') { this.curB = null; this._sched(); }
      else used = false;
      if (used) e.preventDefault();
    });
  }

  _tooltip(p) {
    const tip = this._tip;
    const show = (html) => {
      tip.innerHTML = html;
      tip.hidden = false;
      const W = this.el.clientWidth;
      const tw = tip.offsetWidth;
      tip.style.left = Math.max(4, Math.min(W - tw - 4, p.x + 12)) + 'px';
      tip.style.top = (p.y + 16) + 'px';
    };
    for (const m of this._marks || []) {
      if (p.x >= m.x0 - 2 && p.x <= m.x1 + 2 && p.y >= m.y0 && p.y <= m.y1) {
        const i = m.inst;
        show(`<b>${i.param}</b> ${i.limit.name}<br>measured ${fmtValue(i.value, i.limit.unit)}, limit ${fmtLimit(i.limit)}: <b class="${i.ok ? 'wv-ok' : 'wv-bad'}">${i.ok ? 'within' : 'outside'}</b>${i.detail ? `<br>${i.detail}` : ''}<br><span class="wv-src">Source: ${i.limit.source}</span>`);
        return;
      }
    }
    const row = this._rowAt(p.y);
    if (!row || p.x < this._lw) { tip.hidden = true; return; }
    const t = this._t(p.x);
    if (row.type === 'dec') {
      const items = row.row.items;
      const tol = 3 * this.tpp;
      for (let k = this._firstItem(row.row, t - tol); k < items.length; k++) {
        const it = items[k];
        if (it.t0 > t + tol) break;
        if (it.t0 - tol <= t && t <= it.t1 + tol) {
          show(`<b>${row.d.name}</b> ${row.row.label}: ${it.text}<br><span class="wv-dim">${this._fmtT(it.t0)}${it.t1 > it.t0 ? ` to ${fmtNum(this._tToNs(it.t1), 3)} ns` : ''}</span>`);
          return;
        }
      }
      tip.hidden = true;
      return;
    }
    if (row.type === 'sig') {
      const v = row.s.track.at(t);
      show(`<b>${row.s.name}</b> = ${row.s.kind === 'bus' ? this._busText(row.s, v) : v === Z ? 'z (released)' : v === X ? 'x' : v}<br><span class="wv-dim">${this._fmtT(t)}</span>`);
      return;
    }
    tip.hidden = true;
  }
}

export const WaveView = {
  mount(element, opts) { return new View(element, opts); },
};

if (typeof window !== 'undefined') window.WaveView = WaveView;

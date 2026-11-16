// SPDX-License-Identifier: Apache-2.0
// One logic line for the decoders: value changes in time order, released (z) mapped to the pull level.
// Times are in the view's time unit; ctx.sec is the length of one unit in seconds.

export const X = -2;

export class Line {
  constructor(times, values, n = times.length, pull = 1) {
    const t = [];
    const v = [];
    for (let i = 0; i < n; i++) {
      let x = values[i];
      if (x === -1) x = pull; // released: the pull-up (open drain) or the idle level
      else if (x !== 0 && x !== 1) x = X;
      if (v.length && v[v.length - 1] === x) continue;
      if (t.length && t[t.length - 1] === times[i]) { v[v.length - 1] = x; continue; }
      t.push(times[i]);
      v.push(x);
    }
    this.t = t;
    this.v = v;
    this.n = t.length;
  }

  // index of the last change at or before time `at` (-1 if none)
  idx(at) {
    const t = this.t;
    let lo = 0, hi = t.length - 1, r = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (t[m] <= at) { r = m; lo = m + 1; } else hi = m - 1;
    }
    return r;
  }

  // value at time `at` (changes at `at` included); X before the first change
  at(at) {
    const k = this.idx(at);
    return k < 0 ? X : this.v[k];
  }

  // value just before time `at` (what a flip-flop clocked at `at` samples)
  before(at) {
    const t = this.t;
    let lo = 0, hi = t.length - 1, r = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (t[m] < at) { r = m; lo = m + 1; } else hi = m - 1;
    }
    return r < 0 ? X : this.v[r];
  }

  // time of the first change after `after` to value `to` (0 or 1; undefined: any); Infinity if none
  next(after, to) {
    let k = this.idx(after) + 1;
    for (; k < this.n; k++) if (to === undefined || this.v[k] === to) return this.t[k];
    return Infinity;
  }

  // time of the last change at or before `at`
  lastChange(at) {
    const k = this.idx(at);
    return k < 0 ? -Infinity : this.t[k];
  }

  get end() { return this.n ? this.t[this.n - 1] : 0; }
}

// All change times of several lines in time order, each once: [{ t, lines: Set of indices }]
export function mergedEdges(lines) {
  const map = new Map();
  lines.forEach((l, j) => {
    if (!l) return;
    for (let k = 1; k < l.n; k++) {
      const e = map.get(l.t[k]);
      if (e) e.push(j); else map.set(l.t[k], [j]);
    }
  });
  return [...map.entries()].sort((a, b) => a[0] - b[0]).map(([t, js]) => ({ t, js }));
}

export const hex = (v, digits = 2) => '0x' + v.toString(16).toUpperCase().padStart(digits, '0');

export function fmtTime(sec) {
  const a = Math.abs(sec);
  if (a >= 1) return `${+sec.toFixed(3)} s`;
  if (a >= 1e-3) return `${+(sec * 1e3).toFixed(3)} ms`;
  if (a >= 1e-6) return `${+(sec * 1e6).toFixed(3)} us`;
  return `${+(sec * 1e9).toFixed(1)} ns`;
}

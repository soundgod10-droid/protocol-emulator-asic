// SPDX-License-Identifier: Apache-2.0
// VCD reader (IEEE 1800-2017 section 21.7, four-state VCD). Returns the signals with their value changes.
// Values: a number (bit 0/1, bus integer up to 53 bits), Z (-1, all bits z) or X (-2, any bit x or a mixed value).

export const Z = -1;
export const X = -2;

const UNIT = { s: 1, ms: 1e-3, us: 1e-6, ns: 1e-9, ps: 1e-12, fs: 1e-15 };

export function parseTimescale(text) {
  const m = /^\s*(1|10|100)\s*(s|ms|us|ns|ps|fs)\s*$/.exec(text);
  if (!m) throw new Error(`VCD: timescale "${text.trim()}" not understood`);
  return Number(m[1]) * UNIT[m[2]];
}

export function vecValue(bits) {
  // bits: a string of 0 1 x z (MSB first)
  if (/^[01]+$/.test(bits)) return bits.length <= 53 ? parseInt(bits, 2) : X;
  if (/^z+$/i.test(bits)) return Z;
  return X;
}

function scalarValue(ch) {
  if (ch === '0') return 0;
  if (ch === '1') return 1;
  if (ch === 'z' || ch === 'Z') return Z;
  return X;
}

// Returns { timescale (seconds per unit), signals: [{ name, scope, width, kind, times: number[], values: number[] }],
//           comments: string[], end (last timestamp) }
export function parseVCD(text) {
  const n = text.length;
  let i = 0;
  const signals = [];
  const byId = new Map();
  const scope = [];
  const comments = [];
  let timescale = 1e-12;

  function word() {
    while (i < n && text.charCodeAt(i) <= 32) i++;
    const s = i;
    while (i < n && text.charCodeAt(i) > 32) i++;
    return s < i ? text.slice(s, i) : null;
  }
  function untilEnd() {
    const out = [];
    for (;;) {
      const w = word();
      if (w === null || w === '$end') return out;
      out.push(w);
    }
  }

  // Header
  for (;;) {
    const w = word();
    if (w === null) throw new Error('VCD: no $enddefinitions');
    if (w === '$enddefinitions') { untilEnd(); break; }
    if (w === '$scope') { const p = untilEnd(); scope.push(p[1] || p[0] || ''); }
    else if (w === '$upscope') { untilEnd(); scope.pop(); }
    else if (w === '$timescale') timescale = parseTimescale(untilEnd().join(''));
    else if (w === '$comment') comments.push(untilEnd().join(' '));
    else if (w === '$var') {
      const p = untilEnd(); // type width id ref [range]
      const width = parseInt(p[1], 10) || 1;
      let name = p[3];
      if (p.length > 4 && /^\[/.test(p[4]) && width > 1) name += p[4];
      const sig = { name, scope: scope.join('.'), width, kind: width > 1 ? 'bus' : 'bit', times: [], values: [] };
      signals.push(sig);
      if (!byId.has(p[2])) byId.set(p[2], []);
      byId.get(p[2]).push(sig);
    } else if (w.startsWith('$')) untilEnd();
  }

  // Value changes
  let t = 0;
  let end = 0;
  const set = (id, v) => {
    const list = byId.get(id);
    if (!list) return;
    for (const s of list) {
      const k = s.values.length;
      if (k && s.values[k - 1] === v) continue;
      if (k && s.times[k - 1] === t) { s.values[k - 1] = v; continue; }
      s.times.push(t); s.values.push(v);
    }
  };
  for (;;) {
    const w = word();
    if (w === null) break;
    const c = w[0];
    if (c === '#') { t = Number(w.slice(1)); if (t > end) end = t; }
    else if (c === '0' || c === '1' || c === 'x' || c === 'X' || c === 'z' || c === 'Z') set(w.slice(1), scalarValue(c));
    else if (c === 'b' || c === 'B') set(word(), vecValue(w.slice(1)));
    else if (c === 'r' || c === 'R') word(); // real values are not shown
    else if (w === '$comment') comments.push(untilEnd().join(' '));
    // $dumpvars, $dumpall, $dumpon, $dumpoff, $end: no state of their own here
  }
  return { timescale, signals, comments, end };
}

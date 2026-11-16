// SPDX-License-Identifier: Apache-2.0
// 1-Wire (standard speed). A low pulse of 480 us or more is a reset (DS18B20 data sheet tRSTL); a low pulse that
// starts within 75 us after the reset ends is the presence pulse. Other low pulses are time slots: the bit is the
// line level 15 us after the falling edge (Analog Devices AN126 Table 2: read sample at A + E = 6 + 9 us), so a
// short low is 1 and a long low is 0. After each reset the bits form bytes, LSB first; the first byte is the ROM
// command. Timing limits (DS18B20 AC table) are measured on the controller's own drive line when it is given
// (role `drive`, 0 = the controller pulls low), else on the bus line.

import { hex } from './lines.js';

const ROM = { 0x33: 'Read ROM', 0x55: 'Match ROM', 0xcc: 'Skip ROM', 0xf0: 'Search ROM', 0xec: 'Alarm Search' };
const FUNC = { 0x44: 'Convert T', 0x4e: 'Write Scratchpad', 0xbe: 'Read Scratchpad', 0x48: 'Copy Scratchpad', 0xb8: 'Recall E2', 0xb4: 'Read Power Supply' };

function lows(line) {
  const out = [];
  for (let k = 0; k < line.n; k++) if (line.v[k] === 0) out.push([line.t[k], k + 1 < line.n ? line.t[k + 1] : Infinity]);
  return out;
}

export const onewire = {
  title: '1-Wire',
  roles: ['owr', 'drive'],
  optional: ['drive'],
  options: {},
  decode(L, o, ctx) {
    const us = 1e-6 / ctx.sec;
    const link = [], bytesRow = [], records = [], timing = [];
    let errors = 0;
    let afterReset = null, bits = [], byteT0 = 0, nbyte = 0, rom = null;
    const flush = () => { bits = []; };
    for (const [f, r] of lows(L.owr)) {
      if (r === Infinity) break;
      const d = r - f;
      if (d >= 480 * us) {
        link.push({ t0: f, t1: r, text: 'reset', short: 'R', cls: 'ctrl' });
        records.push({ t0: f, type: 'reset' });
        afterReset = r; flush(); nbyte = 0; rom = null;
        continue;
      }
      if (afterReset !== null && f - afterReset < 75 * us) {
        link.push({ t0: f, t1: r, text: 'presence', short: 'P', cls: 'ctrl' });
        records.push({ t0: f, type: 'presence' });
        afterReset = null;
        continue;
      }
      afterReset = null;
      const b = d < 15 * us ? 1 : 0;
      link.push({ t0: f, t1: f + 60 * us, text: String(b), cls: 'bit' });
      records.push({ t0: f, type: 'bit', value: b });
      if (!bits.length) byteT0 = f;
      bits.push(b);
      if (bits.length === 8) {
        const v = bits.reduce((a, x, j) => a | (x << j), 0);
        let text = hex(v), cls = 'data';
        if (nbyte === 0) { rom = v; text = `ROM command ${hex(v)}${ROM[v] ? ' ' + ROM[v] : ''}`; cls = 'addr'; }
        else if (rom === 0xcc && nbyte === 1) { text = `function ${hex(v)}${FUNC[v] ? ' ' + FUNC[v] : ''}`; cls = 'addr'; }
        else if (rom === 0x33) text = `ROM ${hex(v)}`;
        bytesRow.push({ t0: byteT0, t1: f + 60 * us, text, short: hex(v), cls });
        records.push({ t0: byteT0, type: 'byte', value: v });
        nbyte++;
        bits = [];
      }
    }
    // Controller times (DS18B20 AC table) on the drive line (or the bus)
    const src = L.drive || L.owr;
    const role = L.drive ? 'drive' : 'owr';
    const lw = lows(src).filter(([, r]) => r !== Infinity);
    for (let i = 0; i < lw.length; i++) {
      const [a, b] = lw[i];
      const n = b - a;
      const sec = (x) => x * ctx.sec;
      if (n >= 480 * us * 0.5) {
        timing.push({ param: 'tRSTL', role, t0: a, t1: b, value: sec(n) });
        if (i + 1 < lw.length) timing.push({ param: 'tRSTH', role, t0: b, t1: lw[i + 1][0], value: sec(lw[i + 1][0] - b) });
        continue;
      }
      timing.push({ param: n < 15 * us ? 'tLOW1' : 'tLOW0', role, t0: a, t1: b, value: sec(n) });
      if (i + 1 < lw.length && lw[i + 1][1] - lw[i + 1][0] < 480 * us * 0.5) {
        timing.push({ param: 'tREC', role, t0: b, t1: lw[i + 1][0], value: sec(lw[i + 1][0] - b) });
        timing.push({ param: 'tSLOT + tREC', role, t0: a, t1: lw[i + 1][0], value: sec(lw[i + 1][0] - a) });
      }
    }
    return {
      rows: [{ key: 'bytes', label: 'bytes', items: bytesRow }, { key: 'link', label: 'link', items: link, minor: true }],
      records, errors, timing,
    };
  },
};

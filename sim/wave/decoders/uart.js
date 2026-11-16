// SPDX-License-Identifier: Apache-2.0
// UART (asynchronous serial): start bit, 5 to 9 data bits LSB first, optional parity, stop bit(s).
// Receiver rule: start at the edge to the start level; bit k is read at start + (k + 0.5) bit times (the same rule
// as the chip's own independent receiver, test/test_fw_full.py check_tx). The next start is searched after the
// middle of the stop bit.

import { X, hex } from './lines.js';

export const uart = {
  title: 'UART',
  roles: ['rx'],
  options: { baud: 115200, bits: 8, parity: 'none', stop: 1, invert: false },
  decode(L, o, ctx) {
    const line = L.rx;
    const Tb = 1 / (o.baud * ctx.sec); // bit time in time units
    const nb = o.bits;
    const np = o.parity === 'none' ? 0 : 1;
    const inv = o.invert ? 1 : 0;
    const startLevel = inv ? 1 : 0;
    const lvl = (t) => { const v = line.at(t); return v === X ? X : v ^ inv; };
    const data = [], bits = [], records = [], timing = [];
    let errors = 0;
    let search = line.n ? line.t[0] : 0;
    const nbits = 1 + nb + np + 1; // frame bits up to the first stop bit
    for (let guard = 0; guard < 1e6; guard++) {
      const tf = line.next(search, startLevel);
      if (tf === Infinity) break;
      const at = (k) => tf + (k + 0.5) * Tb;
      if (lvl(at(0)) !== 0) { // the start bit is not low at its middle: a glitch
        data.push({ t0: tf, t1: at(0), text: 'glitch', short: '!', cls: 'warn' });
        search = at(0);
        continue;
      }
      let value = 0, bad = false, ones = 0;
      for (let k = 0; k < nb; k++) {
        const b = lvl(at(1 + k));
        if (b === X) bad = true;
        else { value |= b << k; ones += b; }
        bits.push({ t0: tf + (1 + k) * Tb, t1: tf + (2 + k) * Tb, text: String(b === X ? 'x' : b), cls: 'bit' });
      }
      let parityOk = null;
      if (np) {
        const p = lvl(at(1 + nb));
        const want = o.parity === 'even' ? ones & 1 : (ones & 1) ^ 1;
        parityOk = p === want;
      }
      const stop = lvl(at(1 + nb + np));
      const frameOk = stop === 1;
      const brk = !frameOk && value === 0 && !bad;
      const t1 = tf + nbits * Tb;
      data.push({ t0: tf, t1: tf + Tb, text: 'start', short: 'S', cls: 'ctrl' });
      const ch = value >= 32 && value < 127 ? ` '${String.fromCharCode(value)}'` : '';
      data.push({ t0: tf + Tb, t1: tf + (1 + nb) * Tb, text: bad ? 'x' : hex(value) + ch, short: bad ? 'x' : hex(value),
                  cls: bad ? 'err' : 'data' });
      if (np) data.push({ t0: tf + (1 + nb) * Tb, t1: tf + (2 + nb) * Tb, text: parityOk ? 'parity ok' : 'parity error',
                          short: parityOk ? 'P' : 'P!', cls: parityOk ? 'ctrl' : 'err' });
      data.push({ t0: tf + (1 + nb + np) * Tb, t1, text: brk ? 'break' : frameOk ? 'stop' : 'framing error',
                  short: frameOk ? 'T' : '!', cls: frameOk ? 'ctrl' : 'err' });
      if (!frameOk || parityOk === false || bad) errors++;
      records.push({ t0: tf, t1, value: bad ? null : value, parityOk, frameOk, brk });

      // Bit time: the edge in the frame farthest from the start edge, at k nominal bit times.
      let best = null;
      for (let k = line.idx(tf) + 1; k < line.n && line.t[k] < tf + (nbits - 0.25) * Tb; k++) {
        const nk = Math.round((line.t[k] - tf) / Tb);
        if (nk >= 1) best = { t: line.t[k], nk };
      }
      if (best) {
        const meas = (best.t - tf) / best.nk;
        timing.push({ param: 'bit time error', role: 'rx', t0: tf, t1: best.t, value: meas / Tb - 1, unit: '%',
                      detail: `${best.nk} bit times from the start edge: ${(meas * ctx.sec * 1e9).toFixed(1)} ns per bit, nominal ${(Tb * ctx.sec * 1e9).toFixed(1)} ns` });
      }
      search = at(1 + nb + np);
    }
    return {
      rows: [{ key: 'data', label: 'frames', items: data }, { key: 'bits', label: 'bits', items: bits, minor: true }],
      records, errors, timing,
    };
  },
};

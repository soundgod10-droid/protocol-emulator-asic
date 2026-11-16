// SPDX-License-Identifier: Apache-2.0
// SPI: CPOL sets the clock idle level, CPHA = 0 samples on the leading edge and CPHA = 1 on the trailing edge
// (Motorola SPI block guide convention). The data lines are read just before the sample edge, as a flip-flop does.
// With a chip select, a word in progress when CS ends is flagged as incomplete.

import { X, hex, mergedEdges } from './lines.js';

export const spi = {
  title: 'SPI',
  roles: ['clk', 'mosi', 'miso', 'cs'],
  optional: ['mosi', 'miso', 'cs'],
  options: { cpol: 0, cpha: 0, bitorder: 'msb', wordsize: 8, csActiveLow: true },
  decode(L, o, ctx) {
    const { clk, mosi, miso, cs } = L;
    const sampleLevel = (o.cpol ^ o.cpha) === 0 ? 1 : 0; // clock level after the sample edge
    const csOn = o.csActiveLow ? 0 : 1;
    const rowMosi = [], rowMiso = [], rowXfer = [], records = [];
    let errors = 0;
    let active = !cs, n = 0, vMosi = 0, vMiso = 0, xMosi = false, xMiso = false, w0 = 0, x0 = 0, words = 0;
    const order = (v) => {
      if (o.bitorder === 'msb') return v;
      let r = 0;
      for (let k = 0; k < o.wordsize; k++) r |= ((v >> k) & 1) << (o.wordsize - 1 - k);
      return r;
    };
    const endXfer = (t) => {
      if (n > 0) {
        rowMosi.push({ t0: w0, t1: t, text: `incomplete word: ${n} bits`, short: '!', cls: 'err' });
        errors++;
      }
      if (words) rowXfer.push({ t0: x0, t1: t, text: `${words} word${words > 1 ? 's' : ''}`, cls: 'ctrl' });
      n = 0; words = 0;
    };
    const lines = [clk, cs];
    for (const { t, js } of mergedEdges(lines)) {
      if (js.includes(1)) { // chip select changed
        const v = cs.at(t);
        if (v === csOn && !active) {
          active = true; n = 0; words = 0; x0 = t;
          if (clk.at(t) !== o.cpol) {
            rowXfer.push({ t0: t, t1: t, text: `clock not at its idle level ${o.cpol} at CS`, short: '!', cls: 'warn' });
          }
        } else if (v !== csOn && active) {
          endXfer(t);
          active = false;
        }
      }
      if (js.includes(0) && active && clk.at(t) === sampleLevel) {
        if (n === 0) { w0 = t; vMosi = vMiso = 0; xMosi = xMiso = false; }
        const a = mosi ? mosi.before(t) : 0;
        const b = miso ? miso.before(t) : 0;
        if (a === X) xMosi = true;
        if (b === X) xMiso = true;
        vMosi = (vMosi << 1) | (a === 1 ? 1 : 0);
        vMiso = (vMiso << 1) | (b === 1 ? 1 : 0);
        n++;
        if (n === o.wordsize) {
          const rm = order(vMosi), rs = order(vMiso);
          const d = Math.ceil(o.wordsize / 4);
          if (mosi) rowMosi.push({ t0: w0, t1: t, text: xMosi ? 'x' : hex(rm, d), cls: xMosi ? 'err' : 'data' });
          if (miso) rowMiso.push({ t0: w0, t1: t, text: xMiso ? 'x' : hex(rs, d), cls: xMiso ? 'err' : 'data' });
          records.push({ t0: w0, t1: t, mosi: mosi && !xMosi ? rm : null, miso: miso && !xMiso ? rs : null });
          n = 0; words++;
        }
      }
    }
    const rows = [];
    if (mosi) rows.push({ key: 'mosi', label: 'MOSI', items: rowMosi });
    if (miso) rows.push({ key: 'miso', label: 'MISO', items: rowMiso });
    if (cs) rows.push({ key: 'xfer', label: 'transfers', items: rowXfer, minor: true });
    return { rows, records, errors, timing: [] };
  },
};

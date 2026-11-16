// SPDX-License-Identifier: Apache-2.0
// PS/2 (IBM PS/2 Technical Reference, Keyboards): 11-bit frames, start 0, 8 data bits LSB first, odd parity, stop 1.
// Device to host: DATA is read at each falling CLK edge. Host to device: the host holds CLK low (request to send),
// pulls DATA low (the start bit) and releases CLK; DATA is read at each rising CLK edge, 11 bits, and the device
// acknowledges by holding DATA low at the 12th rising edge. A device frame stops when CLK stays low longer than
// `timeoutUs` (the host inhibits the device).

import { X, hex } from './lines.js';

export const ps2 = {
  title: 'PS/2',
  roles: ['clk', 'data'],
  options: { rtsMinUs: 60, timeoutUs: 300 },
  decode(L, o, ctx) {
    const { clk, data } = L;
    const us = 1e-6 / ctx.sec;
    const row = [], bitsRow = [], records = [];
    let errors = 0;
    // clock edges
    const E = [];
    for (let k = 1; k < clk.n; k++) E.push([clk.t[k], clk.v[k]]);
    let k = 0;
    const frameItem = (dir, t0, t1, bits, ack) => {
      const v = bits.slice(1, 9).reduce((a, b, j) => a | (b << j), 0);
      const pOk = bits.slice(1, 10).reduce((a, b) => a + b, 0) % 2 === 1;
      const ok = bits[0] === 0 && bits[10] === 1 && pOk;
      const what = !ok ? (bits[0] !== 0 ? ' start error' : !pOk ? ' parity error' : ' stop error') : '';
      const ackText = dir === 'host' ? (ack ? ', ACK' : ', no ACK') : '';
      row.push({ t0, t1, text: `${dir === 'host' ? 'host' : 'device'} ${hex(v)}${what}${ackText}`, short: (dir === 'host' ? 'H ' : '') + hex(v), cls: ok ? (dir === 'host' ? 'addr' : 'data') : 'err' });
      if (!ok) errors++;
      records.push({ t0, dir, value: v, parityOk: pOk, frameOk: ok, ack: dir === 'host' ? ack : null });
    };
    while (k < E.length) {
      const [t, v] = E[k];
      if (v === 1) { // rising edge: a request to send if CLK was low long and DATA is low
        const low = k > 0 ? t - E[k - 1][0] : 0;
        if (low >= o.rtsMinUs * us && data.at(t) === 0) {
          row.push({ t0: E[k - 1][0], t1: t, text: 'request to send', short: 'RTS', cls: 'ctrl' });
          // host to device: the next 12 rising edges
          const bits = [];
          let j = k + 1, last = t;
          while (j < E.length && bits.length < 12) {
            if (E[j][1] === 1) {
              if (E[j][0] - last > o.timeoutUs * us) break;
              const b = data.before(E[j][0]);
              bits.push(b === X ? 1 : b);
              bitsRow.push({ t0: last, t1: E[j][0], text: String(bits[bits.length - 1]), cls: 'bit' });
              last = E[j][0];
            }
            j++;
          }
          if (bits.length >= 11) frameItem('host', t, last, bits, bits.length === 12 && bits[11] === 0);
          else { row.push({ t0: t, t1: last, text: `host frame cut after ${bits.length} bits`, short: '!', cls: 'err' }); errors++; }
          k = j;
          continue;
        }
        k++;
        continue;
      }
      // falling edge with DATA low: start of a device frame
      if (data.before(t) !== 0) { k++; continue; }
      const bits = [];
      let j = k, last = t, cut = false;
      const t0 = t;
      while (j < E.length && bits.length < 11) {
        const [tj, vj] = E[j];
        if (vj === 0) {
          if (bits.length && tj - last > o.timeoutUs * us) { cut = true; break; } // no clock for too long
          const b = data.before(tj);
          bits.push(b === X ? 1 : b);
          const nr = j + 1 < E.length ? E[j + 1][0] : Infinity;
          bitsRow.push({ t0: tj, t1: nr === Infinity ? tj : nr, text: String(bits[bits.length - 1]), cls: 'bit' });
          last = tj;
          if (bits.length < 11 && nr - tj > o.timeoutUs * us) { cut = true; j++; break; } // CLK held low (inhibit)
        }
        j++;
      }
      if (bits.length === 11 && !cut) frameItem('device', t0, last, bits, null);
      else {
        row.push({ t0, t1: last, text: `device frame stopped after ${bits.length} bits (CLK held low)`, short: 'stop', cls: 'warn' });
        records.push({ t0, dir: 'device', value: null, stopped: bits.length });
      }
      k = j;
    }
    return { rows: [{ key: 'frames', label: 'frames', items: row }, { key: 'bits', label: 'bits', items: bitsRow, minor: true }], records, errors, timing: [] };
  },
};

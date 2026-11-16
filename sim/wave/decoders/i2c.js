// SPDX-License-Identifier: Apache-2.0
// I2C (NXP UM10204): START = SDA falls while SCL is high, STOP = SDA rises while SCL is high; a bit is read at the
// rising SCL edge; 8 bits then the ACK bit (SDA low = ACK, high = NACK). The first byte after a START is the 7-bit
// address and R/W. The bus times of UM10204 Table 11 are measured between the edges (symbols as in the standard).

import { X, hex, mergedEdges } from './lines.js';

export const i2c = {
  title: 'I2C',
  roles: ['scl', 'sda'],
  options: { mode: 'auto' },
  decode(L, o, ctx) {
    const { scl, sda } = L;
    const row = [], bitsRow = [], records = [], timing = [];
    let errors = 0;
    let inTx = false, nbit = 0, val = 0, first = false, rw = 0, b0 = 0;
    let lastRise = null, lastFall = null, lastStart = null, lastStop = null, riseOk = null, sdaChange = null;
    let startOpen = false, stopSinceRise = true, last8 = 0;
    let pscl = scl.at(scl.t[0] ?? 0), psda = sda.at(sda.t[0] ?? 0);
    const tm = (param, role, t0, t1) => timing.push({ param, role, t0, t1, value: (t1 - t0) * ctx.sec });
    for (const { t, js } of mergedEdges([scl, sda])) {
      const cscl = scl.at(t), csda = sda.at(t);
      const dScl = cscl !== pscl, dSda = csda !== psda;
      if (dScl && dSda) {
        row.push({ t0: t, t1: t, text: 'SCL and SDA change at the same time', short: '!', cls: 'warn' });
      }
      if (dScl && cscl === 1) { // SCL rise
        if (lastFall !== null) {
          tm('tLOW', 'scl', lastFall, t);
          if (sdaChange !== null && sdaChange >= lastFall) tm('tSU;DAT', 'sda', sdaChange, t);
        }
        if (riseOk !== null) timing.push({ param: 'fSCL', role: 'scl', t0: riseOk, t1: t, value: 1 / ((t - riseOk) * ctx.sec), unit: 'Hz' });
        lastRise = riseOk = t;
        stopSinceRise = false;
        if (inTx) {
          const bit = csda === X ? 0 : csda;
          const tf = scl.next(t, 0);
          const tEnd = tf === Infinity ? t : tf;
          if (nbit < 8) {
            if (nbit === 0) { b0 = t; val = 0; }
            val = (val << 1) | bit;
            bitsRow.push({ t0: t, t1: tEnd, text: String(bit), cls: 'bit' });
            nbit++;
          } else { // ACK bit
            const ack = bit === 0;
            if (first) {
              const addr = val >> 1;
              rw = val & 1;
              row.push({ t0: b0, t1: last8, text: `address ${hex(addr)} ${rw ? 'read' : 'write'}`, short: `${hex(addr)}${rw ? 'R' : 'W'}`, cls: 'addr' });
              records.push({ t: b0, type: rw ? 'address read' : 'address write', value: addr });
              first = false;
            } else {
              row.push({ t0: b0, t1: last8, text: `${rw ? 'read' : 'write'} ${hex(val)}`, short: hex(val), cls: 'data' });
              records.push({ t: b0, type: rw ? 'data read' : 'data write', value: val });
            }
            row.push({ t0: last8, t1: tEnd, text: ack ? 'ACK' : 'NACK', short: ack ? 'A' : 'N', cls: ack ? 'ctrl' : 'warn' });
            records.push({ t, type: ack ? 'ack' : 'nack' });
            nbit = 0;
          }
        }
      } else if (dScl && cscl === 0) { // SCL fall
        if (lastRise !== null) tm('tHIGH', 'scl', lastRise, t);
        if (startOpen) { tm('tHD;STA', 'sda', lastStart, t); startOpen = false; }
        lastFall = t;
        if (inTx && nbit === 8) last8 = t;
      } else if (dSda) {
        if (cscl === 1) { // START or STOP
          if (csda === 0) {
            const repeated = inTx;
            if (lastRise !== null && repeated && !stopSinceRise) tm('tSU;STA', 'sda', lastRise, t);
            if (lastStop !== null && !repeated) tm('tBUF', 'sda', lastStop, t);
            if (inTx && nbit > 1) { row.push({ t0: b0, t1: t, text: `byte cut by START after ${nbit} bits`, short: '!', cls: 'err' }); errors++; }
            else if (inTx && nbit === 1) bitsRow.pop(); // the SCL rise before a START or STOP is part of the condition
            row.push({ t0: t, t1: t, text: repeated ? 'repeated START' : 'START', short: repeated ? 'Sr' : 'S', cls: 'ctrl', point: true });
            records.push({ t, type: repeated ? 'repeated start' : 'start' });
            inTx = true; first = true; nbit = 0;
            lastStart = t; startOpen = true;
          } else {
            if (lastRise !== null) tm('tSU;STO', 'sda', lastRise, t);
            if (inTx && nbit > 1) { row.push({ t0: b0, t1: t, text: `byte cut by STOP after ${nbit} bits`, short: '!', cls: 'err' }); errors++; }
            else if (inTx && nbit === 1) bitsRow.pop(); // the SCL rise before a START or STOP is part of the condition
            row.push({ t0: t, t1: t, text: 'STOP', short: 'P', cls: 'ctrl', point: true });
            records.push({ t, type: 'stop' });
            inTx = false; nbit = 0;
            lastStop = t;
            stopSinceRise = true;
          }
          riseOk = null;
        } else {
          if (lastFall !== null && (sdaChange === null || sdaChange < lastFall)) tm('tHD;DAT', 'sda', lastFall, t);
          sdaChange = t;
        }
      }
      pscl = cscl; psda = csda;
    }
    // Mode: from the fastest SCL clock unless set
    let mode = o.mode;
    if (mode === 'auto') {
      const f = Math.max(0, ...timing.filter((x) => x.param === 'fSCL').map((x) => x.value));
      mode = f > 100e3 * 1.001 ? 'Fm' : 'Sm';
    }
    const nack = records.filter((r) => r.type === 'nack').length;
    return {
      rows: [{ key: 'ev', label: 'events', items: row }, { key: 'bits', label: 'bits', items: bitsRow, minor: true }],
      records, errors, warnings: nack, timing, meta: { mode },
    };
  },
};

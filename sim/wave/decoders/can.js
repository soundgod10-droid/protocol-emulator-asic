// SPDX-License-Identifier: Apache-2.0
// Classical CAN (Bosch CAN 2.0 parts A and B; ISO 11898-1 classical frames) on the bus line (0 = dominant).
// Hard synchronization at the start-of-frame edge; each recessive-to-dominant edge before a sample point moves the
// bit boundary to the edge (resynchronization without an SJW limit). Bits are read at the sample point.
// Stuff bits (after 5 equal bits, SOF to the end of the CRC sequence) are removed and checked; CRC-15
// (polynomial 0x4599) over SOF to the end of the data; form checks of the CRC delimiter, ACK delimiter and EOF.

import { hex } from './lines.js';

function crc15(bits) {
  let crc = 0;
  for (const b of bits) {
    const nxt = b ^ ((crc >> 14) & 1);
    crc = (crc << 1) & 0x7fff;
    if (nxt) crc ^= 0x4599;
  }
  return crc;
}

export const can = {
  title: 'CAN',
  roles: ['rx'],
  options: { bitrate: 250000, samplePoint: 0.75 },
  decode(L, o, ctx) {
    const rx = L.rx;
    const Tb = 1 / (o.bitrate * ctx.sec);
    const sp = o.samplePoint;
    const fields = [], bitsRow = [], records = [];
    let errors = 0;
    let search = rx.n ? rx.t[0] : 0;
    const idleAfter = (t) => { // first time after t from which the bus is recessive for 7 bit times
      let a = t;
      for (let g = 0; g < 1e5; g++) {
        if (rx.at(a) !== 1) { a = rx.next(a, 1); if (a === Infinity) return Infinity; }
        const f = rx.next(a, 0);
        if (f - a >= 7 * Tb) return a + 7 * Tb;
        a = f;
      }
      return Infinity;
    };
    for (let guard = 0; guard < 1e5; guard++) {
      const sof = rx.next(search, 0);
      if (sof === Infinity) break;
      let sync = sof, k = 0, prevSample = sof;
      let run = 0, last = -1, stuffing = true, err = null, errT = 0;
      const dbits = [];
      const raw = () => { // next raw bit: [value, bit start, bit end]
        let ts = sync + (k + sp) * Tb;
        const e = rx.next(prevSample, 0);
        if (e < ts && e > prevSample) { sync = e; k = 0; ts = e + sp * Tb; }
        const v = rx.at(ts) === 0 ? 0 : 1;
        const b0 = sync + k * Tb;
        k++;
        prevSample = ts;
        return [v, b0, b0 + Tb, ts];
      };
      const stuffBit = () => { // after 5 equal bits the next bit is a stuff bit of the other level
        const [v, b0, b1, ts] = raw();
        bitsRow.push({ t0: b0, t1: b1, text: 'stuff', short: 's', cls: v === last ? 'err' : 'minor' });
        if (v === last) { err = 'stuff error'; errT = ts; }
        run = 1; last = v;
      };
      const bit = () => { // next destuffed bit
        if (stuffing && run === 5) stuffBit();
        const [v, b0, b1] = raw();
        if (stuffing) {
          if (v === last) run++; else { run = 1; last = v; }
        }
        bitsRow.push({ t0: b0, t1: b1, text: String(v), cls: 'bit' });
        dbits.push(v);
        return [v, b0, b1];
      };
      const field = (n, name, fmt = (x) => hex(x, Math.ceil(n / 4)), cls = 'data') => {
        let v = 0, t0 = 0, t1 = 0;
        for (let j = 0; j < n && !err; j++) {
          const [b, b0, b1] = bit();
          if (j === 0) t0 = b0;
          t1 = b1;
          v = (v << 1) | b;
        }
        if (!err) fields.push({ t0, t1, text: `${name} ${fmt(v)}`, short: fmt(v), cls });
        return v;
      };
      const sofBit = bit();
      fields.push({ t0: sofBit[1], t1: sofBit[2], text: 'SOF', short: 'S', cls: 'ctrl' });
      let id = field(11, 'ID', (x) => hex(x, 3), 'addr');
      const rtrSrr = field(1, 'RTR/SRR', String, 'ctrl');
      const ide = field(1, 'IDE', String, 'ctrl');
      let ext = false, rtr = rtrSrr;
      if (ide === 1 && !err) {
        const idx = field(18, 'ID ext', (x) => hex(x, 5), 'addr');
        id = (id << 18) | idx;
        ext = true;
        rtr = field(1, 'RTR', String, 'ctrl');
        field(2, 'r1 r0', String, 'ctrl');
      } else if (!err) field(1, 'r0', String, 'ctrl');
      const dlc = field(4, 'DLC', String, 'ctrl');
      const data = [];
      if (!rtr) for (let j = 0; j < Math.min(dlc, 8) && !err; j++) data.push(field(8, 'data'));
      const crcCalc = crc15(dbits);
      const crc = field(15, 'CRC', (x) => hex(x, 4), 'ctrl');
      if (!err && run === 5) stuffBit(); // stuffing covers the CRC sequence up to its last bit
      stuffing = false;
      let crcOk = null, ack = null;
      if (!err) {
        crcOk = crc === crcCalc;
        if (!crcOk) { fields[fields.length - 1].cls = 'err'; fields[fields.length - 1].text = `CRC ${hex(crc, 4)} error (calculated ${hex(crcCalc, 4)})`; }
        const [cd, c0] = bit();
        if (cd !== 1) { err = 'CRC delimiter not recessive'; errT = c0; }
        const [a, a0, a1] = bit();
        ack = a === 0;
        if (!err) fields.push({ t0: a0, t1: a1, text: ack ? 'ACK' : 'no ACK', short: ack ? 'A' : 'N', cls: ack ? 'ctrl' : 'warn' });
        const [ad, d0] = bit();
        if (!err && ad !== 1) { err = 'ACK delimiter not recessive'; errT = d0; }
        let e0 = 0, e1 = 0;
        for (let j = 0; j < 7 && !err; j++) {
          const [eb, b0, b1] = bit();
          if (j === 0) e0 = b0;
          e1 = b1;
          if (eb !== 1) { err = `EOF bit ${j + 1} dominant`; errT = b0; }
        }
        if (!err) fields.push({ t0: e0, t1: e1, text: 'EOF', short: 'E', cls: 'ctrl' });
      }
      if (err) {
        fields.push({ t0: errT, t1: errT + Tb, text: err, short: '!', cls: 'err' });
        errors++;
        records.push({ t0: sof, error: err });
        search = idleAfter(errT);
      } else {
        if (!crcOk) errors++;
        records.push({ t0: sof, id, ext, rtr: !!rtr, dlc, data, crc, crcOk, ack });
        search = prevSample;
      }
      if (search === Infinity) break;
    }
    return {
      rows: [{ key: 'fields', label: 'frames', items: fields }, { key: 'bits', label: 'bits', items: bitsRow, minor: true }],
      records, errors, timing: [],
    };
  },
};

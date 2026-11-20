// SPDX-License-Identifier: Apache-2.0
// USB 2.0 low-speed (1.5 Mbit/s) or full-speed (12 Mbit/s) packets on D+ and D- (USB 2.0 chapters 7 and 8).
// Line state: J, K (differential), SE0 (both low). Idle is J. A packet starts at the change from J to K (the first
// SYNC bit) and ends at SE0 (EOP), followed by J. Bits: NRZI, a change of state is a 0 and no change a 1; a 0 after
// six 1s is a stuff bit and is removed (a 1 there is a stuffing error). The receiver counts bit times in each run of
// one state, so it resynchronises at every change, as a USB receiver does. Fields: SYNC (00000001), PID with its
// check nibble, token address and endpoint with CRC5, data bytes with CRC16, handshakes. Bits LSB first.

import { hex, mergedEdges } from './lines.js';

const PID = { 0x1: 'OUT', 0x9: 'IN', 0x5: 'SOF', 0xd: 'SETUP', 0x3: 'DATA0', 0xb: 'DATA1', 0x7: 'DATA2', 0xf: 'MDATA',
  0x2: 'ACK', 0xa: 'NAK', 0xe: 'STALL', 0x6: 'NYET', 0xc: 'PRE', 0x8: 'SPLIT', 0x4: 'PING', 0x0: 'Reserved' };
const TOKEN = new Set(['OUT', 'IN', 'SETUP', 'PING']);
const DATA = new Set(['DATA0', 'DATA1', 'DATA2', 'MDATA']);

// CRC5 of the 11 token bits and CRC16 of the data bits, bit 0 sent first (USB 2.0 8.3.5; reflected polynomials).
function crc5(bits) {
  let c = 0x1f;
  for (const b of bits) c = (c ^ b) & 1 ? (c >>> 1) ^ 0x14 : c >>> 1;
  return c ^ 0x1f;
}
function crc16(bits) {
  let c = 0xffff;
  for (const b of bits) c = (c ^ b) & 1 ? (c >>> 1) ^ 0xa001 : c >>> 1;
  return c ^ 0xffff;
}
const num = (bits) => bits.reduce((a, b, i) => a | (b << i), 0);

export const usb = {
  title: 'USB',
  roles: ['dp', 'dm'],
  options: { speed: 'low' },
  decode(L, o, ctx) {
    const low = o.speed !== 'full';
    const Tb = 1 / ((low ? 1.5e6 : 12e6) * ctx.sec); // bit time in time units
    const dp = L.dp, dm = L.dm;
    const state = (t) => {
      const p = dp.at(t), m = dm.at(t);
      if (p === 0 && m === 0) return '0';
      if (p === 1 && m === 1) return '1';
      if (p < 0 || m < 0) return 'x';
      return (p === 1) === low ? 'K' : 'J'; // low speed: J = D- high; full speed: J = D+ high
    };
    // runs of one state: [t0, state]
    const runs = [];
    const t0 = Math.min(dp.n ? dp.t[0] : 0, dm.n ? dm.t[0] : 0);
    runs.push([t0, state(t0)]);
    for (const e of mergedEdges([dp, dm])) {
      const s = state(e.t);
      if (s !== runs[runs.length - 1][1]) runs.push([e.t, s]);
    }
    const end = Math.max(dp.end, dm.end);
    const items = [], fields = [], records = [];
    let errors = 0;
    for (let i = 0; i < runs.length - 1; i++) {
      if (!(runs[i][1] === 'J' && runs[i + 1][1] === 'K')) continue;
      // a packet: from this K to the first SE0
      const bits = [], times = [];
      let k = i + 1, prev = 'J', eop = null;
      for (; k < runs.length; k++) {
        const [ts, s] = runs[k];
        if (s === '0') { eop = ts; break; }
        if (s !== 'J' && s !== 'K') break;
        const te = k + 1 < runs.length ? runs[k + 1][0] : end;
        const n = Math.round((te - ts) / Tb);
        for (let j = 0; j < n; j++) {
          bits.push(j === 0 && s !== prev ? 0 : 1);
          times.push(ts + j * Tb);
        }
        prev = s;
      }
      if (eop === null) continue;
      const se0End = k + 1 < runs.length ? runs[k + 1][0] : end;
      const eopOk = se0End - eop >= 0.6 * Tb && k + 1 < runs.length && runs[k + 1][1] === 'J';
      // remove stuff bits
      const b = [], bt = [];
      let ones = 0, stuffErr = false;
      for (let j = 0; j < bits.length; j++) {
        if (ones === 6) {
          if (bits[j] === 1) stuffErr = true;
          ones = 0;
          continue;
        }
        b.push(bits[j]);
        bt.push(times[j]);
        ones = bits[j] ? ones + 1 : 0;
      }
      const tStart = runs[i + 1][0];
      const rec = { t0: tStart, t1: se0End, pid: null, name: null, addr: null, endp: null, data: null, crcOk: null,
                    syncOk: false, pidOk: false, stuffErr, eopOk };
      const tb = (j) => (j < bt.length ? bt[j] : eop);
      if (b.length >= 8) {
        rec.syncOk = num(b.slice(0, 8)) === 0x80;
        fields.push({ t0: tb(0), t1: tb(8), text: rec.syncOk ? 'SYNC' : 'SYNC error', short: 'S', cls: rec.syncOk ? 'ctrl' : 'err' });
      }
      if (b.length >= 16) {
        const p = num(b.slice(8, 16));
        rec.pid = p;
        rec.pidOk = (p & 0xf) === (~p >> 4 & 0xf);
        rec.name = rec.pidOk ? PID[p & 0xf] : 'PID error';
        fields.push({ t0: tb(8), t1: tb(16), text: `PID ${rec.name} (${hex(p)})`, short: rec.name, cls: rec.pidOk ? 'ctrl' : 'err' });
      }
      let text = rec.name || 'short packet';
      if (rec.pidOk && TOKEN.has(rec.name) && b.length >= 32) {
        rec.addr = num(b.slice(16, 23));
        rec.endp = num(b.slice(23, 27));
        rec.crcOk = crc5(b.slice(16, 27)) === num(b.slice(27, 32));
        fields.push({ t0: tb(16), t1: tb(23), text: `address ${rec.addr}`, short: `A${rec.addr}`, cls: 'data' });
        fields.push({ t0: tb(23), t1: tb(27), text: `endpoint ${rec.endp}`, short: `E${rec.endp}`, cls: 'data' });
        fields.push({ t0: tb(27), t1: tb(32), text: rec.crcOk ? 'CRC5 ok' : 'CRC5 error', short: 'C', cls: rec.crcOk ? 'ctrl' : 'err' });
        text = `${rec.name} addr ${rec.addr} ep ${rec.endp}`;
      } else if (rec.pidOk && rec.name === 'SOF' && b.length >= 32) {
        rec.frame = num(b.slice(16, 27));
        rec.crcOk = crc5(b.slice(16, 27)) === num(b.slice(27, 32));
        text = `SOF frame ${rec.frame}`;
      } else if (rec.pidOk && DATA.has(rec.name) && b.length >= 32) {
        const db = b.slice(16, b.length - 16);
        rec.data = [];
        for (let j = 0; j + 8 <= db.length; j += 8) {
          const v = num(db.slice(j, j + 8));
          rec.data.push(v);
          fields.push({ t0: tb(16 + j), t1: tb(24 + j), text: hex(v), short: hex(v).slice(2), cls: 'data' });
        }
        rec.crcOk = db.length % 8 === 0 && crc16(db) === num(b.slice(b.length - 16));
        fields.push({ t0: tb(b.length - 16), t1: tb(b.length), text: rec.crcOk ? 'CRC16 ok' : 'CRC16 error', short: 'C',
                      cls: rec.crcOk ? 'ctrl' : 'err' });
        text = `${rec.name} ${rec.data.length ? rec.data.map((v) => hex(v).slice(2)).join(' ') : '(0 bytes)'}`;
      }
      fields.push({ t0: eop, t1: se0End, text: eopOk ? 'EOP' : 'EOP error', short: 'E', cls: eopOk ? 'ctrl' : 'err' });
      const bad = !rec.syncOk || !rec.pidOk || rec.crcOk === false || stuffErr || !eopOk;
      if (bad) errors++;
      items.push({ t0: tStart, t1: se0End, text: text + (stuffErr ? ' (stuffing error)' : ''), short: rec.name || '?',
                   cls: bad ? 'err' : DATA.has(rec.name) ? 'data' : 'ctrl' });
      records.push(rec);
      i = k;
    }
    return {
      rows: [{ key: 'packets', label: 'packets', items }, { key: 'fields', label: 'fields', items: fields, minor: true }],
      records, errors, timing: [],
    };
  },
};

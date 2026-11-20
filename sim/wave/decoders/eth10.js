// SPDX-License-Identifier: Apache-2.0
// 10BASE-T (IEEE 802.3 clause 14) on the transmit pair (TD+, TD-, optional OE) or on a receiver's slicer and squelch
// outputs: the line level is +1, -1 or 0 (idle). Manchester code (7.3.1.1): every bit cell has a change in its
// middle and the level after it is the bit (+1: 1). The receiver takes the first change of a burst as the middle of
// the first bit and then the change nearest each following bit middle (within a quarter bit), so it resynchronises at
// every bit; the packet ends where no middle change follows (TP_IDL). A burst shorter than two bit times with no
// middle change is a link test pulse. Packet: preamble 1010..., SFD 10101011, then octets LSB first; the last four
// octets are the FCS (CRC-32, 3.2.9), checked here. Bits after the last whole octet are dribble bits.

import { hex, mergedEdges } from './lines.js';

function crc32ok(bytes) {
  if (bytes.length < 5) return false;
  let c = 0xffffffff;
  for (const v of bytes.slice(0, -4)) {
    c ^= v;
    for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0xedb88320 : c >>> 1;
  }
  c = (c ^ 0xffffffff) >>> 0;
  const f = bytes.slice(-4);
  return f[0] === (c & 0xff) && f[1] === ((c >>> 8) & 0xff) && f[2] === ((c >>> 16) & 0xff) && f[3] === (c >>> 24);
}
const mac = (b) => b.map((v) => hex(v).slice(2)).join(':');

export const eth10 = {
  title: '10BASE-T',
  roles: ['p', 'n', 'oe', 'rx', 'sq'],
  optional: ['p', 'n', 'oe', 'rx', 'sq'],
  options: {},
  decode(L, o, ctx) {
    const BT = 1e-7 / ctx.sec; // bit time (100 ns) in time units
    const tx = L.p && L.n;
    if (!tx && !(L.rx && L.sq)) return { error: 'pins: p and n (transmit pair) or rx and sq (slicer, squelch)', rows: [], records: [], errors: 0, timing: [] };
    const src = tx ? [L.p, L.n, L.oe] : [L.rx, L.sq];
    const level = (t) => {
      if (tx) {
        if (L.oe && L.oe.at(t) === 0) return 0;
        const p = L.p.at(t), n = L.n.at(t);
        return p === 1 && n !== 1 ? 1 : n === 1 && p !== 1 ? -1 : 0;
      }
      return L.sq.at(t) === 1 ? (L.rx.at(t) === 1 ? 1 : -1) : 0;
    };
    const ch = [];
    for (const e of mergedEdges(src)) {
      const v = level(e.t);
      if (!ch.length || ch[ch.length - 1][1] !== v) ch.push([e.t, v]);
    }
    const items = [], fields = [], records = [];
    let errors = 0;
    let i = 0;
    while (i < ch.length) {
      if (ch[i][1] === 0) { i++; continue; }
      // a burst: until the level is 0 for at least 2 bit times
      let j = i;
      while (j + 1 < ch.length && !(ch[j + 1][1] === 0 && (j + 2 >= ch.length || ch[j + 2][0] - ch[j + 1][0] >= 2 * BT))) j++;
      const t0 = ch[i][0];
      const t1 = j + 1 < ch.length ? ch[j + 1][0] : ch[j][0] + BT;
      // sign changes inside the burst
      const tr = [];
      for (let k = i + 1; k <= j; k++) if (ch[k][1] !== 0 && ch[k - 1][1] !== 0 && ch[k][1] !== ch[k - 1][1]) tr.push([ch[k][0], ch[k][1]]);
      if (!tr.length && t1 - t0 < 2 * BT) {
        items.push({ t0, t1, text: `link test pulse (${((t1 - t0) * ctx.sec * 1e9).toFixed(0)} ns)`, short: 'LP', cls: 'ctrl' });
        records.push({ t0, t1, kind: 'pulse' });
        i = j + 1;
        continue;
      }
      const bits = [], bt = [];
      if (tr.length) {
        let c = tr[0][0];
        bits.push(tr[0][1] > 0 ? 1 : 0);
        bt.push(c - BT / 2);
        let k = 1;
        for (;;) {
          while (k < tr.length && tr[k][0] < c + 0.75 * BT) k++;
          if (k >= tr.length || tr[k][0] > c + 1.25 * BT) break;
          c = tr[k][0];
          bits.push(tr[k][1] > 0 ? 1 : 0);
          bt.push(c - BT / 2);
          k++;
        }
      }
      // SFD: the first two 1s in a row after the preamble
      let sfd = -1;
      for (let k = 1; k < bits.length; k++) if (bits[k] === 1 && bits[k - 1] === 1) { sfd = k; break; }
      const rec = { t0, t1, kind: 'packet', bits: bits.length, preambleBits: sfd >= 0 ? sfd + 1 : null, bytes: [],
                    dribble: 0, fcsOk: false };
      if (sfd >= 0) {
        const d = bits.slice(sfd + 1);
        for (let k = 0; k + 8 <= d.length; k += 8) rec.bytes.push(d.slice(k, k + 8).reduce((a, b, m) => a | (b << m), 0));
        rec.dribble = d.length % 8;
        rec.fcsOk = crc32ok(rec.bytes);
        const at = (n) => (sfd + 1 + 8 * n < bt.length ? bt[sfd + 1 + 8 * n] : t1);
        fields.push({ t0, t1: at(0), text: `preamble and SFD (${sfd + 1} bits)`, short: 'P', cls: 'ctrl' });
        const B = rec.bytes;
        if (B.length >= 14) {
          fields.push({ t0: at(0), t1: at(6), text: `destination ${mac(B.slice(0, 6))}`, short: 'DA', cls: 'data' });
          fields.push({ t0: at(6), t1: at(12), text: `source ${mac(B.slice(6, 12))}`, short: 'SA', cls: 'data' });
          fields.push({ t0: at(12), t1: at(14), text: `type ${hex((B[12] << 8) | B[13], 4)}`, short: 'T', cls: 'data' });
        }
        if (B.length >= 18) {
          fields.push({ t0: at(14), t1: at(B.length - 4), text: `${B.length - 18} data octets`, short: 'D', cls: 'data' });
          fields.push({ t0: at(B.length - 4), t1: at(B.length), text: rec.fcsOk ? 'FCS ok' : 'FCS error', short: 'F',
                        cls: rec.fcsOk ? 'ctrl' : 'err' });
        }
      }
      if (!rec.fcsOk) errors++;
      const n = rec.bytes.length;
      items.push({ t0, t1, text: sfd < 0 ? `burst without SFD (${bits.length} bits)` :
        `frame ${n >= 4 ? n - 4 : n} octets + FCS, ${rec.fcsOk ? 'FCS ok' : 'FCS error'}${rec.dribble ? `, ${rec.dribble} dribble bits` : ''}`,
                   short: rec.fcsOk ? 'frame' : 'frame!', cls: rec.fcsOk ? 'data' : 'err' });
      records.push(rec);
      i = j + 1;
    }
    return {
      rows: [{ key: 'packets', label: 'packets', items }, { key: 'fields', label: 'fields', items: fields, minor: true }],
      records, errors, timing: [],
    };
  },
};

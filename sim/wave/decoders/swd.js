// SPDX-License-Identifier: Apache-2.0
// SWD (Arm ADIv5, IHI0031): every bit is read at the rising SWCLK edge (the host and the target both change SWDIO
// so that it is stable there). Request: Start 1, APnDP, RnW, A[2:3], even parity, Stop 0, Park 1; one turnaround;
// ACK 3 bits LSB first (OK 001, WAIT 010, FAULT 100 as bits 0..2 = 1,0,0 / 0,1,0 / 0,0,1); data 32 bits LSB first and
// even parity (read: after the ACK; write: after a turnaround). 50 or more 1 bits = line reset; 0xE79E (LSB first)
// after a line reset = JTAG-to-SWD select sequence.

import { X, hex } from './lines.js';

const DP_NAMES = { '0,1': 'DPIDR', '0,0': 'ABORT', '4,1': 'CTRL/STAT', '4,0': 'CTRL/STAT', '8,1': 'RESEND', '8,0': 'SELECT', '12,1': 'RDBUFF', '12,0': 'TARGETSEL' };
const ACKS = { 1: 'OK', 2: 'WAIT', 4: 'FAULT', 7: 'no response' };
const par = (v) => { let p = 0; while (v) { p ^= v & 1; v >>>= 1; } return p; };

export const swd = {
  title: 'SWD',
  roles: ['swclk', 'swdio'],
  options: {},
  decode(L, o, ctx) {
    const { swclk, swdio } = L;
    const S = []; // samples at the rising edges: [time, bit]
    for (let k = 1; k < swclk.n; k++) if (swclk.v[k] === 1 && swclk.v[k - 1] === 0) {
      const v = swdio.before(swclk.t[k]);
      S.push([swclk.t[k], v === X ? 1 : v]);
    }
    const row = [], records = [];
    let errors = 0;
    const N = S.length;
    const tEnd = (j) => (j + 1 < N ? S[j + 1][0] : S[j][0]);
    const val = (i, n) => { let v = 0; for (let j = 0; j < n; j++) v |= S[i + j][1] << j; return v >>> 0; };
    const SEL = 0xe79e;
    let i = 0;
    while (i < N) {
      if (S[i][1] === 1) {
        let j = i;
        while (j < N && S[j][1] === 1) j++;
        if (j - i >= 50) {
          row.push({ t0: S[i][0], t1: S[j - 1][0], text: `line reset (${j - i} bits of 1)`, short: 'reset', cls: 'ctrl' });
          records.push({ t0: S[i][0], type: 'line reset' });
          i = j;
          if (i + 16 <= N && val(i, 16) === SEL) {
            row.push({ t0: S[i][0], t1: tEnd(i + 15), text: 'JTAG-to-SWD select 0xE79E', short: 'select', cls: 'ctrl' });
            records.push({ t0: S[i][0], type: 'jtag-to-swd' });
            i += 16;
          }
          continue;
        }
      }
      if (S[i][1] === 1 && i + 12 <= N) {
        const b = (k) => S[i + k][1];
        const apndp = b(1), rnw = b(2), a = (b(3) << 2) | (b(4) << 3), p = b(5);
        if (b(6) === 0 && b(7) === 1 && p === par(apndp | (rnw << 1) | (b(3) << 2) | (b(4) << 3))) {
          const ack = val(i + 9, 3);
          const name = apndp ? `AP ${hex(a, 1)}` : `DP ${DP_NAMES[`${a},${rnw}`] || hex(a, 1)}`;
          row.push({ t0: S[i][0], t1: tEnd(i + 7), text: `${rnw ? 'read' : 'write'} ${name}`, short: `${rnw ? 'R' : 'W'} ${apndp ? 'AP' : 'DP'}${hex(a, 1)}`, cls: 'addr' });
          const ackName = ACKS[ack] || `ACK ${ack.toString(2).padStart(3, '0')}`;
          row.push({ t0: S[i + 9][0], t1: tEnd(i + 11), text: ackName, short: ack === 1 ? 'OK' : ack === 2 ? 'W' : '!', cls: ack === 1 ? 'ctrl' : 'warn' });
          const rec = { t0: S[i][0], type: 'transfer', apndp, rnw, addr: a, ack, data: null, parityOk: null };
          let next = i + 13; // request 8, turnaround 1, ACK 3, then a turnaround for no data phase
          if (ack === 1) {
            const d0 = rnw ? i + 12 : i + 13; // a write has a turnaround after the ACK
            if (d0 + 33 <= N) {
              const data = val(d0, 32);
              const pOk = S[d0 + 32][1] === par(data);
              rec.data = data; rec.parityOk = pOk;
              row.push({ t0: S[d0][0], t1: tEnd(d0 + 31), text: `${rnw ? 'read' : 'write'} data ${hex(data, 8)}`, short: hex(data, 8), cls: 'data' });
              row.push({ t0: S[d0 + 32][0], t1: tEnd(d0 + 32), text: pOk ? 'parity ok' : 'parity error', short: pOk ? 'P' : 'P!', cls: pOk ? 'ctrl' : 'err' });
              if (!pOk) errors++;
              next = rnw ? d0 + 34 : d0 + 33; // a read ends with a turnaround
            } else next = N;
          }
          records.push(rec);
          i = next;
          continue;
        }
      }
      i++;
    }
    return { rows: [{ key: 'tx', label: 'transfers', items: row }], records, errors, timing: [] };
  },
};

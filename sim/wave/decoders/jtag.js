// SPDX-License-Identifier: Apache-2.0
// JTAG (IEEE 1149.1): the TAP controller moves at each rising TCK edge by TMS (state diagram of IEEE 1149.1
// figure 6-1, also TI SSYA002C figure 3-3). In Shift-DR and Shift-IR each rising edge shifts one TDI bit in and one
// TDO bit out (LSB first). Five rising edges with TMS = 1 reach Test-Logic-Reset from any state.

import { X } from './lines.js';

const NEXT = {
  'Test-Logic-Reset': ['Run-Test/Idle', 'Test-Logic-Reset'],
  'Run-Test/Idle': ['Run-Test/Idle', 'Select-DR-Scan'],
  'Select-DR-Scan': ['Capture-DR', 'Select-IR-Scan'],
  'Capture-DR': ['Shift-DR', 'Exit1-DR'],
  'Shift-DR': ['Shift-DR', 'Exit1-DR'],
  'Exit1-DR': ['Pause-DR', 'Update-DR'],
  'Pause-DR': ['Pause-DR', 'Exit2-DR'],
  'Exit2-DR': ['Shift-DR', 'Update-DR'],
  'Update-DR': ['Run-Test/Idle', 'Select-DR-Scan'],
  'Select-IR-Scan': ['Capture-IR', 'Test-Logic-Reset'],
  'Capture-IR': ['Shift-IR', 'Exit1-IR'],
  'Shift-IR': ['Shift-IR', 'Exit1-IR'],
  'Exit1-IR': ['Pause-IR', 'Update-IR'],
  'Pause-IR': ['Pause-IR', 'Exit2-IR'],
  'Exit2-IR': ['Shift-IR', 'Update-IR'],
  'Update-IR': ['Run-Test/Idle', 'Select-DR-Scan'],
};
const SHORT = {
  'Test-Logic-Reset': 'TLR', 'Run-Test/Idle': 'RTI', 'Select-DR-Scan': 'SDR', 'Capture-DR': 'CDR', 'Shift-DR': 'ShDR',
  'Exit1-DR': 'E1DR', 'Pause-DR': 'PDR', 'Exit2-DR': 'E2DR', 'Update-DR': 'UDR', 'Select-IR-Scan': 'SIR',
  'Capture-IR': 'CIR', 'Shift-IR': 'ShIR', 'Exit1-IR': 'E1IR', 'Pause-IR': 'PIR', 'Exit2-IR': 'E2IR', 'Update-IR': 'UIR',
};
export const JTAG_STATES = Object.keys(NEXT);

const bitsHex = (bits) => { // bits[0] = first shifted (LSB)
  let s = '';
  for (let i = 0; i < bits.length; i += 4) {
    let d = 0;
    for (let j = 0; j < 4 && i + j < bits.length; j++) d |= bits[i + j] << j;
    s = d.toString(16).toUpperCase() + s;
  }
  return '0x' + s;
};

export const jtag = {
  title: 'JTAG',
  roles: ['tck', 'tms', 'tdi', 'tdo'],
  optional: ['tdo'],
  options: { initial: 'Test-Logic-Reset' },
  decode(L, o, ctx) {
    const { tck, tms, tdi, tdo } = L;
    const states = [], scans = [], tdoRow = [], records = [];
    let state = o.initial, ones = 0, errors = 0;
    let segStart = null, bi = [], bo = [], scan = 0;
    let boxState = null, boxT0 = 0, boxN = 0;
    const seq = []; // [time of the rising edge, state entered]
    const flushBox = (t) => {
      if (boxState !== null) states.push({ t0: boxT0, t1: t, text: boxN > 1 ? `${boxState} (${boxN})` : boxState, short: SHORT[boxState], cls: 'state' });
    };
    const rises = [];
    for (let k = 1; k < tck.n; k++) if (tck.v[k] === 1 && tck.v[k - 1] === 0) rises.push(tck.t[k]);
    for (let r = 0; r < rises.length; r++) {
      const t = rises[r];
      const m = tms.before(t);
      if (m === X) { errors++; continue; }
      if (state === 'Shift-DR' || state === 'Shift-IR') {
        if (segStart === null) { segStart = t; bi = []; bo = []; }
        const a = tdi.before(t), b = tdo ? tdo.before(t) : 0;
        bi.push(a === 1 ? 1 : 0);
        bo.push(b === 1 ? 1 : 0);
      }
      const next = NEXT[state][m];
      ones = m ? ones + 1 : 0;
      const now = ones >= 5 ? 'Test-Logic-Reset' : next;
      if (now === 'Capture-DR' || now === 'Capture-IR') scan++;
      if ((state === 'Shift-DR' || state === 'Shift-IR') && now !== state) {
        const reg = state.slice(-2);
        const tEnd = r + 1 < rises.length ? rises[r + 1] : t;
        scans.push({ t0: segStart, t1: tEnd, text: `${reg} TDI ${bitsHex(bi)} (${bi.length} bits)`, short: bitsHex(bi), cls: reg === 'IR' ? 'addr' : 'data' });
        if (tdo) tdoRow.push({ t0: segStart, t1: tEnd, text: `${reg} TDO ${bitsHex(bo)} (${bo.length} bits)`, short: bitsHex(bo), cls: reg === 'IR' ? 'addr' : 'data' });
        records.push({ t0: segStart, scan, reg, n: bi.length, tdi: bi.join(''), tdo: tdo ? bo.join('') : null });
        segStart = null;
      }
      // the state box of the state entered at this edge
      if (now !== boxState) { flushBox(t); boxState = now; boxT0 = t; boxN = 1; } else boxN++;
      seq.push([t, now]);
      state = now;
    }
    flushBox(rises.length ? rises[rises.length - 1] + (rises.length > 1 ? rises[rises.length - 1] - rises[rises.length - 2] : 0) : 0);
    const rows = [{ key: 'state', label: 'TAP state', items: states }, { key: 'tdi', label: 'TDI shift', items: scans }];
    if (tdo) rows.push({ key: 'tdo', label: 'TDO shift', items: tdoRow });
    return { rows, records, errors, timing: [], meta: { states: seq } };
  },
};

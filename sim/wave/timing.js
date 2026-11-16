// SPDX-License-Identifier: Apache-2.0
// Timing limits of the standards, with their sources, and the check of the measured times from the decoders.
// A limit: { min, max } in seconds (or as a fraction for '%' values, in Hz for 'Hz' values).

const UM = 'NXP UM10204 I2C-bus specification and user manual, Rev. 7.0 (1 October 2021), Table 11 (timing of the SDA and SCL bus lines), PDF page 44';
const DS = 'Maxim (Analog Devices) DS18B20 data sheet 19-7487 Rev 6 (7/19), AC Electrical Characteristics, page 3';

const I2C = {
  Sm: { fSCL: { max: 100e3 }, 'tHD;STA': { min: 4.0e-6 }, tLOW: { min: 4.7e-6 }, tHIGH: { min: 4.0e-6 }, 'tSU;STA': { min: 4.7e-6 },
        'tHD;DAT': { min: 0 }, 'tSU;DAT': { min: 250e-9 }, 'tSU;STO': { min: 4.0e-6 }, tBUF: { min: 4.7e-6 } },
  Fm: { fSCL: { max: 400e3 }, 'tHD;STA': { min: 0.6e-6 }, tLOW: { min: 1.3e-6 }, tHIGH: { min: 0.6e-6 }, 'tSU;STA': { min: 0.6e-6 },
        'tHD;DAT': { min: 0 }, 'tSU;DAT': { min: 100e-9 }, 'tSU;STO': { min: 0.6e-6 }, tBUF: { min: 1.3e-6 } },
};
const I2C_NAMES = {
  fSCL: 'SCL clock frequency', 'tHD;STA': 'hold time (repeated) START', tLOW: 'LOW period of SCL', tHIGH: 'HIGH period of SCL',
  'tSU;STA': 'set-up time for a repeated START', 'tHD;DAT': 'data hold time', 'tSU;DAT': 'data set-up time',
  'tSU;STO': 'set-up time for STOP', tBUF: 'bus free time between a STOP and a START',
};

const OW = {
  tRSTL: { min: 480e-6, name: 'reset time low' }, tRSTH: { min: 480e-6, name: 'reset time high (presence detect)' },
  tLOW0: { min: 60e-6, max: 120e-6, name: 'write-0 low time' }, tLOW1: { min: 1e-6, max: 15e-6, name: 'write-1 (and read) low time' },
  tREC: { min: 1e-6, name: 'recovery time' }, 'tSLOT + tREC': { min: 61e-6, name: 'time slot plus recovery, fall to next fall (tSLOT 60 us minimum plus tREC 1 us minimum)' },
};

// limits for a decoder result: { param: { min, max, unit, name, source } }
export function limitsFor(protocol, res, options = {}) {
  if (protocol === 'i2c') {
    const mode = res.meta && res.meta.mode === 'Fm' ? 'Fm' : 'Sm';
    const out = {};
    for (const [p, l] of Object.entries(I2C[mode])) {
      out[p] = { ...l, unit: p === 'fSCL' ? 'Hz' : 's', name: `${I2C_NAMES[p]} (${mode === 'Fm' ? 'Fast-mode' : 'Standard-mode'})`, source: UM };
    }
    return out;
  }
  if (protocol === 'onewire') {
    const out = {};
    for (const [p, l] of Object.entries(OW)) out[p] = { ...l, unit: 's', source: DS };
    return out;
  }
  if (protocol === 'uart') {
    const nb = 1 + (options.bits || 8) + ((options.parity || 'none') === 'none' ? 0 : 1) + 1;
    const calc = 0.5 / (nb - 0.5);
    return {
      'bit time error': {
        min: -0.01, max: 0.01, unit: '%', name: 'bit time against the nominal 1 / baud rate',
        source: 'No UART standard sets a rate tolerance. Limit shown: +/-1 %, the rate error the chip transmitter is designed to stay '
          + 'within (chip repository test/test_fw_full.py SHARE = 0.01, checked by check_tx). For reference, '
          + `a receiver that reads each bit at its middle reads the last frame bit (bit ${nb}) inside that bit while the total `
          + `error stays under 0.5 / ${nb - 0.5} = ${(calc * 100).toFixed(2)} % (calculation).`,
      },
    };
  }
  return {};
}

export function fmtValue(v, unit) {
  if (unit === '%') return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(3)} %`;
  if (unit === 'Hz') return v >= 1e6 ? `${(v / 1e6).toFixed(3)} MHz` : `${(v / 1e3).toFixed(2)} kHz`;
  const a = Math.abs(v);
  if (a >= 1e-3) return `${(v * 1e3).toFixed(3)} ms`;
  if (a >= 1e-6) return `${(v * 1e6).toFixed(3)} us`;
  return `${(v * 1e9).toFixed(1)} ns`;
}

export function fmtLimit(l) {
  if (l.min !== undefined && l.max !== undefined) return `${fmtValue(l.min, l.unit)} to ${fmtValue(l.max, l.unit)}`;
  if (l.min !== undefined) return `>= ${fmtValue(l.min, l.unit)}`;
  return `<= ${fmtValue(l.max, l.unit)}`;
}

const inside = (v, l) => (l.min === undefined || v >= l.min - 1e-15) && (l.max === undefined || v <= l.max + 1e-15);
const margin = (v, l) => Math.min(l.min === undefined ? Infinity : v - l.min, l.max === undefined ? Infinity : l.max - v);

// Per parameter: the worst instance, the count, pass/fail; each instance gets .ok and .limit
export function checkTiming(protocol, res, options) {
  const lim = limitsFor(protocol, res, options);
  const by = new Map();
  for (const m of res.timing || []) {
    const l = lim[m.param];
    if (!l) continue;
    m.limit = l;
    m.ok = inside(m.value, l);
    let e = by.get(m.param);
    if (!e) { e = { param: m.param, limit: l, n: 0, fails: 0, worst: null }; by.set(m.param, e); }
    e.n++;
    if (!m.ok) e.fails++;
    if (!e.worst || margin(m.value, l) < margin(e.worst.value, l)) e.worst = m;
  }
  return [...by.values()];
}

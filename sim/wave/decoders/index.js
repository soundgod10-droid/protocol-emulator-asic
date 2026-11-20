// SPDX-License-Identifier: Apache-2.0
// Protocol decoders of the waveform view. Each decoder: { title, roles, optional, options, decode(lines, options, ctx) }
// decode returns { rows: [{ key, label, items: [{ t0, t1, text, short, cls }], minor }], records, errors, timing, meta }.
// ctx.sec = seconds per time unit of the lines.

import { uart } from './uart.js';
import { spi } from './spi.js';
import { i2c } from './i2c.js';
import { can } from './can.js';
import { jtag } from './jtag.js';
import { swd } from './swd.js';
import { ps2 } from './ps2.js';
import { onewire } from './onewire.js';
import { usb } from './usb.js';
import { eth10 } from './eth10.js';
import { Line } from './lines.js';

export const PROTOCOLS = { uart, spi, i2c, can, jtag, swd, ps2, onewire, usb, eth10 };
export { Line };

// lines: { role: Line }; returns the decoder result, or { error } when a required pin is missing
export function runDecoder(protocol, lines, options = {}, ctx) {
  const d = PROTOCOLS[protocol];
  if (!d) return { error: `no decoder "${protocol}"`, rows: [], records: [], errors: 0, timing: [] };
  const missing = d.roles.filter((r) => !(d.optional || []).includes(r) && !lines[r]);
  if (missing.length) return { error: `pin missing: ${missing.join(', ')}`, rows: [], records: [], errors: 0, timing: [] };
  const o = { ...d.options, ...options };
  const res = d.decode(lines, o, ctx);
  for (const row of res.rows) row.items.sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
  return res;
}

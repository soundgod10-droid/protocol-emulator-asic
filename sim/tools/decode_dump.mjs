// SPDX-License-Identifier: Apache-2.0
// Runs the waveform page's decoders on every recorded sample, outside the browser (check L3, page side).
//
// Use: node sim/tools/decode_dump.mjs WAVE_DIR SAMPLES_DIR OUT.json
//   WAVE_DIR: a copy of the site's wave/ folder with a package.json {"type": "module"}, so that Node loads its .js
//   files as ES modules; SAMPLES_DIR: the *.vcd files and manifest.json.
// Writes, for each sample and decoder of the manifest: the decoder records, error count, error text and meta, the same
// objects that the page draws (wave/decoders/index.js runDecoder on the lines of wave/vcd.js parseVCD).

import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [waveDir, samplesDir, outPath] = process.argv.slice(2);
const { parseVCD } = await import(pathToFileURL(join(waveDir, 'vcd.js')).href);
const { runDecoder, Line } = await import(pathToFileURL(join(waveDir, 'decoders', 'index.js')).href);

const manifest = JSON.parse(readFileSync(join(samplesDir, 'manifest.json'), 'utf8'));
const res = {};
const t0 = performance.now();
for (const s of manifest.samples) {
  const v = parseVCD(readFileSync(join(samplesDir, s.file), 'utf8'));
  const by = {};
  v.signals.forEach((x) => { by[x.name] = x; });
  res[s.file] = { timescale: v.timescale, decoders: {} };
  for (const d of s.decoders) {
    const lines = {};
    for (const [role, name] of Object.entries(d.pins)) {
      const x = by[name];
      if (x) lines[role] = new Line(x.times, x.values, x.times.length, 1);
    }
    const a = performance.now();
    const r = runDecoder(d.protocol, lines, d.options || {}, { sec: v.timescale });
    res[s.file].decoders[d.name] = {
      protocol: d.protocol, records: r.records, errors: r.errors, error: r.error || null, meta: r.meta || null,
      ms: +(performance.now() - a).toFixed(2),
    };
    console.log(`${s.file} ${d.name}: ${r.error || `${r.records.length} records, ${r.errors} errors`}`);
  }
}
res._total_ms = +(performance.now() - t0).toFixed(1);
writeFileSync(outPath, JSON.stringify(res));

// SPDX-License-Identifier: Apache-2.0
// Demo of the waveform view with the recorded samples (samples/manifest.json).
// Link fragment: #s=<sample file without .vcd>&timing=<decoder name|off>&range=<ns0>-<ns1>&bits=1&theme=light|dark&a=<ns>&b=<ns>

import { WaveView } from './waveview.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.hash.slice(1));
const view = WaveView.mount($('wave'));
let manifest = null;

function setTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  view.setTheme(t || 'auto');
}
if (params.get('theme')) setTheme(params.get('theme'));
$('theme').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  setTheme(cur === 'dark' ? 'light' : 'dark');
});
$('wave').addEventListener('wavetheme', (e) => setTheme(e.detail));

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function load(file, first) {
  const s = manifest.samples.find((x) => x.file === file);
  const text = await (await fetch(`samples/${file}`)).text();
  view.loadVCD(text, { title: s.title, nsPerCycle: manifest.ns_per_cycle });
  for (const d of s.decoders) view.addDecoder(d.name, d);
  const tParam = first ? params.get('timing') : null;
  view.setTiming(tParam === 'off' ? null : tParam || s.timing || null);
  if (first && params.get('bits')) view.showMinor(true);
  if (first && params.get('range')) {
    const [a, b] = params.get('range').split('-').map(Number);
    view.showRange(a, b);
  }
  if (first && params.get('a')) view.setCursor(Number(params.get('a')) / manifest.ns_per_cycle);
  if (first && params.get('b')) view.setCursor(Number(params.get('b')) / manifest.ns_per_cycle, 'B');
  const w = s.window_cycles;
  $('info').innerHTML = [
    ['Test', `${esc(s.test)} (test/test_rtl_fw.py)`],
    ['Level', esc(s.level)],
    ['Simulator', esc(s.simulator)],
    ['Commit', `<code>${esc(s.commit.slice(0, 12))}</code>`],
    ['File', `${esc(s.file)}, ${s.bytes.toLocaleString('en-US')} bytes`],
    ['Window', `${esc(s.window_note)}; cycles ${w[0].toLocaleString('en-US')} to ${w[1].toLocaleString('en-US')} of the test${s.gl_equal_to_rtl ? `. Check: ${esc(s.gl_equal_to_rtl)}` : ''}`, true],
  ].map(([k, v, wide]) => `<div${wide ? ' class="wide"' : ''}><b>${k}</b>${v}</div>`).join('');
  window.__wave = { view, sample: s, manifest };
  document.body.dataset.ready = file;
}

async function main() {
  manifest = await (await fetch('samples/manifest.json')).json();
  const sel = $('sample');
  for (const level of ['RTL', 'gate level']) {
    const og = document.createElement('optgroup');
    og.label = level === 'RTL' ? 'RTL simulation' : 'Gate-level netlist simulation';
    for (const s of manifest.samples.filter((x) => x.level === level)) {
      const o = document.createElement('option');
      o.value = s.file;
      o.textContent = s.title;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  const want = params.get('s') ? `${params.get('s')}.vcd` : 'four_protocols_rtl.vcd';
  sel.value = manifest.samples.some((x) => x.file === want) ? want : manifest.samples[0].file;
  sel.addEventListener('change', () => load(sel.value, false));
  $('foot').innerHTML = `Clock of the tests: ${manifest.clock_ps.toLocaleString('en-US')} ps per cycle (60 MHz). The decoders and the timing checks run in this page; `
    + 'the timing limits name their source (standard, table, page) in the tooltip and under the waveform.';
  await load(sel.value, true);
}

main().catch((e) => { $('info').textContent = `Could not load the samples: ${e.message}`; });

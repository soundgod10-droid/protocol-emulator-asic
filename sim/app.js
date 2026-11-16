// SPDX-License-Identifier: Apache-2.0
// Page of the browser simulator: example list, editor, thread cards, pins, waveform, controls.
//
// The engine (worker.js) runs the example procedure of the model tests and streams the recorded run
// (pin changes, device inputs, host words, thread state). The page plays that run at the chosen
// speed; for any cycle it asks the engine for the exact model state (the engine runs the test bench
// from its nearest state copy to that cycle).

import { createEditor } from "./editor.js";
// The waveform viewer is a separate part (wave/); if it fails to load, a plain placeholder is used.
let WaveView = null;
async function loadWave() {
  try {
    ({ WaveView } = await import("./wave/waveview.js"));
  } catch (err) {
    console.warn("wave/waveview.js did not load, using the placeholder:", err);
    ({ WaveView } = await import("./waveview_stub.js"));
  }
}

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
if (params.get("theme")) document.documentElement.dataset.theme = params.get("theme");
const T_PAGE = performance.now();
const NF = new Intl.NumberFormat("en-US");
const fmt = (n) => NF.format(n);
const hex4 = (v) => "0x" + (v & 0xffff).toString(16).toUpperCase().padStart(4, "0");

// ---------- engine ----------

class Engine {
  constructor(onPy) {
    this.onPy = onPy;
    this.n = 0;
    this.pending = new Map();
    this.w = new Worker(new URL("worker.js", import.meta.url), { type: "module" });
    this.w.onmessage = (ev) => {
      const d = ev.data;
      if (d.kind === "py") {
        this.onPy(JSON.parse(d.data));
        return;
      }
      const p = this.pending.get(d.id);
      if (!p) return;
      this.pending.delete(d.id);
      if (d.ok) p.resolve(d.value);
      else p.reject(new Error(d.error));
    };
    this.w.onerror = (e) => {
      for (const p of this.pending.values()) p.reject(new Error(e.message || "worker error"));
      this.pending.clear();
    };
  }
  call(op, args) {
    const id = ++this.n;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.w.postMessage({ id, op, args });
    });
  }
  terminate() {
    this.w.terminate();
    for (const p of this.pending.values()) p.reject(new Error("engine restarted"));
    this.pending.clear();
  }
}

// ---------- state ----------

let engine = null;
let meta = null; // {examples, sources, fw}
let ready = false;
let bootInfo = null;
const edits = {}; // example id -> {file name: text}
let run = null;
const ui = { cursor: 0, playing: false, speed: 0, acc: 0, lastT: 0, exact: null, pending: false, dirty: true, follow: null, followCycle: null };
let view = null;
let editor = null;

function newRun(ex) {
  return {
    ex, programs: [], config: null, pins: [], inputs: [], words: [[], [], [], []], sends: [], states: [],
    frontier: 0, done: false, result: null, t0: performance.now(), files: [], openFile: null,
    sigs: [], wave: { ip: 0, ii: 0, P: [0, 0, 0], I: [0, 0], last: null, upto: -1 }, addr: new Map(),
  };
}

// ---------- engine messages ----------

function onPy(msg) {
  if (!run) return;
  if (msg.kind === "begin") {
    run.programs = msg.programs;
    run.config = msg.config;
    run.addr = new Map();
    msg.programs.forEach((p, fi) => {
      for (const [a, ln] of Object.entries(p.lines)) run.addr.set(+a, { file: fi, line: ln });
    });
    showFiles();
    ui.dirty = true;
    return;
  }
  const d = msg.delta;
  if (d) {
    for (const x of d.pins) run.pins.push(x);
    for (const x of d.inputs) run.inputs.push(x);
    d.words.forEach((ws, n) => ws.forEach((x) => run.words[n].push(x)));
    for (const x of d.sends) run.sends.push(x);
    run.frontier = d.cycle;
  }
  if (msg.state) run.states.push(msg.state);
  if (msg.kind === "done") {
    run.done = true;
    run.result = msg.result;
    run.wall = (performance.now() - run.t0) / 1000;
    showResult();
    const at = params.get("at");
    if (at !== null && !run.seeked) {
      run.seeked = true;
      ui.playing = false;
      setCursor(Math.min(+at, run.frontier));
    }
  }
  ui.dirty = true;
}

// ---------- example selection and runs ----------

function exById(id) {
  return meta.examples.find((e) => e.id === id);
}

async function restartEngine() {
  if (engine) engine.terminate();
  setEngine("busy", "Restarting the engine");
  engine = new Engine(onPy);
  const t = performance.now();
  await engine.call("init");
  setEngine("ready", `Engine restarted in ${((performance.now() - t) / 1000).toFixed(1)} s`);
}

async function startExample(id, keepCursor = false) {
  const ex = exById(id);
  if (run && !run.done) await restartEngine();
  run = newRun(ex);
  ui.exact = null;
  ui.pending = false;
  if (!keepCursor) ui.cursor = 0;
  ui.playing = params.get("at") === null;
  setupWave(ex);
  showExample(ex);
  setEngine("busy", `Running ${ex.id}`);
  updateButtons();
  try {
    await engine.call("start", { id, edits: edits[id] || {} });
    setEngine("ready", `Engine ready (Pyodide ${bootInfo.pyodide}, Python ${bootInfo.python.split(" ")[0]})`);
  } catch (err) {
    setEngine("error", "Engine error: " + err.message);
  }
  updateButtons();
}

function setEngine(cls, text) {
  const el = $("engine");
  el.className = "engine " + cls;
  $("engine-text").textContent = text;
}

// ---------- waveform ----------

function parsePin(name) {
  const m = /^(uo|uio|ui)(\d)$/.exec(name);
  return { t: m[1], k: +m[2] };
}

function setupWave(ex) {
  run.sigs = ex.pins.map(([pin, label]) => ({ name: label ? `${pin} ${label}` : pin, ...parsePin(pin) }));
  if (!view) view = WaveView.mount($("wave"));
  view.setSignals(run.sigs.map((s) => ({ name: s.name, width: 1, kind: "bit" })));
  view.setTimebase(1e9 / ex.clock_hz);
  for (const d of ex.decoders) {
    const options = { ...d.options };
    if (d.protocol === "uart" && options.bit_cycles) options.baud = Math.round(ex.clock_hz / options.bit_cycles);
    if (d.protocol === "can" && options.bit_cycles) options.bitrate = Math.round(ex.clock_hz / options.bit_cycles);
    try {
      view.addDecoder(d.name, { protocol: d.protocol, pins: d.pins, options });
    } catch (err) {
      console.warn("decoder", d.name, err);
    }
  }
  if ("onCursor" in view || true) view.onCursor = (c) => setCursor(Math.max(0, Math.min(run.frontier, Math.round(c))));
  $("wave-hint").textContent = `${ex.pins.length} pin${ex.pins.length === 1 ? "" : "s"}, bus levels; time at ${ex.clock_hz / 1e6} MHz`;
}

function sigValue(s, P, I) {
  if (s.t === "uo") return (P[0] >> s.k) & 1;
  if (s.t === "ui") return (I[0] >> s.k) & 1;
  return (P[2] >> s.k) & 1 ? (P[1] >> s.k) & 1 : (I[1] >> s.k) & 1;
}

function feedWave(upto) {
  const w = run.wave;
  if (upto < w.upto) {
    run.wave = { ip: 0, ii: 0, P: [0, 0, 0], I: [0, 0], last: null, upto: -1 };
    setupWave(run.ex);
    return feedWave(upto);
  }
  const { pins, inputs } = run;
  while (true) {
    const cp = w.ip < pins.length ? pins[w.ip][0] : Infinity;
    const ci = w.ii < inputs.length ? inputs[w.ii][0] : Infinity;
    const c = Math.min(cp, ci);
    if (c === Infinity || c > upto) break;
    while (w.ip < pins.length && pins[w.ip][0] === c) {
      const x = pins[w.ip++];
      w.P = [x[1], x[2], x[3]];
    }
    while (w.ii < inputs.length && inputs[w.ii][0] === c) {
      const x = inputs[w.ii++];
      w.I = [x[1], x[2]];
    }
    const vals = run.sigs.map((s) => sigValue(s, w.P, w.I));
    if (!w.last || vals.some((v, i) => v !== w.last[i])) {
      view.append(c, vals);
      w.last = vals;
    }
  }
  w.upto = upto;
  if (view.setCursor) view.setCursor(upto);
}

// ---------- values at the cursor from the recorded trace ----------

function at(list, cycle) {
  let lo = 0;
  let hi = list.length - 1;
  let r = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid][0] <= cycle) {
      r = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return r < 0 ? null : list[r];
}

function stateFor(cycle) {
  if (ui.exact && ui.exact.cycle === cycle) return { st: ui.exact, exact: true };
  const list = run.states;
  let best = null;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].cycle <= cycle) {
      best = list[i];
      break;
    }
  }
  if (!best && ui.exact) best = ui.exact;
  return { st: best, exact: false };
}

// ---------- rendering ----------

const PINS = [
  ...Array.from({ length: 8 }, (_, k) => ({ name: `uio${k}`, out: k, inp: k, t: "uio", k })),
  ...Array.from({ length: 7 }, (_, k) => ({ name: `uo${k}`, out: 8 + k, inp: null, t: "uo", k })),
  ...Array.from({ length: 5 }, (_, k) => ({ name: `ui${k}`, out: null, inp: 8 + k, t: "ui", k })),
];

function lineOf(pc) {
  const a = run.addr.get(pc);
  if (!a) return null;
  const f = run.programs[a.file];
  return { file: f.name, fi: a.file, line: a.line };
}

function sourceText(fi, line) {
  const f = run.programs[fi];
  if (!f) return "";
  const t = f.text.split("\n")[line - 1] || "";
  return t.replace(/;.*$/, "").trim();
}

function renderThreads(st, exact) {
  const slot = ui.cursor % 4;
  const el = $("threads");
  if (!st) {
    el.innerHTML = [0, 1, 2, 3]
      .map((n) => `<div class="th idle" style="--tc:var(--t${n})"><div class="th-head"><b>Thread ${n}</b></div><div class="waits">no state yet</div></div>`)
      .join("");
    return;
  }
  const cfg = run.config || {};
  el.innerHTML = st.threads
    .map((th, n) => {
      const used = cfg.run ? cfg.run[n] || th.run || th.pc !== cfg.start_pc[n] : true;
      const src = lineOf(th.pc);
      const where = src ? `${src.file}:${src.line}  ${sourceText(src.fi, src.line)}` : "";
      const h4 = (v) => (v & 0xffff).toString(16).toUpperCase().padStart(4, "0");
      const r = th.r.map((v, i) => `<span class="rk">r${i}</span><span class="rv">${h4(v)}</span><span class="rd">${v}</span>`).join("");
      const flag = (k, v) => `<span class="flag${v ? " set" : ""}">${k}</span>`;
      const fin = st.fifo_in[n].length;
      const fout = st.fifo_out[n].length;
      const hq = st.host_queue ? st.host_queue[n] : 0;
      if (!used) {
        return `<div class="th idle${n === slot ? " slot" : ""}" style="--tc:var(--t${n})"><div class="th-head"><b>Thread ${n}</b>
          <span class="badge off">STOP</span></div><div class="th-pc">not started by the host (RUN = 0)</div></div>`;
      }
      return `<div class="th${n === slot ? " slot" : ""}" style="--tc:var(--t${n})">
        <div class="th-head"><b>Thread ${n}</b>
          <span class="badge ${th.run ? "on" : "off"}">${th.run ? "RUN" : "STOP"}</span>
          <span class="badge late${th.status & 1 ? "" : " off"}">LATE</span>
          <span class="badge err${th.status & 2 ? "" : " off"}">ERR</span></div>
        <div class="th-pc" title="${escapeHtml(where)}">${used ? `PC ${th.pc} <span>${escapeHtml(where)}</span>` : "not started by the host (RUN = 0)"}</div>
        <div class="regs" title="registers: hex and decimal">${r}<span class="rk">T</span><span class="rv">${h4(th.t)}</span><span class="rd">${th.t}</span></div>
        <div class="flags">${flag("Z", th.z)}${flag("C", th.c)}${flag("N", th.n)}${flag("E", th.e)}
          <span class="fifo" title="host FIFO words: host to thread (waiting in the host), thread to host">in ${fin}${hq ? "+" + hq : ""} / out ${fout}</span></div>
        <div class="waits">${used && th.waits ? "<b>waits:</b> " + escapeHtml(th.waits) : "&nbsp;"}</div></div>`;
    })
    .join("");
  $("state-hint").textContent = exact ? `state at cycle ${fmt(st.cycle)}` : `nearest recorded state: cycle ${fmt(st.cycle)}`;
}

function renderPins(st) {
  const cfg = run.config;
  const used = new Map(run.ex.pins.map(([p, l]) => [p, l]));
  const P = at(run.pins, ui.cursor);
  const I = at(run.inputs, ui.cursor);
  const p3 = P ? [P[1], P[2], P[3]] : [0x7f, 0xff, 0];
  const i2 = I ? [I[1], I[2]] : [0, 0];
  const rows = PINS.map((pin) => {
    let drive = "";
    let lvl = null;
    if (pin.t === "uo") {
      lvl = (p3[0] >> pin.k) & 1;
      drive = `${lvl}`;
    } else if (pin.t === "uio") {
      const oe = (p3[2] >> pin.k) & 1;
      drive = oe ? `${(p3[1] >> pin.k) & 1}` : "released";
      lvl = oe ? (p3[1] >> pin.k) & 1 : (i2[1] >> pin.k) & 1;
    } else {
      drive = "input";
      lvl = (i2[0] >> pin.k) & 1;
    }
    const owner = cfg && pin.out !== null ? cfg.owner[pin.out] : null;
    const od = cfg && pin.out !== null ? cfg.od[pin.out] : false;
    let ev = "";
    if (st && pin.out !== null && st.slot[pin.out]) {
      const [c, v, oe] = st.slot[pin.out];
      ev = `${oe ? v : "Z"} at ${fmt(c)}`;
    }
    const isUsed = used.has(pin.name) || owner !== null;
    return `<tr class="${isUsed ? "" : "unused"}"><td class="mono">${pin.name}</td><td class="fn">${escapeHtml(used.get(pin.name) || "")}</td>
      <td class="mono">${drive}</td><td><span class="lvl ${lvl ? "h" : "l"}">${lvl}</span></td>
      <td class="own${owner !== null ? " t" + owner : ""}">${owner !== null ? "T" + owner : ""}</td>
      <td class="mono">${pin.out === null ? "" : od ? "OD" : "PP"}</td><td class="mono">${ev}</td></tr>`;
  });
  $("pins").innerHTML = `<thead><tr><th>Pin</th><th>Use</th><th>Drive</th><th>Level</th><th>Owner</th><th>Mode</th><th>Next event</th></tr></thead><tbody>${rows.join("")}</tbody>`;
}

function renderHost() {
  const c = ui.cursor;
  const cols = [0, 1, 2, 3].map((n) => {
    const out = run.words[n].filter((x) => x[0] <= c);
    const sends = run.sends.filter((x) => x[1] === n && x[0] <= c);
    if (!out.length && !sends.length) return "";
    const sentWords = sends.reduce((a, x) => a + x[2].length, 0);
    const lastSends = sends.slice(-2).map((x) => `<li><span>${fmt(x[0])}</span><span>${x[2].slice(0, 3).map(hex4).join(" ")}${x[2].length > 3 ? " +" + (x[2].length - 3) : ""}</span></li>`).join("");
    const lastOut = out.slice(-5).map((x) => `<li><span>${fmt(x[0])}</span><span>${hex4(x[1])}</span></li>`).join("");
    return `<div class="hcol" style="--tc:var(--t${n})"><h3>Thread ${n}</h3>
      <div class="dir">host to thread: ${fmt(sentWords)} words</div><ol>${lastSends}</ol>
      <div class="dir">thread to host: ${fmt(out.length)} words</div><ol>${lastOut}</ol></div>`;
  });
  const html = cols.join("");
  $("host").innerHTML = html || `<p class="hint" style="margin:0">no host words up to this cycle</p>`;
}

function renderEditorPc(st, exact) {
  if (!st || !editor || !run.programs.length) return;
  const cur = run.openFile;
  const list = [];
  const inFile = run.programs.map(() => []);
  let follow;
  // While paused on an exact state, scroll to the PC line of the thread chosen for "Step instr."
  // (or of the first thread in this file).
  const want = exact && !ui.playing && ui.followCycle !== st.cycle ? +$("stepi-thread").value : null;
  st.threads.forEach((th, n) => {
    const src = lineOf(th.pc);
    if (!src || !(th.run || (run.config && run.config.run[n]))) return;
    inFile[src.fi].push(n);
    if (src.file === cur) {
      list.push({ line: src.line, thread: n });
      if (ui.follow === n || (want !== null && (want === n || follow === undefined))) follow = src.line;
    }
  });
  if (want !== null || ui.follow !== null) ui.followCycle = st.cycle;
  editor.setPcLines(list, follow);
  ui.follow = null;
  [...$("tabs").children].forEach((b, i) => {
    const tags = inFile[i] ? inFile[i].map((n) => `<span class="pc-tag t${n}">T${n}</span>`).join("") : "";
    const label = b.dataset.label;
    const html = `${escapeHtml(label)}${tags ? ` <span class="pc-tags">${tags}</span>` : ""}`;
    if (b.innerHTML !== html) b.innerHTML = html;
  });
}

function render() {
  if (!run) return;
  const ex = run.ex;
  const ns = 1e9 / ex.clock_hz;
  $("cycle").textContent = fmt(ui.cursor);
  const us = (ui.cursor * ns) / 1000;
  $("time").textContent = us < 1000 ? `${us.toFixed(2)} us` : `${(us / 1000).toFixed(3)} ms`;
  $("slot").textContent = "T" + (ui.cursor % 4);
  const pct = run.frontier ? (100 * ui.cursor) / run.frontier : 0;
  $("bar").style.width = `${pct}%`;
  if (run.done) {
    const r = run.result || {};
    $("progress").textContent = `${fmt(run.frontier)} cycles computed in ${(r.wall_s || 0).toFixed(1)} s (${fmt(Math.round((r.cycles_main || 0) / (r.wall_s || 1)))} cycles/s)`;
  } else {
    $("progress").textContent = `computing: ${fmt(run.frontier)} cycles`;
  }
  feedWave(ui.cursor);
  const { st, exact } = stateFor(ui.cursor);
  renderThreads(st, exact);
  renderPins(st);
  renderHost();
  renderEditorPc(st, exact);
  $("run").textContent = ui.playing ? "Pause" : "Run";
  if (run.done && !exact && !ui.pending) requestExact();
}

async function requestExact() {
  if (!run || !run.done || ui.pending) return;
  ui.pending = true;
  const c = ui.cursor;
  const r = run;
  try {
    const st = await engine.call("state", { cycle: c });
    if (r !== run) return;
    ui.exact = st;
  } catch (err) {
    console.warn(err);
  } finally {
    ui.pending = false;
    ui.dirty = true;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]);
}

// ---------- example panel and files ----------

function showExample(ex) {
  $("ex-title").textContent = `${ex.id}: ${ex.title}`;
  $("ex-text").textContent = ex.text;
  $("ex-source").textContent = ex.source;
  $("ex-clock").textContent = `${ex.clock_hz / 1e6} MHz (${(1e9 / ex.clock_hz).toFixed(3)} ns per cycle)`;
  $("ex-check").textContent = "running";
  $("ex-check").className = "";
  $("ex-run").textContent = "";
  $("ex-hash").textContent = "";
  $("asm-msg").textContent = "";
  $("asm-msg").className = "asm-msg";
}

function showResult() {
  const r = run.result;
  const el = $("ex-check");
  el.className = r.ok ? "pass" : "fail";
  el.textContent = (r.ok ? "pass: " : "fail: ") + r.text;
  const cps = r.wall_s ? Math.round(r.cycles_main / r.wall_s) : 0;
  const other = r.cycles_other ? `, plus ${fmt(r.cycles_other)} cycles of the procedure's search runs` : "";
  $("ex-run").textContent = r.has_main ? `${fmt(r.cycles || 0)} cycles in ${r.wall_s.toFixed(2)} s, ${fmt(cps)} cycles/s${other}` : "no run";
  $("ex-hash").textContent = r.hash || "";
  if (r.asm_errors && r.asm_errors.length) {
    const f = r.failed_files[0];
    $("asm-msg").textContent = r.asm_errors.join("\n");
    $("asm-msg").className = "asm-msg err";
    if (f) {
      run.programs = [{ name: f.name, base: f.base, text: f.text, size: 0, lines: {} }];
      showFiles();
      editor.setErrors(parseErrors(r.asm_errors, f.name));
    }
  }
}

function parseErrors(list, name) {
  return list
    .map((e) => {
      const m = /^(.*?):(\d+): (.*)$/.exec(e);
      return m && m[1] === name ? { line: +m[2], message: m[3] } : null;
    })
    .filter(Boolean);
}

function showFiles() {
  const tabs = $("tabs");
  tabs.innerHTML = "";
  editor.reset();
  run.programs.forEach((p, i) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.label = p.name === "<test>" ? "program (from the test)" : p.name;
    b.textContent = b.dataset.label;
    b.dataset.i = i;
    b.onclick = () => openFile(i);
    tabs.appendChild(b);
  });
  if (run.programs.length) openFile(0);
  else editor.open("", "", true);
}

function openFile(i) {
  const p = run.programs[i];
  const exEdits = edits[run.ex.id] || {};
  const editable = meta && Object.prototype.hasOwnProperty.call(meta.fw, p.name);
  const text = exEdits[p.name] !== undefined ? exEdits[p.name] : p.text;
  run.openFile = p.name;
  editor.open(p.name, text, !editable);
  [...$("tabs").children].forEach((b, k) => {
    b.classList.toggle("on", k === i);
    const name = run.programs[k].name;
    b.classList.toggle("edited", exEdits[name] !== undefined);
  });
  $("revert").disabled = exEdits[p.name] === undefined;
  $("apply").disabled = !editable || !Object.keys(exEdits).length;
  const where = editable ? `fw/${p.name} at address ${p.base}, ${p.size} words; the host's .equ values are filled in` : "text from the test function (read only)";
  $("asm-msg").className = "asm-msg";
  $("asm-msg").textContent = where;
  ui.dirty = true;
}

let lintTimer = 0;
function onEdit(name, text) {
  if (!run || !name) return;
  const p = run.programs.find((x) => x.name === name);
  if (!p || !Object.prototype.hasOwnProperty.call(meta.fw, name)) return;
  edits[run.ex.id] = edits[run.ex.id] || {};
  if (text === p.text) delete edits[run.ex.id][name];
  else edits[run.ex.id][name] = text;
  const i = run.programs.indexOf(p);
  const b = $("tabs").children[i];
  if (b) b.classList.toggle("edited", edits[run.ex.id][name] !== undefined);
  $("apply").disabled = !Object.keys(edits[run.ex.id]).length;
  $("revert").disabled = edits[run.ex.id][name] === undefined;
  clearTimeout(lintTimer);
  lintTimer = setTimeout(async () => {
    if (!run.done) return;
    const r = await engine.call("lint", { name, base: p.base, text });
    if (run.openFile !== name) return;
    editor.setErrors(parseErrors(r.errors, name));
    $("asm-msg").className = r.errors.length ? "asm-msg err" : "asm-msg";
    $("asm-msg").textContent = r.errors.length ? r.errors.join("\n") : `assembles: ${r.size} words (was ${p.size})`;
  }, 350);
}

// ---------- controls ----------

function setCursor(c) {
  ui.cursor = Math.max(run.config ? run.config.cycle0 : 0, c);
  ui.dirty = true;
}

function updateButtons() {
  const on = ready && run;
  for (const id of ["run", "step", "stepi", "reset"]) $(id).disabled = !on;
  $("example").disabled = !ready;
}

async function stepCycle() {
  if (!run) return;
  ui.playing = false;
  if (ui.cursor < run.frontier) setCursor(ui.cursor + 1);
  else if (run.done) await extend(1);
}

async function extend(n) {
  try {
    await engine.call("extend", { cycles: n });
    setCursor(Math.min(ui.cursor + n, run.frontier));
  } catch (err) {
    console.warn(err);
  }
}

async function stepInstr() {
  if (!run || !run.done) return;
  ui.playing = false;
  const n = +$("stepi-thread").value;
  const r = await engine.call("stepi", { cycle: ui.cursor, thread: n });
  ui.exact = r.state;
  ui.follow = n;
  setCursor(r.state.cycle);
  if (r.note) $("state-hint").textContent = r.note;
}

function frame(t) {
  const dt = Math.min(0.25, (t - ui.lastT) / 1000 || 0);
  ui.lastT = t;
  if (run && ui.playing) {
    const before = ui.cursor;
    if (ui.speed === 0 && !run.done) ui.cursor = run.frontier;
    else {
      const rate = ui.speed || Math.max(100000, run.frontier / 4);
      ui.acc += rate * dt;
      const k = Math.floor(ui.acc);
      ui.acc -= k;
      ui.cursor = Math.min(run.frontier, ui.cursor + k);
    }
    if (run.done && ui.cursor >= run.frontier) ui.playing = false;
    if (ui.cursor !== before) ui.dirty = true;
  }
  if (ui.dirty) {
    ui.dirty = false;
    render();
  }
  requestAnimationFrame(frame);
}

function bindControls() {
  $("run").onclick = () => {
    if (!run) return;
    if (!ui.playing && run.done && ui.cursor >= run.frontier) setCursor(0);
    ui.playing = !ui.playing;
    ui.dirty = true;
  };
  $("step").onclick = stepCycle;
  $("stepi").onclick = stepInstr;
  $("reset").onclick = () => {
    ui.playing = false;
    setCursor(0);
  };
  $("speed").onchange = (e) => {
    ui.speed = +e.target.value;
  };
  $("example").onchange = (e) => {
    history.replaceState(null, "", `?ex=${e.target.value}`);
    startExample(e.target.value);
  };
  $("apply").onclick = () => startExample(run.ex.id);
  $("revert").onclick = () => {
    const ex = edits[run.ex.id] || {};
    delete ex[run.openFile];
    startExample(run.ex.id);
  };
  document.addEventListener("keydown", (e) => {
    if (e.target.closest && e.target.closest(".cm-editor, select, input")) return;
    if (e.key === " ") {
      e.preventDefault();
      $("run").click();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      (e.shiftKey ? stepInstr : stepCycle)();
    }
  });
}

// ---------- check mode ----------

async function checkMode() {
  $("check-card").hidden = false;
  let ref = {};
  try {
    const j = await (await fetch("check/cpython.json", { cache: "no-store" })).json();
    for (const r of j.examples) ref[r.id] = r;
    $("check-hint").textContent = `CPython reference: ${j.meta.python.split(" ")[0]}, ${j.meta.date}`;
  } catch (err) {
    $("check-hint").textContent = "no CPython reference file (check/cpython.json)";
  }
  const ids = params.get("ids") ? params.get("ids").split(",") : meta.examples.map((e) => e.id);
  const rows = [];
  const table = $("check");
  const head = `<thead><tr><th>Example</th><th>Test check</th><th class="r">Cycles</th><th class="r">Time</th><th class="r">Cycles/s</th><th>Browser hash</th><th>CPython hash</th><th>Equal</th></tr></thead>`;
  const draw = () => {
    table.innerHTML = head + "<tbody>" + rows.map((r) => `<tr><td>${r.id}</td><td class="${r.ok ? "pass" : "fail"}">${r.ok ? "pass" : "fail"}</td>
      <td class="r mono">${fmt(r.cycles || 0)}</td><td class="r mono">${r.wall_s.toFixed(2)} s</td><td class="r mono">${fmt(Math.round(r.cycles_per_s))}</td>
      <td class="mono">${(r.hash || "").slice(0, 16)}</td><td class="mono">${(r.ref || "").slice(0, 16)}</td>
      <td class="${r.equal ? "pass" : "fail"}">${r.equal ? "yes" : "no"}</td></tr>`).join("") + "</tbody>";
  };
  for (const id of ids) {
    setEngine("busy", `Check: running ${id}`);
    const r = await engine.call("check", { id });
    r.cycles_per_s = r.wall_s ? r.cycles_main / r.wall_s : 0;
    r.ref = ref[id] ? ref[id].hash : null;
    r.equal = !!r.hash && r.hash === r.ref;
    rows.push(r);
    draw();
  }
  const all = rows.every((r) => r.equal && r.ok);
  setEngine(all ? "ready" : "error", all ? `Check done: ${rows.length} of ${rows.length} hashes equal` : "Check done: differences");
  const out = { done: true, all_equal: all, boot: bootInfo, page_ms: performance.now() - T_PAGE, ua: navigator.userAgent, rows };
  $("check-result").textContent = JSON.stringify(out);
  document.title = all ? "CHECK PASS" : "CHECK FAIL";
}

// ---------- start ----------

async function boot() {
  editor = createEditor($("editor"), { onChange: onEdit });
  await loadWave();
  bindControls();
  requestAnimationFrame(frame);
  engine = new Engine(onPy);
  setEngine("busy", "Loading the engine (Pyodide, chip files)");
  try {
    bootInfo = await engine.call("init");
  } catch (err) {
    setEngine("error", "Engine failed to load: " + err.message);
    return;
  }
  meta = await engine.call("meta");
  const main = performance.getEntriesByType("resource").concat(performance.getEntriesByType("navigation"));
  const pageBytes = main.reduce((a, e) => a + (e.transferSize || 0), 0);
  const engBytes = bootInfo.resources.reduce((a, e) => a + (e.transfer || 0), 0);
  const engSize = bootInfo.resources.reduce((a, e) => a + (e.decoded || 0), 0);
  const pageSize = main.reduce((a, e) => a + (e.decodedBodySize || 0), 0);
  bootInfo.engine_size = engSize;
  bootInfo.page_size = pageSize;
  bootInfo.page_bytes = pageBytes;
  bootInfo.engine_bytes = engBytes;
  bootInfo.ready_page_ms = performance.now() - T_PAGE;
  ready = true;
  window.__v1 = { boot: bootInfo, editor, ui, run: () => run };
  setEngine("ready", `Engine ready in ${(bootInfo.ready_page_ms / 1000).toFixed(1)} s`);
  const src = meta.sources;
  $("foot-src").innerHTML = `Chip files: <a href="${src.repository}/tree/${src.commit}">${escapeHtml(src.branch)} ${src.commit.slice(0, 10)}</a>
    (${Object.keys(src.files).length} files, SHA-256 checked when loaded). Engine: Pyodide ${bootInfo.pyodide}, Python ${bootInfo.python.split(" ")[0]}.
    Files: ${(engSize / 1e6).toFixed(1)} MB for the engine, ${(pageSize / 1e6).toFixed(2)} MB for the page${engBytes < engSize / 2 ? " (from the browser cache this time)" : ""}; engine ready after ${(bootInfo.ready_page_ms / 1000).toFixed(1)} s.`;
  const sel = $("example");
  sel.innerHTML = meta.examples.map((e) => `<option value="${e.id}">${e.id}: ${escapeHtml(e.title)}</option>`).join("");
  updateButtons();
  if (params.get("check")) {
    await checkMode();
    return;
  }
  const first = params.get("ex") && exById(params.get("ex")) ? params.get("ex") : "E1";
  sel.value = first;
  if (params.get("speed")) {
    $("speed").value = params.get("speed");
    ui.speed = +params.get("speed");
  }
  await startExample(first);
}

boot();

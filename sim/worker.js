// SPDX-License-Identifier: Apache-2.0
// Web worker: loads Pyodide and the chip files of one commit, and runs the example procedures.
//
// Messages from the page: {id, op, args}. Replies: {id, ok, value} or {id, ok: false, error}.
// The runner also sends {kind: "py", data: <JSON text>} while a procedure runs (begin, chunk, done).

import { loadPyodide } from "./vendor/pyodide-314.0.8/pyodide.mjs";

const T0 = performance.now();
let py = null;
let glue = null;
const timing = {};

async function sha256(buf) {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function init() {
  const base = new URL("./", import.meta.url);
  const pyodideReady = loadPyodide({ indexURL: new URL("vendor/pyodide-314.0.8/", base).href });
  const man = await (await fetch(new URL("py/SOURCES.json", base))).json();
  const ours = ["v1_runner.py", "v1_glue.py", "examples.py", "stub/cocotb/__init__.py"];
  const chip = Object.keys(man.files);
  const fetched = await Promise.all([
    ...chip.map(async (rel) => {
      const buf = await (await fetch(new URL("py/pe/" + rel, base))).arrayBuffer();
      const h = await sha256(buf);
      if (h !== man.files[rel].sha256) throw new Error(`${rel}: SHA-256 ${h} differs from SOURCES.json`);
      return ["pe/" + rel, buf];
    }),
    ...ours.map(async (rel) => [rel, await (await fetch(new URL("py/" + rel, base))).arrayBuffer()]),
  ]);
  timing.files_ms = performance.now() - T0;
  py = await pyodideReady;
  timing.pyodide_ms = performance.now() - T0;
  const root = "/home/pyodide/v1";
  const write = (rel, buf) => {
    const path = `${root}/${rel}`;
    const dirs = path.split("/").slice(1, -1);
    let d = "";
    for (const part of dirs) {
      d += "/" + part;
      try {
        py.FS.mkdir(d);
      } catch (e) {
        /* exists */
      }
    }
    py.FS.writeFile(path, new Uint8Array(buf));
  };
  for (const [rel, buf] of fetched) write(rel, buf);
  write("SOURCES.json", new TextEncoder().encode(JSON.stringify(man)));
  py.runPython(`import sys; sys.path.insert(0, "${root}")`);
  glue = py.pyimport("v1_glue");
  glue.set_post((text) => postMessage({ kind: "py", data: text }));
  timing.ready_ms = performance.now() - T0;
  const res = performance.getEntriesByType("resource").map((e) => ({
    name: e.name.replace(base.href, ""),
    transfer: e.transferSize,
    encoded: e.encodedBodySize,
    decoded: e.decodedBodySize,
  }));
  return { timing, resources: res, python: py.runPython("import sys; sys.version"), pyodide: py.version };
}

const ops = {
  init,
  meta: () => JSON.parse(glue.meta()),
  start: ({ id, edits, limit }) => JSON.parse(glue.start(id, JSON.stringify(edits || {}), limit || 4000000)),
  state: ({ cycle }) => JSON.parse(glue.state(cycle)),
  stepi: ({ cycle, thread }) => JSON.parse(glue.stepi(cycle, thread)),
  extend: ({ cycles }) => JSON.parse(glue.extend(cycles)),
  lint: ({ name, base, text }) => JSON.parse(glue.lint(name, base, text)),
  check: ({ id }) => {
    const t = performance.now();
    const r = JSON.parse(glue.check(id));
    r.worker_ms = performance.now() - t;
    return r;
  },
};

let queue = Promise.resolve();
self.onmessage = (ev) => {
  const { id, op, args } = ev.data;
  queue = queue.then(async () => {
    try {
      const value = await ops[op](args || {});
      postMessage({ id, ok: true, value });
    } catch (err) {
      postMessage({ id, ok: false, error: String(err && err.message ? err.message : err) });
    }
  });
};

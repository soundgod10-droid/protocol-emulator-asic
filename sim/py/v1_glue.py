# SPDX-License-Identifier: Apache-2.0
"""Calls of the page's web worker into the runner. Every function returns a JSON text.

The worker gives `post`, a function that sends one JSON text to the page.
"""

import json
import os
import time

import examples
import pe_asm
import v1_runner

FW = os.path.join(v1_runner.PE, "fw")
ORIGINAL = {n: open(os.path.join(FW, n), encoding="utf-8").read() for n in sorted(os.listdir(FW)) if n.endswith(".s")}
POST = None
SESSION = None


def set_post(fn):
    global POST
    POST = fn


def _post(msg):
    POST(json.dumps(msg, separators=(",", ":")))


def meta():
    src = json.load(open(os.path.join(v1_runner.HERE, "SOURCES.json")))
    return json.dumps({"examples": examples.meta(), "sources": src, "fw": ORIGINAL})


def _write_files(edits):
    """Puts the firmware files of the commit back, then the reader's edits (only in the worker's file system)."""
    for name, text in ORIGINAL.items():
        body = edits.get(name, text)
        with open(os.path.join(FW, name), encoding="utf-8") as f:
            if f.read() == body:
                continue
        with open(os.path.join(FW, name), "wb") as f:
            f.write(body.encode("utf-8"))


def start(ex_id, edits_json="{}", limit=v1_runner.LIMIT):
    """Runs an example; sends begin, chunk and done messages; keeps the session for state requests."""
    global SESSION
    edits = json.loads(edits_json)
    _write_files(edits)
    s = v1_runner.Session(examples.BY_ID[ex_id], post=_post, limit=int(limit))
    SESSION = s
    s.run()
    res = dict(s.result)
    if s.main is not None:
        s.main.post("done", result=res)
    else:
        _post({"kind": "done", "result": res})
    _write_files({})
    return json.dumps({"ok": res["ok"]})


def state(cycle):
    s = SESSION
    if s is None or s.main is None:
        return "null"
    return json.dumps(s.main.state_at(int(cycle)), separators=(",", ":"))


def stepi(cycle, n):
    s = SESSION
    st, note = s.main.step_instruction(int(cycle), int(n))
    return json.dumps({"state": st, "note": note}, separators=(",", ":"))


def extend(cycles):
    s = SESSION
    s.extend(int(cycles))
    s.main.post("chunk")
    return json.dumps({"cycle": s.main.bench.m.cycle})


def lint(name, base, text):
    try:
        prog = v1_runner.ORIG_ASSEMBLE(text, name, int(base))
    except pe_asm.AsmError as err:
        return json.dumps({"errors": list(err.errors)})
    return json.dumps({"errors": [], "size": prog.size})


def check(ex_id):
    """Runs one example without messages; returns its hash, cycles and time (check mode of the page)."""
    _write_files({})
    t0 = time.perf_counter()
    s = v1_runner.run_example(examples.BY_ID[ex_id])
    r = dict(s.result)
    r.pop("log", None)
    r.pop("failed_files", None)
    r["id"] = ex_id
    r["total_s"] = time.perf_counter() - t0
    return json.dumps(r)

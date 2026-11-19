#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Mutation testing of the RTL tests.

What it does:
  plan    Finds every mutation site in the synthesized RTL files (info.yaml source_files, without the
          black-box macro file and without src/pe_cells.v, whose chip content is two library cells), gives each
          mutant an id from a hash of its file, line text and change, takes a hash sample (--rate), deals the
          sample into shards and picks one canary per file group.
  run     Runs one shard: each mutant is one changed copy of src/, built with Icarus Verilog and run through a
          ladder of the existing cocotb tests, cheapest rung first; the first failing rung kills it. Each rung
          first runs on the unchanged RTL in the same job (baseline): it must pass, and its time sets the
          rung's timeout (3 times the baseline, at least 2 minutes more). The canaries must be killed.
  report  Joins the shard results, checks that every sampled mutant has exactly one result, that every
          baseline passed and every canary was killed, and writes the tables.

Why:
  A test suite that passes on the RTL shows only that the RTL meets it. A mutant is a small, readable change of
  the RTL (one operator, one constant, one condition, one held register). A mutant that every test passes shows
  a place where the tests do not look, or a change that cannot alter any output (an equivalent mutant). The
  survivors are read one by one (docs/verification.md).

Operators: rel (== != < <= > >=), logic (&& ||), bit (& | ^, binary), not (remove ~ or !), arith (+ -, binary),
shift (<< >>), const (a literal +1 and -1, or a 1-bit literal inverted), stuck (an if condition made 1'b1 or
1'b0), hold (a register assignment made to keep its value). Not mutated: comments, `ifdef FORMAL blocks and any
code that is not in both the simulation and the synthesis, declarations (except the expression after = of a
wire, reg or localparam), bit and part selects, replication counts, for-loop headers, and every line that names
the build parameters SPEC or FULL (the configuration of the line helpers is not under test here).

Conditions: RTL simulation (Icarus Verilog, behavioural latch and clock-gate models, zero delay). A killed
mutant shows that a test sees the change in logic simulation; it says nothing about timing or the layout.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
TEST = os.path.join(ROOT, "test")

NOT_MUTATED = {"sram_bb.v", "pe_cells.v"}
GROUPS = {"pe_slu.v": "slu", "pe_slu_loop.v": "slu",
          "pe_host_port.v": "host", "pe_host_fifo.v": "host", "pe_latch_ram.v": "host"}  # else "core"

# Rungs: (cocotb modules, test filter). Times from the RTL test job (results.xml of test run 44266106276).
T = r"(^|\.)"
RUNGS = {
    "core_dir": (["test_core"], T + r"test_core_(alu_flags|start_top|shifts|branches_memory|time|out_events|horizon|"
                 r"sample_wait|spike_filter|host_io|illegal_halt|barrel|wait_far_expired|sample_input15)$"),  # 52 s
    "host": (["test"], T + r"test_(reset_keeps_line_idle|reset_during_frame|fixed_patterns|host_id_status_rules|"
             r"host_load_readback|host_sck_limit|stop_start|r2_stop_pins|host_status_reserved_bits|"
             r"host_partial_byte|host_default_start|helper_pin_stopped_level|host_reset_clears_config)$"),  # 49 s
    "fw_fast": (["test_rtl_fw"], T + r"test_rtl_(uart_tx|spi_ctl|spi_tgt|jtag|swd|pdi|four_protocols|trigger|"
                r"pull_rule)$"),                                                                 # 211 s
    "slu_unit": (["test_slu"], T + r"test_slu_(usb_tx|usb_rx|eth_tx|eth_rx|tx_late_underflow|eth_collision|"
                 r"usb_line_conditions|rx_state_codes|tx_buffer_full|line_after_reset|"
                 r"squelch_after_idle|ports_after_reset|rxinfo_words|txtime_lead_limit|eth_frame_end|"
                 r"tx_write_at_take|tstamp_hold|crca_after_frame|link_pulse_width|sel_last_pin|port_rules|"
                 r"line_cond_exact|runt_crc|rx_overflow|clk_part_word)$"),                       # 109 s
    "slu_loop": (["test_slu"], T + r"test_slu_loop_(equiv|window_edge|acq_window|acq_end)$"),    # 70 s
    "slu_clk": (["test_slu_clk"], T + r"test_slc_\w+$"),                                         # 398 s (local)
    "slu_chip": (["test_slu"], T + r"test_slu_chip_\w+$"),                                       # 12 s
    "dev_fast": (["test_usb_dev", "test_eth_dev"], T + r"test_(usb_dev_(enumerate|errors|hid)|"
                 r"eth_dev_(rx|tx|collision))$"),                                                # 332 s
    "core_rand": (["test_core"], T + r"test_core_(random|fw_uart_tx)$"),                         # 1,146 s
    "fw_slow": (["test", "test_rtl_fw", "test_i2c_tgt"], T + r"test_(rtl_(uart_rx|i2c|can|ps2|onewire|am2302)|"
                r"i2c_tgt_rtl|counter_sequence|r2_host_sck_clocks)$"),                           # 1,263 s
    "dev_slow": (["test_slu", "test_usb_dev", "test_eth_dev"], T + r"test_(slu_jabber|usb_dev_(drift|suspend)|"
                 r"eth_dev_(link|nlp_period))$"),                                                # 1,640 s
}
LADDERS = {
    "core": ["core_dir", "host", "fw_fast", "slu_chip", "core_rand", "fw_slow", "dev_fast", "dev_slow"],
    "host": ["host", "core_dir", "fw_fast", "slu_chip", "fw_slow", "core_rand", "dev_fast", "dev_slow"],
    "slu": ["slu_unit", "slu_loop", "slu_clk", "slu_chip", "dev_fast", "dev_slow"],
}
# Canary per group: a mutant that rung 1 must kill: the cycle counter does not count; the host port state machine
# never enters its response state; the helper's data input synchronizer stops at its first stage.
CANARIES = {"core": ("pe_cycle_counter.v", r"\bcount\s*<=\s*count\s*\+", "hold"),
            "host": ("pe_host_port.v", r"\bst\s*<=\s*S_RESP\s*;", "hold"),
            "slu": ("pe_slu.v", r"\bdin_f2\s*<=\s*din_f1\s*;", "hold")}


def group_of(f):
    return GROUPS.get(f, "core")


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ----- sources -----

def info_sources():
    out, on = [], False
    for line in open(os.path.join(ROOT, "info.yaml"), encoding="utf-8"):
        if re.match(r"\s*source_files:", line):
            on = True
            continue
        if on:
            m = re.match(r"\s*-\s*\"?([\w.]+)\"?", line)
            if m:
                out.append(m.group(1))
            elif line.strip() and not line.strip().startswith("#"):
                break
    return out


def sim_sources():
    text = open(os.path.join(TEST, "Makefile"), encoding="utf-8").read()
    m = re.search(r"PROJECT_SOURCES\s*=\s*((?:[^\n]*\\\n)*[^\n]*)", text)
    return m.group(1).replace("\\\n", " ").split()


# ----- site finder -----

def code_only(text):
    """The text with comments and strings blanked (same length, newlines kept)."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"' and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            for k in range(i, min(j + 1, n)):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def active_lines(code):
    """For each line: True if it is compiled both without macros (RTL simulation) and with SYNTHESIS, and it is
    not a preprocessor line. `ifdef FORMAL blocks are thus never active."""
    res = []
    stacks = {(): [], ("SYNTHESIS",): []}  # per define set: list of [taken_now, any_taken]
    for line in code.split("\n"):
        s = line.strip()
        m = re.match(r"`(ifdef|ifndef|elsif|else|endif)\b\s*(\w*)", s)
        if m:
            kw, mac = m.groups()
            for defs, st in stacks.items():
                parent = all(f[0] for f in st)
                if kw in ("ifdef", "ifndef"):
                    c = (mac in defs) == (kw == "ifdef")
                    st.append([parent and c, c])
                elif kw == "elsif":
                    c = (mac in defs) and not st[-1][1]
                    st[-1] = [all(f[0] for f in st[:-1]) and c, st[-1][1] or c]
                elif kw == "else":
                    c = not st[-1][1]
                    st[-1] = [all(f[0] for f in st[:-1]) and c, True]
                else:
                    st.pop()
            res.append(False)
            continue
        on = all(all(f[0] for f in st) for st in stacks.values())
        res.append(on and not s.startswith("`"))
    return res


TOKEN = re.compile(r"""
 (?P<sized>\d+\s*'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ_?]+)
|(?P<num>\d[\d_]*)
|(?P<id>[A-Za-z_][A-Za-z0-9_$]*|\$[A-Za-z_][A-Za-z0-9_$]*)
|(?P<op><<<|>>>|===|!==|==|!=|<=|>=|&&|\|\||<<|>>|~&|~\||~\^|\^~|\+:|-:|\*\*|->|[-+*/%&|^~!<>=?:;,.(){}\[\]\#@'`])
|(?P<ws>\s+)
""", re.X)
DECL = {"input", "output", "inout", "wire", "reg", "logic", "integer", "genvar", "parameter", "localparam",
        "module", "endmodule", "function", "endfunction", "task", "endtask", "generate", "endgenerate", "real",
        "time", "assert", "assume", "cover"}
DECL_EXPR = {"wire", "reg", "logic", "localparam"}
OPENERS = {"if", "case", "casez", "casex", "@", "always", "while", "for", "repeat"}
BOUNDARY = {";", "begin", "end", "else", "default", "endcase", "fork", "join"}
OPERAND_END = {"id", "num", "sized"}
SIMPLE_LHS = re.compile(r"^[A-Za-z_]\w*(\s*\[[^\]]*\])?$")


def tokens(code):
    toks, pos = [], 0
    while pos < len(code):
        m = TOKEN.match(code, pos)
        if not m:
            toks.append(("other", code[pos], pos, pos + 1))
            pos += 1
            continue
        kind = m.lastgroup
        if kind != "ws":
            toks.append((kind, m.group(), m.start(), m.end()))
        pos = m.end()
    return toks


def line_bad(s):
    return (re.search(r"\b(SPEC|FULL)\b|\$|\bfor\s*\(", s) is not None) or not s.strip()


def sites(fname, text):
    """All mutants of one file: dicts with the span [a, b) of the original text and its replacement."""
    code = code_only(text)
    lines = code.split("\n")
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    act = active_lines(code)

    def line_of(p):
        lo, hi = 0, len(starts) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if starts[mid] <= p:
                lo = mid
            else:
                hi = mid
        return lo

    # per line: first allowed position (declarations: after their first '='), or None
    allow = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not act[i] or line_bad(s):
            allow.append(None)
            continue
        first = re.match(r"[A-Za-z_]\w*", s)
        if first and first.group() in DECL:
            if first.group() in DECL_EXPR and "=" in s:
                allow.append(starts[i] + ln.index("=") + 1)
            else:
                allow.append(None)
            continue
        allow.append(starts[i])

    def ok(a, b):
        la, lb = line_of(a), line_of(max(a, b - 1))
        for k in range(la, lb + 1):
            if allow[k] is None:
                return False
        return a >= allow[la]

    toks = tokens(code)
    out = []

    def add(op, a, b, new):
        if ok(a, b) and text[a:b] != new:
            out.append({"op": op, "a": a, "b": b, "before": text[a:b], "after": new})

    paren = sq = 0
    stack = []          # opener token of each open paren at depth 0..
    stmt_start = 0      # code index where the statement begins
    seen_assign = False
    tern = 0
    prev = None
    for idx, (kind, val, a, b) in enumerate(toks):
        nxt = toks[idx + 1] if idx + 1 < len(toks) else None
        binary = prev is not None and (prev[0] in OPERAND_END or prev[1] in (")", "]", "}"))
        if kind == "op" and val == "(":
            stack.append(prev[1] if prev else "")
            paren += 1
        elif kind == "op" and val == ")":
            paren -= 1
            opener = stack.pop() if stack else ""
            if paren == 0 and opener in OPENERS:
                stmt_start, seen_assign, tern = b, False, 0
        elif kind == "op" and val == "[":
            sq += 1
        elif kind == "op" and val == "]":
            sq -= 1
        elif (kind == "id" and val in BOUNDARY) or (kind == "op" and val == ";"):
            stmt_start, seen_assign, tern = b, False, 0
        elif kind == "op" and val == "?":
            tern += 1
        elif kind == "op" and val == ":" and sq == 0 and paren == 0:
            if tern:
                tern -= 1
            else:
                stmt_start, seen_assign, tern = b, False, 0
        if sq == 0 and not (kind == "op" and val == "]"):
            if kind == "sized":
                m = re.match(r"(\d+)\s*'\s*([sS]?)([bBoOdDhH])\s*([0-9a-fA-F_]+)$", val)
                if m and not (nxt and nxt[1] == "{"):
                    w, sg, base, dig = int(m.group(1)), m.group(2), m.group(3).lower(), m.group(4).replace("_", "")
                    v = int(dig, {"b": 2, "o": 8, "d": 10, "h": 16}[base])
                    if w == 1:
                        add("const", a, b, f"1'{sg}b{1 - (v & 1)}")
                    elif w <= 64:
                        add("const", a, b, f"{w}'{sg}d{(v + 1) % (1 << w)}")
                        add("const", a, b, f"{w}'{sg}d{(v - 1) % (1 << w)}")
            elif kind == "num" and not (nxt and nxt[1] in ("{", "'")) and not (prev and prev[1] in ("#", "'")):
                v = int(val.replace("_", ""))
                add("const", a, b, str(v + 1))
                if v > 0:
                    add("const", a, b, str(v - 1))
            elif kind == "op":
                if val in ("==", "!="):
                    add("rel", a, b, "!=" if val == "==" else "==")
                elif val in ("<", ">", ">="):
                    add("rel", a, b, {"<": "<=", ">": ">=", ">=": ">"}[val])
                elif val == "<=":
                    if paren == 0 and not seen_assign:
                        seen_assign = True
                        lhs = code[stmt_start:a].strip()
                        end = code.find(";", b)
                        if SIMPLE_LHS.match(lhs) and end > b and "\n\n" not in code[b:end]:
                            rhs = text[b:end]
                            if rhs.strip() != lhs and paren == 0:
                                add("hold", b, end, " " + lhs)
                    else:
                        add("rel", a, b, "<")
                elif val == "=":
                    seen_assign = True
                elif val in ("&&", "||"):
                    add("logic", a, b, "||" if val == "&&" else "&&")
                elif val in ("&", "|", "^") and binary:
                    add("bit", a, b, {"&": "|", "|": "&", "^": "|"}[val])
                elif val in ("~", "!") and not binary:
                    add("not", a, b, "")
                elif val in ("+", "-") and binary:
                    add("arith", a, b, "-" if val == "+" else "+")
                elif val in ("<<", ">>"):
                    add("shift", a, b, ">>" if val == "<<" else "<<")
            elif kind == "id" and val == "if" and nxt and nxt[1] == "(":
                depth, j = 0, idx + 1
                while j < len(toks):
                    if toks[j][1] == "(":
                        depth += 1
                    elif toks[j][1] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if j < len(toks):
                    ca, cb = toks[idx + 1][3], toks[j][2]
                    add("stuck", ca, cb, "1'b1")
                    add("stuck", ca, cb, "1'b0")
        prev = (kind, val)

    # hash-stable ids: file, the line text (whitespace-normalized), its occurrence among equal lines, the
    # column in that line, the operator and the replacement
    seen = {}
    norm = [" ".join(ln.split()) for ln in text.split("\n")]
    occ = []
    for ln in norm:
        seen[ln] = seen.get(ln, 0) + 1
        occ.append(seen[ln])
    tl = text.split("\n")
    for m in out:
        li = line_of(m["a"])
        col = m["a"] - starts[li] - (len(tl[li]) - len(tl[li].lstrip()))
        m["file"] = fname
        m["line"] = li + 1
        m["id"] = sha(f"{fname}|{norm[li]}|{occ[li]}|{col}|{m['op']}|{m['after']}")[:12]
        m["group"] = group_of(fname)
        m["text"] = tl[li].strip()
    return out


# ----- plan -----

def plan(rate, shards, ids=None):
    files = [f for f in info_sources() if f not in NOT_MUTATED]
    allm, src_sha = [], {}
    for f in files:
        text = open(os.path.join(SRC, f), encoding="utf-8", newline="").read()
        src_sha[f] = sha(text)
        allm += sites(f, text)
    dup = len(allm) - len({m["id"] for m in allm})
    assert dup == 0, f"{dup} duplicate mutant ids"
    for m in allm:
        m["sampled"] = (int(m["id"][:8], 16) / 0xFFFFFFFF < rate) if ids is None else (m["id"] in ids)
    sample = sorted((m for m in allm if m["sampled"]), key=lambda m: m["id"])
    canaries = {}
    for g, (f, needle, op) in CANARIES.items():
        c = [m for m in allm if m["file"] == f and m["op"] == op and re.search(needle, m["text"])
             and m["text"].count("<=") == 1]
        assert c, f"no canary for group {g}"
        canaries[g] = c[0]["id"]
    deal = [[] for _ in range(shards)]
    for i, m in enumerate(sample):
        deal[i % shards].append(m["id"])
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    return {"commit": commit, "rate": rate, "src_sha": src_sha, "mutants": allm, "shards": deal, "canaries": canaries}


def plan_summary(p):
    rows = {}
    for m in p["mutants"]:
        r = rows.setdefault(m["file"], [0, 0])
        r[0] += 1
        r[1] += m["sampled"]
    ops = {}
    for m in p["mutants"]:
        ops[m["op"]] = ops.get(m["op"], 0) + 1
    lines = [f"commit {p['commit'][:7]}, rate {p['rate']}, {len(p['shards'])} shards; "
             f"sites {len(p['mutants'])}, sampled {sum(r[1] for r in rows.values())}",
             "| file | sites | sampled |", "|---|---|---|"]
    lines += [f"| {f} | {r[0]} | {r[1]} |" for f, r in sorted(rows.items())]
    lines.append("operators: " + ", ".join(f"{k} {v}" for k, v in sorted(ops.items())))
    lines.append("canaries: " + ", ".join(f"{g} {i}" for g, i in sorted(p["canaries"].items())))
    return "\n".join(lines)


# ----- running -----

def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COCOTB", "PYGPI", "GPI_", "TOPLEVEL", "MODULE"))}
    env["PYTHONPATH"] = TEST + os.pathsep + os.path.join(ROOT, "mutation")
    return env


def call(args, timeout):
    """Runs this script with args in a new process group. Returns (rc, seconds, timed_out, output tail)."""
    t0 = time.time()
    kw = {"start_new_session": True} if os.name == "posix" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=clean_env(), **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, time.time() - t0, False, out[-3000:]
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        out, _ = p.communicate()
        return -9, time.time() - t0, True, (out or "")[-3000:]


def do_build(src, build, always=True):
    from cocotb_tools.runner import get_runner
    r = get_runner("icarus")
    r.build(sources=[os.path.join(src, f) for f in sim_sources()] + [os.path.join(TEST, "tb.v")],
            hdl_toplevel="tb", includes=[src], build_dir=build, defines={"PIN_VCD": 1}, always=always)
    return r


def do_test(src, build, run, modules, filt):
    # The runner's test step needs the state of its build step; with always=False the build is reused.
    r = do_build(src, build, always=False)
    os.makedirs(run, exist_ok=True)
    xml = os.path.join(run, "results.xml")
    if os.path.exists(xml):
        os.remove(xml)
    r.test(test_module=modules.split(","), hdl_toplevel="tb", build_dir=build, test_dir=run, test_filter=filt,
           results_xml=xml)


def read_results(run):
    xml = os.path.join(run, "results.xml")
    if not os.path.exists(xml):
        return None
    cases, failed = 0, []
    for tc in ET.parse(xml).getroot().iter("testcase"):
        if tc.find("skipped") is not None:  # tests outside the rung's filter are listed as skipped
            continue
        cases += 1
        bad = tc.find("failure")
        if bad is None:
            bad = tc.find("error")
        if bad is not None:
            failed.append(f"{tc.get('name')}: {(bad.get('message') or bad.text or '')[:200]}")
    return cases, failed


class Shard:
    def __init__(self, p, k, workers, work, deadline_s, max_rungs=0, out=None):
        self.p, self.k, self.work, self.deadline = p, k, work, time.time() + deadline_s
        self.max_rungs = max_rungs
        self.out = out          # written after every mutant, so a job stopped by its time limit keeps its results
        self.done = []
        self.muts = {m["id"]: m for m in p["mutants"]}
        self.base = {}          # rung -> dict(ok, seconds, cases, note)
        self.lock = threading.Lock()
        self.base_locks = {r: threading.Lock() for r in RUNGS}
        self.slots = list(range(workers))
        self.slot_cv = threading.Condition()
        self.results = []
        self.built = False

    def base_build(self):
        with self.lock:
            if not self.built:
                os.makedirs(self.work, exist_ok=True)
                d = os.path.join(self.work, "base")
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(SRC, os.path.join(d, "src"))
                rc, s, to, out = call(["_build", os.path.join(d, "src"), os.path.join(d, "build")], 1800)
                assert rc == 0, f"baseline build failed:\n{out}"
                self.built = True
                print(f"baseline build {s:.0f} s", flush=True)

    def baseline(self, rung):
        with self.base_locks[rung]:
            if rung not in self.base:
                self.base_build()
                d = os.path.join(self.work, "base")
                mods, filt = RUNGS[rung]
                run = os.path.join(d, "run_" + rung)
                rc, s, to, out = call(["_test", os.path.join(d, "src"), os.path.join(d, "build"), run, ",".join(mods),
                                       filt], 4 * 3600)
                res = read_results(run)
                ok = res is not None and res[0] > 0 and not res[1] and not to
                self.base[rung] = {"ok": ok, "seconds": round(s, 1), "cases": res[0] if res else 0,
                                   "note": "" if ok else (str(res[1][:3]) if res else out[-500:])}
                print(f"baseline {rung}: {'pass' if ok else 'FAIL'} {res[0] if res else 0} tests {s:.0f} s", flush=True)
            return self.base[rung]

    def one(self, mid, canary=False):
        m = self.muts[mid]
        rec = {"id": mid, "file": m["file"], "line": m["line"], "op": m["op"], "group": m["group"],
               "before": m["before"], "after": m["after"], "canary": canary}
        if time.time() > self.deadline:
            rec.update(status="notrun")
            return rec
        with self.slot_cv:
            while not self.slots:
                self.slot_cv.wait()
            slot = self.slots.pop()
        t0 = time.time()
        try:
            d = os.path.join(self.work, f"slot{slot}")
            if not os.path.isdir(os.path.join(d, "src")):
                shutil.copytree(SRC, os.path.join(d, "src"))
            path = os.path.join(d, "src", m["file"])
            orig = open(os.path.join(SRC, m["file"]), encoding="utf-8", newline="").read()
            assert sha(orig) == self.p["src_sha"][m["file"]], f"{m['file']} differs from the plan"
            assert orig[m["a"]:m["b"]] == m["before"], f"{mid}: span text differs"
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(orig[:m["a"]] + m["after"] + orig[m["b"]:])
            build = os.path.join(d, "build")
            shutil.rmtree(build, ignore_errors=True)
            rc, s, to, out = call(["_build", os.path.join(d, "src"), build], 900)
            if rc != 0:
                rec.update(status="invalid", note=out[-300:])
                return rec
            rec["status"] = "survived"
            rec["rungs"] = []
            ladder = LADDERS[m["group"]][:self.max_rungs] if self.max_rungs else LADDERS[m["group"]]
            for rung in ladder:
                b = self.baseline(rung)
                if not b["ok"]:
                    rec.update(status="error", note=f"baseline of {rung} failed")
                    break
                limit = max(3 * b["seconds"], b["seconds"] + 120)
                mods, filt = RUNGS[rung]
                run = os.path.join(d, "run")
                shutil.rmtree(run, ignore_errors=True)
                rc, s, to, out = call(["_test", os.path.join(d, "src"), build, run, ",".join(mods), filt], limit)
                rec["rungs"].append([rung, round(s, 1)])
                if to:
                    rec.update(status="timeout", rung=rung, note=f"over {limit:.0f} s")
                    break
                res = read_results(run)
                if res is None or res[0] < b["cases"] or res[1]:
                    why = res[1][:2] if res and res[1] else (f"{res[0]} of {b['cases']} tests ran" if res else
                                                              "no results: " + out[-200:])
                    rec.update(status="killed", rung=rung, note=str(why)[:400])
                    break
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(orig)
        except Exception as e:  # a harness fault is recorded, never counted as a kill
            rec.update(status="error", note=f"{type(e).__name__}: {e}"[:400])
        finally:
            rec["seconds"] = round(time.time() - t0, 1)
            with self.slot_cv:
                self.slots.append(slot)
                self.slot_cv.notify()
        print(f"{mid} {m['file']}:{m['line']} {m['op']} {rec['status']} {rec.get('rung', '')} {rec['seconds']} s",
              flush=True)
        if self.out:
            with self.lock:
                self.done.append(rec)
                with open(self.out + ".tmp", "w") as fh:
                    json.dump(self.summary(self.done), fh, indent=1)
                os.replace(self.out + ".tmp", self.out)
        return rec

    def summary(self, results):
        return {"shard": self.k, "commit": self.p["commit"], "baselines": dict(self.base), "results": list(results)}

    def run(self, workers):
        ids = self.p["shards"][self.k]
        groups = sorted({self.muts[i]["group"] for i in ids})
        canaries = [self.p["canaries"][g] for g in groups]
        with ThreadPoolExecutor(workers) as ex:
            futs = [ex.submit(self.one, c, True) for c in canaries] + [ex.submit(self.one, i) for i in ids]
            self.results = [f.result() for f in futs]
        return self.summary(self.results)


# ----- report -----

def report(p, shard_files):
    shards = [json.load(open(f)) for f in shard_files]
    sampled = {m["id"]: m for m in p["mutants"] if m["sampled"]}
    res, problems = {}, []
    for s in shards:
        for r in s["results"]:
            if r["canary"]:
                if r["status"] not in ("killed", "timeout"):
                    problems.append(f"shard {s['shard']}: canary {r['id']} {r['status']}")
                continue
            if r["id"] in res:
                problems.append(f"{r['id']} has two results")
            res[r["id"]] = r
        for rung, b in s["baselines"].items():
            if not b["ok"]:
                problems.append(f"shard {s['shard']}: baseline {rung} failed: {b['note']}")
    for i in sampled:
        if i not in res:
            problems.append(f"{i} has no result")
        elif res[i]["status"] in ("notrun", "error"):
            problems.append(f"{i} {res[i]['status']}: {res[i].get('note', '')}")
    seen_shards = sorted(s["shard"] for s in shards)
    if seen_shards != list(range(len(p["shards"]))):
        problems.append(f"shard results {seen_shards} of {len(p['shards'])}")
    st = ["killed", "timeout", "survived", "invalid", "error", "notrun"]
    by = {}
    for r in res.values():
        by.setdefault(r["file"], {k: 0 for k in st})[r["status"]] += 1
    tab = ["| file | " + " | ".join(st) + " |", "|---" * (len(st) + 1) + "|"]
    tot = {k: 0 for k in st}
    for f in sorted(by):
        tab.append(f"| {f} | " + " | ".join(str(by[f][k]) for k in st) + " |")
        for k in st:
            tot[k] += by[f][k]
    tab.append("| all | " + " | ".join(str(tot[k]) for k in st) + " |")
    valid = tot["killed"] + tot["timeout"] + tot["survived"]
    score = (tot["killed"] + tot["timeout"]) / valid if valid else 0.0
    rungs = {}
    for r in res.values():
        if r["status"] in ("killed", "timeout"):
            rungs[r["rung"]] = rungs.get(r["rung"], 0) + 1
    ops = {}
    for r in res.values():
        o = ops.setdefault(r["op"], [0, 0])
        o[0] += r["status"] in ("killed", "timeout", "survived")
        o[1] += r["status"] == "survived"
    head = (f"commit {p['commit'][:7]}, rate {p['rate']}: {len(sampled)} sampled mutants, {valid} valid "
            f"(built), killed {tot['killed']} + timeout {tot['timeout']}, survived {tot['survived']}: "
            f"{100 * score:.1f} % killed before triage")
    text = [head, ""] + tab + ["", "killed by rung: " + ", ".join(f"{k} {v}" for k, v in sorted(rungs.items())),
                               "operators (valid, survived): " +
                               ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(ops.items()))]
    surv = sorted((r for r in res.values() if r["status"] == "survived"), key=lambda r: (r["file"], r["line"]))
    slines = [f"{r['id']} {r['file']}:{r['line']} {r['op']}: `{r['before'].strip()}` -> `{r['after'].strip()}`"
              for r in surv]
    return head, "\n".join(text), slines, problems, res


# ----- equivalence check of survivors -----

# The top module and parameter sets under which a file's mutants reach the chip (src/pe_proc.v: the USB helper is
# pe_slu SPEC 1 FULL 0, the 10BASE-Te helper SPEC 2 FULL 1; pe_slu_loop is inside both). A file not listed is
# checked as its first module with its default parameters: a proof there holds for the chip, a failure says nothing.
EQUIV_TOPS = {"pe_slu.v": ("pe_slu", ["pe_slu.v", "pe_slu_loop.v"], [{"SPEC": 1, "FULL": 0}, {"SPEC": 2, "FULL": 1}]),
              "pe_slu_loop.v": ("pe_slu", ["pe_slu.v", "pe_slu_loop.v"],
                                [{"SPEC": 1, "FULL": 0}, {"SPEC": 2, "FULL": 1}])}
# Falling-edge samples cut into free inputs of both copies: the check takes one step per clock cycle, and a free
# sample covers every level the pad can have at the falling edge (an over-approximation).
EQUIV_CUT = {"pe_slu": ["din_f1"]}


def find_yosys():
    here = os.path.dirname(sys.executable)
    for c in (os.environ.get("YOSYS"), "yosys", "yowasp-yosys", os.path.join(here, "Scripts", "yowasp-yosys.exe"),
              os.path.join(here, "yowasp-yosys")):
        if c and shutil.which(c):
            return shutil.which(c)
    return None


def equiv_checks(p, ids, work):
    """Writes each mutant as <work>/<id>_<file> and returns its checks: (id, params, gold files, gate files, top).
    Both copies are elaborated with the same parameters, flattened and optimized (constant configuration registers
    of the chip's helper forms fold away), then compared by Yosys sequential equivalence: equiv_make matches the
    ports and the named signals, equiv_simple and equiv_induct prove each match. Every $equiv cell proven means the
    two copies have the same port behaviour after a reset, for every input sequence. A partial result is no proof:
    equiv_induct assumes every match in the earlier steps, also the ones it fails to prove, so with a register left
    unproven the port matches can show proven although the ports differ (seen with the USB stuffing mutant)."""
    os.makedirs(work, exist_ok=True)
    muts = {m["id"]: m for m in p["mutants"]}
    allf = info_sources()
    checks = []
    for mid in ids:
        m = muts[mid]
        orig = open(os.path.join(SRC, m["file"]), encoding="utf-8", newline="").read()
        assert sha(orig) == p["src_sha"][m["file"]], f"{m['file']} differs from the plan"
        assert orig[m["a"]:m["b"]] == m["before"], f"{mid}: span text differs"
        mf = os.path.join(work, f"{mid}_{m['file']}")
        with open(mf, "w", encoding="utf-8", newline="") as fh:
            fh.write(orig[:m["a"]] + m["after"] + orig[m["b"]:])
        if m["file"] in EQUIV_TOPS:
            top, names, psets = EQUIV_TOPS[m["file"]]
        else:
            top, names, psets = re.search(r"^\s*module\s+(\w+)", code_only(orig), re.M).group(1), allf, [{}]
        gold = [os.path.join(SRC, f) for f in names]
        gate = [mf if f == m["file"] else os.path.join(SRC, f) for f in names]
        for ps in psets:
            checks.append((mid, ps, gold, gate, top))
    return checks


def equiv_script(ps, gold, gate, top, path=lambda f: f.replace("\\", "/")):
    def load(files, name):
        ch = "".join(f" -chparam {k} {v}" for k, v in ps.items())
        cut = "".join(f"expose -cut w:{w}\n" for w in EQUIV_CUT.get(top, []))
        return (f"read_verilog -DSYNTHESIS -sv {' '.join(path(f) for f in files)}\nhierarchy -top {top}{ch}\nproc\n"
                f"flatten\nopt -full\n{cut}async2sync\nopt -full\nopt_clean -purge\nrename -top {name}\n"
                f"design -stash {name}\n")
    return (load(gold, "gold") + load(gate, "gate") + "design -copy-from gold -as gold gold\n"
            "design -copy-from gate -as gate gate\nequiv_make gold gate equiv\nhierarchy -top equiv\n"
            "equiv_simple -seq 5\nequiv_induct -seq 5\n")


def equiv_parse(text):
    m = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", text)
    if m:
        return ("equivalent" if m.group(2) == "0" else "unproven"), f"{m.group(1)} proven, {m.group(2)} unproven"
    if re.search(r"Found 0 \$equiv cells", text):
        return "unproven", "no $equiv cells"
    return "error", text[-300:]


def equiv(p, ids, work, sby=None):
    """Runs every check with Yosys if it is installed; else (or with sby) writes one SymbiYosys job that runs them
    all and appends each result to equiv_status.txt in its work folder."""
    checks = equiv_checks(p, ids, work)
    ys = find_yosys()
    if ys and not sby:
        res = []
        for k, (mid, ps, gold, gate, top) in enumerate(checks):
            sf = os.path.join(work, f"check{k}.ys")
            st = os.path.join(work, f"check{k}.txt")
            with open(sf, "w") as fh:
                fh.write(equiv_script(ps, gold, gate, top) + f"tee -o {st.replace(chr(92), '/')} equiv_status\n")
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
            r = subprocess.run([ys, "-q", "-s", sf], capture_output=True, text=True, env=env)
            text = open(st).read() if os.path.exists(st) else r.stdout[-2000:] + r.stderr[-2000:]
            status, note = equiv_parse(text) if r.returncode == 0 else ("error", (r.stdout + r.stderr)[-300:])
            res.append({"id": mid, "params": ps, "status": status, "note": note})
        return res
    files, body = set(), []
    for mid, ps, gold, gate, top in checks:
        files.update(gold + gate)
        tag = " ".join(f"{k}={v}" for k, v in ps.items()) or "default"
        body.append(f"design -reset\ntee -q -a equiv_status.txt log === {mid} {tag}\n" +
                    equiv_script(ps, gold, gate, top, path=os.path.basename) + "tee -q -a equiv_status.txt equiv_status\n")
    end = os.path.join(work, "equiv_end.v")
    with open(end, "w") as fh:
        fh.write("module equiv_end (input clk, input a);\n  always @(posedge clk) assert (a | !a);\nendmodule\n")
    files.add(end)
    with open(sby or os.path.join(work, "equiv.sby"), "w") as fh:
        fh.write("[options]\nmode bmc\ndepth 1\n\n[engines]\nsmtbmc z3\n\n[script]\n" + "".join(body) +
                 "design -reset\nread_verilog -formal equiv_end.v\nprep -top equiv_end\n\n[files]\n" +
                 "\n".join(sorted(f.replace("\\", "/") for f in files)) + "\n")
    return None


def notice(title, body):
    body = body.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::notice title={title}::{body}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--rate", type=float, default=0.3)
    a.add_argument("--shards", type=int, default=12)
    a.add_argument("--ids", default="", help="space- or comma-separated ids: run only these (replaces --rate)")
    a.add_argument("--out", default="plan.json")
    a = sub.add_parser("run")
    a.add_argument("--plan", default="plan.json")
    a.add_argument("--shard", type=int, required=True)
    a.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    a.add_argument("--work", default=os.path.join(ROOT, "build", "mutation"))
    a.add_argument("--deadline-min", type=float, default=320)
    a.add_argument("--max-rungs", type=int, default=0, help="trial runs only: stop each ladder after N rungs")
    a.add_argument("--out", required=True)
    a = sub.add_parser("report")
    a.add_argument("--plan", default="plan.json")
    a.add_argument("--out", default="mutation_report.md")
    a.add_argument("results", nargs="+")
    a = sub.add_parser("equiv")
    a.add_argument("--plan", default="plan.json")
    a.add_argument("--ids", required=True, help="space- or comma-separated ids of the survivors to check")
    a.add_argument("--work", default=os.path.join(ROOT, "build", "mutation_equiv"))
    a.add_argument("--sby", default="", help="write this SymbiYosys job instead of running Yosys")
    a.add_argument("--out", default="equiv.json")
    a = sub.add_parser("_build")
    a.add_argument("src")
    a.add_argument("build")
    a = sub.add_parser("_test")
    a.add_argument("src")
    a.add_argument("build")
    a.add_argument("run")
    a.add_argument("modules")
    a.add_argument("filt")
    args = ap.parse_args()

    if args.cmd == "plan":
        ids = set(re.split(r"[\s,]+", args.ids.strip())) if args.ids.strip() else None
        p = plan(args.rate, args.shards, ids)
        json.dump(p, open(args.out, "w"), indent=0)
        s = plan_summary(p)
        print(s)
        if os.environ.get("GITHUB_ACTIONS"):
            notice("mutation plan", s)
    elif args.cmd == "run":
        p = json.load(open(args.plan))
        out = Shard(p, args.shard, args.workers, args.work, 60 * args.deadline_min, args.max_rungs,
                    args.out).run(args.workers)
        json.dump(out, open(args.out, "w"), indent=1)
        n = {}
        for r in out["results"]:
            n[r["status"]] = n.get(r["status"], 0) + 1
        print(f"shard {args.shard}: {n}; baselines " +
              ", ".join(f"{k} {'pass' if v['ok'] else 'FAIL'} {v['seconds']} s" for k, v in out["baselines"].items()))
    elif args.cmd == "report":
        p = json.load(open(args.plan))
        head, text, slines, problems, res = report(p, args.results)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("# Mutation campaign\n\n" + text + "\n\n## Survivors\n\n" + "\n".join(f"- {s}" for s in slines) +
                     "\n\n## Self-checks\n\n" + ("\n".join(f"- {x}" for x in problems) or "all passed") + "\n")
        json.dump(res, open(os.path.splitext(args.out)[0] + ".json", "w"), indent=1)
        print(text)
        print("\n".join(slines))
        print("self-checks: " + ("; ".join(problems) if problems else "all passed"))
        if os.environ.get("GITHUB_ACTIONS"):
            notice("mutation table", text)
            notice("mutation self-checks", "\n".join(problems[:60]) if problems else "all passed: every sampled "
                   "mutant has one result, every baseline passed, every canary was killed")
            chunk, size, n = [], 0, 0
            for s in slines:
                if size + len(s) > 3500 and chunk and n < 7:
                    notice(f"mutation survivors {n + 1}", "\n".join(chunk))
                    chunk, size, n = [], 0, n + 1
                chunk.append(s)
                size += len(s) + 1
            if chunk:
                notice(f"mutation survivors {n + 1}", "\n".join(chunk))
        sys.exit(1 if problems else 0)
    elif args.cmd == "equiv":
        p = json.load(open(args.plan))
        res = equiv(p, re.split(r"[\s,]+", args.ids.strip()), args.work, args.sby or None)
        if res is None:
            print(f"wrote {args.sby or os.path.join(args.work, 'equiv.sby')}")
        else:
            json.dump(res, open(args.out, "w"), indent=1)
            text = "\n".join(f"{r['id']} {r['params']} {r['status']}: {r['note']}" for r in res)
            print(text)
            if os.environ.get("GITHUB_ACTIONS"):
                notice("mutation equivalence", text)
    elif args.cmd == "_build":
        do_build(args.src, args.build)
    elif args.cmd == "_test":
        do_test(args.src, args.build, args.run, args.modules, args.filt)


if __name__ == "__main__":
    main()

"""Toggle coverage points that the chip cannot toggle: bits of the line helpers that are constants of their fixed
configuration, found by Yosys.

Usage: python formal/unreach/consts.py gen [--src src] [--out formal/unreach/build] [--yosys yosys]
       python formal/unreach/consts.py check [--exclusions coverage/exclusions.csv] [--teeth formal/unreach/teeth.csv]
                                             [--out formal/unreach/build] [--results formal/unreach/build/results.json]
       python formal/unreach/consts.py triage --toggle-holes coverage_toggle_holes.txt [--out formal/unreach/build]
                                              [--csv candidates.csv]

gen runs Yosys (--yosys, default $YOSYS or yosys) for two netlists next to the proof jobs of
formal/unreach/instrument.py: yconst_usb (SPEC 1, FULL 0, the chip's u_usb) and yconst_eth (SPEC 2, FULL 1, u_eth).
Each reads src/pe_slu.v and src/pe_slu_loop.v unchanged inside formal/unreach/unreach_fv.v (every port access,
written word and pin input free in every cycle) and flattens the design with a keep attribute on every wire (the
wrapper has no outputs, so Yosys would otherwise remove the helper) (pre.json). Every flip-flop with an
asynchronous reset then loses the reset and gets its reset value as its initial value (init.json): the states
this model reaches from its initial state are the states the helper reaches after a reset, because a reset at
any later time only returns those flip-flops to the same values (flip-flops without a reset stay free). Yosys
then simplifies it: opt -full with opt_dff -sat, so a flip-flop whose next value always equals its initial value
whenever it has that value (every other signal free) becomes a constant, and the constants propagate through the
logic. The names of the simplified netlist with their bits go to <out>/<job>/yconst.json.

A bit that is a constant ("0" or "1") in yconst.json has one value from the first reset on, for every input
sequence, so neither of its toggle points can be hit in the chip. The physical condition is the reset at power-up
(the chip starts with rst_n low; the shuttle's reset does this). Verilator's toggle points of the coverage run
count changes after its time 0, when the reset is applied, so a reset that sets a constant 1 can hit the 0->1
point of that bit; only the not-hit point is listed.

check reads the toggle rows of the exclusion file (point <copy>:<file>:<line>:<column>:toggle:<signal>[<bit>]:<dir>,
proof yconst_usb, yconst_eth, or yconst_usb+yconst_eth for a point of the loop copy, which merges both helpers'
receive loops). A row passes only if its bit is a constant in every flattened instance that its coverage copy adds
together (four step instances u_a, u_a0, u_b0, u_b1 for pe_slu_loop_step) in the netlist of each job named. Its
source column must equal the declaration line in src/ (else the row is stale). Toggle rows of the teeth file are
bits that the tests toggle: check fails if one of them is a constant (the check would accept a wrong exclusion).
The status of each yconst job (PASS only if all its rows pass) goes into results.json, which coverage/scope.py
--proofs reads. Returns 1 on any failure.

triage lists the not-hit toggle points (coverage/scope.py --toggle-holes) that are constants, as exclusion rows.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys

JOBS = {"yconst_usb": 1, "yconst_eth": 2}
STEP_INSTANCES = ("u_a", "u_a0", "u_b0", "u_b1")
PREP = """read -formal -sv pe_slu_loop.v pe_slu.v unreach_fv.v
hierarchy -top unreach_fv -chparam SPEC {spec}
proc
flatten
setattr -set keep 1 w:*
prep -top unreach_fv
delete t:$assert t:$assume t:$cover t:$check
write_json pre.json
"""
SIMPLIFY = """read_json init.json
opt -full -sat
opt -full -sat
opt -full -sat
delete t:*
write_json yconst.json
"""
ARST = {"$adff": "$dff", "$adffe": "$dffe"}


def reset_as_init(pre, out):
    """init.json from pre.json: each flip-flop with an asynchronous reset becomes one without it, initialised to
    its reset value. Returns (flip-flops changed, {other flip-flop types left as they are: count})."""
    design = json.load(open(pre, encoding="utf-8"))
    top = design["modules"]["unreach_fv"]
    changed, other = 0, {}
    for name, cell in sorted(top["cells"].items()):
        kind = cell["type"]
        if kind in ARST:
            value = cell["parameters"].pop("ARST_VALUE")
            cell["parameters"].pop("ARST_POLARITY")
            cell["connections"].pop("ARST")
            cell["port_directions"].pop("ARST", None)
            cell["type"] = ARST[kind]
            top["netnames"]["$reset_init$%d" % changed] = {"hide_name": 1, "bits": cell["connections"]["Q"],
                                                           "attributes": {"init": value}}
            changed += 1
        elif "ff" in kind or "dlatch" in kind:
            other[kind] = other.get(kind, 0) + 1
    json.dump(design, open(out, "w", encoding="utf-8"))
    return changed, other


def gen(args):
    here = os.path.dirname(os.path.abspath(__file__))
    for job, spec in JOBS.items():
        out = os.path.join(args.out, job)
        os.makedirs(out, exist_ok=True)
        for f in ("pe_slu.v", "pe_slu_loop.v"):
            shutil.copyfile(os.path.join(args.src, f), os.path.join(out, f))
        shutil.copyfile(os.path.join(here, "unreach_fv.v"), os.path.join(out, "unreach_fv.v"))
        for f in ("pre.json", "init.json", "yconst.json"):
            if os.path.isfile(os.path.join(out, f)):
                os.remove(os.path.join(out, f))
        open(os.path.join(out, "prep.ys"), "w", encoding="utf-8").write(PREP.format(spec=spec))
        open(os.path.join(out, "simplify.ys"), "w", encoding="utf-8").write(SIMPLIFY)
        subprocess.run([args.yosys, "-q", "-l", "prep.log", "-s", "prep.ys"], cwd=out, check=True)
        changed, other = reset_as_init(os.path.join(out, "pre.json"), os.path.join(out, "init.json"))
        subprocess.run([args.yosys, "-q", "-l", "simplify.log", "-s", "simplify.ys"], cwd=out, check=True)
        print("%s: SPEC %d, %d flip-flop cells with a reset value as initial value; other flip-flop cells %s"
              % (job, spec, changed, other or "none"))
    return 0


def modules(text):
    """[(first line, name)] of the modules of a source file."""
    out = []
    for m in re.finditer(r"^\s*module\s+(\w+)", text, re.M):
        out.append((text.count("\n", 0, m.start()) + 1, m.group(1)))
    return out


def module_at(mods, line):
    name = None
    for first, n in mods:
        if first <= line:
            name = n
    return name


def prefixes(f, module):
    if f == "pe_slu.v":
        return ["u_slu."]
    if module == "pe_slu_loop":
        return ["u_slu.u_loop."]
    return ["u_slu.u_loop.%s." % i for i in STEP_INSTANCES]


def bit_of(net, b):
    """The bit b (declared index) of a JSON netname: "0", "1", "x" or a signal number (not constant)."""
    bits = net["bits"]
    off = net.get("offset", 0)
    i = (len(bits) - 1 - (b - off)) if net.get("upto", 0) else (b - off)
    return bits[i] if 0 <= i < len(bits) else None


def parse_toggle(pid):
    copy, f, line, col, kind, comment = pid.split(":", 5)
    name, direction = comment.rsplit(":", 1)
    m = re.match(r"^(.*?)(?:\[(\d+)\])?$", name)
    return copy, f, int(line), kind, m.group(1), int(m.group(2)) if m.group(2) is not None else 0, direction


def jobs_of(copy):
    return ["yconst_usb", "yconst_eth"] if copy == "loop" else ["yconst_" + copy]


class Netlists:
    def __init__(self, out, src):
        self.nets = {}
        for job in JOBS:
            path = os.path.join(out, job, "yconst.json")
            if os.path.isfile(path):
                top = json.load(open(path, encoding="utf-8"))["modules"]["unreach_fv"]
                self.nets[job] = top["netnames"]
        self.text = {f: open(os.path.join(src, f), encoding="utf-8").read() for f in ("pe_slu.v", "pe_slu_loop.v")}
        self.lines = {f: t.splitlines() for f, t in self.text.items()}
        self.mods = {f: modules(t) for f, t in self.text.items()}

    def constant(self, pid, jobs=None):
        """(constant?, why) for one toggle point in the netlists of the jobs (default: those of its copy)."""
        copy, f, line, _kind, sig, b, _d = parse_toggle(pid)
        module = module_at(self.mods[f], line)
        for job in jobs or jobs_of(copy):
            if job not in self.nets:
                return False, "no netlist of %s (job not run)" % job
            for pre in prefixes(f, module):
                net = self.nets[job].get(pre + sig)
                if net is None:
                    return False, "%s: no net %s%s" % (job, pre, sig)
                v = bit_of(net, b)
                if v not in ("0", "1"):
                    return False, "%s: %s%s[%d] is not a constant" % (job, pre, sig, b)
        return True, "constant"

    def source(self, pid):
        _c, f, line, *_ = parse_toggle(pid)
        return self.lines[f][line - 1].strip() if 0 < line <= len(self.lines[f]) else None


def read_rows(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("point", "").strip() and not r["point"].startswith("#")]


def check(args):
    nl = Netlists(args.out, args.src)
    errors = []
    status = {job: "PASS" if job in nl.nets else "ERROR" for job in JOBS}
    n = 0
    for r in read_rows(args.exclusions):
        pid = r["point"].strip()
        if pid.split(":")[4] != "toggle":
            continue
        n += 1
        if nl.source(pid) != r.get("source", "").strip():
            errors.append("%s: declaration line changed or missing (stale row): %r" % (pid, nl.source(pid)))
            for job in r["proof"].strip().split("+"):
                status[job] = "FAIL"
            continue
        jobs = r["proof"].strip().split("+")
        if sorted(jobs) != sorted(jobs_of(pid.split(":")[0])):
            errors.append("%s: proof must be %s" % (pid, "+".join(jobs_of(pid.split(":")[0]))))
        ok, why = nl.constant(pid, jobs)
        if not ok:
            errors.append("%s: %s" % (pid, why))
            for job in jobs:
                status[job] = "FAIL"
    teeth = 0
    for r in read_rows(args.teeth):
        pid = r["point"].strip()
        if pid.split(":")[4] != "toggle":
            continue
        teeth += 1
        ok, why = nl.constant(pid)
        print("tooth %s: %s" % (pid, "constant (wrong)" if ok else "not a constant, as expected (%s)" % why))
        if ok or nl.source(pid) != r.get("source", "").strip():
            errors.append("tooth %s: the check accepts a bit that the tests toggle, or the row is stale" % pid)
    results = json.load(open(args.results, encoding="utf-8")) if os.path.isfile(args.results) else {}
    results.update(status)
    json.dump(results, open(args.results, "w"), indent=1, sort_keys=True)
    print("constant-bit check: %d toggle exclusion(s), %d tooth(s); %s" % (
        n, teeth, ", ".join("%s %s" % kv for kv in sorted(status.items()))))
    for e in errors:
        print("ERROR: " + e)
    return 1 if errors or teeth == 0 else 0


def triage(args):
    nl = Netlists(args.out, args.src)
    rows, other = [], 0
    for ln in open(args.toggle_holes, encoding="utf-8"):
        pid = ln.strip()
        if not pid:
            continue
        copy, f = pid.split(":")[:2]
        if f not in ("pe_slu.v", "pe_slu_loop.v") or copy not in ("usb", "eth", "loop"):
            other += 1
            continue
        ok, _why = nl.constant(pid)
        if ok:
            rows.append([pid, "+".join(jobs_of(copy)), nl.source(pid), ""])
    with open(args.csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["point", "proof", "source", "reason"])
        w.writerows(rows)
    print("toggle holes: %d constant (rows in %s); %d outside the helper copies" % (len(rows), args.csv, other))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("gen", "check", "triage"))
    ap.add_argument("--src", default="src")
    ap.add_argument("--yosys", default=os.environ.get("YOSYS", "yosys"))
    ap.add_argument("--out", default="formal/unreach/build")
    ap.add_argument("--exclusions", default="coverage/exclusions.csv")
    ap.add_argument("--teeth", default="formal/unreach/teeth.csv")
    ap.add_argument("--results", default="formal/unreach/build/results.json")
    ap.add_argument("--toggle-holes")
    ap.add_argument("--csv", default="toggle_candidates.csv")
    args = ap.parse_args()
    return {"gen": gen, "check": check, "triage": triage}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

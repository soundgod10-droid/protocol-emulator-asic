#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Outside oracle: sigrok-cli at the pinned libsigrokdecode commit judges the chip pins of each protocol scenario.

Use:
  python -I flex/sigrok_judge.py list DECODERS.txt              decoder list check (sigrok-cli -L output)
  python -I flex/sigrok_judge.py judge SCENARIO.json PINS.vcd OUT_DIR

What it does:
  SCENARIO.json is written by the scenario tests (test/test_sigrok.py): for each scenario, its time window in the pin
  dump of the same simulation (PINS.vcd, test/tb.v PIN_VCD), the chip lines it uses, the sigrok decoder stack, and its
  content: for each decoder, the annotation classes that carry the content and the texts that the scenario's own data
  gives in the decoder's output format (the format strings of each decoder's pd.py at the pinned commit). For each
  scenario the judge
    1. writes the window as a VCD of named lines and runs sigrok-cli on it as a separate program;
    2. fails on any annotation whose class names a warning, an error or a break (class name contains "warn", "err"
       or "break"), from any decoder of the stack;
    3. compares, for each decoder and class set, the decoded texts in order with the scenario's texts: they must be
       equal and not empty;
    4. negative control: inverts the scenario's data line in the middle of the first content annotation that spans
       at least 2 samples (40 to 60 % of its span; for i2c a data byte, see CONTROL_CLASSES) and decodes again; the copy must fail step 2 or step 3. A check whose control does not fail is a
       failure, because then the comparison could not see a wrong byte.
  The list check: every decoder of the pool (flex/pool.csv, status pool), sbus_futaba (for the reserve list) and every
  decoder used by a scenario must be in the list of the pinned build.

Why:
  A protocol model written by the test writer can share a misreading of the standard with the firmware. sigrok's
  decoders are written by other people from the standards; they read the same pins with no knowledge of the chip.

Conditions: logic simulation (RTL or the functional gate-level netlist, as the scenario run states); a line that the
chip leaves undriven (output enable 0) is read with the level of the board resistor that the scenario names (src
"drvh:N" = pull-up, "drvl:N" = pull-down), not as a measured level.
"""

import csv
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim", "tools"))
import trim_vcd  # noqa: E402

SG = os.environ.get("SIGROK_CLI") or shutil.which("sigrok-cli") or "sigrok-cli"
DS = 8334  # ps per sigrok sample: half the 16,668 ps clock; every pin change of the dump is on this grid
ROW = re.compile(r"^(\d+)-(\d+) ([\w.-]+): ([\w.-]+): (.*)$")
FAIL_CLASS = re.compile(r"warn|err|break", re.I)
VCD_ID = [chr(c) for c in range(33, 127)]


def line_of(raw, src):
    """Changes of one named line; drvh/drvl: a push-pull uio output read with a pull-up or pull-down when undriven;
    bus: a line that the chip drives push-pull or releases to the other side (uio_out when uio_oe is 1, else the
    level that the model returns on uio_in)."""
    kind, n = src.split(":")
    if kind == "bus":
        drv, inp = trim_vcd.line(raw, f"drv:{n}"), trim_vcd.line(raw, f"in:{n}")
        ev = sorted([(t, 0, v) for t, v in drv] + [(t, 1, v) for t, v in inp])
        d = i = "x"
        out = []
        for t, k, v in ev:
            if k == 0:
                d = v
            else:
                i = v
            val = i if d == "z" else d
            if out and out[-1][0] == t:
                out[-1] = (t, val)
            elif not out or out[-1][1] != val:
                out.append((t, val))
        return out
    if kind in ("drvh", "drvl"):
        fill = "1" if kind == "drvh" else "0"
        out = []
        for t, v in trim_vcd.line(raw, f"drv:{n}"):
            v = fill if v == "z" else v
            if not out or out[-1][1] != v:
                out.append((t, v))
        return out
    return trim_vcd.line(raw, src)


def write_vcd(lines, t0, t1, path, comments):
    """lines: [(name, [(t, v)])]; writes the window [t0, t1] with times from 0."""
    out = ["$date", "\t(scenario window)", "$end"] + [f"$comment {c} $end" for c in comments]
    out += ["$timescale", "\t1ps", "$end", "$scope module chip $end"]
    for k, (name, _) in enumerate(lines):
        out.append(f"$var wire 1 {VCD_ID[k]} {name} $end")
    out += ["$upscope $end", "$enddefinitions $end"]
    init, ev = [], {}
    for k, (_, ch) in enumerate(lines):
        v0 = "x"
        for t, v in ch:
            if t <= t0:
                v0 = v
            elif t <= t1:
                ev.setdefault(t - t0, []).append(f"{v}{VCD_ID[k]}")
        init.append(f"{v0}{VCD_ID[k]}")
    out += ["#0", "$dumpvars"] + init + ["$end"]
    for t in sorted(ev):
        out.append(f"#{t}")
        out += ev[t]
    out.append(f"#{t1 - t0}")
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")


def first_text(s):
    """At log level 4 sigrok-cli 0.7.2 prints every text variant of an annotation in quotes, longest first."""
    if not s.startswith('"'):
        return s
    end = s.find('" "', 1)
    return s[1:end] if end > 0 else s[1:-1] if s.endswith('"') else s[1:]


def run_sigrok(vcd, stack):
    # sigrok-cli 0.7.2 has no --protocol-decoder-ann-class; it prints the annotation class (its id, as named in
    # the decoder's pd.py) when the log level is above info (decode.c show_pd_annotations), so -l 4.
    # Without -A, sigrok-cli shows only the top decoder of a stack; -A with every decoder id shows all of them.
    ids = ",".join(x.split(":")[0] for x in stack.split(","))
    cmd = [SG, "-l", "4", "-i", vcd, "-I", f"vcd:downsample={DS}", "--protocol-decoder-samplenum", "-P", stack,
           "-A", ids]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    rows = []
    for ln in p.stdout.splitlines():
        m = ROW.match(ln.strip())
        if m:
            rows.append((int(m.group(1)), int(m.group(2)), m.group(3), m.group(4), first_text(m.group(5))))
    err = "\n".join(x for x in p.stderr.splitlines() if not x.startswith(("srd:", "sr:", "cli:")))[-800:]
    return rows, p.returncode, err, " ".join(["sigrok-cli"] + cmd[1:]).replace(vcd, os.path.basename(vcd))


def pd_match(row_pd, pd):
    return row_pd == pd or row_pd.startswith(pd + "-")


# Decoders whose bit annotations run from one clock edge to the next: the middle of such a span is where the data
# line may change with no effect (i2c: SCL low; the R/W bit is a bit annotation in the address class), so the
# negative control of these decoders tampers inside a byte annotation of the listed classes.
CONTROL_CLASSES = {"i2c": {"data-read", "data-write"}}


def control_ok(r):
    return all(not pd_match(r[2], pd) or r[3] in cls for pd, cls in CONTROL_CLASSES.items())


def allowed(r, rows, allow):
    """allow: {"decoder:class": anchor} for classes that are part of a protocol although their name matches
    FAIL_CLASS. An empty anchor allows the class anywhere; an anchor "decoder:class" allows it only inside the span of
    an annotation of that class (dmx512: each packet starts with a BREAK, and the uart decoder below it marks the
    BREAK and a frame error inside it)."""
    for key, anchor in allow.items():
        pd, _, cls = key.partition(":")
        if pd_match(r[2], pd) and r[3] == cls:
            if not anchor:
                return True
            apd, _, acls = anchor.partition(":")
            return any(pd_match(a[2], apd) and a[3] == acls and a[0] <= r[0] and r[1] <= a[1] for a in rows)
    return False


def evaluate(rows, content, forbid=(), allow=None):
    """forbid: further "decoder:class" names that a decoder uses for errors only (swd: parity). allow: see allowed()."""
    allow = allow or {}
    fails = [f"{r[2]}: {r[3]}: {r[4]}" for r in rows
             if (FAIL_CLASS.search(r[3]) or any(pd_match(r[2], f.split(":")[0]) and r[3] == f.split(":")[1]
                                                for f in forbid)) and not allowed(r, rows, allow)]
    got, first = {}, None
    for key in content:
        pd, _, cls = key.partition(":")
        classes = set(cls.split("|"))
        sel = [r for r in rows if pd_match(r[2], pd) and r[3] in classes]
        got[key] = [r[4] for r in sel]
        # the control needs a span to invert: an annotation of a single sample point (i2c "Start") has none
        wide = [r for r in sel if r[1] - r[0] >= 2 and control_ok(r)]
        if wide and first is None:
            first = wide[0]
    equal = all(got[k] == list(v) for k, v in content.items()) and all(content.values())
    return fails, got, equal, first


def first_diff(a, b):
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            return {"index": i, "scenario": x, "sigrok": y}
    return None


def judge(scen_path, vcd_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    scen = json.load(open(scen_path, encoding="utf-8"))
    raw, end = trim_vcd.read_raw(vcd_path)
    version = subprocess.run([SG, "-V"], capture_output=True, text=True).stdout.splitlines()
    results = []
    for s in scen["scenarios"]:
        t0, t1 = s["window_ps"]
        lines = [(name, line_of(raw, src)) for name, src in s["lines"]]
        comments = [f"scenario {s['name']} ({s['test']}), {scen.get('level', 'RTL')}", s.get("note", "")]
        vcd = os.path.join(out_dir, f"{s['name']}.vcd")
        write_vcd(lines, t0, t1, vcd, comments)
        rows, rc, err, cmd = run_sigrok(vcd, s["stack"])
        fails, got, equal, first = evaluate(rows, s["content"], s.get("forbid", ()))
        # negative control: invert the data line in the middle of the first content annotation
        control = {"bites": False, "note": "no content annotation to tamper"}
        if first is not None:
            a = t0 + first[0] * DS + int(0.4 * (first[1] - first[0]) * DS)
            b = t0 + first[0] * DS + int(0.6 * (first[1] - first[0]) * DS)
            tl = []
            for name, ch in lines:
                if name == s["tamper"]:
                    inv = {"0": "1", "1": "0"}
                    before = [x for x in ch if x[0] < a]
                    mid = [(t, inv.get(v, v)) for t, v in ch if a <= t < b]
                    after = [x for x in ch if x[0] >= b]
                    lev = before[-1][1] if before else "x"
                    lev_b = (mid[-1][1] if mid else inv.get(lev, lev))
                    ch2 = before + [(a, inv.get(lev, lev))] + mid + [(b, inv.get(lev_b, lev_b))] + after
                    tl.append((name, ch2))
                else:
                    tl.append((name, ch))
            cvcd = os.path.join(out_dir, f"{s['name']}_control.vcd")
            write_vcd(tl, t0, t1, cvcd, comments + [f"negative control: {s['tamper']} inverted {a - t0}..{b - t0} ps"])
            crows, _, _, _ = run_sigrok(cvcd, s["stack"])
            cfails, cgot, cequal, _ = evaluate(crows, s["content"], s.get("forbid", ()))
            control = {"bites": bool(cfails) or not cequal, "inverted_ps": [a - t0, b - t0], "line": s["tamper"],
                       "fail_annotations": cfails[:4],
                       "first_difference": next((first_diff(s["content"][k], cgot[k]) for k in s["content"]
                                                 if cgot[k] != s["content"][k]), None)}
        n = sum(len(v) for v in got.values())
        ok = rc == 0 and not fails and equal and n > 0 and control["bites"]
        results.append({"name": s["name"], "test": s["test"], "stack": s["stack"], "window_ps": [t0, t1],
                        "pass": ok, "sigrok_rc": rc, "sigrok_stderr": err if rc else "", "fail_annotations": fails[:8],
                        "equal": equal, "items": n, "control": control, "cmd": cmd,
                        "differences": {k: first_diff(v, got[k]) for k, v in s["content"].items() if got[k] != v},
                        "sample": {k: v[:6] for k, v in got.items()}})
        print(f"{'PASS' if ok else 'FAIL'} {s['name']}: {n} items, equal {equal}, warning/error annotations "
              f"{len(fails)}, control {'fails as it must' if control['bites'] else 'DOES NOT FAIL'}", flush=True)
    summary = {"sigrok_version": version, "pins": open(os.path.join(os.path.dirname(os.path.dirname(SG)), "PINS")).read()
               if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(SG)), "PINS")) else "",
               "level": scen.get("level"), "simulator": scen.get("simulator"), "commit": scen.get("commit"),
               "results": results}
    json.dump(summary, open(os.path.join(out_dir, "sigrok_judge.json"), "w"), indent=1)
    tab = ["| scenario | decoder stack | items | equal to the scenario | warning/error annotations | control | result |",
           "|---|---|---|---|---|---|---|"]
    for r in results:
        stack = r["stack"].split(",")
        tab.append(f"| {r['name']} | {', '.join(x.split(':')[0] for x in stack)} | {r['items']} | {r['equal']} | "
                   f"{len(r['fail_annotations'])} | {'fails' if r['control']['bites'] else 'does not fail'} | "
                   f"{'PASS' if r['pass'] else 'FAIL'} |")
    text = (f"sigrok judge ({scen.get('level', 'RTL')}, commit {str(scen.get('commit'))[:7]}): "
            f"{sum(r['pass'] for r in results)} of {len(results)} scenarios pass\n"
            + "\n".join(v for v in version[:3]) + "\n\n" + "\n".join(tab))
    open(os.path.join(out_dir, "sigrok_judge.md"), "w", encoding="utf-8").write(text + "\n")
    print(text)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice title=sigrok judge::" + text.replace("%", "%25").replace("\n", "%0A"), flush=True)
    return 0 if results and all(r["pass"] for r in results) else 1


def decoder_list(list_path, scen_path=None):
    text = open(list_path, encoding="utf-8").read()
    have = set(re.findall(r"^\s{2}(\S+)\s", text.split("Supported protocol decoders")[-1].split("Supported output")[0],
                          re.M))
    pool = [r["decoder"] for r in csv.DictReader(open(os.path.join(HERE, "pool.csv"), encoding="utf-8"))
            if r["status"] == "pool"]
    need = set(pool) | {"sbus_futaba"}
    if scen_path and os.path.exists(scen_path):
        for s in json.load(open(scen_path, encoding="utf-8"))["scenarios"]:
            need |= {x.split(":")[0] for x in s["stack"].split(",")}
    missing = sorted(need - have)
    msg = (f"pinned sigrok build: {len(have)} decoders; pool {len(pool)} of 44 present: "
           f"{len(set(pool) & have)}; missing {missing or 'none'}")
    print(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=sigrok decoders::{msg}", flush=True)
    return 0 if not missing and len(pool) == 44 else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "list":
        sys.exit(decoder_list(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    if len(sys.argv) == 5 and sys.argv[1] == "judge":
        sys.exit(judge(sys.argv[2], sys.argv[3], sys.argv[4]))
    print(__doc__)
    sys.exit(2)

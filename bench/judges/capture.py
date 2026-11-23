#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bench judge for logic-analyser captures (docs/bench.md row P1, USB wire row): the pinned sigrok-cli decodes the
capture and the decoded content must equal what the host program sent or received; a copy with one pulse inverted
must fail.

The decoding, the warning/error rule and the content comparison are flex/sigrok_judge.py (the judge of the RTL pin
dumps, run_sigrok and evaluate); this file reads analyser exports (VCD of named 1-bit channels, any time scale, as
sigrok-cli, PulseView and the RP2040/RP2350 LogicAnalyzer export them), maps analyser channels to chip lines, and
applies a bench row.

Use:
  capture.py row ROW.json CAPTURE.vcd OUT_DIR
      ROW.json: {"name", "stack", "lines": [[line, channel]], "content": {...}, "tamper": line, "allow", "forbid"}
      (the scenario format of flex/sigrok_judge.py, with analyser channel names instead of chip sources)
  capture.py ci SCENARIO.json WINDOW_DIR OUT_DIR RATE_HZ [RATE_HZ ...]
      CI proof on the RTL: each scenario window that flex/sigrok_judge.py wrote (WINDOW_DIR/<name>.vcd, chip lines)
      is sampled as an analyser at RATE_HZ would sample it (each edge seen at the next sample instant, a random
      phase per scenario), written as an analyser export with channels D0, D1, ..., and judged as a bench row.
Conditions: the CI proof uses logic-simulation pin dumps of the RTL; it shows that the bench path (export format,
channel map, sample grid, decoder stack) works and that its control fails, not that a bench capture will pass.
"""

import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "flex"))
import sigrok_judge as sj  # noqa: E402

UNIT_PS = {"s": 10 ** 12, "ms": 10 ** 9, "us": 10 ** 6, "ns": 10 ** 3, "ps": 1, "fs": 0.001}


def read_vcd(path):
    """{name: [(t_ps, '0'|'1'|'x'|'z')]} of the 1-bit wires of a VCD; the end time in ps."""
    text = open(path, encoding="ascii", errors="replace").read()
    m = re.search(r"\$timescale\s+(\d+)\s*(\w+)\s+\$end", text)
    scale = int(m.group(1)) * UNIT_PS[m.group(2)] if m else 1
    ids = {}
    for v in re.finditer(r"\$var\s+\w+\s+1\s+(\S+)\s+(\S+)(?:\s+\[[^\]]*\])?\s+\$end", text):
        ids[v.group(1)] = v.group(2)
    chans = {n: [] for n in ids.values()}
    body = text.split("$enddefinitions", 1)[1].split("$end", 1)[1]
    t = 0
    for tok in body.split():
        if tok.startswith("#"):
            t = int(int(tok[1:]) * scale)
        elif tok[0] in "01xzXZ" and tok[1:] in ids:
            ch = chans[ids[tok[1:]]]
            v = tok[0].lower()
            if not ch or ch[-1][1] != v:
                if ch and ch[-1][0] == t:
                    ch[-1] = (t, v)
                else:
                    ch.append((t, v))
    return chans, t


def sample(ch, rate_hz, phase_ps):
    """The channel as an analyser at rate_hz sees it: each change appears at the first sample instant at or after it."""
    per = 1e12 / rate_hz
    out = []
    for t, v in ch:
        k = -(-(t - phase_ps) // per) if t > phase_ps else 0
        # the level at the start of the window is the export's first sample (time 0); without this the export
        # began with up to one sample period of 'x', which the 1-Wire and CAN decoders read as a short low pulse
        ts = 0 if t <= 0 else int(round(phase_ps + k * per))
        if out and out[-1][0] == ts:
            out[-1] = (ts, v)
        elif not out or out[-1][1] != v:
            out.append((ts, v))
    # two changes in one sample period collapse to the last level
    clean = []
    for t, v in out:
        if not clean or clean[-1][1] != v:
            clean.append((t, v))
    return clean


def write_export(chans, end_ps, path, rate_hz):
    """An analyser export: channels D0.., time scale of one sample (rounded to ps)."""
    sj.write_vcd([(n, ch) for n, ch in chans], 0, end_ps, path,
                 [f"analyser export, {rate_hz / 1e6:g} MS/s"])


def judge_row(row, chans, end_ps, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    lines = [(name, chans[channel]) for name, channel in row["lines"]]
    vcd = os.path.join(out_dir, f"{row['name']}.vcd")
    sj.write_vcd(lines, 0, end_ps, vcd, [f"bench row {row['name']}"])
    rows, rc, err, cmd = sj.run_sigrok(vcd, row["stack"])
    fails, got, equal, first = sj.evaluate(rows, row["content"], row.get("forbid", ()), row.get("allow"))
    control = {"bites": False, "note": "no content annotation to tamper"}
    if first is not None:
        a = first[0] * sj.DS + int(0.4 * (first[1] - first[0]) * sj.DS)
        b = first[0] * sj.DS + int(0.6 * (first[1] - first[0]) * sj.DS)
        inv = {"0": "1", "1": "0"}
        tl = []
        for name, ch in lines:
            if name == row["tamper"]:
                before = [x for x in ch if x[0] < a]
                mid = [(t, inv.get(v, v)) for t, v in ch if a <= t < b]
                after = [x for x in ch if x[0] >= b]
                lev = before[-1][1] if before else "x"
                lev_b = mid[-1][1] if mid else inv.get(lev, lev)
                ch = before + [(a, inv.get(lev, lev))] + mid + [(b, inv.get(lev_b, lev_b))] + after
            tl.append((name, ch))
        cvcd = os.path.join(out_dir, f"{row['name']}_control.vcd")
        sj.write_vcd(tl, 0, end_ps, cvcd, [f"control: {row['tamper']} inverted {a}..{b} ps"])
        crows, _, _, _ = sj.run_sigrok(cvcd, row["stack"])
        cfails, cgot, cequal, _ = sj.evaluate(crows, row["content"], row.get("forbid", ()), row.get("allow"))
        control = {"bites": bool(cfails) or not cequal, "inverted_ps": [a, b], "fail_annotations": cfails[:3]}
    n = sum(len(v) for v in got.values())
    ok = rc == 0 and not fails and equal and n > 0 and control["bites"]
    return {"name": row["name"], "pass": ok, "items": n, "equal": equal, "fail_annotations": fails[:6],
            "control_fails": control["bites"], "sigrok_rc": rc, "cmd": cmd,
            "differences": {k: sj.first_diff(v, got[k]) for k, v in row["content"].items() if got[k] != v}}


def ci(scen_path, win_dir, out_dir, rates):
    scen = json.load(open(scen_path, encoding="utf-8"))
    rng = random.Random(20261122)
    results = []
    for rate in rates:
        for s in scen["scenarios"]:
            win = os.path.join(win_dir, f"{s['name']}.vcd")
            if not os.path.exists(win):
                results.append({"name": s["name"], "rate": rate, "pass": False, "note": "window file missing"})
                continue
            chans, end = read_vcd(win)
            names = [n for n, _ in s["lines"]]
            phase = rng.uniform(0, 1e12 / rate)
            exp = [(f"D{k}", sample(chans[n], rate, phase)) for k, n in enumerate(names)]
            tag = f"{s['name']}_{rate // 1000000}M"
            export = os.path.join(out_dir, "exports", f"{tag}.vcd")
            os.makedirs(os.path.dirname(export), exist_ok=True)
            write_export(exp, end, export, rate)
            echans, eend = read_vcd(export)
            row = {"name": tag, "stack": s["stack"], "content": s["content"], "allow": s.get("allow"),
                   "forbid": s.get("forbid", ()), "lines": [[n, f"D{k}"] for k, n in enumerate(names)],
                   "tamper": s["tamper"]}
            r = judge_row(row, echans, eend, os.path.join(out_dir, "rows"))
            r["rate"] = rate
            results.append(r)
            print(f"{'PASS' if r['pass'] else 'FAIL'} {tag}: {r.get('items')} items, equal {r.get('equal')}, "
                  f"control {'fails' if r.get('control_fails') else 'DOES NOT FAIL'}", flush=True)
    json.dump(results, open(os.path.join(out_dir, "capture_judge.json"), "w"), indent=1)
    tab = ["| scenario | analyser rate | items | equal | control | result |", "|---|---|---|---|---|---|"]
    for r in results:
        tab.append(f"| {r['name']} | {r['rate'] / 1e6:g} MS/s | {r.get('items', '-')} | {r.get('equal', '-')} | "
                   f"{'fails' if r.get('control_fails') else 'does not fail'} | {'PASS' if r['pass'] else 'FAIL'} |")
    by_rate = {rate: [r for r in results if r["rate"] == rate] for rate in rates}
    head = "; ".join(f"{rate / 1e6:g} MS/s: {sum(r['pass'] for r in rr)} of {len(rr)} pass" for rate, rr in by_rate.items())
    text = "bench capture judge on the RTL scenario windows: " + head + "\n\n" + "\n".join(tab)
    open(os.path.join(out_dir, "capture_judge.md"), "w", encoding="utf-8").write(text + "\n")
    print(text)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice title=bench capture judge::" + text.replace("%", "%25").replace("\n", "%0A"), flush=True)
    return results


# Scenario windows whose RTL timing sits on a decoder threshold, so that any analyser sample grid decides the result
# by its phase. They are judged and reported but do not count in the CI exit status; each reason gives the numbers.
# The 1-Wire row is counted on the window onewire_hold30 (a read 0 held 30 us).
AT_THRESHOLD = {
    "onewire": "the DS18B20 model (test/test_holdout.py OneWireDevice, t_rd = tRDV = 15 us) holds a read 0 for 900 "
               "cycles = 15.0012 us, 1.2 ns above the 15 us bit-0 limit of onewire_link (LOWR max); an analyser "
               "sample period moves the measured low time by up to one period, so the bit is decided by the phase",
}


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "ci":
        res = ci(sys.argv[2], sys.argv[3], sys.argv[4], [int(float(x)) for x in sys.argv[5:]])
        # every scenario must pass at the fastest rate given; slower rates are reported (an analyser that is too slow
        # for a row shows here as FAIL with its reason)
        top = max(int(float(x)) for x in sys.argv[5:])
        for name, why in AT_THRESHOLD.items():
            print(f"not counted: {name}: {why}", flush=True)
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::notice title=bench capture judge, not counted::{name}: {why}", flush=True)
        counted = [r for r in res if r["rate"] == top and r["name"].rsplit("_", 1)[0] not in AT_THRESHOLD]
        sys.exit(0 if counted and all(r["pass"] for r in counted) else 1)
    if len(sys.argv) == 5 and sys.argv[1] == "row":
        row = json.load(open(sys.argv[2], encoding="utf-8"))
        chans, end = read_vcd(sys.argv[3])
        r = judge_row(row, chans, end, sys.argv[4])
        print(json.dumps(r, indent=1))
        sys.exit(0 if r["pass"] else 1)
    print(__doc__)
    sys.exit(2)

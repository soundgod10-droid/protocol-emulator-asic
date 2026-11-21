# SPDX-License-Identifier: Apache-2.0
"""Pin terms of the firmware timing certificates.

For each program that cert/leadcert.py certifies (never LATE), every event occurs at its own cycle t. The pin
registers take the event at the clock edge at the end of cycle t (docs/isa.md 4.4); this script gives, for each output
pin of the program, the window from that clock edge to the edge at the pad, at each corner and over all corners, and
for each input pin the pad term and the synchronizer rule.

Usage: python cert/pinwin.py [--results cert_results.json] [--delays cert/pin_delays.csv] [--selftest]

Output term = cell term + pad term:
  - cell term: chip clock pin to the output port, measured in the SDF runs of the layout (cert/pin_delays.csv: the
    smallest and largest delay over the 0/1 changes of each uio_out, uio_oe and uo_out bit in test_core with
    SDF_DELAYS=1; wire delays not loaded; a bit with few changes may not show both polarities);
  - pad term: core input 50 % to pad 50 % of the output pads, rise and fall at the corner, 5 pF (PAD_OUT, from the pad
    SPICE table of docs/verification.md section 9).
  A uio pin uses both its value bit and its output-enable bit (open-drain pins change OE).
Input term: the pin level of cycle t - 2 (docs/isa.md 6.1; t - 7 for the filtered inputs 6 and 7) after the input pad
(PAD_IN, same table); the first synchronizer stage samples once a clock, so an input edge is placed within one clock
period.
Checks (exit 1): PAD_OUT and PAD_IN equal the table of docs/verification.md section 9; every output pin of a certified
program has a measured cell term at each corner. --selftest plants a wrong pad value and a pin with no measured delay;
each must be rejected.
Conditions: logic simulation with the SDF cell delays (Icarus, no timing checks) and pad SPICE of the schematic cells at
5 pF; board, package and the shuttle's pad multiplexer are not modelled.
"""

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import leadcert  # noqa: E402

CORNERS = ("nom_slow_1p08V_125C", "nom_typ_1p20V_25C", "nom_fast_1p32V_m40C")
# docs/verification.md section 9: output delay rise / fall and input pad delay rise / fall (ns) at slow, typ, fast
PAD_OUT = {"nom_slow_1p08V_125C": (3.32, 3.18), "nom_typ_1p20V_25C": (2.03, 1.74), "nom_fast_1p32V_m40C": (1.46, 1.13)}
PAD_IN = {"nom_slow_1p08V_125C": (0.45, 0.42), "nom_typ_1p20V_25C": (0.32, 0.34), "nom_fast_1p32V_m40C": (0.26, 0.28)}
T_CLK_NS = 1e3 / 60.0


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def doc_pads():
    """(output, input) rise / fall triples of the pad SPICE table, read from docs/verification.md."""
    text = _read(os.path.join(ROOT, "docs", "verification.md"))
    out = {}
    for key, label in (("out", "Output delay, core input 50 % to pad 50 %"), ("in", "Input pad delay, pad 50 % to core 50 %")):
        m = re.search(r"^\| " + re.escape(label) + r", rise / fall \| ([\d.]+) / ([\d.]+) ns \| ([\d.]+) / ([\d.]+) ns \| "
                      r"([\d.]+) / ([\d.]+) ns \|", text, re.M)
        if m is None:
            return None
        v = [float(x) for x in m.groups()]
        out[key] = {CORNERS[0]: (v[0], v[1]), CORNERS[1]: (v[2], v[3]), CORNERS[2]: (v[4], v[5])}
    return out


def load_delays(path):
    """{corner: {signal: (min_ns, max_ns, changes)}} and the source line of each corner."""
    d, src = {}, {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["corner"].startswith("#"):
                continue
            d.setdefault(r["corner"], {})[r["signal"]] = (float(r["min_ns"]), float(r["max_ns"]), int(r["changes"]))
            src[r["corner"]] = r["source"]
    return d, src


def program_pins(name):
    """(output pins, input pins) of the OUT, SAMPLE and WAIT instructions of fw/<name>."""
    prog = leadcert.load(name, leadcert.PROGRAMS.get(name, {}).get("equ"))
    outs, ins = set(), set()
    for w in prog.image()[:prog.size]:
        op, pin = w >> 12, (w >> 8) & 0xF
        if op == 0xC:
            (outs if ((w >> 3) & 7) <= 5 else ins).add(pin)
        elif op == 0xD:
            ins.add(pin)
    return sorted(p for p in outs if p != 15), sorted(p for p in ins if p != 15)


def signals(pin):
    return ["uio_out[%d]" % pin, "uio_oe[%d]" % pin] if pin < 8 else ["uo_out[%d]" % (pin - 8)]


def pin_name(pin, out=True):
    if pin < 8:
        return "uio[%d]" % pin
    return "uo_out[%d]" % (pin - 8) if out else ("ui_in[%d]" % (pin - 8) if pin < 13 else "helper event %d" % pin)


def windows(programs, delays, pad_out=PAD_OUT):
    """Rows (program, pin, per-corner windows, overall window) and errors."""
    rows, errors = [], []
    for name, (outs, ins) in programs.items():
        for p in outs:
            per, ok = {}, True
            for c in CORNERS:
                vals = [delays.get(c, {}).get(s) for s in signals(p)]
                if any(v is None for v in vals):
                    errors.append("%s: pin %d (%s) has no measured cell delay at %s" % (name, p, pin_name(p), c))
                    ok = False
                    continue
                lo, hi = min(v[0] for v in vals), max(v[1] for v in vals)
                per[c] = (lo + min(pad_out[c]), hi + max(pad_out[c]))
            if ok:
                rows.append((name, p, per, (min(w[0] for w in per.values()), max(w[1] for w in per.values()))))
    return rows, errors


def check_pads(pad_out, pad_in):
    doc = doc_pads()
    if doc is None:
        return ["docs/verification.md section 9: pad table row not found"]
    errs = []
    for c in CORNERS:
        if tuple(pad_out[c]) != doc["out"][c]:
            errs.append("output pad term at %s is %s, docs/verification.md section 9 says %s" % (c, pad_out[c], doc["out"][c]))
        if tuple(pad_in[c]) != doc["in"][c]:
            errs.append("input pad term at %s is %s, docs/verification.md section 9 says %s" % (c, pad_in[c], doc["in"][c]))
    return errs


def selftest(programs, delays, log=print):
    caught = 0
    bad_pad = dict(PAD_OUT, nom_fast_1p32V_m40C=(1.46, 0.13))
    e1 = check_pads(bad_pad, PAD_IN)
    log("planted fault (a wrong output pad term): %s" % ("caught" if e1 else "NOT caught"))
    caught += bool(e1)
    name = next(iter(programs))
    outs, ins = programs[name]
    missing = {c: {k: v for k, v in delays.get(c, {}).items() if k not in signals(outs[0])} for c in delays} if outs else {}
    e2 = windows({name: (outs, ins)}, missing)[1] if outs else []
    log("planted fault (a pin with no measured delay): %s" % ("caught" if e2 else "NOT caught"))
    caught += bool(e2)
    log("pin-term self-test: %d of 2 planted faults caught" % caught)
    return caught == 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="cert_results.json")
    ap.add_argument("--delays", default=os.path.join(HERE, "pin_delays.csv"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if os.path.isfile(args.results):
        with open(args.results, encoding="utf-8") as f:
            certified = [r["program"] for r in json.load(f) if r["certified"]]
    else:
        certified = [r["program"] for r in (leadcert.certify(n) for n in
                                            sorted(x for x in os.listdir(os.path.join(ROOT, "fw")) if x.endswith(".s")))
                     if r["certified"]]
    programs = {n: program_pins(n) for n in certified}
    delays, src = load_delays(args.delays)
    errors = check_pads(PAD_OUT, PAD_IN)
    rows, errs = windows(programs, delays)
    errors += errs
    print("| Program | Output pin | Window after the event's clock edge, ns: slow / typ / fast | All corners, ns |")
    print("|---|---|---|---|")
    for name, p, per, (lo, hi) in rows:
        print("| %s | %d (%s) | %s | %.2f to %.2f |" % (name, p, pin_name(p), " / ".join(
            "%.2f-%.2f" % per[c] for c in CORNERS), lo, hi))
    print("\nInput pins (the level of cycle t - 2 after the input pad; t - 7 on the filtered inputs 6 and 7; placed "
          "within one clock period of %.3f ns by the first synchronizer stage):" % T_CLK_NS)
    for name, (outs, ins) in programs.items():
        print("- %s: %s" % (name, ", ".join("%d (%s)" % (p, pin_name(p, out=False)) for p in ins) or "none"))
    print("\nInput pad: %s ns (slow / typ / fast, rise / fall); output pad: %s ns. Cell terms: %s." % (
        " / ".join("%.2f/%.2f" % PAD_IN[c] for c in CORNERS), " / ".join("%.2f/%.2f" % PAD_OUT[c] for c in CORNERS),
        "; ".join("%s %s" % (c, src.get(c, "missing")) for c in CORNERS)))
    ok = not errors
    for e in errors:
        print("ERROR: " + e)
    if args.selftest:
        ok = selftest(programs, delays) and ok
    print("pin terms: %d certified programs, %d output pins with windows; %d errors" % (len(programs), len(rows),
                                                                                       len(errors)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

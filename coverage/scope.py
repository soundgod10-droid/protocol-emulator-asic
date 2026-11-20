"""Coverage figures of the RTL tests in three scopes, from a Verilator coverage.dat.

Usage: python coverage/scope.py coverage.dat [--info info.yaml] [--exclusions coverage/exclusions.csv]
                                [--proofs results.json] [--holes holes.csv] [--toggle-holes toggle.txt]

1. raw: every point of every RTL file (test bench left out), with the instances of a module that have the
   same parameter set added together, as Verilator does in a build without coverage/split.py. It is the
   figure published since the first coverage job. In a split build the unit-test copies (pe_slu_ut,
   pe_slu_loop_ut) are added back to the chip copy with the same parameters, so the raw figure does not
   depend on the split.
2. chip: points of the chip instances only (hierarchy below tb.user_project) in the chip's source files
   (info.yaml source_files; the SRAM simulation model sram_sim.v is not one of them). Needs a split build:
   in a build without it, the USB helper and the receive loops share their points with the unit tests.
3. chip with exclusions: as 2, without the points listed in coverage/exclusions.csv. Each listed point needs
   a passing unreachability proof (formal/unreach/, the proof id in the file). The script fails if a listed
   point is hit (the list is stale), if a listed point does not exist (the RTL changed), or, with --proofs,
   if its proof did not pass.

Chip points that are not hit and not listed are holes; they are printed and written to --holes.

A point is named <copy>:<file>:<line>:<column>:<type>:<comment>, with copy usb (u_usb, pe_slu SPEC 1),
eth (u_eth, pe_slu SPEC 2), loop (the chip's two pe_slu_loop instances, one Verilator copy) or the module
name for the other chip files. Columns and comments are Verilator's.
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict

KINDS = ("line", "branch", "toggle")
TESTBENCH = {"tb.v"}
# Parameter sets of the pe_slu instances (test/tb.v, src/pe_proc.v): the copies Verilator keeps apart.
PE_SLU_PARAMS = {
    "tb.u_slu_ut": "FULL 1 SPEC 0",
    "tb.u_slc_ut": "FULL 0 SPEC 1",
    "tb.user_project.u_proc.u_usb": "FULL 0 SPEC 1",
    "tb.user_project.u_proc.u_eth": "FULL 1 SPEC 2",
}
CHIP_COPY = {"tb.user_project.u_proc.u_usb": "usb", "tb.user_project.u_proc.u_eth": "eth"}


def parse(line):
    start = line.index("'") + 1
    end = line.rindex("'")
    fields = {}
    for item in line[start:end].split("\x01"):
        if "\x02" in item:
            key, value = item.split("\x02", 1)
            fields[key] = value
    return fields, int(line[end + 1:].strip())


def chip_files(info):
    files, inside = set(), False
    for raw in open(info, encoding="utf-8"):
        if re.match(r"\s*source_files\s*:", raw):
            inside = True
            continue
        if inside:
            m = re.match(r"\s*-\s*\"?([^\"#\s]+)\"?", raw)
            if m:
                files.add(m.group(1))
            elif raw.strip() and not raw.strip().startswith("#"):
                break
    return files


def read_points(path):
    """One entry per coverage.dat record: kind, module, hier, file, line, col, comment, count."""
    points = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("C "):
                continue
            fl, count = parse(line)
            kind, _, module = fl.get("page", "?").partition("/")
            src = fl.get("f", "?").replace("\\", "/").rsplit("/", 1)[-1]
            if src in TESTBENCH:
                continue
            points.append({"kind": kind.removeprefix("v_"), "module": module, "h": fl.get("h", "?"), "file": src,
                           "line": int(fl.get("l", "0") or 0), "col": fl.get("n", ""), "comment": fl.get("o", ""),
                           "count": count})
    return points


def raw_copy(p):
    """The copy a build without coverage/split.py has for this point (instances with one parameter set)."""
    base = p["module"].removesuffix("_ut")
    if base.startswith("pe_slu_loop"):     # pe_slu_loop, pe_slu_loop_step: no parameters, one copy
        return base
    if base == "pe_slu":
        if p["h"] in PE_SLU_PARAMS:
            return "pe_slu " + PE_SLU_PARAMS[p["h"]]
        return "pe_slu " + p["h"]          # a build without the split: Verilator's own merged copy
    return p["module"] + " " + p["h"]


def chip_copy(p, files):
    if not p["h"].startswith("tb.user_project") or p["file"] not in files:
        return None
    if p["module"].endswith("_ut"):
        return None
    if p["module"] == "pe_slu":
        return CHIP_COPY.get(p["h"])         # None: merged with a unit-test copy (build without the split)
    if p["module"].startswith("pe_slu_loop"):
        return "loop"                         # the chip's two loops (u_usb, u_eth), one Verilator copy
    return p["module"]


def point_id(copy, p):
    return "%s:%s:%d:%s:%s:%s" % (copy, p["file"], p["line"], p["col"], p["kind"], p["comment"])


def pct(h, n):
    return "%d of %d = %.1f %%" % (h, n, 100.0 * h / n) if n else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dat")
    ap.add_argument("--info", default="info.yaml")
    ap.add_argument("--exclusions")
    ap.add_argument("--proofs")
    ap.add_argument("--holes")
    ap.add_argument("--toggle-holes")
    ap.add_argument("--src", default="src")
    args = ap.parse_args()
    files = chip_files(args.info)
    points = read_points(args.dat)
    errors = []

    # 1. raw, as before the split: one point per (copy with its parameter set, file, line, column, text, type)
    raw = defaultdict(int)
    for p in points:
        raw[(raw_copy(p), p["file"], p["line"], p["col"], p["comment"], p["kind"])] += p["count"]
    raw_tot, raw_hit = defaultdict(int), defaultdict(int)
    for (copy, src, _l, _c, _o, kind), count in raw.items():
        for key in ((kind, src), (kind, "(all RTL files)")):
            raw_tot[key] += 1
            raw_hit[key] += count > 0

    # 2. chip
    split = any(p["module"].endswith("_ut") for p in points)
    chip = {}
    for p in points:
        c = chip_copy(p, files)
        if c is None and split is False and p["h"].startswith("tb.user_project") and p["file"] in files:
            c = "(merged with a unit copy)"
        if c is not None:
            pid = point_id(c, p)
            if pid in chip:
                chip[pid]["count"] += p["count"]
            else:
                chip[pid] = dict(p, copy=c)

    # 3. exclusions
    excl = {}
    if args.exclusions:
        with open(args.exclusions, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                pid = row["point"].strip()
                excl[pid] = row
                if pid not in chip:
                    errors.append("exclusion %s: no such chip point (RTL or Verilator changed: stale)" % pid)
                elif chip[pid]["count"] > 0:
                    errors.append("exclusion %s: the point is hit %d times, so it is reachable: stale list"
                                  % (pid, chip[pid]["count"]))
    if args.proofs and excl:
        proofs = json.load(open(args.proofs, encoding="utf-8"))
        for pid, row in excl.items():
            for job in row["proof"].strip().split("+"):
                status = proofs.get(job, "missing")
                if status != "PASS":
                    errors.append("exclusion %s: proof %s %s" % (pid, job, status))

    tot, hit, ex = defaultdict(int), defaultdict(int), defaultdict(int)
    for pid, p in chip.items():
        for key in ((p["kind"], "chip"), (p["kind"], p["copy"])):
            tot[key] += 1
            hit[key] += p["count"] > 0
            ex[key] += pid in excl

    print("| Scope | line | branch | toggle |")
    print("|---|---|---|---|")
    print("| raw (all RTL files, as published) | %s |" % " | ".join(
        pct(raw_hit[(k, "(all RTL files)")], raw_tot[(k, "(all RTL files)")]) for k in KINDS))
    if not split:
        print("| chip | no split build: the USB helper and the receive loops share points with the unit tests |"
              " | |")
    else:
        print("| chip (tb.user_project, info.yaml source files) | %s |" % " | ".join(
            pct(hit[(k, "chip")], tot[(k, "chip")]) for k in KINDS))
        print("| chip with exclusions (%d points with a proof) | %s |" % (len(excl), " | ".join(
            pct(hit[(k, "chip")], tot[(k, "chip")] - ex[(k, "chip")]) for k in KINDS)))
        print()
        print("| Chip copy | line | branch | toggle |")
        print("|---|---|---|---|")
        for copy in sorted({c for (_k, c) in tot if c != "chip"}):
            print("| %s | %s |" % (copy, " | ".join(
                pct(hit[(k, copy)], tot[(k, copy)] - ex[(k, copy)]) + (" (%d excluded)" % ex[(k, copy)] if ex[(k, copy)]
                                                                       else "") for k in KINDS)))
    print()
    print("| Type | File | Points hit | Points | Coverage |")
    print("|---|---|---|---|---|")
    for kind, src in sorted(raw_tot, key=lambda k: (k[0], k[1].startswith("("), k[1])):
        n, h = raw_tot[(kind, src)], raw_hit[(kind, src)]
        print("| %s | %s | %d | %d | %.1f %% |" % (kind, src, h, n, 100.0 * h / n))

    text = {}

    def line_text(src, n):
        if src not in text:
            try:
                text[src] = open("%s/%s" % (args.src, src), encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                text[src] = []
        return text[src][n - 1].strip() if 0 < n <= len(text[src]) else ""

    holes = sorted((p for pid, p in chip.items() if p["count"] == 0 and pid not in excl and p["kind"] != "toggle"),
                   key=lambda p: (p["copy"], p["file"], p["line"], p["col"], p["kind"], p["comment"]))
    toggles = sorted((pid for pid, p in chip.items() if p["count"] == 0 and pid not in excl and p["kind"] == "toggle"))
    if split:
        print()
        print("Chip line and branch points not hit and not excluded (holes): %d; toggle: %d" % (len(holes), len(toggles)))
        for p in holes:
            print("- %s `%s`" % (point_id(p["copy"], p), line_text(p["file"], p["line"])))
    if args.holes:
        with open(args.holes, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["point", "copy", "file", "line", "col", "type", "comment", "source"])
            for p in holes:
                w.writerow([point_id(p["copy"], p), p["copy"], p["file"], p["line"], p["col"], p["kind"], p["comment"],
                            line_text(p["file"], p["line"])])
    if args.toggle_holes:
        open(args.toggle_holes, "w", encoding="utf-8").write("\n".join(toggles) + "\n")
    for e in errors:
        print("ERROR: " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

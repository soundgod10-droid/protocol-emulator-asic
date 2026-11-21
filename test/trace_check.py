# SPDX-License-Identifier: Apache-2.0
"""Traceability matrix: each numbered section of docs/isa.md, docs/host_port.md and docs/helpers.md -> the functional
coverage bins, tests and proofs that check it -> the CI job that runs them. The rows are in test/trace.csv.

Usage: python test/trace_check.py [--results PATTERN ...] [--out trace_report.md] [--selftest]

Checks (a failure gives exit 1):
  - every numbered heading of the three documents has one row with the same title (a new or renamed section fails until
    its row is written), and every row names a heading that exists;
  - a row with no evidence gives a reason (sections that state no checkable behaviour: summaries, area figures, choices
    and their reasons, figures measured outside CI);
  - each evidence item exists: bin:<group> is a group of test/fcov.py; test:<module>:<name> is a function defined in
    test/<module>.py; sweep:<family> is a case family of test/test_rtl_sweep.py; formal:<job> is formal/<job>.sby and a
    job of .github/workflows/formal.yaml;
  - with --results (cocotb results.xml files): every cited test of a module that has testcases in those files passed
    there, and every cited sweep family has passed testcases and no failed one.
--selftest plants four faults (a missing row; a test, a bin and a proof that do not exist) and checks that each fails.
"""

import argparse
import csv
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSV = os.path.join(HERE, "trace.csv")
DOCS = ("docs/isa.md", "docs/host_port.md", "docs/helpers.md")
CI_OF_MODULE = {"test_rtl_sweep": "sweep", "test_r3_sdf": "sdf"}       # every other test module runs in test.yaml


def headings(path):
    """(number, title) of each '## n' and '### n.m' heading outside code blocks."""
    out, fence = [], False
    for line in open(os.path.join(ROOT, path), encoding="utf-8"):
        if line.startswith("```"):
            fence = not fence
            continue
        m = None if fence else re.match(r"^#{2,3}\s+(\d+(?:\.\d+)?)\s+(.+?)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def fcov_groups():
    sys.path.insert(0, HERE)
    import fcov
    return set(fcov.GROUPS)


def load_rows(path=CSV):
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("doc") or "").startswith("#"):
                continue
            rows.append({k: (v or "").strip() for k, v in r.items()})
    return rows


def read_results(patterns):
    passed, failed, modules = set(), set(), set()
    for pat in patterns:
        for f in sorted(glob.glob(pat, recursive=True)):
            for tc in ET.parse(f).getroot().iter("testcase"):
                key = (tc.get("classname"), tc.get("name"))
                modules.add(key[0])
                bad = any(tc.find(t) is not None for t in ("failure", "error", "skipped"))
                (failed if bad else passed).add(key)
    return passed, failed, modules


def check(rows, results=None):
    """results: None, or (passed, failed, modules) from read_results. Returns (errors, table)."""
    errors, table = [], []
    groups = fcov_groups()
    formal_yaml = open(os.path.join(ROOT, ".github", "workflows", "formal.yaml"), encoding="utf-8").read()
    sweep_text = open(os.path.join(HERE, "test_rtl_sweep.py"), encoding="utf-8").read()
    sources = {}
    passed, failed, modules = results if results is not None else (set(), set(), set())
    want = {(d, s): t for d in DOCS for s, t in headings(d)}
    seen = set()
    for r in rows:
        key = (r["doc"], r["section"])
        if key in seen:
            errors.append("%s %s: more than one row" % key)
            continue
        seen.add(key)
        if key not in want:
            errors.append("%s %s: no such numbered section (stale row)" % key)
            continue
        if r["title"] != want[key]:
            errors.append("%s %s: title %r, the document says %r" % (key + (r["title"], want[key])))
        items = r["evidence"].split()
        if not items and not r["reason"]:
            errors.append("%s %s: no evidence and no reason" % key)
        ci = set()
        for it in items:
            kind, _, val = it.partition(":")
            if kind == "bin":
                ok = val in groups
                ci.add("sweep")
            elif kind == "test":
                mod, _, name = val.partition(":")
                p = os.path.join(HERE, mod + ".py")
                if p not in sources:
                    sources[p] = open(p, encoding="utf-8").read() if os.path.isfile(p) else ""
                ok = bool(name) and re.search(r"\bdef\s+%s\s*\(" % re.escape(name), sources[p]) is not None
                ci.add(CI_OF_MODULE.get(mod, "test"))
                if ok and mod in modules and (mod, name) not in passed:
                    errors.append("%s %s: %s did not pass in the given results" % (key + (it,)))
            elif kind == "sweep":
                ok = bool(val) and re.search(r',\s*"%s",' % re.escape(val), sweep_text) is not None
                ci.add("sweep")
                if ok and "test_rtl_sweep" in modules:
                    pre = "test_sweep_" + val
                    n_pass = sum(1 for m, n in passed if m == "test_rtl_sweep" and n.startswith(pre))
                    n_fail = sum(1 for m, n in failed if m == "test_rtl_sweep" and n.startswith(pre))
                    if n_pass == 0 or n_fail:
                        errors.append("%s %s: %s has %d passed and %d failed testcases in the given results"
                                      % (key + (it, n_pass, n_fail)))
            elif kind == "formal":
                ok = (bool(val) and os.path.isfile(os.path.join(ROOT, "formal", val + ".sby"))
                      and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(val), formal_yaml) is not None)
                ci.add("formal")
            else:
                ok = False
            if not ok:
                errors.append("%s %s: evidence %s does not exist" % (key + (it,)))
        table.append((r["doc"], r["section"], r["title"], " ".join(items) or "-", ", ".join(sorted(ci)) or "-",
                      r["reason"] or "-"))
    for key, title in want.items():
        if key not in seen:
            errors.append("%s %s: no row (section %r)" % (key + (title,)))
    return errors, table


def selftest(rows, log=print):
    last = rows[-1]

    def plus(item):
        return rows[:-1] + [dict(last, evidence=(last["evidence"] + " " + item).strip())]

    faults = [("a missing row", rows[1:]),
              ("a test that does not exist", plus("test:test_core:test_core_no_such_test")),
              ("a bin that does not exist", plus("bin:no.such_group")),
              ("a proof that does not exist", plus("formal:no_such_job"))]
    caught = 0
    for name, rs in faults:
        errs, _ = check(rs)
        caught += bool(errs)
        log("planted fault (%s): %s" % (name, "caught" if errs else "NOT caught"))
    log("self-test: %d of %d planted faults caught" % (caught, len(faults)))
    return caught == len(faults)


def summary(table, errors):
    n_ev = sum(1 for t in table if t[3] != "-")
    return ("traceability: %d numbered sections, %d with bins, tests or proofs, %d with a written reason; %d failures"
            % (len(table), n_ev, len(table) - n_ev, len(errors)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="*", default=None)
    ap.add_argument("--out", default="trace_report.md")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    rows = load_rows()
    results = read_results(args.results) if args.results else None
    errors, table = check(rows, results)
    out = ["## Traceability matrix (test/trace.csv, test/trace_check.py)\n",
           "**%s.**\n" % summary(table, errors)]
    if results is not None:
        out.append("Results checked: the cited tests of modules %s.\n" % (", ".join(sorted(results[2])) or "none"))
    out += ["| Document | Section | Title | Evidence | CI job | Reason (no evidence) |", "|---|---|---|---|---|---|"]
    out += ["| %s |" % " | ".join(x.replace("|", "/") for x in t) for t in table]
    if errors:
        out += ["\n### Failures\n"] + ["- " + e for e in errors]
    text = "\n".join(out) + "\n"
    open(args.out, "w", encoding="utf-8").write(text)
    print(text)
    ok = not errors
    if args.selftest:
        ok = selftest(rows) and ok
    print(summary(table, errors))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

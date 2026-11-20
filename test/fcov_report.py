# SPDX-License-Identifier: Apache-2.0
"""Functional coverage report: merges the fcov.yml and fcov_first.json files of the CI jobs (test/fcov.py).

Usage: python test/fcov_report.py DIR [--reasons test/fcov_reasons.csv] [--out fcov_report.md]

DIR holds one folder per job with fcov.yml and fcov_first.json. The report gives, per group of test/fcov.py, its
clause, the bins declared and the bins hit (in any job), and the trace table: clause -> group -> bins hit -> the bench
(test label) of the first hit and its CI job.

Zero-bin rule: a bin with no hit in any job must have a row in the reasons file (columns group, bin, kind, ref): kind
"sby" with the cover job that shows it is unreachable, or kind "reason" with the written reason (for example a bin that
the encoding or a fixed configuration excludes). A zero bin without a row, a row whose bin is hit (stale), or a row
for a bin that is not declared fails the report (exit 1).
"""

import argparse
import csv
import glob
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fcov  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--reasons", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fcov_reasons.csv"))
    ap.add_argument("--out", default="fcov_report.md")
    args = ap.parse_args()
    hits, first, jobs = {}, {}, []
    for yml in sorted(glob.glob(os.path.join(args.dir, "**", "fcov.yml"), recursive=True)):
        data = yaml.safe_load(open(yml, encoding="utf-8")) or {}
        for g in fcov.GROUPS:
            for b, n in (data.get("top." + g, {}).get("bins:_hits") or {}).items():
                hits[(g, str(b))] = hits.get((g, str(b)), 0) + int(n)
        fj = os.path.join(os.path.dirname(yml), "fcov_first.json")
        if os.path.isfile(fj):
            d = json.load(open(fj, encoding="utf-8"))
            jobs.append(d.get("job", os.path.basename(os.path.dirname(yml))))
            for k, label in d.get("first", {}).items():
                first.setdefault(k, (label, d.get("job", "")))
    reasons = {}
    if os.path.isfile(args.reasons):
        for r in csv.DictReader(open(args.reasons, encoding="utf-8", newline="")):
            if r.get("group", "").strip() and not r["group"].startswith("#"):
                reasons[(r["group"].strip(), r["bin"].strip())] = (r["kind"].strip(), r["ref"].strip())
    declared, errors, out = set(), [], []
    tot = got = 0
    out.append("## Functional coverage (test/fcov.py, cocotb-coverage 2.0)\n")
    out.append("Jobs: %s\n" % (", ".join(sorted(set(jobs))) or "none"))
    rows, zero_rows = [], []
    for g, (bins, clause) in fcov.GROUPS.items():
        h = 0
        for b in bins:
            key = (g, str(b))
            declared.add(key)
            if hits.get(key, 0) > 0:
                h += 1
                if key in reasons:
                    errors.append("%s %s: listed in the reasons file but hit (stale row)" % key)
            else:
                rs = reasons.get(key)
                zero_rows.append((g, str(b), rs))
                if rs is None:
                    errors.append("%s %s: zero bin without an SBY cover or a reason" % key)
        tot += len(bins)
        got += h
        rows.append((g, clause, len(bins), h))
    for key in reasons:
        if key not in declared:
            errors.append("%s %s: reasons row for a bin that is not declared" % key)
    excused = sum(1 for _, _, rs in zero_rows if rs is not None)
    out.append("**%d bins declared in %d groups; %d hit (%.1f %%); %d zero bins with an SBY cover or a reason; %d zero "
               "bins without one.**\n" % (tot, len(rows), got, 100.0 * got / tot, excused, len(zero_rows) - excused))
    out.append("| Group | Clause | Bins | Hit | % |")
    out.append("|---|---|---|---|---|")
    for g, clause, n, h in rows:
        out.append("| %s | %s | %d | %d | %.1f |" % (g, clause, n, h, 100.0 * h / n))
    out.append("\n### Zero bins\n")
    out.append("| Group | Bin | SBY cover or reason |")
    out.append("|---|---|---|")
    for g, b, rs in zero_rows:
        out.append("| %s | %s | %s |" % (g, b, "%s: %s" % rs if rs else "**none (hole)**"))
    out.append("\n### Trace table: requirement -> test -> CI job\n")
    out.append("| Clause | Group | Bins hit | First hit by (bench label) | CI job |")
    out.append("|---|---|---|---|---|")
    for g, clause, n, h in rows:
        labels = {}
        for b in fcov.GROUPS[g][0]:
            k = repr((g, b))
            if k in first:
                labels.setdefault(first[k], 0)
                labels[first[k]] += 1
        top = sorted(labels.items(), key=lambda kv: -kv[1])[:3]
        out.append("| %s | %s | %d of %d | %s | %s |" % (
            clause, g, h, n, "; ".join("%s (%d)" % (lab, k) for (lab, _job), k in top) or "-",
            ", ".join(sorted({job for (_lab, job), _k in top})) or "-"))
    if errors:
        out.append("\n### Rule failures\n")
        out += ["- " + e for e in errors[:400]]
        if len(errors) > 400:
            out.append("- ... %d more" % (len(errors) - 400))
    text = "\n".join(out) + "\n"
    open(args.out, "w", encoding="utf-8").write(text)
    print(text)
    print("functional coverage: %d of %d bins hit, %d zero bins excused, %d rule failures" % (got, tot, excused,
                                                                                            len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

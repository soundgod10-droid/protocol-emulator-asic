"""Run the unreachability proof jobs written by formal/unreach/instrument.py and check their results.

Usage: python formal/unreach/run.py [--build formal/unreach/build] [--results results.json] [--jobs a,b]

Each job folder <build>/<job>/ holds <job>.sby. A job of excluded points must PASS (every assertion proved
unreachable by k-induction). A tooth job (first line of its .sby says "expected counterexample") must FAIL
with a counterexample: it shows that a wrong exclusion (a point the tests reach) is caught. results.json maps
each job to its SymbiYosys status (PASS, FAIL, UNKNOWN, ERROR, TIMEOUT); coverage/scope.py --proofs reads it.
The script returns 1 if any job has an unexpected result.
"""

import argparse
import json
import os
import subprocess
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="formal/unreach/build")
    ap.add_argument("--results", default="formal/unreach/build/results.json")
    ap.add_argument("--jobs", default="")
    ap.add_argument("--timeout", type=int, default=3000)
    args = ap.parse_args()
    want = set(j for j in args.jobs.split(",") if j)
    results, bad = {}, []
    for job in sorted(os.listdir(args.build)):
        sby = os.path.join(args.build, job, job + ".sby")
        if not os.path.isfile(sby) or (want and job not in want):
            continue
        tooth = "expected counterexample" in open(sby, encoding="utf-8").readline()
        t0 = time.time()
        try:
            subprocess.run(["sby", "-f", job + ".sby"], cwd=os.path.join(args.build, job), timeout=args.timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            status_file = os.path.join(args.build, job, job, "status")
            status = open(status_file).read().split()[0] if os.path.isfile(status_file) else "ERROR"
        except subprocess.TimeoutExpired:
            status = "TIMEOUT"
        results[job] = status
        ok = (status == "FAIL") if tooth else (status == "PASS")
        print("%-28s %-8s %-7s %6.0f s%s" % (job, "tooth" if tooth else "proof", status, time.time() - t0,
                                             "" if ok else "   <- unexpected"))
        if not ok:
            bad.append(job)
    json.dump(results, open(args.results, "w"), indent=1, sort_keys=True)
    if not results:
        print("no jobs found in %s" % args.build)
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

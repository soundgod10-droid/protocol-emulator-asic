# SPDX-License-Identifier: Apache-2.0
"""Post-route results of nextpnr runs (--report JSON): fmax of each clock and the utilisation per BEL type, one line
per seed, and the worst fmax over the seeds against the gate (G1: worst of the seeds >= 63 MHz).

Usage: fmax.py target gate_mhz report_seed1.json [report_seed2.json ...]
"""

import json
import re
import sys


def main(target, gate, paths):
    gate = float(gate)
    worst = None
    lines = []
    util = ""
    for p in paths:
        seed = re.search(r"(\d+)\.json$", p).group(1)
        try:
            with open(p) as f:
                rep = json.load(f)
        except (OSError, ValueError) as e:
            lines.append("seed %s: no report (%s)" % (seed, e))
            worst = 0.0
            continue
        fm = rep.get("fmax", {})
        if not fm:
            lines.append("seed %s: no fmax in the report" % seed)
            worst = 0.0
            continue
        ach = min(v["achieved"] for v in fm.values())
        worst = ach if worst is None else min(worst, ach)
        lines.append("seed %s: fmax %s" % (seed, ", ".join("%s %.2f MHz (target %.2f)" % (k, v["achieved"],
                                                                                          v["constraint"])
                                                       for k, v in fm.items())))
        u = rep.get("utilization", {})
        util = ", ".join("%s %d/%d" % (k, v["used"], v["available"]) for k, v in sorted(u.items()) if v["used"])
    verdict = "PASS" if worst is not None and worst >= gate else "FAIL"
    print("%s: worst fmax over %d seeds %.2f MHz, gate %.1f MHz: %s" % (target, len(paths), worst or 0.0, gate,
                                                                       verdict))
    for line in lines:
        print("  " + line)
    print("  utilisation (last seed): " + util)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))

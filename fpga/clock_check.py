# SPDX-License-Identifier: Apache-2.0
"""Clock-network check of a synthesized FPGA netlist (Yosys write_json): every flip-flop must be clocked by the
top-level clock input net, so no flip-flop sits on a derived (LUT-driven) clock. Prints the counts per clock net
and the flip-flop and memory cell counts; exits 1 if a flip-flop has another clock.

Usage: clock_check.py netlist.json top_module clock_port
"""

import json
import sys
from collections import Counter

FF_CLOCK_PORTS = ("CLK", "C")       # ECP5 TRELLIS_FF, Gowin DFF*: CLK; iCE40 SB_DFF*: C


def is_ff(cell_type):
    t = cell_type.upper()
    return ("FF" in t) and not t.startswith("$")


def main(path, top, port):
    with open(path) as f:
        net = json.load(f)
    mod = net["modules"][top]
    clk_bits = mod["ports"][port]["bits"]
    names = {}
    for name, n in mod["netnames"].items():
        for b in n["bits"]:
            names.setdefault(b, name)
    per_net = Counter()
    types = Counter()
    bad = []
    for cname, cell in mod["cells"].items():
        types[cell["type"]] += 1
        if not is_ff(cell["type"]):
            continue
        conn = cell["connections"]
        p = next((p for p in FF_CLOCK_PORTS if p in conn), None)
        if p is None:
            continue
        b = conn[p][0]
        per_net[names.get(b, str(b))] += 1
        if b not in clk_bits:
            bad.append((cname, names.get(b, str(b))))
    print("flip-flops per clock net: " + ", ".join("%s %d" % kv for kv in per_net.most_common()))
    print("cell types: " + ", ".join("%s %d" % kv for kv in sorted(types.items())))
    print("flip-flops not on %s: %d" % (port, len(bad)))
    for cname, n in bad[:20]:
        print("  %s clocked by %s" % (cname, n))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))

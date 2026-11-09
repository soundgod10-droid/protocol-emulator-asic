# SPDX-License-Identifier: Apache-2.0
"""Renames the escaped names in a flat netlist and in its SDF file, for Icarus Verilog.

Icarus splits an SDF instance name at each dot, so a cell of a flat netlist with a name such as
u_proc.u_mem.g_word[1].u_cg.u_cell is not found ("Cannot find u_proc in scope") and gets no delay.
This changes the same names in the same way in both files. The SDF lines of the wires (INTERCONNECT)
are left out: vvp cannot load them.

Usage: sdf_names.py netlist.v delays.sdf
"""

import re
import sys


def plain(name):
    return name.replace("\\", "").replace(".", "__").replace("[", "_").replace("]", "_")


def main(netlist, sdf):
    with open(netlist) as f:
        text = f.read()
    text, n_net = re.subn(r"\\(\S+)", lambda m: plain(m.group(1)), text)
    with open(netlist, "w") as f:
        f.write(text)

    lines, n_cell, n_wire = [], 0, 0
    with open(sdf) as f:
        for line in f:
            if "(INTERCONNECT " in line:
                n_wire += 1
                continue
            m = re.match(r"(\s*\(INSTANCE\s+)(\S+)(\).*)", line, re.S)
            if m and re.search(r"[.\[\\]", m.group(2)):
                line = m.group(1) + plain(m.group(2)) + m.group(3)
                n_cell += 1
            lines.append(line)
    with open(sdf, "w") as f:
        f.writelines(lines)
    print(f"names changed: {n_net} in the netlist, {n_cell} SDF instances; SDF wire lines removed: {n_wire}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

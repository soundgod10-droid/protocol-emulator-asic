# SPDX-License-Identifier: Apache-2.0
"""Renames the escaped names in a flat netlist and in its SDF file, for Icarus Verilog.

Icarus splits an SDF instance name at each dot, so a cell of a flat netlist with a name such as
u_proc.u_mem.g_word[1].u_cg.u_cell is not found ("Cannot find u_proc in scope") and gets no delay.
This changes the same names in the same way in both files. The SDF lines of the wires (INTERCONNECT)
are left out: vvp cannot load them.

The CELL entries of the SRAM macros are left out too. Their paths name bus bits (IOPATH A_CLK A_DOUT[0]),
which the SDF parser of vvp does not accept: it stops with "Too many errors" at the first macro entry, so
the about 3,600 cells after it in the file got no delay (sdf run 44045660470, notices). The macros are
simulated by the functional models of src/sram_sim.v, which have no specify paths. With --macro-vh the script
writes the largest value of the IOPATH entries of each macro (all bits, rise and fall, min:typ:max, in ns) to that
file as `define SRAM_512_DLY / SRAM_1024_DLY, for the macro models of test/sram_sdf.v, which put A_DOUT that long
after the clock edge; --macro-scale multiplies it (the control run of the SDF job inflates it beyond a clock period).

Usage: sdf_names.py netlist.v delays.sdf [--macro-vh sram_delay.vh] [--macro-scale 1.0]
"""

import re
import sys

MACRO = re.compile(r'\(CELLTYPE\s+"RM_IHPSG13_')


def plain(name):
    return name.replace("\\", "").replace(".", "__").replace("[", "_").replace("]", "_")


TRIPLE = re.compile(r"\(\s*(-?[\d.]+)\s*:\s*(-?[\d.]+)\s*:\s*(-?[\d.]+)\s*\)")
UNIT_NS = {"ps": 1e-3, "ns": 1.0, "us": 1e3}


def macro_delay(block, unit_ns):
    """(words, largest IOPATH value in ns) of one macro CELL block."""
    text = "".join(block)
    words = re.search(r'\(CELLTYPE\s+"RM_IHPSG13_1P_(\d+)x16', text).group(1)
    vals = [float(x) for b in block if "IOPATH" in b for t in TRIPLE.findall(b) for x in t]
    return words, (max(vals) * unit_ns if vals else 0.0)


def main(netlist, sdf, macro_vh=None, scale=1.0):
    with open(netlist) as f:
        text = f.read()
    text, n_net = re.subn(r"\\(\S+)", lambda m: plain(m.group(1)), text)
    with open(netlist, "w") as f:
        f.write(text)

    lines, block, depth, n_cell, n_wire, n_macro = [], None, 0, 0, 0, 0
    unit_ns, delays = 1.0, {}
    with open(sdf) as f:
        for line in f:
            ts = re.search(r"\(TIMESCALE\s+([\d.]+)\s*(ps|ns|us)\s*\)", line)
            if ts:
                unit_ns = float(ts.group(1)) * UNIT_NS[ts.group(2)]
            if "(INTERCONNECT " in line:
                n_wire += 1
                continue
            m = re.match(r"(\s*\(INSTANCE\s+)(\S+)(\).*)", line, re.S)
            if m and re.search(r"[.\[\\]", m.group(2)):
                line = m.group(1) + plain(m.group(2)) + m.group(3)
                n_cell += 1
            if block is None and re.match(r"\s*\(CELL\s*$", line):
                block, depth = [], 0
            if block is None:
                lines.append(line)
                continue
            block.append(line)
            depth += line.count("(") - line.count(")")
            if depth <= 0:
                if any(MACRO.search(b) for b in block):
                    n_macro += 1
                    words, d = macro_delay(block, unit_ns)
                    delays[words] = max(delays.get(words, 0.0), d)
                else:
                    lines.extend(block)
                block = None
    with open(sdf, "w") as f:
        f.writelines(lines)
    print(f"names changed: {n_net} in the netlist, {n_cell} SDF instances; SDF wire lines removed: {n_wire}; "
          f"SRAM macro entries removed: {n_macro}")
    if macro_vh:
        with open(macro_vh, "w") as f:
            f.write("// written by test/sdf_names.py from %s (largest macro IOPATH value x %g)\n" % (sdf, scale))
            for words in ("512", "1024"):
                f.write("`define SRAM_%s_DLY %.3f\n" % (words, delays.get(words, 0.0) * scale))
        print(f"macro read delay (ns, largest IOPATH value x {scale:g}): " +
              ", ".join(f"{w} words {delays.get(w, 0.0) * scale:.3f}" for w in ("512", "1024")) + f" -> {macro_vh}")


if __name__ == "__main__":
    opts = dict(zip(sys.argv[3::2], sys.argv[4::2]))
    main(sys.argv[1], sys.argv[2], opts.get("--macro-vh"), float(opts.get("--macro-scale", "1")))

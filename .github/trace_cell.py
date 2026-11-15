"""Pin connections of one cell instance in the netlist of every step of a LibreLane run.

Usage: python trace_cell.py <folder that holds runs/wokwi> <label> <instance substring>

For each step folder (in step order) that has a gate-level netlist (*.nl.v or
*.pnl.v), prints the pin list of the first instance whose name contains the
substring, so the first step where a pin is missing or changes can be seen.
Also gives the disconnected-pin report of the run. Output: notice annotations.
"""

import glob
import os
import re
import sys


def notice(title, text):
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def pins_of(path, needle):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    i = text.find(needle)
    if i < 0:
        return None
    start = text.rfind("\n", 0, i) + 1
    end = text.find(";", i)
    inst = " ".join(text[start:end].split())
    return " ".join(re.findall(r"\.\w+\s*\([^)]*\)", inst)) or inst[:300]


def main(root, label, needle):
    wokwi = os.path.join(root, "runs", "wokwi")
    steps = sorted((d for d in glob.glob(os.path.join(wokwi, "[0-9]*-*")) if os.path.isdir(d)),
                   key=lambda d: int(os.path.basename(d).split("-")[0]))
    lines, last = [], None
    for step in steps:
        nets = sorted(glob.glob(os.path.join(step, "*.pnl.v")) or glob.glob(os.path.join(step, "*.nl.v")))
        if not nets:
            continue
        pins = pins_of(nets[0], needle)
        if pins != last:
            lines.append(f"{os.path.basename(step)}: {pins}")
            last = pins
    notice(f"{label} pins of {needle} (only steps where they change)", "\n".join(lines) or "no netlists found")
    for rpt in sorted(glob.glob(os.path.join(wokwi, "*disconnected*", "*"))):
        if os.path.isfile(rpt) and rpt.endswith((".rpt", ".log", ".txt")):
            with open(rpt, encoding="utf-8", errors="replace") as f:
                tail = f.read().splitlines()[-30:]
            notice(f"{label} {os.path.basename(os.path.dirname(rpt))}/{os.path.basename(rpt)}", "\n".join(tail))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))

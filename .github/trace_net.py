"""Fanout of the net driven by one instance pin, in the netlist of every step of a LibreLane run.

Usage: python trace_net.py <folder that holds runs/wokwi> <label> <instance> <pin> [<instance> <pin> ...]

For each step folder (in step order) with a gate-level netlist (*.pnl.v or *.nl.v),
finds the net on <instance>.<pin> and counts the other pins on that net. Prints the
steps where the net or its fanout changes, as one notice annotation per instance.
"""

import glob
import os
import re
import sys


def notice(title, text):
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def fanout(text, inst, pin):
    m = re.search(r"\s" + re.escape(inst) + r"\s*\((.*?)\);", text, re.S)
    if not m:
        return None
    p = re.search(r"\." + re.escape(pin) + r"\s*\(\s*([^\s)]+)\s*\)", m.group(1))
    if not p:
        return None
    net = p.group(1)
    loads = len(re.findall(r"\.\w+\s*\(\s*" + re.escape(net) + r"\s*\)", text)) - 1
    return f"{net} fanout {loads}"


def main(root, label, pairs):
    wokwi = os.path.join(root, "runs", "wokwi")
    steps = sorted((d for d in glob.glob(os.path.join(wokwi, "[0-9]*-*")) if os.path.isdir(d)),
                   key=lambda d: int(os.path.basename(d).split("-")[0]))
    texts = []
    for step in steps:
        nets = sorted(glob.glob(os.path.join(step, "*.pnl.v")) or glob.glob(os.path.join(step, "*.nl.v")))
        if nets:
            with open(nets[0], encoding="utf-8", errors="replace") as f:
                texts.append((os.path.basename(step), f.read()))
    for inst, pin in zip(pairs[0::2], pairs[1::2]):
        lines, last = [], None
        for step, text in texts:
            fo = fanout(text, inst, pin)
            if fo != last:
                lines.append(f"{step}: {fo}")
                last = fo
        if texts:
            lines.append("load cells in the last netlist: " + load_cells(texts[-1][1], inst, pin))
        notice(f"{label} {inst}/{pin} through the steps", "\n".join(lines) or "no netlists found")
    return 0


def load_cells(text, inst, pin):
    """Cell type and pin of every load on the net of inst.pin, counted by type."""
    m = re.search(r"\s" + re.escape(inst) + r"\s*\((.*?)\);", text, re.S)
    p = re.search(r"\." + re.escape(pin) + r"\s*\(\s*([^\s)]+)\s*\)", m.group(1)) if m else None
    if not p:
        return "net not found"
    net = p.group(1)
    found = {}
    for b in re.finditer(r"^\s*(\w+)\s+(\\?\S+)\s*\((.*?)\);", text, re.S | re.M):
        master, name, body = b.group(1), b.group(2), b.group(3)
        if name == inst:
            continue
        for q in re.finditer(r"\.(\w+)\s*\(\s*" + re.escape(net) + r"\s*\)", body):
            key = f"{master}.{q.group(1)}"
            found[key] = found.get(key, 0) + 1
    return ", ".join(f"{k} x{v}" for k, v in sorted(found.items()))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))

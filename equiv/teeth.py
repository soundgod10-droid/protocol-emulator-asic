# SPDX-License-Identifier: Apache-2.0
"""Planted faults and the netlist-identity check for the netlist-to-RTL equivalence (equiv/chip.eqy).

What it does:
  plant NETLIST OUTDIR   writes one copy of the final netlist per planted fault (the six faults of the verification
                         plan): ff_d_swap (the D nets of two flip-flops exchanged), cts_inv (a clock-tree buffer made an
                         inverter), hold_inv (a hold-repair buffer made an inverter), cg_gate_swap (the GATE nets of two
                         clock-gate cells exchanged), lat_d_swap (the D nets of two latch cells exchanged), sync_bypass
                         (the second stage of an input synchronizer fed from the stage's input instead of the first
                         stage). It prints the instance and nets that each fault changed. eqy must report every copy
                         as not equivalent, or the job fails (a check that cannot fail proves nothing).
  same NL PNL [SUB]      checks that the power netlist PNL without its power connections and without the cells that
                         have no logic (fill, tap, decap, antenna diode) has the same instances, cell types and signal
                         connections as NL, and that SUB (the netlist of the submission) is one of the two.

Why:
  The equivalence result is used as evidence that the simulated and taped-out logic is the RTL; it must be shown that
  the job detects the faults a layout flow could make, and that the netlist compared is the one that is taped out.
"""

import os
import re
import sys

INST = re.compile(r"^[ \t]*([A-Za-z_][\w$]*)[ \t]+(\\\S+[ \t]|[A-Za-z_][\w$\[\]]*)[ \t]*\((.*?)\);", re.M | re.S)
PIN = re.compile(r"\.([A-Za-z_][\w!]*)[ \t]*\(([^()]*)\)")
POWER_PINS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "VDD!", "VSS!", "VDDARRAY!", "VDDARRAY", "VDDA", "VSSA"}
NO_LOGIC = ("fill", "decap", "tap", "antenna", "diode")


class Inst:
    def __init__(self, m):
        self.m = m
        self.cell, self.name = m.group(1), m.group(2).strip()
        self.body = m.group(3)
        self.pins = {p: n.strip() for p, n in PIN.findall(self.body)}


def instances(text):
    out = []
    for m in INST.finditer(text):
        if m.group(1) in ("module", "input", "output", "wire", "assign", "inout", "reg"):
            continue
        out.append(Inst(m))
    return out


def with_pins(text, inst, changes, cell=None):
    """Text with the pin nets of one instance changed ({pin: net} or {pin: (new_pin, net)}) and its cell renamed."""
    body = inst.body
    for pin, new in changes.items():
        new_pin, net = new if isinstance(new, tuple) else (pin, new)
        body, n = re.subn(r"\.%s([ \t]*)\(([^()]*)\)" % re.escape(pin),
                          lambda mm: ".%s%s(%s)" % (new_pin, mm.group(1), net if net.endswith(" ") or not
                                                     net.startswith("\\") else net + " "), body, count=1)
        assert n == 1, "pin %s of %s not found" % (pin, inst.name)
    a, b = inst.m.span(3)
    text = text[:a] + body + text[b:]
    if cell:
        a, b = inst.m.span(1)
        text = text[:a] + cell + text[b:]
    return text


def is_const(net):
    return "'" in net or net in ("", "1'b0", "1'b1")


def by_cell(insts, pred):
    return [i for i in insts if pred(i.cell)]


def drivers(insts):
    """Net -> (instance, pin) for the output pins of the cells used here."""
    out = {}
    for i in insts:
        for p in ("Q", "X", "Y", "GCLK"):
            if p in i.pins:
                out[i.pins[p]] = (i, p)
    return out


def plant(path, outdir):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    insts = instances(text)
    os.makedirs(outdir, exist_ok=True)
    made = []

    def write(name, new_text, what):
        with open(os.path.join(outdir, name + ".v"), "w", encoding="utf-8") as f:
            f.write(new_text)
        made.append(name)
        print("%s: %s" % (name, what))

    ffs = [i for i in by_cell(insts, lambda c: re.match(r"sg13cmos5l_df\w*", c)) if "D" in i.pins
           and not is_const(i.pins["D"])]
    a = ffs[0]
    b = next(i for i in ffs[1:] if i.pins["D"] != a.pins["D"])
    t = with_pins(text, b, {"D": a.pins["D"]})
    t = with_pins(t, instances(t)[[x.name for x in instances(t)].index(a.name)], {"D": b.pins["D"]})
    write("ff_d_swap", t, "D of %s (%s) and %s (%s) exchanged" % (a.name, a.pins["D"], b.name, b.pins["D"]))

    cts = next(i for i in insts if i.name.startswith("clkbuf_") and re.match(r"sg13cmos5l_buf_\d+$", i.cell))
    size = cts.cell.rsplit("_", 1)[1]
    write("cts_inv", with_pins(text, cts, {"X": ("Y", cts.pins["X"])}, cell="sg13cmos5l_inv_%s" % size),
          "%s %s made sg13cmos5l_inv_%s" % (cts.cell, cts.name, size))

    hold = next(i for i in insts if i.name.startswith("hold") and "X" in i.pins and "A" in i.pins)
    write("hold_inv", with_pins(text, hold, {"X": ("Y", hold.pins["X"])}, cell="sg13cmos5l_inv_1"),
          "%s %s made sg13cmos5l_inv_1" % (hold.cell, hold.name))

    cgs = by_cell(insts, lambda c: c == "sg13cmos5l_lgcp_1")
    a = cgs[0]
    b = next(i for i in cgs[1:] if i.pins["GATE"] != a.pins["GATE"])
    t = with_pins(text, b, {"GATE": a.pins["GATE"]})
    t = with_pins(t, instances(t)[[x.name for x in instances(t)].index(a.name)], {"GATE": b.pins["GATE"]})
    write("cg_gate_swap", t, "GATE of %s (%s) and %s (%s) exchanged" % (a.name, a.pins["GATE"], b.name,
                                                                      b.pins["GATE"]))

    lats = by_cell(insts, lambda c: c == "sg13cmos5l_dlhq_1")
    a = lats[0]
    b = next(i for i in lats[1:] if i.pins["D"] != a.pins["D"])
    t = with_pins(text, b, {"D": a.pins["D"]})
    t = with_pins(t, instances(t)[[x.name for x in instances(t)].index(a.name)], {"D": b.pins["D"]})
    write("lat_d_swap", t, "D of %s (%s) and %s (%s) exchanged" % (a.name, a.pins["D"], b.name, b.pins["D"]))

    drv = drivers(insts)

    def source_ff(net, depth=0):
        """The flip-flop that drives net, through buffers and delay cells."""
        if net not in drv or depth > 8:
            return None
        i, _ = drv[net]
        if re.match(r"sg13cmos5l_df\w*", i.cell):
            return i
        if re.match(r"sg13cmos5l_(buf|dlygate)\w*", i.cell) and "A" in i.pins:
            return source_ff(i.pins["A"], depth + 1)
        return None

    sync = None
    for i in ffs:
        q = i.pins.get("Q", "")
        if "stage[1]" not in q:
            continue
        first = source_ff(i.pins["D"])
        if first is not None and "stage[0]" in first.pins.get("Q", "") and not is_const(first.pins["D"]):
            sync = (i, first)
            break
    assert sync is not None, "no two-stage synchronizer found"
    second, first = sync
    write("sync_bypass", with_pins(text, second, {"D": first.pins["D"]}),
          "D of %s (Q %s) taken from %s instead of the first stage %s" % (second.name, second.pins["Q"],
                                                                         first.pins["D"], first.name))
    return made


def logic_view(path):
    with open(path, encoding="utf-8") as f:
        insts = instances(f.read())
    view = {}
    for i in insts:
        if any(k in i.cell.lower() for k in NO_LOGIC):
            continue
        view[i.name] = (i.cell, tuple(sorted((p, n) for p, n in i.pins.items() if p not in POWER_PINS)))
    return view


def same(nl, pnl, sub=None):
    a, b = logic_view(nl), logic_view(pnl)
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    diff = sorted(n for n in set(a) & set(b) if a[n] != b[n])
    print("nl: %d logic instances; pnl: %d; only in nl %d, only in pnl %d, different %d" % (
        len(a), len(b), len(only_a), len(only_b), len(diff)))
    for n in (only_a + only_b + diff)[:10]:
        print("  %s: nl %s / pnl %s" % (n, a.get(n), b.get(n)))
    ok = not only_a and not only_b and not diff
    if sub:
        s = logic_view(sub)
        print("submission netlist: %d logic instances, equal to nl: %s" % (len(s), s == a))
        ok = ok and s == a
    return ok


if __name__ == "__main__":
    if sys.argv[1] == "plant":
        plant(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "same":
        sys.exit(0 if same(*sys.argv[2:5]) else 1)
    else:
        sys.exit("usage: teeth.py plant NETLIST OUTDIR | same NL PNL [SUB]")

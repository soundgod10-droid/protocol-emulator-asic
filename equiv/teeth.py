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
OUT_PINS = {"Q", "Q_N", "X", "Y", "GCLK", "A_DOUT"}
ASSIGN = re.compile(r"^[ \t]*assign[ \t]+(\\\S+|[\w$.\[\]]+)[ \t]*=[ \t]*(\\\S+|[\w$.\[\]']+)[ \t]*;", re.M)
# Clock pins of the sequential cells: flip-flops, latches (GATE, from a clock gate), clock gates, SRAM macros.
SINKS = (("flip-flop", r"sg13cmos5l_s?df\w*", "CLK"), ("latch", r"sg13cmos5l_dlh\w*", "GATE"),
         ("clock gate", r"sg13cmos5l_lgcp\w*", "CLK"), ("SRAM macro", r"RM_IHPSG13\w*", "A_CLK"))


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


def aliases(text):
    """Net -> net for the port assignments of the netlist (assign uo_out[7] = net12;)."""
    return {m.group(1).strip(): m.group(2).strip() for m in ASSIGN.finditer(text)}


def clock_view(path):
    """Sequential cell -> (kind, source, inversions): what its clock pin reaches through buffers, delay cells and
    inverters (the chip's clk port, a clock-gate cell, or the first other cell or net), and how many inverters
    are on the way, modulo 2."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    insts = instances(text)
    drv = drivers(insts)
    alias = aliases(text)
    view = {}
    for i in insts:
        kind, pin = next(((k, p) for k, pat, p in SINKS if re.match(pat + "$", i.cell)), (None, None))
        if kind is None or pin not in i.pins:
            continue
        net, inv, src = i.pins[pin], 0, "loop"
        for _ in range(256):
            if net == "clk":
                src = "clk"
                break
            if net not in drv and net in alias:
                net = alias[net]
                continue
            if net not in drv:
                src = "net " + net
                break
            d, p = drv[net]
            if p == "GCLK":
                src = "clock gate " + d.name
                break
            if re.match(r"sg13cmos5l_(buf|dlygate)\w*$", d.cell):
                net = d.pins["A"]
            elif re.match(r"sg13cmos5l_inv\w*$", d.cell):
                inv ^= 1
                net = d.pins["A"]
            else:
                src = "cell %s %s" % (d.cell, d.name)
                break
        view[i.name] = (kind, src, inv)
    return view


def clocks(nl, synth=None):
    """Clock-tree check of the final netlist NL: every flip-flop, clock gate and SRAM macro is clocked from the clk
    port through buffers and inverters only, every latch from a clock gate through buffers only, the clock gates
    and macros with no inversion; with SYNTH (the synthesis netlist, before the clock tree and the repair
    buffers) every sequential cell keeps its clock source and its inversion. The netlist equivalence (dprove)
    takes one global clock and cannot see the edge a cell is clocked on; this check covers that."""
    a = clock_view(nl)
    bad = []
    counts = {}
    for name, (kind, src, inv) in sorted(a.items()):
        short = "clock gate" if src.startswith("clock gate") else src
        counts[(kind, short, inv)] = counts.get((kind, short, inv), 0) + 1
        want = "clock gate" if kind == "latch" else "clk"
        if short != want or (inv and kind != "flip-flop"):
            bad.append("%s %s: clock from %s, %d inversion(s)" % (kind, name, src, inv))
    for (kind, src, inv), n in sorted(counts.items()):
        print("%s: %d clocked from %s with %d inversion(s)" % (kind, n, src, inv))
    if synth:
        b = clock_view(synth)
        only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
        diff = sorted(n for n in set(a) & set(b) if a[n] != b[n])
        print("synthesis netlist: %d sequential cells; only in the final netlist %d, only in synthesis %d, "
              "clock source or inversion changed %d" % (len(b), len(only_a), len(only_b), len(diff)))
        bad += ["%s: final %s / synthesis %s" % (n, a[n], b[n]) for n in diff]
        bad += ["%s: only in the final netlist" % n for n in only_a] + ["%s: only in synthesis" % n for n in only_b]
    for line in bad[:12]:
        print("  " + line)
    print("clock tree: %d sequential cells, %d problem(s)" % (len(a), len(bad)))
    return not bad


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
    drv = drivers(insts)
    alias = aliases(text)

    def tooth(name, make):
        """Plants one fault; a fault that has no place in this netlist is reported and the others are still made."""
        try:
            new_text, what = make()
        except (AssertionError, StopIteration, LookupError) as e:
            print("%s: not planted on this netlist (%s)" % (name, str(e) or type(e).__name__))
            return
        write(name, new_text, what)

    def swap(a, b, pin):
        t = with_pins(text, b, {pin: a.pins[pin]})
        t = with_pins(t, instances(t)[[x.name for x in instances(t)].index(a.name)], {pin: b.pins[pin]})
        return t, "%s of %s (%s) and %s (%s) exchanged" % (pin, a.name, a.pins[pin], b.name, b.pins[pin])

    def root(net, depth=0):
        """(net, driving cell) in front of the buffers and delay cells that lead to net."""
        if depth < 32 and net not in drv and net in alias:
            return root(alias[net], depth + 1)
        if depth < 32 and net in drv:
            i, _ = drv[net]
            if re.match(r"sg13cmos5l_(buf|dlygate)\w*", i.cell) and "A" in i.pins:
                return root(i.pins["A"], depth + 1)
            return net, i
        return net, None

    def is_ff(i):
        return i is not None and re.match(r"sg13cmos5l_df\w*", i.cell) is not None

    # A flip-flop D swap that reaches a pin within a few cycles: the flip-flop that drives HOST_MISO (uo_out[7],
    # docs/host_port.md 2) and the nearest flip-flop in the logic in front of its D. Two flip-flops taken by their
    # place in the file showed no output difference within 57 cycles from reset (equiv run 44919684023), so a
    # bounded check could not show that the miter catches the fault.
    def ff_d_swap():
        a = root("uo_out[7]")[1]
        assert is_ff(a), "uo_out[7] is not driven by a flip-flop through buffers"
        seen, todo = set(), [(a.pins["D"], 0)]
        while todo:
            net, depth = todo.pop(0)
            if net in seen or depth > 6:
                continue
            seen.add(net)
            if net not in drv and net in alias:
                todo.append((alias[net], depth))
                continue
            if net not in drv:
                continue
            i, _ = drv[net]
            if is_ff(i):
                if i is not a and not is_const(i.pins.get("D", "")) and i.pins["D"] != a.pins["D"]:
                    return swap(a, i, "D")
                continue
            todo += [(n, depth + 1) for p, n in i.pins.items() if p not in OUT_PINS and p not in POWER_PINS
                     and not is_const(n)]
        raise AssertionError("no flip-flop in front of the D of %s" % a.name)

    tooth("ff_d_swap", ff_d_swap)

    def cts_inv():
        cts = next(i for i in insts if i.name.startswith("clkbuf_") and re.match(r"sg13cmos5l_buf_\d+$", i.cell))
        size = cts.cell.rsplit("_", 1)[1]
        return (with_pins(text, cts, {"X": ("Y", cts.pins["X"])}, cell="sg13cmos5l_inv_%s" % size),
                "%s %s made sg13cmos5l_inv_%s" % (cts.cell, cts.name, size))

    tooth("cts_inv", cts_inv)

    def hold_inv():
        hold = next(i for i in insts if i.name.startswith("hold") and "X" in i.pins and "A" in i.pins)
        return (with_pins(text, hold, {"X": ("Y", hold.pins["X"])}, cell="sg13cmos5l_inv_1"),
                "%s %s made sg13cmos5l_inv_1" % (hold.cell, hold.name))

    tooth("hold_inv", hold_inv)

    def pair_swap(cell, pin):
        cells = by_cell(insts, lambda c: c == cell)
        a = cells[0]
        b = next(i for i in cells[1:] if i.pins[pin] != a.pins[pin])
        return swap(a, b, pin)

    tooth("cg_gate_swap", lambda: pair_swap("sg13cmos5l_lgcp_1", "GATE"))
    tooth("lat_d_swap", lambda: pair_swap("sg13cmos5l_dlhq_1", "D"))

    # The second stage of the HOST_SCK synchronizer (ui_in[7]) fed from the pin instead of from the first stage.
    # The first stage is the flip-flop whose D comes from the pin through buffers, the second the one whose D comes
    # from the first one's Q: the stage names do not survive the layout (equiv run 44919684023).
    def sync_bypass():
        for pin in ["ui_in[7]"] + ["ui_in[%d]" % k for k in range(7)]:
            first = next((i for i in ffs if root(i.pins["D"])[0] == pin), None)
            second = first and next((i for i in ffs if root(i.pins["D"])[1] is first), None)
            if second is not None:
                return (with_pins(text, second, {"D": first.pins["D"]}),
                        "D of %s (second stage of %s) taken from %s instead of %s" % (
                            second.name, pin, first.pins["D"], first.name))
        raise AssertionError("no two-stage synchronizer on a ui_in pin")

    tooth("sync_bypass", sync_bypass)
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
    elif sys.argv[1] == "clocks":
        sys.exit(0 if clocks(*sys.argv[2:4]) else 1)
    else:
        sys.exit("usage: teeth.py plant NETLIST OUTDIR | same NL PNL [SUB] | clocks NL [SYNTH_NL]")

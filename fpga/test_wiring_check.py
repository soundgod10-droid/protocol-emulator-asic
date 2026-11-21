# SPDX-License-Identifier: Apache-2.0
"""Simulation test of the wiring check (fpga/wiring_check.v) with the host judge (fpga/wiring_judge.py).

A host model reads the 16 FPGA outputs through a wiring map and drives the 8 ui inputs and rst_n through another; it
drives every host output low, then one at a time high, each for two frames, and judges the frames it decodes. The
correct wiring must give no fault; a swapped pair of output wires, a moved input wire and an open output wire must
each be named, and nothing else. Run by .github/workflows/fpga.yaml (job wiring) with TOPLEVEL=wiring_check.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

import wiring_judge

DIV = 25          # cycles a bit (wiring_check.v default)
FRAME = 42 * DIV  # cycles a frame


async def run_case(dut, out_map, in_map):
    """out_map: host input -> FPGA output index (None: open wire, the host's pull-up reads 1);
    in_map: host output -> ui bit (None: unplugged). Returns the judge's faults."""
    steps = [set()] + [{k} for k in range(8)] + [set()]
    samples = {h: [] for h in out_map}
    hist = []
    for n, high in enumerate(steps):
        rst = n % 2
        ui = 0
        for b in range(8):
            wired = [h for h in in_map if in_map[h] == b]
            if not wired or any(h in high for h in wired):
                ui |= 1 << b  # a pin with no wire reads 1 (pull-up); a driven-high wire reads 1
        dut.ui.value = ui
        dut.rst_n.value = rst
        for _ in range(2 * FRAME):
            await FallingEdge(dut.clk)
            pins = int(dut.uo.value) | int(dut.uio.value) << 8
            for h, p in out_map.items():
                samples[h].append(1 if p is None else pins >> p & 1)
            hist.append((frozenset(high), rst))
    out_frames = {h: wiring_judge.decode(s, DIV) for h, s in samples.items()}
    ref = next((f for h, f in sorted(out_frames.items()) if f), [])
    in_steps, rst_steps = [], []
    for t0, _, ui_seen, rst_seen in ref:
        window = hist[max(0, t0 - 3 * DIV):t0]
        if len(set(window)) != 1:
            continue  # the host changed its outputs while the FPGA sampled them
        high, rst = window[0]
        in_steps.append((set(high), ui_seen))
        rst_steps.append((rst, rst_seen))
    expected_out = {h: (p if p is not None else h) for h, p in out_map.items()}
    faults = wiring_judge.judge(out_frames, expected_out, in_steps, {h: h for h in in_map}, rst_steps)
    return faults, sum(len(f) for f in out_frames.values())


@cocotb.test()
async def test_wiring_check(dut):
    cocotb.start_soon(Clock(dut.clk, 40, unit="ns").start())  # the ULX3S's 25 MHz oscillator
    dut.ui.value = 0xFF
    dut.rst_n.value = 1
    ident_out = {h: h for h in range(16)}
    ident_in = {h: h for h in range(8)}

    swapped = dict(ident_out)
    swapped[3], swapped[9] = 9, 3
    moved = dict(ident_in)
    moved[2], moved[5] = 5, None
    open_out = dict(ident_out)
    open_out[12] = None

    cases = [
        ("correct wiring", ident_out, ident_in, []),
        ("output wires of host inputs 3 and 9 swapped", swapped, ident_in,
         ["host input 3: wired to uio[1], expected uo[3]", "host input 9: wired to uo[3], expected uio[1]"]),
        ("host output 2 moved to ui[5], ui[5]'s wire unplugged", ident_out, moved,
         ["ui[2]: high with every host output low", "host output 2: reaches ui[5], expected ui[2]",
          "host output 5: reaches no ui pin"]),
        ("wire of host input 12 open", open_out, ident_in, ["host input 12: no valid frame"]),
    ]
    summary = []
    for name, out_map, in_map, expect in cases:
        faults, nframes = await run_case(dut, out_map, in_map)
        summary.append("%s: %d frames decoded, faults %s" % (name, nframes, faults or "none"))
        assert len(faults) == len(expect), "%s: faults %s, expected %s" % (name, faults, expect)
        for e in expect:
            assert sum(f.startswith(e) for f in faults) == 1, "%s: %r not named once in %s" % (name, e, faults)
    dut._log.info("wiring check summary:\n  " + "\n  ".join(summary))

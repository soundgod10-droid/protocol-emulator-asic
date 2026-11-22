# SPDX-License-Identifier: Apache-2.0
"""Sega Mega Drive / Genesis 6-button pad host firmware (fw/sega6.s) against an independent pad model, in the reference
model and on the RTL at 60 MHz.

What it does:
  The pad model (Sega6Pad) is written from Charles Rosenberg's notes "Sega Six Button Controller Hardware Info" (version
  1.0, 9 September 1996, web.archive.org copy of cs.cmu.edu/~chuck/infopg/segasix.txt): the DB-9 table (select low:
  pins 1, 2 = up, down, pins 3, 4 = logic low, pins 6, 9 = A, Start; select high: up, down, left, right, B, C), the
  pulse-3 state (immediately after the third select pulse goes from low to high, pins 1, 2, 3 = Z, Y, X; the fourth
  pulse resets the pad to compatible mode) and the pin 3 waveforms. Pressed = low. It does not use the firmware, the
  assembler or the processor model. The notes give no figure for the custom chip's response time and only suspect a
  time-out; the model answers 2 us after each select edge and returns to compatible mode after 1 ms with no select
  edge (both stated assumptions). Pins 4, 6, 9 in the pulse-3 state are not in the notes: the model drives them with
  wrong data on purpose, so that a host which reads them fails. The three-button pad (74HC157, 20 ns) has no pulse-3
  state.
  The model test polls the pad with no button, each of the 11 buttons alone, all buttons and three mixed sets; it
  checks a three-button pad, no pad (host pull-ups), the select timing (20 us half period, as the notes' 13.8 us to
  100 us) and two negative controls (read at the select edge: RD=0, and 1 us after it: RD=60). The RTL test polls three
  sets in lockstep with the reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  Twenty-sixth row of list P (flex/BLIND.md, record of beacon round 34159012), a replacement row for a deferred row
  (L2c-19, L2c-22). The pinned libsigrokdecode has no decoder for the pad, so the row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the 5 V pad levels (a level shifter is needed on the board),
the cable and the pad chip's real response time are not simulated.
"""

import os
import sys

import cocotb
from cocotb.utils import get_sim_time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
P_SEL = 12  # UO4 = DB-9 pin 7
PINS = {P_SEL: 0}
BUTTONS = ["up", "down", "left", "right", "B", "C", "A", "Start", "Z", "Y", "X"]  # bit order of the host word


def expect(pressed, present=True, three=False):
    """Host word for a set of pressed buttons (bit 15 = a pad answered)."""
    if not present:
        return 0
    p = set(pressed)
    if three:  # a three-button pad shows up, down, left again after the third pulse
        p = {b for b in p if b not in ("X", "Y", "Z")}
        p |= {z for d, z in (("up", "Z"), ("down", "Y"), ("left", "X")) if d in p}
    return sum(1 << BUTTONS.index(b) for b in p) | 0x8000


class Sega6Pad:
    """6-button pad on DB-9 pins 1, 2, 3, 4, 6, 9 = uio0..5 (see the module text). sets: one set of pressed buttons
    for each poll (the next set starts at the fourth select rise); delay_us: select edge to new pin data; three: a
    three-button pad (74HC157, no pulse-3 state); present=False: no pad (host pull-ups, all pins high)."""

    def __init__(self, sets, us=F_MHZ, delay_us=2.0, timeout_us=1000.0, three=False, present=True):
        self.us = us
        self.sets, self.k = list(sets), 0
        self.delay = max(1, int(round((0.02 if three else delay_us) * us)))
        self.timeout = int(round(timeout_us * us))
        self.three, self.present = three, present
        self.sel, self.rises, self.t_edge = 1, 0, 0
        self.out = [(0, self._pins())]  # (cycle from which the pins show this value, pins)
        self.edges = []  # (cycle, "sel", level)
        self.errors = []

    def _pressed(self):
        return set(self.sets[min(self.k, len(self.sets) - 1)]) if self.sets else set()

    def _pins(self):
        if not self.present:
            return 0x3F
        p = self._pressed()
        lv = lambda b: 0 if b in p else 1  # noqa: E731
        if not self.sel:
            if self.rises == 3 and not self.three:  # fourth low: not in the notes, pins 3 and 4 not grounded here
                return lv("up") | lv("down") << 1 | 1 << 2 | 1 << 3 | lv("A") << 4 | lv("Start") << 5
            return lv("up") | lv("down") << 1 | lv("A") << 4 | lv("Start") << 5
        if self.rises == 3 and not self.three:  # pulse-3: Z, Y, X; pins 4, 6, 9 wrong on purpose
            return lv("Z") | lv("Y") << 1 | lv("X") << 2 | (1 - lv("right")) << 3 | (1 - lv("B")) << 4 \
                | (1 - lv("C")) << 5
        return lv("up") | lv("down") << 1 | lv("left") << 2 | lv("right") << 3 | lv("B") << 4 | lv("C") << 5

    def __call__(self, c, uo, uio, oe):
        sel = (uo >> 4) & 1
        if self.rises and c - self.t_edge > self.timeout:
            self.rises = 0
            self.out.append((c, self._pins()))
        if sel != self.sel:
            self.edges.append((c, "sel", sel))
            self.sel, self.t_edge = sel, c
            if sel:
                self.rises += 1
                if self.rises == 4 or (self.three and self.rises == 4):
                    self.rises, self.k = 0, self.k + 1
            self.out.append((c + self.delay, self._pins()))
        while len(self.out) > 1 and self.out[1][0] <= c:
            self.out.pop(0)
        return 0, self.out[0][1] | 0xC0


SETS = [[], *[[b] for b in BUTTONS], BUTTONS, ["up", "A", "X", "Start"], ["down", "right", "C", "Y"],
        ["left", "B", "Z", "Start"]]


def assemble(**equ):
    return ff.assemble("sega6.s", **equ)


def model_run(pad, polls, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners(PINS))
    m.late = []  # (cycle, lead, address, program line) of late pin events and samples, for the failure message
    event_cycle = m._event_cycle

    def watch(th, c, t):
        d = (t - c) & 0xFFFF
        if not pe_model.LEAD <= d < pe_model.HORIZON and len(m.late) < 5:
            m.late.append((c, d - 0x10000 if d & 0x8000 else d, th.pc, prog.source[th.pc][1].strip()))
        return event_cycle(th, c, t)

    m._event_cycle = watch
    bench = ff.Bench(m, inputs=pad)
    bench.send(0, *range(polls))
    for _ in range(polls * 2 + 4):
        if len(bench.words(0)) >= polls:
            break
        bench.run(10_000)
    bench.run(2_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_sega6_words(dut):
    """Memory words of fw/sega6.s."""
    size = assemble().size
    dut._log.info(f"fw/sega6.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_sega6_model(dut):
    """fw/sega6.s in the reference model against the pad model: 16 button sets, a three-button pad, no pad, the select
    timing; negative controls for the read point."""
    log = dut._log
    pad = Sega6Pad(SETS)
    bench, m = model_run(pad, len(SETS))
    want = [expect(s) for s in SETS]
    assert not m.late, f"late events {m.late}"
    assert bench.words(0) == want, f"host words {[hex(w) for w in bench.words(0)]}, expected {[hex(w) for w in want]}"
    ff.assert_status_clean(m, [0])
    falls = [c for c, n, v in pad.edges if v == 0]
    rises = [c for c, n, v in pad.edges if v == 1]
    lows = [(r - f) / F_MHZ for f, r in zip(falls, rises)]
    highs = [(falls[i + 1] - rises[i]) / F_MHZ for i in range(len(falls) - 1) if i % 4 != 3]
    poll_t = (rises[3] - falls[0]) / F_MHZ
    log.info(f"model: {len(SETS)} polls as expected {[hex(w) for w in want]}; select low {min(lows):.2f} to "
             f"{max(lows):.2f} us, high within a poll {min(highs):.2f} to {max(highs):.2f} us (notes: 13.8 us in a "
             f"game, 100 us works); first fall to fourth rise {poll_t:.1f} us")
    assert all(abs(x - 20.0) < 0.1 for x in lows + highs), f"select half periods {lows} {highs}"
    sets3 = [[], ["up"], ["left", "B"], BUTTONS]
    pad = Sega6Pad(sets3, three=True)
    bench, m = model_run(pad, len(sets3))
    want = [expect(s, three=True) for s in sets3]
    assert not m.late and bench.words(0) == want, f"three-button pad: {[hex(w) for w in bench.words(0)]}"
    ff.assert_status_clean(m, [0])
    log.info(f"three-button pad: {[hex(w) for w in want]} (its up, down, left also read as Z, Y, X: stated limit)")
    pad = Sega6Pad([["A"]] * 2, present=False)
    bench, m = model_run(pad, 2)
    assert not m.late and bench.words(0) == [0, 0], f"no pad: {[hex(w) for w in bench.words(0)]}"
    ff.assert_status_clean(m, [0])
    log.info(f"no pad: {[hex(w) for w in bench.words(0)]} (bit 15 clear)")
    for equ in (dict(RD=0), dict(RD=60)):
        sets = [["X", "Y", "Z"], []]
        pad = Sega6Pad(sets)
        bench, m = model_run(pad, len(sets), **equ)
        got, want = bench.words(0), [expect(s) for s in sets]
        log.info(f"control {equ}: host words {[hex(w) for w in got]}, expected {[hex(w) for w in want]}")
        assert got != want, f"control {equ}: no fault found"


@cocotb.test()
async def test_sega6_rtl(dut):
    """fw/sega6.s on the chip at 60 MHz against the pad model: three button sets, in lockstep with the reference
    model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl sega6"
    sets = [["X", "Start"], ["up", "A", "Z"], BUTTONS]
    want = [expect(s) for s in sets]
    pad = Sega6Pad(sets)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners(PINS), inputs=pad, label=label)
    bench.send(0, *range(len(sets)))
    if hasattr(dut, "dump_off"):  # waveform of the first poll (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 12_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; select edges (ps, level) "
                 f"{[(round(now - (bench.m.cycle - c) * rf.CLK_PS), v) for c, n, v in pad.edges[:8]]}")
    await bench.collect(0, len(want), limit=100_000)
    await bench.run(2000)
    await bench.finish(log)
    assert bench.words(0) == want, f"{label}: {[hex(w) for w in bench.words(0)]}, expected {[hex(w) for w in want]}"
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}")

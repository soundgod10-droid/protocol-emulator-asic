# SPDX-License-Identifier: Apache-2.0
"""HX711 reader firmware (fw/hx711.s) against an independent HX711 model, in the reference model and on the RTL at
60 MHz.

What it does:
  The device model (Hx711) is written from the Avia Semiconductor HX711 data sheet (24-Bit Analog-to-Digital
  Converter for Weigh Scales): "Output Data Rate and Format" and "Serial Interface" (page 4), Table 3 (input and gain
  by the number of PD_SCK pulses), Fig. 2 and its timing table (page 5: T1 DOUT fall to the first PD_SCK rise at
  least 0.1 us, T2 PD_SCK rise to DOUT data at most 0.1 us, T3 PD_SCK high 0.2 to 50 us, T4 PD_SCK low at least
  0.2 us), "Reset and Power-Down" with Fig. 3 (PD_SCK high for more than 60 us powers down; its return to low resets to
  channel A, gain 128) and the output settling time (page 3: four conversion periods after a reset, 400 ms at 10
  samples/s, 50 ms at 80 samples/s). It does not use the firmware, the assembler or the processor model. It drives
  DOUT low when a conversion is ready, shifts the 24 bits MSB first, each T2 after a PD_SCK rise, drives DOUT high at
  the 25th rise, counts the pulses of each read (25 to 27 needed) and checks T1, T3, T4, that no pulse comes while
  DOUT is high, and that no read is cut by a power-down. Model assumption (the data sheet does not say it): after the
  last pulse of a read the next conversion starts with the selected input and gain and is ready one conversion period
  later; the analog settling after a gain change is not modelled (each input gives a fixed result).
  The model test reads with all three pulse counts, with a power-down and reset between reads, at 80 samples/s; a
  host that is late (the result waits) and negative controls for T3, T4 and the read time. The RTL test runs the
  firmware on the chip in lockstep with the reference model with a shortened conversion period; the same test runs on
  the gate-level netlist (gl_fw workflow).

Why:
  Sixth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). The HX711 has no registers:
  the pulse count after each read selects the input, and the clock line is also its power-down control.

Conditions: logic simulation at 60 MHz with ideal pins; the analog front end (load cell, PGA, ADC noise) is not
simulated; the conversion period is 12.5 ms (80 samples/s) in the model test and 0.2 ms in the RTL test (shortened so
that the RTL run lasts minutes; the firmware has no time constant tied to it).
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
P_SCK = 12  # PD_SCK = uo_out[4]
UI_DOUT = 3  # DOUT = ui_in[3]

# HX711 data sheet, page 5 (us): T1 min, T2 max, T3 min and max, T4 min; Fig. 3 power-down time
T1, T2, T3_MIN, T3_MAX, T4, T_PD = 0.1, 0.1, 0.2, 50.0, 0.2, 60.0
SETTLE = 4  # conversion periods after a reset (page 3: 400 ms at 10 SPS, 50 ms at 80 SPS)
SEL = {25: "A128", 26: "B32", 27: "A64"}  # Table 3
VALUE = {"A128": 0x123456, "B32": 0xF0A1B2, "A64": 0x7FFFFF}  # fixed input of each channel and gain (test choice)


class Hx711:
    """HX711 on PD_SCK and DOUT (see the module text). period: conversion period in cycles. Records reads
    [(cycle of the first rise, pulses, value shifted out, selection used)], errors."""

    def __init__(self, period, us=F_MHZ):
        self.us, self.p = us, period
        self.sck, self.high_since, self.low_since = 1, 0, 0
        self.initial = True  # PD_SCK high from the chip reset until the firmware sets it low
        self.pd = False
        self.sel = "A128"
        self.ready_at = SETTLE * period  # power-on reset at cycle 0
        self.ready, self.dout, self.ready_c = False, 1, None
        self.word, self.pulses, self.first = 0, 0, None
        self.pend = None  # (cycle, DOUT level) T2 after a rise
        self.reads, self.errors = [], []

    def _end_read(self, c):
        n = self.pulses
        if not 25 <= n <= 27:
            self.errors.append(f"cycle {self.first}: {n} PD_SCK pulses in a read (25 to 27 needed, page 4)")
        self.reads.append((self.first, n, self.word, self.sel_used))
        self.sel = SEL.get(n, self.sel)
        self.pulses, self.first, self.ready, self.dout = 0, None, False, 1
        self.ready_at = c + self.p

    def __call__(self, c, uo, uio, oe):
        us = self.us
        if self.pend and c >= self.pend[0]:
            self.dout, self.pend = self.pend[1], None
        sck = (uo >> 4) & 1
        if sck and not self.sck:  # rise
            low = (c - self.low_since) / us
            if self.pd:
                pass
            elif not self.ready:
                self.errors.append(f"cycle {c}: PD_SCK pulse while DOUT is high (data not ready, page 4)")
            else:
                if self.pulses == 0:
                    t1 = (c - self.ready_c) / us
                    if t1 < T1:
                        self.errors.append(f"cycle {c}: first rise {t1:.3f} us after the DOUT fall (T1 0.1 us)")
                    self.first, self.sel_used = c, self.sel_now
                elif low < T4:
                    self.errors.append(f"cycle {c}: PD_SCK low {low:.3f} us (T4 0.2 us)")
                self.pulses += 1
                k = self.pulses
                level = (self.data >> (24 - k)) & 1 if k <= 24 else 1
                if k <= 24:
                    self.word = (self.word << 1) | level
                self.pend = (c + max(1, round(T2 * us)), level)
            self.high_since = c
        elif self.sck and not sck:  # fall
            high = (c - self.high_since) / us
            if self.pd or (self.initial and high > T_PD):
                self.pd = False  # reset (Fig. 3), channel A gain 128
                self.sel, self.ready, self.dout, self.pulses = "A128", False, 1, 0
                self.ready_at = c + SETTLE * self.p
            elif self.pulses and not T3_MIN <= high <= T3_MAX:
                self.errors.append(f"cycle {c}: PD_SCK high {high:.3f} us in a read (T3 0.2 to 50 us)")
            self.initial = False
            self.low_since = c
        self.sck = sck
        if sck and not self.pd and (c - self.high_since) / us > T_PD and not self.initial:
            if self.pulses:
                self.errors.append(f"cycle {c}: power-down inside a read after {self.pulses} pulses")
            self.pd, self.ready, self.dout, self.pulses = True, False, 1, 0
        if not sck and self.pulses and (c - self.low_since) / us > 20.0:
            self._end_read(c)
        if not self.pd and not self.ready and not self.pulses and self.ready_at is not None and c >= self.ready_at:
            self.sel_now = self.sel
            self.data = VALUE[self.sel]
            self.ready, self.dout, self.ready_c, self.word = True, 0, c, 0
        return self.dout << UI_DOUT, 0


# ----- host words -----

CODES = [1, 2, 3, 3, 1, 4, 2, 1]  # pulse codes (4: power down) of the host
WANT_SEL = ["A128", "A128", "B32", "A64", "A64", "A128", "B32"]  # per read: the selection of the conversion read


def words_of(sel):
    v = VALUE[sel]
    hi = (v >> 16) & 0xFF
    return [hi | 0xFF00 if hi & 0x80 else hi, v & 0xFFFF]


WANT = [w for s in WANT_SEL for w in words_of(s)]


def assemble(**equ):
    return ff.assemble("hx711.s", **equ)


def model_run(dev, codes=CODES, steps=None, limit=40_000_000, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_SCK: 0}))
    bench = ff.Bench(m, inputs=dev)
    bench.watch("sck", lambda uo, uio, oe: (uo >> 4) & 1)
    nwords = 2 * sum(1 for x in codes if x != 4)
    for words, wait in steps or [(codes, 0)]:
        bench.send(0, *words)
        bench.run(wait)
    while m.cycle < limit and len(bench.words(0)) < nwords:
        bench.run(20_000)
    bench.run(2_000)
    return bench, m


def check(dev, words, label, want=WANT, want_sel=WANT_SEL):
    assert not dev.errors, f"{label}: {dev.errors[:3]}"
    assert words == want, f"{label}: host words {[hex(w) for w in words]}, expected {[hex(w) for w in want]}"
    assert [r[3] for r in dev.reads] == want_sel and [r[2] for r in dev.reads] == [VALUE[s] for s in want_sel], (
        f"{label}: reads {[(r[1], hex(r[2]), r[3]) for r in dev.reads]}")


def summary(bench, dev):
    sck = bench.changes("sck")
    highs = [b[0] - a[0] for a, b in zip(sck, sck[1:]) if a[1] == 1 and b[0] - a[0] < 3000]
    lows = [b[0] - a[0] for a, b in zip(sck, sck[1:]) if a[1] == 0 and b[0] - a[0] < 300]
    return (f"{len(dev.reads)} reads, pulses {[r[1] for r in dev.reads]}, selections {[r[3] for r in dev.reads]}; "
            f"PD_SCK high {min(highs) / F_MHZ:.3f} to {max(highs) / F_MHZ:.3f} us, low inside a read "
            f"{min(lows) / F_MHZ:.3f} to {max(lows) / F_MHZ:.3f} us")


@cocotb.test(skip=GL)
async def test_hx711_words(dut):
    """Memory words of fw/hx711.s."""
    size = assemble().size
    dut._log.info(f"fw/hx711.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_hx711_model(dut):
    """fw/hx711.s in the reference model against the HX711 model at 80 samples/s: the three pulse counts, a power-down
    and reset, a late host; negative controls for T3, T4 and the read time."""
    log = dut._log
    period = 750_000  # 12.5 ms
    dev = Hx711(period)
    bench, m = model_run(dev)
    check(dev, bench.words(0), "model")
    ff.assert_status_clean(m, [0])
    pds = [c for c, v in bench.changes("sck")]
    log.info(f"model (80 SPS): {summary(bench, dev)}; host words {[hex(w) for w in bench.words(0)]}; first read at "
             f"{dev.reads[0][0] / F_MHZ / 1000:.3f} ms (settling 50 ms); {m.cycle} cycles, {len(pds)} PD_SCK changes")
    # late host: the second word comes 2.5 periods after the first read; the result waits in the HX711
    dev = Hx711(period)
    bench, m = model_run(dev, codes=[1, 2], steps=[([1], 3_200_000), ([2], 2 * period + period // 2)])
    check(dev, bench.words(0), "model late host", want=words_of("A128") * 2, want_sel=["A128", "A128"])
    ff.assert_status_clean(m, [0])
    log.info(f"model late host: {summary(bench, dev)}; second read {(dev.reads[1][0] - dev.reads[0][0]) / F_MHZ / 1000:.3f}"
             f" ms after the first")
    # Negative controls (conversion period shortened to 0.2 ms): the model must catch T3 and T4, and the data check
    # must catch a read before T2.
    for equ, want in ((dict(TH=3200), "T3 0.2 to 50 us"), (dict(TL=10), "T4 0.2 us")):
        dev = Hx711(12_000)
        model_run(dev, codes=[1], limit=200_000, **equ)
        log.info(f"control {equ}: {dev.errors[0] if dev.errors else 'no fault found'}")
        assert dev.errors and want in dev.errors[0], f"control {equ}: expected '{want}', got {dev.errors[:2]}"
    dev = Hx711(12_000)
    bench, m = model_run(dev, codes=[1, 1], limit=200_000, TS=4)
    log.info(f"control {{'TS': 4}} (DOUT read 67 ns after the rise, before T2): host words "
             f"{[hex(w) for w in bench.words(0)]}")
    assert bench.words(0) != words_of("A128") * 2, "control TS 4: the early read was not noticed"


@cocotb.test()
async def test_hx711_rtl(dut):
    """fw/hx711.s on the chip at 60 MHz against the HX711 model (conversion period 0.2 ms): the three pulse counts, a
    power-down and reset, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl hx711"
    period = 12_000
    dev = Hx711(period)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_SCK: 0}), inputs=dev, label=label)
    bench.watch("sck", lambda uo, uio, oe: (uo >> 4) & 1)
    bench.send(0, *CODES)
    if hasattr(dut, "dump_off"):
        await bench.run(SETTLE * period - 500 - bench.m.cycle)
        dut.dump_off.value = 0
        await bench.run(4_000)
        dut.dump_off.value = 1
        rises = [c for c, v in bench.changes("sck") if v == 1 and c >= SETTLE * period - 500]
        log.info(f"{label}: waveform window model cycles {SETTLE * period - 500} to {bench.m.cycle}, simulation time "
                 f"about {get_sim_time(unit='ps') - 4_000 * rf.CLK_PS} ps at its start; DOUT fall at model cycle "
                 f"{dev.reads[0][0] if dev.reads else dev.ready_c}; PD_SCK rises {rises[:3]} ... ({len(rises)} in it)")
    for _ in range(100):
        if len(bench.words(0)) >= len(WANT):
            break
        await bench.run(10_000)
    await bench.run(2_000)
    await bench.finish(log)
    check(dev, bench.words(0), label)
    log.info(f"{label}: {summary(bench, dev)}; host words {[hex(w) for w in bench.words(0)]}")

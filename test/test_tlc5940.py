# SPDX-License-Identifier: Apache-2.0
"""TLC5940 LED driver controller firmware (fw/tlc5940.s) against an independent TLC5940 model, in the reference model
and on the RTL at 60 MHz.

What it does:
  The chip model (Tlc5940) is written from the TI TLC5940 data sheet SLVS515D (December 2004, revised November 2015):
  timing requirements 7.3 (pulse durations, setup and hold times, clock frequencies), serial interface 8.3.1 and
  Figure 11, output enable 8.3.6, operating modes 8.4.1, dot correction 8.4.2, grayscale 8.4.3, grayscale PWM 8.4.5.
  It does not use the firmware, the assembler or the processor model. SCLK rises shift SIN into the input shift
  register, MSB first; an XLAT rise latches the last 96 bits into the dot correction register (VPRG high) or the last
  192 bits into the grayscale register (VPRG low); exactly 96 or 192 bits must have been shifted since the latch
  before. A grayscale latch must come while BLANK is high (8.4.3). The first grayscale latch after a dot correction
  latch takes effect only with an additional SCLK pulse after XLAT (8.4.3); a PWM cycle that starts before it is a
  fault. A PWM cycle starts at the BLANK fall; each GSCLK rise counts; an output with grayscale value GSn is on for
  GSn GSCLK periods (the reading of 8.4.5 that the output on-time error of 7.5, GSn = 01h against one GSCLK period,
  confirms), the counter stops at FFFh. Every setup, hold and pulse duration of 7.3 is checked on the pins:
  tsu0 to tsu5, th0 to th4, twh0/twl0, twh1/twl1, twh2, twh3, and SCLK and GSCLK at most 30 MHz; SIN, SCLK and VPRG
  must not change while XLAT is high.
  The model test sends a dot correction frame and two grayscale frames (values from 0 to 4095) and checks the latch
  order, the registers, 4,096 GSCLK pulses a cycle and the grayscale values of each PWM cycle; negative controls move
  the SIN change onto the SCLK rise (tsu0) and halve the cycle (2,048 pulses). The RTL test runs the same frames with
  256-pulse cycles (values below 256), in lockstep with the reference model; the same test runs on the gate-level
  netlist (gl_fw workflow).

Why:
  Thirteenth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode decoder
  at the pinned commit decodes this interface, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins at logic level; the LED currents, the 22 V EEPROM programming
level and the status read-back on SOUT are not simulated.
"""

import math
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
PINS = {"SIN": 8, "SCLK": 9, "XLAT": 10, "BLANK": 11, "GSCLK": 12, "VPRG": 13}  # uo_out[0..5]

# 7.3 timing requirements (ns) and clock limits
TW = dict(sclk=16, gsclk=16, xlat=20, blank=20)
TSU = dict(tsu0=5, tsu1=10, tsu2=10, tsu3=10, tsu4=10, tsu5=30)
TH = dict(th0=3, th1=10, th2=10, th3=10, th4=10)
F_MAX_MHZ = 30.0
FFF = 4095


class Tlc5940:
    """TLC5940 on the pins (see the module text). Records latches [(kind, values)] and PWM cycles [(gs values or None,
    GSCLK pulses)]."""

    def __init__(self, us=F_MHZ):
        self.c = lambda ns: int(math.ceil(ns * us / 1000.0 - 1e-9))
        self.us = us
        self.lv = {k: 1 for k in PINS}  # every uo pin is 1 from reset
        self.t = {}
        self.sr = []
        self.dc = self.gs = None
        self.extra = False  # the next grayscale latch needs the additional SCLK pulse
        self.pending = None  # grayscale values waiting for that pulse
        self.cycle_gs, self.pulses, self.counter, self.in_cycle = None, 0, 0, False
        self.latches, self.cycles, self.errors = [], [], []
        self.sclk_rises = []

    def _err(self, c, text):
        self.errors.append(f"cycle {c}: {text}")

    def _since(self, key, c):
        return c - self.t[key] if key in self.t else 10**9

    def _check(self, c, key, limit_ns, what):
        if self._since(key, c) < self.c(limit_ns):
            self._err(c, f"{what}: {self._since(key, c)} cycles ({limit_ns} ns)")

    def _values(self, bits, width):
        return [int("".join(map(str, bits[i * width:(i + 1) * width])), 2) for i in range(16)]  # OUT15 first

    def __call__(self, c, uo, uio, oe):
        new = {k: (uo >> (p - 8)) & 1 for k, p in PINS.items()}
        ch = [k for k in PINS if new[k] != self.lv[k]]
        lv = self.lv
        for k in ch:
            v = new[k]
            if k in ("SIN", "SCLK", "VPRG") and lv["XLAT"]:
                self._err(c, f"{k} changed while XLAT is high")
            if k == "SIN":
                if lv["SCLK"]:
                    self._check(c, "SCLK_r", TH["th0"], "th0 SCLK rise to SIN")
                self.t["SIN"] = c
            elif k == "VPRG":
                self._check(c, "SCLK_r", TH["th2"], "th2 SCLK rise to VPRG")
                self._check(c, "XLAT_f", TH["th3"], "th3 XLAT fall to VPRG")
                self.t["VPRG"] = c
            elif k == "SCLK":
                if v:
                    self._check(c, "SCLK_f", TW["sclk"], "twl0 SCLK low")
                    self._check(c, "SIN", TSU["tsu0"], "tSU0 SIN to SCLK rise")
                    self._check(c, "VPRG", TSU["tsu2"], "tsu2 VPRG to SCLK rise")
                    self._check(c, "XLAT_f", TH["th1"], "th1 XLAT fall to SCLK rise")
                    if "SCLK_r" in self.t and self.us / (c - self.t["SCLK_r"]) > F_MAX_MHZ:
                        self._err(c, "SCLK above 30 MHz")
                    if self.pending is not None:
                        self.gs, self.pending = self.pending, None  # the additional pulse completes the update
                    else:
                        self.sr.append(new["SIN"])
                        self.sclk_rises.append(c)
                    self.t["SCLK_r"] = c
                else:
                    self._check(c, "SCLK_r", TW["sclk"], "twh0 SCLK high")
                    self.t["SCLK_f"] = c
            elif k == "XLAT":
                if v:
                    if new["SCLK"]:
                        self._err(c, "XLAT rise while SCLK is high")
                    self._check(c, "SCLK_f", TSU["tsu1"], "tsu1 SCLK fall to XLAT rise")
                    self._check(c, "VPRG", TSU["tsu3"], "tsu3 VPRG to XLAT rise")
                    if "XLAT_r" not in self.t and "XLAT_f" not in self.t:
                        pass
                    width = 6 if new["VPRG"] else 12
                    if len(self.sr) != 16 * width:
                        self._err(c, f"XLAT after {len(self.sr)} bits ({16 * width} for {'dot correction' if width == 6 else 'grayscale'})")
                    bits = (self.sr + [0] * 192)[-16 * width:] if len(self.sr) >= 16 * width else None
                    if bits is not None:
                        vals = self._values(bits, width)
                        if width == 6:
                            self.dc, self.extra = vals, True
                            self.latches.append(("DC", vals))
                        else:
                            if not new["BLANK"]:
                                self._err(c, "grayscale latched while BLANK is low (8.4.3: at the end of a PWM cycle)")
                            self.latches.append(("GS", vals))
                            if self.extra:
                                self.pending, self.extra = vals, False
                            else:
                                self.gs = vals
                    self.sr = []
                    self.t["XLAT_r"] = c
                else:
                    self._check(c, "XLAT_r", TW["xlat"], "twh2 XLAT high")
                    self.t["XLAT_f"] = c
            elif k == "BLANK":
                if v:
                    self._check(c, "GSCLK_r", TH["th4"], "th4 GSCLK rise to BLANK rise")
                    if self.in_cycle:
                        self.cycles.append((self.cycle_gs, self.pulses))
                    self.in_cycle, self.counter = False, 0
                    self.t["BLANK_r"] = c
                else:
                    self._check(c, "BLANK_r", TW["blank"], "twh3 BLANK high")
                    if self.pending is not None:
                        self._err(c, "PWM cycle starts before the additional SCLK pulse after the first grayscale "
                                     "latch after dot correction (8.4.3)")
                    self.in_cycle, self.counter, self.pulses = True, 0, 0
                    self.cycle_gs = tuple(self.gs) if self.gs is not None else None
                    self.t["BLANK_f"] = c
            elif k == "GSCLK":
                if v:
                    self._check(c, "GSCLK_f", TW["gsclk"], "twl1 GSCLK low")
                    if "GSCLK_r" in self.t and self.us / (c - self.t["GSCLK_r"]) > F_MAX_MHZ:
                        self._err(c, "GSCLK above 30 MHz")
                    if not new["BLANK"]:
                        self._check(c, "BLANK_f", TSU["tsu4"], "tsu4 BLANK fall to GSCLK rise")
                        self._check(c, "XLAT_r", TSU["tsu5"], "tsu5 XLAT rise to GSCLK rise")
                        if self.in_cycle:
                            self.counter = min(self.counter + 1, FFF)
                            self.pulses += 1
                    self.t["GSCLK_r"] = c
                else:
                    self._check(c, "GSCLK_r", TW["gsclk"], "twh1 GSCLK high")
                    self.t["GSCLK_f"] = c
        self.lv = new
        return 0, 0

    def on_periods(self, cycle):
        """GSCLK periods each output OUT15..OUT0 is on in a recorded cycle (8.4.5, counter stops at FFFh)."""
        gs, pulses = cycle
        return [min(v, pulses, FFF) for v in gs]


def frame(kind, values):
    """Host words of one frame: header (0 grayscale, 1 dot correction) and 16 left-aligned values, OUT15 first."""
    return [kind] + [(v << (4 if kind == 0 else 10)) & 0xFFFF for v in values]


DC = [42, 21, 63, 0, 1, 2, 4, 8, 16, 32, 62, 61, 59, 55, 47, 31]
GS_A = [4095, 0, 1, 2048, 1000, 3000, 10, 4094, 7, 77, 777, 2222, 3333, 444, 55, 6]
GS_B = [5, 4000, 2, 3, 1234, 0, 4095, 100, 200, 300, 400, 500, 600, 700, 800, 900]


def assemble(**equ):
    return ff.assemble("tlc5940.s", **equ)


def owner():
    return ff.owners({p: 0 for p in PINS.values()})


def model_run(dev, words, cycles, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=owner())
    bench = ff.Bench(m, inputs=dev)
    bench.send(0, *words)
    bench.run(cycles)
    return bench, m


def check(dev, label, gs_frames, pulses):
    assert not dev.errors, f"{label}: {dev.errors[:3]}"
    kinds = [(k, v) for k, v in dev.latches]
    assert kinds[:1 + len(gs_frames)] == [("DC", DC)] + [("GS", g) for g in gs_frames], f"{label}: latches {kinds}"
    assert dev.dc == DC, f"{label}: dot correction register {dev.dc}"
    got = [c for c in dev.cycles if c[0] is not None]
    assert len(got) >= len(gs_frames), f"{label}: {len(got)} PWM cycles with grayscale data"
    for i, g in enumerate(gs_frames):
        assert got[i][0] == tuple(g) and got[i][1] == pulses, f"{label}: cycle {i}: {got[i][1]} pulses, {got[i][0]}"
        assert dev.on_periods(got[i]) == [min(v, pulses, FFF) for v in g]
    assert all(c[0] is None for c in dev.cycles[:len(dev.cycles) - len(got)]), f"{label}: a lit cycle before the latch"
    return got


@cocotb.test(skip=GL)
async def test_tlc5940_words(dut):
    """Memory words of fw/tlc5940.s."""
    size = assemble().size
    dut._log.info(f"fw/tlc5940.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_tlc5940_model(dut):
    """fw/tlc5940.s in the reference model against the TLC5940 model: a dot correction frame and two grayscale frames
    at 4,096 GSCLK pulses a cycle; negative controls for tsu0 and the cycle length."""
    log = dut._log
    words = frame(1, DC) + frame(0, GS_A) + frame(0, GS_B)
    dev = Tlc5940()
    bench, m = model_run(dev, words, 2_100_000)
    got = check(dev, "model", [GS_A, GS_B], 4096)
    ff.assert_status_clean(m, [0])
    rises = dev.sclk_rises
    log.info(f"model: latches {[k for k, _ in dev.latches]}; dot correction register {dev.dc}; {len(dev.cycles)} "
             f"PWM cycles, {len(got)} lit, {got[0][1]} GSCLK pulses each; on-times (GSCLK periods) cycle 1 "
             f"{dev.on_periods(got[0])}, cycle 2 {dev.on_periods(got[1])}; SCLK period "
             f"{min(b - a for a, b in zip(rises, rises[1:]))} cycles")
    for equ, want in ((dict(SL=40), "tSU0"), (dict(NGSH=256), "pulses")):
        dev = Tlc5940()
        model_run(dev, words, 1_400_000, **equ)
        if want == "pulses":
            got = [c for c in dev.cycles if c[0] is not None]
            found = [f"{got[0][1]} GSCLK pulses a cycle (8.4.5: 4096)"] if got and got[0][1] != 4096 else []
        else:
            found = [e for e in dev.errors if want in e]
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: {dev.errors[:2]}"


GS_A8 = [v & 0xFF for v in GS_A]
GS_B8 = [v & 0xFF for v in GS_B]


@cocotb.test()
async def test_tlc5940_rtl(dut):
    """fw/tlc5940.s on the chip at 60 MHz against the TLC5940 model with 256 GSCLK pulses a cycle (NGSH 32): the dot
    correction frame and two grayscale frames (values below 256), in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl tlc5940"
    dev = Tlc5940()
    bench = rf.RtlBench(dut, assemble(NGSH=32).words, owner=owner(), inputs=dev, label=label)
    bench.send(0, *(frame(1, DC) + frame(0, GS_A8) + frame(0, GS_B8)))
    if hasattr(dut, "dump_off"):  # waveform of the start of the dot correction frame (tb.fst), for review
        await bench.run(19_000)
        dut.dump_off.value = 0
        await bench.run(12_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform window model cycles 19000 to {bench.m.cycle}; first SCLK rises at simulation "
                 f"times (ps) {[round(now - (bench.m.cycle - c) * rf.CLK_PS) for c in dev.sclk_rises[:12]]}")
    for _ in range(40):
        if len([c for c in dev.cycles if c[0] is not None]) >= 2:
            break
        await bench.run(10_000)
    await bench.finish(log)
    got = check(dev, label, [GS_A8, GS_B8], 256)
    log.info(f"{label}: latches {[k for k, _ in dev.latches]}; {len(dev.cycles)} PWM cycles, {len(got)} lit, "
             f"{got[0][1]} GSCLK pulses each; cycle 1 {list(got[0][0])}; cycle 2 {list(got[1][0])}")

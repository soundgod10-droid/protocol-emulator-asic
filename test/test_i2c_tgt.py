# SPDX-License-Identifier: Apache-2.0
"""I2C target firmware (fw/i2c_tgt.s) against an independent I2C controller model, in the reference model and on
the RTL at 60 MHz.

What it does:
  A controller model makes the transfers of a 24C02-type register map on the bus: a write of 5 bytes, a random
  read (write of the pointer, repeated START, read with ACK and a final NACK), a current-address read, a transfer
  to another address (NACK expected), and a write that wraps at the end of the map and ends with a repeated START.
  Standard-mode and Fast-mode, each at two corners of Table 11 that meet the highest SCL clock frequency (shortest
  LOW period, shortest HIGH period), with the controller's SDA change at the start or at the end of each LOW period.
  The model test sweeps the start phase of the controller against the thread over 20 cycles (the period in which the
  firmware reads the pins); the RTL test runs the firmware on the chip, in lockstep with the reference model, and the
  same test runs on the gate-level netlist (gl_fw workflow).

  The controller model is written from NXP UM10204 Rev. 7.0 (I2C-bus specification and user manual, 1 October
  2021): START and STOP 3.1.4, byte format 3.1.5, ACK and NACK 3.1.6, clock synchronization 3.1.7, clock stretching
  3.1.9, target address and R/W bit 3.1.10, timing Table 11 and its notes (PDF page 44). It does not use the
  firmware, the assembler or the processor model. It checks the target on the bus:
    - the target changes SDA only while SCL is low, and not earlier than tf(max) = 300 ns after the SCL fall
      (Table 11 note [2]: SCL must be low before SDA moves);
    - a bit that the target drives (data and ACK) is valid within tVD;DAT (tVD;ACK) after the SCL fall when the
      target does not stretch the clock, and by tr(max) + tSU;DAT before the SCL rise when it stretches (notes [3]
      and [4]);
    - the target never pulls SCL low during a HIGH period (it may only extend a LOW period, 3.1.9);
    - SDA does not change while SCL is high, except for the controller's START and STOP.

Why:
  The firmware of the I2C controller (fw/i2c_ctl.s) shows one role of the protocol. A target needs the other side of
  the timing: it must follow a clock that it does not make, find START and STOP at any time, and answer within
  the data valid time. It shows whether the instruction set can do this at Fast-mode rates, with the clock
  stretching that UM10204 allows for a target, and with no change of the chip.
"""

import math
import os
import random
import sys

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
P_SCL, P_SDA = 6, 7  # uio[6], uio[7] (docs/isa.md 5.1)

# UM10204 Rev. 7.0, Table 11 (PDF page 44), in us; tr is the rise time maximum, vd the data valid time maximum
# (tVD;DAT and tVD;ACK are equal).
LIMITS = {
    "Sm": dict(f_khz=100, hd_sta=4.0, low=4.7, high=4.0, su_sta=4.7, su_dat=0.25, su_sto=4.0, buf=4.7, vd=3.45,
               tr=1.0),
    "Fm": dict(f_khz=400, hd_sta=0.6, low=1.3, high=0.6, su_sta=0.6, su_dat=0.1, su_sto=0.6, buf=1.3, vd=0.9,
               tr=0.3),
}
TF_MAX = 0.3  # fall time maximum of SDA and SCL in both modes (Table 11)


class I2cController:
    """I2C controller from UM10204 Rev. 7.0 (not from the firmware), with checks of the target's timing.

    us: clock cycles per microsecond. mode: "Sm" or "Fm". t_low, t_high: the controller's SCL LOW and HIGH periods in
    us (each at least the Table 11 minimum; 1 / (t_low + t_high) at most fSCL). hd: cycles from the controller's SCL
    fall to its SDA change (tHD;DAT, minimum 0); the change is at least tSU;DAT before the SCL release. start: cycle
    of the first operation. script: operations ("start",), ("w", byte), ("r", ack), ("stop",); the result of each
    "w" (the ACK bit, 0 = ACK) and "r" (the byte) is in results. After its SCL release the controller waits until SCL
    is high on the bus (clock synchronization, 3.1.7 and 3.1.9): a target may extend the LOW period.
    """

    def __init__(self, us, mode, t_low, t_high, hd=0, start=0, script=()):
        lim = LIMITS[mode]
        assert t_low >= lim["low"] and t_high >= lim["high"], (t_low, t_high)
        assert 1e3 / (t_low + t_high) <= lim["f_khz"] + 1e-9, (t_low, t_high)
        self.mode, self.lim = mode, lim
        self.cyc = lambda u: int(math.ceil(u * us - 1e-9))
        self.t_low, self.t_high = self.cyc(t_low), self.cyc(t_high)
        self.hd = hd
        assert self.t_low - hd >= self.cyc(lim["su_dat"]), "tSU;DAT"
        self.scl_o = self.sda_o = 1  # controller outputs (1 = released)
        self.scl = self.sda = 1  # bus levels
        self.tgt_scl = self.tgt_sda = 1  # target outputs (1 = released)
        self.tgt_sda_t = 0  # cycle of the last change of the target's SDA output
        self.t_fall = self.t_rise = None
        self.t_stop = -10**9
        self.c = 0
        self.results, self.errors, self.stretch = [], [], []
        self.changes = [(0, (1, 1))]
        self.done = False
        self.gen = self._run(list(script), start)

    # ----- the bus, each cycle -----

    def __call__(self, c, uo, uio, oe):
        tgt_scl = 0 if (oe >> P_SCL) & 1 and not (uio >> P_SCL) & 1 else 1
        tgt_sda = 0 if (oe >> P_SDA) & 1 and not (uio >> P_SDA) & 1 else 1
        if tgt_scl != self.tgt_scl:
            if tgt_scl == 0 and self.scl == 1:
                self.errors.append(f"cycle {c}: the target pulled SCL low in a HIGH period")
            self.tgt_scl = tgt_scl
        if tgt_sda != self.tgt_sda:
            if self.scl == 1:
                self.errors.append(f"cycle {c}: the target changed SDA while SCL was high")
            elif self.t_fall is not None and c - self.t_fall < self.cyc(TF_MAX):
                self.errors.append(f"cycle {c}: the target changed SDA {c - self.t_fall} cycles after the SCL fall "
                                   f"(tf max {self.cyc(TF_MAX)})")
            self.tgt_sda, self.tgt_sda_t = tgt_sda, c
        scl, sda = self.scl_o & tgt_scl, self.sda_o & tgt_sda
        if scl != self.scl:
            if scl:
                self.t_rise = c
            else:
                self.t_fall = c
        self.scl, self.sda = scl, sda
        if (scl, sda) != self.changes[-1][1]:
            self.changes.append((c, (scl, sda)))
        self.c = c
        if not self.done:
            try:
                next(self.gen)
            except StopIteration:
                self.done = True
        return 0, (scl << P_SCL) | (sda << P_SDA)

    # ----- the controller -----

    def _wait(self, n):
        for _ in range(n):
            yield

    def _release_scl(self):
        """Releases SCL and waits until it is high on the bus; returns True if the target extended the LOW period."""
        self.scl_o, rel = 1, self.c
        while True:
            yield
            if self.scl:
                break
        if self.t_rise > rel + 1:
            self.stretch.append(self.t_rise - rel - 1)
            return True
        return False

    def _clock(self, sda_out, target_drives):
        """One clock pulse after the SCL fall: SDA = sda_out (1 = released) hd cycles after the fall, SCL released
        t_low after the fall, held high t_high from the rise. Returns the SDA level of the HIGH period."""
        yield from self._wait(self.hd)
        self.sda_o = sda_out
        yield from self._wait(self.t_low - self.hd)
        fall = self.t_fall
        stretched = yield from self._release_scl()
        if target_drives:
            valid = max(self.tgt_sda_t, fall)
            if not stretched and valid - fall > self.cyc(self.lim["vd"]):
                self.errors.append(f"cycle {self.c}: target data valid {valid - fall} cycles after the SCL fall "
                                   f"(tVD max {self.cyc(self.lim['vd'])})")
            need = self.cyc(self.lim["tr"] + self.lim["su_dat"])
            if stretched and self.t_rise - valid < need:
                self.errors.append(f"cycle {self.c}: target data valid {self.t_rise - valid} cycles before the "
                                   f"stretched SCL rise (tr max + tSU;DAT {need})")
        levels = set()
        for _ in range(self.t_high):
            levels.add(self.sda)
            yield
        if len(levels) > 1:
            self.errors.append(f"cycle {self.c}: SDA changed while SCL was high in a data or ACK bit")
        value = self.sda
        self.scl_o = 0
        yield
        return value

    def _start(self):
        if self.scl_o == 0:  # repeated START (Figure 38: tSU;STA, tHD;STA)
            yield from self._wait(self.hd)
            self.sda_o = 1
            yield from self._wait(self.t_low - self.hd)
            yield from self._release_scl()
            yield from self._wait(self.cyc(self.lim["su_sta"]))
            if not self.sda:
                self.errors.append(f"cycle {self.c}: SDA not high before the repeated START")
        else:
            while self.c - self.t_stop < self.cyc(self.lim["buf"]):
                yield
        self.sda_o = 0
        yield from self._wait(self.cyc(self.lim["hd_sta"]))
        self.scl_o = 0
        yield

    def _stop(self):
        yield from self._wait(self.hd)
        self.sda_o = 0
        yield from self._wait(self.t_low - self.hd)
        yield from self._release_scl()
        yield from self._wait(self.cyc(self.lim["su_sto"]))
        self.sda_o = 1
        yield
        self.t_stop = self.c

    def _write(self, byte):
        for i in range(8):
            bit = (byte >> (7 - i)) & 1
            if (yield from self._clock(bit, False)) != bit:
                self.errors.append(f"cycle {self.c}: SDA differs from the controller's bit {bit} of 0x{byte:02X}")
        return (yield from self._clock(1, True))

    def _read(self, ack):
        byte = 0
        for _ in range(8):
            byte = (byte << 1) | (yield from self._clock(1, True))
        yield from self._clock(0 if ack else 1, False)
        return byte

    def _run(self, script, start):
        yield from self._wait(start)
        for op in script:
            if op[0] == "start":
                yield from self._start()
            elif op[0] == "stop":
                yield from self._stop()
            elif op[0] == "w":
                self.results.append(("w", op[1], (yield from self._write(op[1]))))
            else:
                self.results.append(("r", (yield from self._read(op[1]))))


# ----- transfers and expected results -----

ADDR = 0x50
DATA = [0x3C, 0xC3, 0x00, 0xFF, 0x5A]
S, P = ("start",), ("stop",)


def w(b):
    return ("w", b)


def r(ack):
    return ("r", ack)


SCRIPT = ([S, w(ADDR << 1), w(0x10)] + [w(b) for b in DATA] + [P]                              # write at 0x10
          + [S, w(ADDR << 1), w(0x10), S, w(ADDR << 1 | 1)] + [r(True)] * 4 + [r(False), P]   # random read
          + [S, w(ADDR << 1 | 1), r(False), P]                                                 # current address
          + [S, w(0x52 << 1), P]                                                               # other address
          + [S, w(ADDR << 1), w(0xFE), w(0x11), w(0x22), w(0x33),                              # write with wrap,
             S, w(ADDR << 1), w(0xFE), S, w(ADDR << 1 | 1), r(True), r(True), r(False), P])     # Sr, read back


def expected():
    """Results of SCRIPT for a 24C02-type map: ACK for every byte except the other address; reads give the written
    bytes, map[0x15] = 0 (cleared), and the 3 bytes written at 0xFE, 0xFF and 0x00."""
    reads = iter(DATA + [0x00, 0x11, 0x22, 0x33])
    res = []
    for op in SCRIPT:
        if op[0] == "w":
            res.append(("w", op[1], 1 if op[1] >> 1 == 0x52 else 0))
        elif op[0] == "r":
            res.append(("r", next(reads)))
    return res


# Corners (us): the shortest LOW period and the shortest HIGH period of Table 11 at the highest fSCL.
CORNERS = {
    "Fm": [(1.3, 1.2), (1.9, 0.6)],
    "Sm": [(4.7, 5.3), (6.0, 4.0)],
}
START = 6000  # cycles from the thread start to the first START: the firmware clears 258 words first


def assemble(**equ):
    with open(os.path.join(ROOT, "fw", "i2c_tgt.s"), encoding="utf-8") as f:
        text = f.read()
    for key, val in equ.items():
        text = text.replace(f".equ    {key}, ", f".equ    {key}, {val} ; was ", 1)
    return pe_asm.assemble(text, "i2c_tgt.s", 0)


def stretch_text(ctl):
    if not ctl.stretch:
        return "the target never extended a LOW period"
    return f"the target extended {len(ctl.stretch)} LOW periods by {min(ctl.stretch)} to {max(ctl.stretch)} cycles"


def check(ctl, label):
    assert ctl.done, f"{label}: the controller did not finish ({len(ctl.results)} results)"
    assert not ctl.errors, f"{label}: {ctl.errors[:3]}"
    assert ctl.results == expected(), f"{label}: results {ctl.results}"


def model_run(mode, t_low, t_high, hd, phase, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners({P_SCL: 0, P_SDA: 0}), open_drain=ff.open_drain({P_SCL, P_SDA}))
    ctl = I2cController(F_MHZ, mode, t_low, t_high, hd=hd, start=START + phase, script=SCRIPT)
    bench = ff.Bench(m, inputs=ctl)
    for _ in range(400):
        if ctl.done:
            break
        bench.run(5000)
    bench.run(1000)
    return m, ctl


@cocotb.test(skip=GL)
async def test_i2c_tgt_words(dut):
    """Memory words of fw/i2c_tgt.s and fw/i2c_ctl.s (controller and target of one bus fit together)."""
    size = assemble().size
    ctl = pe_asm.assemble_file(os.path.join(ROOT, "fw", "i2c_ctl.s")).size
    dut._log.info(f"fw/i2c_tgt.s: {size} words; with fw/i2c_ctl.s {size + ctl} of {pe_model.PROG_WORDS}")
    assert size + ctl <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_i2c_tgt_model(dut):
    """fw/i2c_tgt.s in the reference model against the controller model: Standard-mode and Fast-mode at both corners,
    controller SDA change at the start (1 cycle) and at the end (tSU;DAT before the release) of the LOW period, and
    for the Fast-mode corner with the shortest LOW period 20 start phases."""
    log = dut._log
    rng = random.Random(5)
    for mode, corners in CORNERS.items():
        for t_low, t_high in corners:
            lo = int(math.ceil(t_low * F_MHZ - 1e-9))
            for hd in (1, lo - int(math.ceil(LIMITS[mode]["su_dat"] * F_MHZ - 1e-9))):
                phases = range(20) if (mode, t_low, hd) == ("Fm", 1.3, 1) else [rng.randrange(20)]
                for phase in phases:
                    label = f"model {mode} tLOW {t_low} us tHIGH {t_high} us, SDA change {hd} cycles, phase {phase}"
                    m, ctl = model_run(mode, t_low, t_high, hd, phase)
                    check(ctl, label)
                    ff.assert_status_clean(m, [0])
                log.info(f"{label} (and {len(phases) - 1} more phases): {len(ctl.results)} results correct, no timing "
                         f"fault; {stretch_text(ctl)}")
    # Negative controls: the checks of the controller model must catch a target that breaks them.
    for equ, want in ((dict(HOLD=4), "after the SCL fall"), (dict(TSU=8), "before the stretched SCL rise"),
                      (dict(ADDR=0x51), "results")):
        _, ctl = model_run("Fm", 1.3, 1.2, 1, 0, **equ)
        found = ctl.errors[:1] if ctl.errors else ([] if ctl.results == expected() else ["results"])
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found and want in found[0], f"control {equ}: expected a fault with '{want}', got {found}"


@cocotb.test()
async def test_i2c_tgt_rtl(dut):
    """fw/i2c_tgt.s on the chip at 60 MHz against the controller model: Fast-mode at both corners (SDA change 1 cycle
    after the SCL fall) and Standard-mode at the shortest LOW period, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    prog = assemble()
    for mode, t_low, t_high in (("Fm", 1.3, 1.2), ("Fm", 1.9, 0.6), ("Sm", 4.7, 5.3)):
        ctl = I2cController(F_MHZ, mode, t_low, t_high, hd=1, start=rf.HOST_START + START, script=SCRIPT)
        label = f"rtl i2c_tgt {mode} tLOW {t_low} us tHIGH {t_high} us"
        bench = rf.RtlBench(dut, prog.words, owner=ff.owners({P_SCL: 0, P_SDA: 0}),
                            od=ff.open_drain({P_SCL, P_SDA}), inputs=ctl, label=label)
        if mode == "Fm" and t_low == 1.3:  # waveform of the first write transfer (tb.fst), for review
            await bench.run(rf.HOST_START + START - 500)
            dut.dump_off.value = 0
            await bench.run(30_000)
            dut.dump_off.value = 1
        for _ in range(400):
            if ctl.done:
                break
            await bench.run(5000)
        await bench.run(1000)
        check(ctl, label)
        await bench.finish(log)
        log.info(f"{label}: {len(ctl.results)} results correct, no timing fault; {stretch_text(ctl)}; "
                 f"{len(ctl.changes)} bus changes")

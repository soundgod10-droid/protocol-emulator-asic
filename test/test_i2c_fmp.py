# SPDX-License-Identifier: Apache-2.0
"""I2C controller with Fast-mode Plus timing (fw/i2c_ctl_fmp.s) in the reference model and on the RTL at 60 MHz,
checked against NXP UM10204 Rev. 7.0, Table 11, Fast-mode Plus column.

What it does:
  The bus and target model is test_fw_full.I2cBus (written from UM10204 3.1, not from the firmware): a wired-AND bus
  with pull-ups and one target that acknowledges its address and each written byte, sends read data 100 ns (6 cycles)
  after each SCL fall, and can hold SCL low after each acknowledged byte (byte-level clock stretching, UM10204 3.1.9).
  The script is that of the Fast-mode test (test_fw_full.i2c_script): write with ACK, repeated START, two reads (ACK,
  then NACK and STOP), an address without ACK and STOP. Every bus time is measured from the bus levels and compared
  with Table 11 (PDF page 44): fSCL 1000 kHz maximum, tLOW 0.5 us, tHIGH 0.26 us, tHD;STA, tSU;STA, tSU;STO 0.26 us,
  tBUF 0.5 us, tSU;DAT 50 ns, tHD;DAT 0 minimum, tVD;DAT and tVD;ACK 0.45 us maximum. The model test runs without and
  with 300 cycles of stretching after each byte; the data bits must come at exactly 60 cycles (1.000 MHz).
  Negative controls: tLOW of 29 cycles (0.483 us), a bit time of 57 cycles (1,053 kHz), and a target that holds SCL
  low inside a byte (bit-level stretching, which the firmware does not see, as its header states): the HIGH period
  of that bit falls below 0.26 us and the check must find it. The RTL test runs the firmware on the chip in lockstep
  with the reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Conditions: logic simulation at 60 MHz with ideal pins: no rise and fall times, no bus capacitance. The pads cannot
sink the 20 mA of Fast-mode Plus (docs/verification.md section 9), so an Fm+ bus needs a buffer between the chip pins
and the bus (docs/info.md, External hardware); the buffer's propagation delays add to the target's data valid time
at the chip pins, which the firmware samples 0.483 us after its SCL fall.
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
P_SCL, P_SDA = ff.P_SCL, ff.P_SDA
# UM10204 Rev. 7.0, Table 11, Fast-mode Plus column (PDF page 44); minimum times in us, fSCL maximum in kHz
FMP = dict(f_khz=1000, hd_sta=0.26, low=0.5, high=0.26, su_sta=0.26, hd_dat=0.0, su_dat=0.05, su_sto=0.26, buf=0.5)
VD_US = 0.45  # tVD;DAT and tVD;ACK maximum
ff.I2C_LIMITS.setdefault("Fm+", FMP)


class BitStretchBus(ff.I2cBus):
    """I2cBus whose target also holds SCL low for `width` cycles from the SCL fall after bit `at[1]` of byte `at[0]`
    (bit-level clock stretching, UM10204 3.1.9)."""

    def __init__(self, *args, at=(1, 3), width=50, **kw):
        super().__init__(*args, **kw)
        self.at, self.width = at, width

    def _target(self, c, scl, sda):
        pscl = self.prev[0]
        super()._target(c, scl, sda)
        if pscl and not scl and self.state != "idle" and (self.nbyte, self.pos) == self.at:
            self.scl_until = c + self.width


def assemble(**equ):
    return ff.assemble("i2c_ctl_fmp.s", **equ)


def model_run(words, stretch=0, bus=None, cycles=12_000, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_SCL: 0, P_SDA: 0}),
                         open_drain=ff.open_drain({P_SCL, P_SDA}))
    bus = bus or ff.I2cBus(ff.I2C_ADDR, ff.I2C_READ, stretch=stretch)
    bench = ff.Bench(m, inputs=bus)
    bench.lates = []  # events issued with a lead outside 4 to 32,767 cycles: (cycle, lead, instruction)
    if hasattr(m, "_event_cycle"):
        prog, orig = assemble(**equ), m._event_cycle

        def event_cycle(th, c, t):
            lead = (t - c) & 0xFFFF
            if not 4 <= lead <= ff.HORIZON:
                bench.lates.append((c, lead - 0x10000 if lead > ff.HORIZON else lead, prog.source[th.pc]))
            return orig(th, c, t)
        m._event_cycle = event_cycle
    bench.send(0, *words)
    bench.run(cycles)
    return bench, m, bus


def assert_on_time(m, bench, label):
    th = m.threads[0]
    assert th.status == 0 and th.run, f"{label}: status {th.status}, run {th.run}, late events {bench.lates[:4]}"
    assert not bench.lates, f"{label}: late events {bench.lates[:4]}"


def check_fmp(log, changes, label):
    """Table 11 Fm+ minima (test_fw_full.check_i2c_times) and the tVD;DAT / tVD;ACK maximum. Returns the bus times."""
    ff.check_i2c_times(log, changes, "Fm+", F_MHZ, label)
    t = ff.i2c_times(changes)
    vd = max(t["hd_dat"])
    need = math.floor(VD_US * F_MHZ + 1e-9)
    assert vd <= need, f"{label}: SDA change {vd} cycles after the SCL fall, tVD;DAT maximum {VD_US} us = {need} cycles"
    log.info(f"{label}: latest SDA change {vd} cycles ({vd / F_MHZ:.3f} us) after an SCL fall (tVD 0.45 us = {need})")
    return t


@cocotb.test(skip=GL)
async def test_i2c_fmp_words(dut):
    """Memory words of fw/i2c_ctl_fmp.s."""
    size = assemble().size
    dut._log.info(f"fw/i2c_ctl_fmp.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_i2c_fmp_model(dut):
    """fw/i2c_ctl_fmp.s in the reference model at 60 MHz: the Fast-mode script without and with byte-level clock
    stretching, every bus time against Table 11 Fast-mode Plus, data bits at 60 cycles; three negative controls."""
    log = dut._log
    words, replies, events = ff.i2c_script()
    for stretch in (0, 300):
        bench, m, bus = model_run(words, stretch)
        label = f"i2c_ctl_fmp model, stretch {stretch}"
        assert bench.words(0) == replies, f"{label}: replies {[hex(w) for w in bench.words(0)]}"
        assert ff.i2c_events(bus) == events, f"{label}: bus events {ff.i2c_events(bus)}"
        ff.assert_status_clean(m, [0])
        t = check_fmp(log, bus.changes, label)
        assert min(t["period"]) == 60, f"{label}: shortest SCL period {min(t['period'])} cycles, want 60"
        n60 = sum(1 for p in t["period"] if p == 60)
        log.info(f"{label}: {len(replies)} replies and {len(events)} bus events correct; {n60} SCL periods of "
                 f"exactly 60 cycles (1.000 MHz), longest {max(t['period'])}; SCL low {min(t['low'])} to "
                 f"{max(t['low'])}, high {min(t['high'])} to {max(t['high'])} cycles")
        if stretch:
            assert max(t["low"]) >= stretch, f"{label}: no stretched low period ({max(t['low'])})"
    for equ, want in ((dict(TLOW=29), "low"), (dict(TLOW=30), "SCL period")):
        bench, m, bus = model_run(words, 0, **equ)
        try:
            check_fmp(log, bus.changes, f"control {equ}")
        except AssertionError as e:
            log.info(f"control {equ}: {e}")
            assert want in str(e), f"control {equ}: wrong fault: {e}"
        else:
            raise AssertionError(f"control {equ}: Table 11 check found no fault")
    # A target whose data and ACK come at the tVD;DAT / tVD;ACK maximum (0.45 us = 27 cycles after the SCL fall) is read
    # correctly; 29 cycles (0.483 us, beyond Table 11) is a control: the firmware reads SDA as it was 28 cycles after
    # the fall, so it must read a wrong bit.
    for td, want_ok in ((27, True), (29, False)):
        bench, m, bus = model_run(words, bus=ff.I2cBus(ff.I2C_ADDR, ff.I2C_READ, td=td))
        ok = bench.words(0) == replies and ff.i2c_events(bus) == events
        log.info(f"target data {td} cycles ({td / F_MHZ:.3f} us) after the SCL fall: replies "
                 f"{[hex(w) for w in bench.words(0)]} ({'correct' if ok else 'wrong'})")
        assert ok == want_ok, f"target data at {td} cycles: {'correct' if ok else 'wrong'}, want the other"
    bus = BitStretchBus(ff.I2C_ADDR, ff.I2C_READ, at=(1, 3), width=50)
    bench, m, bus = model_run(words, bus=bus)
    try:
        check_fmp(log, bus.changes, "control: bit-level stretch of 50 cycles in byte 1")
    except AssertionError as e:
        log.info(f"control bit-level stretch (not seen by the firmware, as stated): {e}")
        assert "high" in str(e), f"bit-level stretch: wrong fault: {e}"
    else:
        raise AssertionError("bit-level stretch: Table 11 check found no fault")


@cocotb.test()
async def test_i2c_fmp_rtl(dut):
    """fw/i2c_ctl_fmp.s on the chip at 60 MHz in lockstep with the reference model: the Fast-mode script with 300 cycles
    of byte-level stretching, every bus time against Table 11 Fast-mode Plus, data bits at 60 cycles."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl i2c_ctl_fmp"
    words, replies, events = ff.i2c_script()
    bus = ff.I2cBus(ff.I2C_ADDR, ff.I2C_READ, stretch=300)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_SCL: 0, P_SDA: 0}),
                        od=ff.open_drain({P_SCL, P_SDA}), inputs=bus, label=label)
    bench.send(0, *words)
    if hasattr(dut, "dump_off"):  # waveform of the first two bytes (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 2600)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        rises = [c for i, (c, (scl, _)) in enumerate(bus.changes) if i and scl and not bus.changes[i - 1][1][0]]
        log.info(f"{label}: waveform from the start; SCL rises at simulation times (ps) "
                 f"{[round(now - (bench.m.cycle - c) * rf.CLK_PS) for c in rises[:12]]}")
    await bench.collect(0, len(replies), limit=40_000)
    await bench.run(1000)
    await bench.finish(log)
    assert bench.words(0) == replies, f"{label}: replies {[hex(w) for w in bench.words(0)]}"
    assert ff.i2c_events(bus) == events, f"{label}: bus events {ff.i2c_events(bus)}"
    t = check_fmp(log, bus.changes, label)
    assert min(t["period"]) == 60, f"{label}: shortest SCL period {min(t['period'])} cycles, want 60"
    log.info(f"{label}: {len(replies)} replies and {len(events)} bus events correct; "
             f"{sum(1 for p in t['period'] if p == 60)} SCL periods of 60 cycles (1.000 MHz); SCL low "
             f"{min(t['low'])} to {max(t['low'])}, high {min(t['high'])} to {max(t['high'])} cycles")

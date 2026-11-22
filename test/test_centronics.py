# SPDX-License-Identifier: Apache-2.0
"""Centronics printer host firmware (fw/centronics.s) against an independent printer model, in the reference model and
on the RTL at 60 MHz.

What it does:
  The printer model (Printer730) is written from the Centronics 730-1 Printer Owner's Manual (December 1979,
  bitsavers.org): interface timing Figure 9 and its text (page 16: data set-up 1 us min before the strobe, strobe 1 us
  min, data hold 1 us min after it; BUSY high within 50 ns of the strobe's trailing edge; ACK about 5 us; BUSY low within
  50 ns of the ACK leading edge; transfer cycle nominally 100 us, about 200 us in 10 % of the cycles, up to 2.6 s for a
  carriage return) and the signal descriptions (pages 18 and 19: no new data before the ACK leading edge, no data while
  BUSY is high). IEEE 1284 is paid; the 730-1 manual is the interface's original maker's description. It does not use
  the firmware, the assembler or the processor model. It latches the data at the strobe's trailing edge and checks the
  set-up, width and hold times, that the host does not strobe while BUSY is high and does not change the data before
  the ACK. The carriage-return time is shortened to 1 ms in the test (stated).
  The model test prints a line with every tenth transfer slow and a carriage return; negative controls shorten the data
  set-up and the strobe. The RTL test prints three bytes in lockstep with the reference model; the same test runs on
  the gate-level netlist (gl_fw workflow).

Why:
  Twenty-fifth row of list P (flex/BLIND.md, record of beacon round 34159012), the first row after the pre-tag 24 to
  enter for a deferred row (L2c-19). The pinned libsigrokdecode has no Centronics decoder, so the row has no sigrok
  scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the cable, the TTL levels and the printer's input filters are not
simulated.
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
PINS = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 12: 0}  # UIO0..7 data, UO4 strobe
SETUP, WIDTH, HOLD = 1.0, 1.0, 1.0  # us, Figure 9
BUSY_NS = 50  # BUSY after the strobe trailing edge and after the ACK leading edge


class Printer730:
    """Centronics 730-1 printer interface (see the module text). busy_us: strobe end to ACK; slow_every: every n-th
    transfer takes twice as long; cr_us: time for a carriage return."""

    def __init__(self, us=F_MHZ, busy_us=100.0, slow_every=10, cr_us=1000.0, ack_us=5.0):
        self.us = us
        self.cyc = lambda u: int(round(u * us))
        self.busy_us, self.slow_every, self.cr_us, self.ack_us = busy_us, slow_every, cr_us, ack_us
        self.stb, self.data, self.t_data = 1, None, -10**9
        self.t_fall = None
        self.busy, self.ack = 0, 1
        self.events = []  # (cycle, name, level) scheduled
        self.received, self.errors, self.edges = [], [], []
        self.hold_until, self.pending_data = None, None
        self.ack_at = None

    def _err(self, c, text):
        if len(self.errors) < 20:
            self.errors.append(f"cycle {c}: {text}")

    def __call__(self, c, uo, uio, oe):
        while self.events and self.events[0][0] <= c:
            _, name, level = self.events.pop(0)
            setattr(self, name, level)
            self.edges.append((c, name, level))
        data = uio & oe & 0xFF if oe & 0xFF == 0xFF else None
        stb = (uo >> 4) & 1
        if data != self.data:
            if self.data is not None and self.t_fall is not None:
                self._err(c, f"data changed {(c - self.t_fall) / self.us:.2f} us after the strobe leading edge, "
                             f"before the ACK (set-up/hold window)")
            if self.busy:
                self._err(c, "data changed while BUSY is high (no new data before the ACK leading edge)")
            if self.hold_until is not None and c < self.hold_until:
                self._err(c, f"data hold {(c - (self.hold_until - self.cyc(HOLD))) / self.us:.2f} us after the "
                             f"strobe (Figure 9: 1 us min)")
            self.data, self.t_data = data, c
        if stb != self.stb:
            self.edges.append((c, "strobe", stb))
            if not stb:  # leading edge
                if self.busy:
                    self._err(c, "strobe while BUSY is high")
                su = (c - self.t_data) / self.us
                if self.data is None or su < SETUP - 1e-9:
                    self._err(c, f"data set-up {su:.2f} us before the strobe (Figure 9: 1 us min)")
                self.t_fall = c
            else:  # trailing edge
                if self.t_fall is not None:
                    w = (c - self.t_fall) / self.us
                    if w < WIDTH - 1e-9:
                        self._err(c, f"strobe width {w:.2f} us (1 us min)")
                    self.received.append(self.data)
                    self.hold_until = c + self.cyc(HOLD)
                    n = len(self.received)
                    t = self.cr_us if self.data == 0x0D else self.busy_us * (2 if n % self.slow_every == 0 else 1)
                    b = c + self.cyc(BUSY_NS / 1000.0)
                    a = c + self.cyc(t)
                    self.events += [(b, "busy", 1), (a, "ack", 0), (a + self.cyc(BUSY_NS / 1000.0), "busy", 0),
                                    (a + self.cyc(self.ack_us), "ack", 1)]
                    self.events.sort()
                self.t_fall = None
            self.stb = stb
        return (self.busy << 3) | (self.ack << 4), 0


LINE = list(b"Hello, 730-1!") + [0x00, 0xFF, 0xA5, 0x0D, 0x0A]


def assemble(**equ):
    return ff.assemble("centronics.s", **equ)


def model_run(printer, data, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners(PINS))
    bench = ff.Bench(m, inputs=printer)
    bench.send(0, *data)
    for _ in range(400):
        if len(printer.received) >= len(data):
            break
        bench.run(10_000)
    bench.run(20_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_centronics_words(dut):
    """Memory words of fw/centronics.s."""
    size = assemble().size
    dut._log.info(f"fw/centronics.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_centronics_model(dut):
    """fw/centronics.s in the reference model against the printer model: a line with slow transfers and a carriage
    return; negative controls for the data set-up and the strobe width."""
    log = dut._log
    printer = Printer730()
    bench, m = model_run(printer, LINE)
    assert not printer.errors, f"printer: {printer.errors[:3]}"
    assert printer.received == LINE, f"printer received {printer.received}"
    ff.assert_status_clean(m, [0])
    strobes = [c for c, n, v in printer.edges if n == "strobe" and v == 0]
    gaps = [(b - a) / F_MHZ for a, b in zip(strobes, strobes[1:])]
    log.info(f"model: {len(LINE)} bytes printed as sent ({bytes(LINE)!r}); strobe-to-strobe {min(gaps):.2f} to "
             f"{max(gaps):.2f} us (100 us transfers, 200 us every tenth, 1 ms after the CR); no set-up, width, hold, "
             f"busy or ACK fault")
    for equ, text in ((dict(SU=40), "set-up"), (dict(SW=40), "strobe width")):
        printer = Printer730()
        model_run(printer, LINE[:3], **equ)
        found = [e for e in printer.errors if text in e]
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: expected '{text}', got {printer.errors[:2]}"


@cocotb.test()
async def test_centronics_rtl(dut):
    """fw/centronics.s on the chip at 60 MHz against the printer model (transfer 20 us): three bytes, in lockstep with
    the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl centronics"
    data = [0x41, 0x5A, 0x0A]
    printer = Printer730(busy_us=20.0)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners(PINS), inputs=printer, label=label)
    bench.send(0, *data)
    if hasattr(dut, "dump_off"):  # waveform of the three transfers (tb.fst), for review
        dut.dump_off.value = 0
    for _ in range(100):
        if len(printer.received) >= len(data):
            break
        await bench.run(2000)
    await bench.run(3000)
    if hasattr(dut, "dump_off"):
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        to_ps = lambda c: round(now - (bench.m.cycle - c) * rf.CLK_PS)  # noqa: E731
        log.info(f"{label}: edges (ps, signal, level) {[(to_ps(c), n, v) for c, n, v in printer.edges[:12]]}")
    await bench.finish(log)
    assert not printer.errors and printer.received == data, f"{label}: {printer.errors[:3]} {printer.received}"
    log.info(f"{label}: printer received {[hex(b) for b in printer.received]}")

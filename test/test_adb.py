# SPDX-License-Identifier: Apache-2.0
"""ADB host firmware (fw/adb.s) against an independent ADB device model, in the reference model and on the RTL at
60 MHz.

What it does:
  The device model (AdbDevice) is written from Apple's Guide to the Macintosh Family Hardware, 2nd edition (1990),
  chapter 8 Apple Desktop Bus: ADB transactions, commands (Table 8-13), signals (Attention, Sync, Global Reset,
  Service Request), Table 8-14 ADB timing, register 3 (Tables 8-15 and 8-17). It does not use the firmware, the
  assembler or the processor model. It reads the host's drive of the bus (open drain with a pull-up) and checks it
  against the host column of Table 8-14: Attention low 800 us +/- 3 %, Sync high 65 us +/- 3 %, bit cells 100 us
  +/- 3 % with a low time of 65 % (a "0") or 35 % (a "1") of the cell +/- 5 %, stop bit low 70 us +/- 3 %, Global
  Reset low at least 3 ms, and for a Listen the start bit 140 to 260 us after the stop bit; the host must not pull
  the bus low while the device answers. The device answers a Talk to its address at its own bit-cell time (the
  device column allows +/- 30 %) after a set stop-bit-to-start-bit time, with register 0 only when it has data,
  register 2 as the last Listen wrote it and register 3 always (bit 14 = 1, bit 13 service request enable, bits 11..8
  the address, bits 7..0 the handler ID). A Listen to register 3 with handler $FE moves the device to the address
  sent, $00 sets address and enable, a device-defined handler (1 or 2) is stored, and others are ignored. With data
  in register 0 and requests enabled, it holds the bus low in the stop bit of every command that is not a Talk to it
  (service request: 300 us nominal, at least 140 us longer than the stop bit). SendReset and Global Reset return it
  to its power-on state.
  The model test runs a session: Global Reset, Talk R3, Listen R3 with $FE (address 3 to $A), Talk R3 to the old and
  to the new address, Listen R2 with 4 bytes, a Talk R0 to an absent address (the device asks for service in its stop
  bit), Talk R0 (the data), Talk R0 again (no data), Flush and SendReset; and the first transactions at the device
  corners (bit cell 70 and 130 us with low times at 40/60 % and 30/70 %, answer after 140 and 260 us, service request
  210 and 390 us). Negative controls change the host's "0" low time, the Attention, the Sync and the Listen start
  time. The RTL test runs a Talk R3, a Talk R0 to an absent address with a service request and the Talk R0 that
  reads the data, in lockstep with the reference model; the same test runs on the gate-level netlist (gl_fw).

Why:
  Eighteenth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode
  decoder at the pinned commit decodes ADB, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the pull-up and the bus capacitance are not simulated (the
Guide gives the timing at the levels of the bus; a slow rise lengthens the measured low times).
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
P_ADB = 0  # uio[0], open drain

# Table 8-14 (host column), in us
CELL, LOW0, LOW1, LOW_TOL, ATT, SYNC_T, STOP_T, HOST_TOL = 100.0, 0.65, 0.35, 0.05, 800.0, 65.0, 70.0, 0.03
GRESET, TLT_MIN, TLT_MAX = 3000.0, 140.0, 260.0


class AdbDevice:
    """ADB device on the bus (see the module text). cell_us, f1, f0: its bit cell and the low fractions of a "1" and
    a "0"; resp_us: its stop-bit-to-start-bit time; srq_us: its service request low time from the start of the stop
    bit; pend: register 0 data that becomes ready after pend_after commands."""

    def __init__(self, us=F_MHZ, addr=3, handler=1, cell_us=100.0, f1=0.35, f0=0.65, resp_us=200.0, srq_us=300.0,
                 pend=(0x81, 0x7F), pend_after=0):
        self.us = us
        self.cyc = lambda u: int(round(u * us))
        self.addr0, self.handler0 = addr, handler
        self.cell_us, self.f1, self.f0, self.resp_us, self.srq_us = cell_us, f1, f0, resp_us, srq_us
        self.pend_data, self.pend_after = list(pend) if pend else None, pend_after
        self.hlow, self.fall, self.rise = 0, 0, 0
        self.mode, self.bits, self.cell_start, self.cell_low = "idle", [], 0, 0
        self.cmd, self.listen_reg, self.stop_rise = None, None, 0
        self.drive, self.answer_end = [], -1
        self.commands, self.listened, self.answers, self.srqs, self.resets, self.errors = [], [], [], 0, 0, []
        self.edges = []  # (cycle, host level) for the waveform review
        self.power_on()
        self.pending = None

    def power_on(self):
        self.addr, self.handler, self.srq_en, self.reg2, self.pending = self.addr0, self.handler0, True, [], None

    def _err(self, c, text):
        self.errors.append(f"cycle {c}: {text}")

    def _within(self, x_us, nom, tol):
        return nom * (1 - tol) - 1 / self.us <= x_us <= nom * (1 + tol) + 1 / self.us

    def _bit(self, c, low, cell):
        cell_us, r = cell / self.us, low / cell
        if not self._within(cell_us, CELL, HOST_TOL):
            self._err(c, f"host bit cell {cell_us:.2f} us (100 us +/- 3 %, Table 8-14)")
        if abs(r - LOW0) <= LOW_TOL + 1e-9:
            return 0
        if abs(r - LOW1) <= LOW_TOL + 1e-9:
            return 1
        self._err(c, f"host low time {100 * r:.1f} % of the bit cell (65 % or 35 % +/- 5 %, Table 8-14)")
        return int(r < 0.5)

    def _send(self, start, data):
        cell = self.cyc(self.cell_us)
        t = start
        for byte in [None] + list(data):
            for b in ([1] if byte is None else [(byte >> (7 - k)) & 1 for k in range(8)]):
                self.drive.append((t, t + round((self.f1 if b else self.f0) * cell)))
                t += cell
        self.drive.append((t, t + round(STOP_T * self.cell_us / CELL * self.us)))
        self.answer_end = self.drive[-1][1]
        self.answers.append(list(data))

    def _command(self, c):
        cmd = self.cmd
        self.commands.append(cmd)
        if self.pend_data is not None and len(self.commands) == self.pend_after:
            self.pending = list(self.pend_data)
        a, code, r = cmd >> 4, (cmd >> 2) & 3, cmd & 3
        if cmd & 0x0F == 0:  # SendReset
            self.power_on()
            self.resets += 1
        elif code == 0 and r == 1 and a == self.addr:  # Flush
            self.pending = None
        elif code == 3 and a == self.addr:  # Talk
            data = None
            if r == 0 and self.pending:
                data, self.pending = self.pending, None
            elif r == 2 and self.reg2:
                data = self.reg2
            elif r == 3:
                data = [0x40 | (0x20 if self.srq_en else 0) | self.addr, self.handler]
            if data:
                self._send(c + self.cyc(self.resp_us), data)
        elif code == 2 and a == self.addr:  # Listen
            self.mode, self.listen_reg = "await", r
            return
        self.mode = "idle"

    def _listen_end(self, c):
        bits = self.bits
        if not bits or bits[0] != 1:
            self._err(c, "Listen data without a start bit 1")
        data_bits = bits[1:]
        if len(data_bits) % 8 or not 2 <= len(data_bits) // 8 <= 8:
            self._err(c, f"Listen data of {len(data_bits)} bits (2 to 8 bytes)")
        if not self._within(self.cell_low / self.us, STOP_T, HOST_TOL):
            self._err(c, f"host stop bit low {self.cell_low / self.us:.2f} us (70 us +/- 3 %)")
        data = [sum(b << (7 - k) for k, b in enumerate(data_bits[i:i + 8])) for i in range(0, len(data_bits), 8)]
        self.listened.append((self.listen_reg, data))
        if self.listen_reg == 3 and len(data) >= 2:
            hi, lo = data[0], data[1]
            if lo in (0xFE, 0x00):
                self.addr = hi & 0x0F
                if lo == 0x00:
                    self.srq_en = bool(hi & 0x20)
            elif lo in (1, 2):
                self.handler = lo
        elif self.listen_reg == 2:
            self.reg2 = data
        self.mode = "idle"

    def __call__(self, c, uo, uio, oe):
        hlow = int(bool((oe >> P_ADB) & 1) and not (uio >> P_ADB) & 1)
        while self.drive and self.drive[0][1] <= c:
            self.drive.pop(0)
        dlow = int(bool(self.drive) and self.drive[0][0] <= c)
        if hlow and c < self.answer_end and c >= self.stop_rise:
            self._err(c, "the host pulls the bus low while the device answers")
            self.answer_end = -1
        if hlow != self.hlow:
            self.edges.append((c, hlow))
            if hlow:  # host falling edge
                if self.mode == "sync":
                    sync = (c - self.rise) / self.us
                    if not self._within(sync, SYNC_T, HOST_TOL):
                        self._err(c, f"Sync high {sync:.2f} us (65 us +/- 3 %, Table 8-14)")
                    self.mode, self.bits, self.cell_start = "cmd", [], c
                elif self.mode in ("cmd", "data"):
                    self.bits.append(self._bit(c, self.cell_low, c - self.cell_start))
                    self.cell_start = c
                    if self.mode == "cmd" and len(self.bits) == 8:
                        self.cmd = sum(b << (7 - k) for k, b in enumerate(self.bits))
                        talk_me = (self.cmd >> 2) & 3 == 3 and self.cmd >> 4 == self.addr
                        if self.pending and self.srq_en and not talk_me:  # service request in the stop bit
                            self.drive.append((c + self.cyc(5), c + self.cyc(self.srq_us)))
                            self.srqs += 1
                elif self.mode == "await":
                    d = (c - self.stop_rise) / self.us
                    if not TLT_MIN - 1 / self.us <= d <= TLT_MAX + 1 / self.us:
                        self._err(c, f"Listen start bit {d:.2f} us after the stop bit (140 to 260 us, Table 8-14)")
                    self.mode, self.bits, self.cell_start = "data", [], c
                self.fall = c
            else:  # host rising edge
                low = c - self.fall
                low_us = low / self.us
                if low_us >= GRESET - 1 / self.us:
                    self.power_on()
                    self.resets += 1
                    self.mode = "idle"
                elif low_us > 300:
                    if not self._within(low_us, ATT, HOST_TOL):
                        self._err(c, f"Attention low {low_us:.2f} us (800 us +/- 3 %, Table 8-14)")
                    if self.mode == "data":
                        self._listen_end(c)
                    self.mode = "sync"
                elif self.mode == "cmd" and len(self.bits) == 8:
                    if not self._within(low_us, STOP_T, HOST_TOL):
                        self._err(c, f"host stop bit low {low_us:.2f} us (70 us +/- 3 %, Table 8-14)")
                    self.stop_rise = c
                    self._command(c)
                else:
                    self.cell_low = low
                self.rise = c
            self.hlow = hlow
        if self.mode == "data" and not hlow and c - self.rise > self.cyc(250):
            self._listen_end(c)
        line = 0 if hlow or dlow else 1
        return 0, line << P_ADB


GRST = 0x8000


def talk(a, r):
    return a << 4 | 0xC | r


def listen(a, r, data):
    return [len(data) << 8 | a << 4 | 0x8 | r] + list(data)


def status(n=0, srq=False):
    return 0x8000 | (0x100 if srq else 0) | n


SESSION = ([GRST], [talk(3, 3)], listen(3, 3, [0x0A, 0xFE]), [talk(3, 3)], [talk(0xA, 3)],
           listen(0xA, 2, [0x12, 0x34, 0x56, 0x78]), [talk(2, 0)], [talk(0xA, 0)], [talk(0xA, 0)], [0xA1], [0x00],
           [talk(3, 3)])
EXPECT = ([status()], [0x63, 0x01, status(2)], [status()], [status()], [0x6A, 0x01, status(2)], [status()],
          [status(srq=True)], [0x81, 0x7F, status(2)], [status()], [status()], [status()], [0x63, 0x01, status(2)])
PEND_AFTER = 5  # register 0 data is ready after the Listen R2 (fifth command; the Global Reset is a signal)


def flat(lists):
    return [w for x in lists for w in x]


def assemble(**equ):
    return ff.assemble("adb.s", **equ)


def model_run(dev, words, want, cycles=4_000_000, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_ADB: 0}), open_drain=ff.open_drain({P_ADB}))
    bench = ff.Bench(m, inputs=dev)
    bench.send(0, *words)
    for _ in range(0, cycles, 100_000):
        if len(bench.words(0)) >= want:
            break
        bench.run(100_000)
    bench.run(20_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_adb_words(dut):
    """Memory words of fw/adb.s."""
    size = assemble().size
    dut._log.info(f"fw/adb.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_adb_model(dut):
    """fw/adb.s in the reference model against the ADB device model: the session; the device corners; negative
    controls for the "0" low time, the Attention, the Sync and the Listen start time."""
    log = dut._log
    want = flat(EXPECT)
    dev = AdbDevice(pend_after=PEND_AFTER)
    bench, m = model_run(dev, flat(SESSION), len(want))
    assert not dev.errors, f"session: {dev.errors[:3]}; host edges {dev.edges[:12]}"
    assert bench.words(0) == want, f"session: {[hex(w) for w in bench.words(0)]}"
    assert dev.listened == [(3, [0x0A, 0xFE]), (2, [0x12, 0x34, 0x56, 0x78])] and dev.srqs == 1, (dev.listened, dev.srqs)
    ff.assert_status_clean(m, [0])
    log.info(f"session: {len(want)} host words as expected; commands {[hex(c) for c in dev.commands]}; Listen data "
             f"{dev.listened}; device answers {dev.answers}; service requests {dev.srqs}; resets {dev.resets}; "
             f"{m.cycle} cycles ({m.cycle / F_MHZ / 1000:.2f} ms)")
    part = [[talk(3, 3)], [talk(2, 0)], [talk(3, 0)]]  # the device stays at address 3
    pw = [0x63, 0x01, status(2), status(srq=True), 0x81, 0x7F, status(2)]
    for cell, f1, f0, resp, srq in ((70.0, 0.40, 0.60, 140.0, 210.0), (130.0, 0.30, 0.70, 260.0, 390.0),
                                    (130.0, 0.40, 0.60, 140.0, 390.0), (70.0, 0.30, 0.70, 260.0, 210.0)):
        dev = AdbDevice(cell_us=cell, f1=f1, f0=f0, resp_us=resp, srq_us=srq, pend_after=1)
        bench, m = model_run(dev, flat(part), len(pw))
        label = f"device cell {cell} us, low {f1:.2f}/{f0:.2f}, answer after {resp} us, service request {srq} us"
        assert not dev.errors and bench.words(0) == pw, f"{label}: {dev.errors[:3]} {[hex(w) for w in bench.words(0)]}"
        ff.assert_status_clean(m, [0])
        log.info(f"{label}: host words as expected")
    for equ, want_text in ((dict(L0=3400, H0=2600), "host low time"), (dict(ATTW=20000), "Attention"),
                           (dict(SYNC=3000), "Sync"), (dict(STTW=3000), "Listen start bit")):
        dev = AdbDevice(pend_after=PEND_AFTER)
        words = flat(SESSION[1:3])
        model_run(dev, words, 4, cycles=1_000_000, **equ)
        found = [e for e in dev.errors if want_text in e]
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: expected a fault with '{want_text}', got {dev.errors[:2]}"


@cocotb.test()
async def test_adb_rtl(dut):
    """fw/adb.s on the chip at 60 MHz against the ADB device model (bit cell 120 us): Talk R3, Talk R0 to an absent
    address with a service request, Talk R0 with the data, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl adb"
    words = [talk(3, 3), talk(2, 0), talk(3, 0)]
    want = [0x63, 0x01, status(2), status(srq=True), 0x81, 0x7F, status(2)]
    dev = AdbDevice(cell_us=120.0, resp_us=180.0, pend_after=1)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_ADB: 0}), od=ff.open_drain({P_ADB}), inputs=dev,
                        label=label)
    bench.send(0, *words)
    if hasattr(dut, "dump_off"):  # waveform of the first command and the start of the answer (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 130_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; host edges (ps, level) "
                 f"{[(round(now - (bench.m.cycle - c) * rf.CLK_PS), v) for c, v in dev.edges[:20]]}")
    await bench.collect(0, len(want), limit=900_000)
    await bench.run(2000)
    await bench.finish(log)
    assert not dev.errors and bench.words(0) == want, f"{label}: {dev.errors[:3]} {[hex(w) for w in bench.words(0)]}"
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}; commands {[hex(c) for c in dev.commands]}; "
             f"answers {dev.answers}; service requests {dev.srqs}")

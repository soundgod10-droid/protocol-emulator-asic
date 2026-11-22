# SPDX-License-Identifier: Apache-2.0
"""Joybus host firmware (fw/joybus.s) against an independent controller pad model, in the reference model and on the
RTL at 60 MHz.

What it does:
  The controller line has no official public document. The pad model (JoybusPad) is written from the public notes
  "N64 Controller" on qwertymodo.com (sections Line protocol and Data protocol): one-wire, half-duplex, idle high,
  250 kbit/s, a "1" is 1 us low and 3 us high, a "0" 3 us low and 1 us high, MSB first; the console's stop bit is
  1 us low then 2 us high, the controller's 2 us low then 1 us high, and the controller can answer right after the
  console's stop bit (for command 02h within 7 us of it: 10 us from the stop-bit fall to the first data fall). Commands:
  00h status and FFh reset (3 bytes: 05h 00h and a flag byte; 02h = no controller pak), 01h poll (4 bytes: two
  button bytes, stick X and Y; X reads FEh and Y 00h after power-on and both 00h after FFh), 02h read (2 address
  bytes, 33 bytes 00h when no pak is inserted, whatever the address CRC). It does not use the firmware, the assembler or
  the processor model. It decodes the host's bits by their low time and checks the host timing; the notes give no
  tolerance, so the model accepts +/- 10 % of the nominal times (stated assumption). It answers at its own bit time
  and answer delay, and checks that the host does not pull the line low while it answers.
  The model test runs a session (status, poll, reset, poll, read with 33 reply bytes), the same at the pad corners
  (bit time 3.6 and 4.4 us, answer 3 and 10 us after the stop-bit fall), a pad that answers only 2 of 3 bytes, and no
  pad; negative controls change the host's "1" low time, the bit cell, the stop bit and the host's sample point. The RTL
  test runs status and poll in lockstep with the reference model; the same test runs on the gate-level netlist
  (gl_fw workflow).

Why:
  Twentieth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode decoder at
  the pinned commit decodes this line, so the row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the pull-up, the cable capacitance and the slow rise of an
open-drain line are not simulated (a slow rise lengthens every low time on a real line).
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
P_D = 0  # uio[0], open drain
T1, T0, TCELL, TSTOP, TOL = 1.0, 3.0, 4.0, 1.0, 0.10  # us; +/- 10 % (assumption, the notes give none)
CMD_LEN = {0x00: 1, 0x01: 1, 0x02: 3, 0xFF: 1}


class JoybusPad:
    """N64 controller pad on the line (see the module text). cell_us: its bit time; resp_us: stop-bit fall of the host
    to its first data fall; short: bytes it leaves out at the end of a status reply; present: False = no pad."""

    def __init__(self, us=F_MHZ, buttons=(0x12, 0x34), cell_us=4.0, resp_us=4.0, short=0, present=True):
        self.us, self.buttons, self.cell_us, self.resp_us, self.short, self.present = us, buttons, cell_us, resp_us, \
            short, present
        self.x, self.y = 0xFE, 0x00  # after power-on
        self.hlow, self.fall, self.last_fall = 0, None, None
        self.bits, self.reply_end = [], -1
        self.drive = []
        self.commands, self.replies, self.errors, self.edges = [], [], [], []

    def _err(self, c, text):
        if len(self.errors) < 20:
            self.errors.append(f"cycle {c}: {text}")

    def _near(self, x_us, nom):
        return abs(x_us - nom) <= nom * TOL + 1 / self.us

    def _reply(self, start, data):
        cell = round(self.cell_us * self.us)
        t = start
        for byte in data:
            for k in range(8):
                b = (byte >> (7 - k)) & 1
                self.drive.append((t, t + round((T1 if b else T0) / TCELL * cell)))
                t += cell
        self.drive.append((t, t + round(2.0 / TCELL * cell)))  # the pad's stop bit
        self.reply_end = t + cell
        self.replies.append(list(data))

    def _command(self, c_stop_fall, cmd):
        self.commands.append(cmd)
        op = cmd[0]
        if op in (0x00, 0xFF):
            if op == 0xFF:
                self.x = self.y = 0x00
            data = [0x05, 0x00, 0x02][:3 - self.short]
        elif op == 0x01:
            data = [self.buttons[0], self.buttons[1], self.x, self.y]
        elif op == 0x02:
            data = [0x00] * 33
        else:
            return
        if self.present:
            self._reply(c_stop_fall + round(self.resp_us * self.us), data)

    def __call__(self, c, uo, uio, oe):
        hlow = int(bool((oe >> P_D) & 1) and not (uio >> P_D) & 1)
        while self.drive and self.drive[0][1] <= c:
            self.drive.pop(0)
        dlow = int(bool(self.drive) and self.drive[0][0] <= c)
        if hlow != self.hlow:
            self.edges.append((c, "host", 1 - hlow))
            if hlow:  # host fall
                if c < self.reply_end:
                    self._err(c, "the host pulls the line low while the pad answers")
                    self.reply_end = -1
                if self.bits and self.last_fall is not None:
                    cell_us = (c - self.last_fall) / self.us
                    if not self._near(cell_us, TCELL):
                        self._err(c, f"host bit cell {cell_us:.3f} us (4 us +/- 10 %)")
                self.fall = self.last_fall = c
            elif self.fall is not None:  # host rise
                low_us = (c - self.fall) / self.us
                n = len(self.bits)
                op = sum(b << (7 - k) for k, b in enumerate(self.bits[:8])) if n >= 8 else None
                if op is not None and n == 8 * CMD_LEN.get(op, 99):
                    if not self._near(low_us, TSTOP):
                        self._err(c, f"host stop bit low {low_us:.3f} us (1 us +/- 10 %)")
                    cmd = [sum(b << (7 - k) for k, b in enumerate(self.bits[i:i + 8])) for i in range(0, n, 8)]
                    self.bits, self.last_fall = [], None
                    self._command(self.fall, cmd)
                elif self._near(low_us, T1):
                    self.bits.append(1)
                elif self._near(low_us, T0):
                    self.bits.append(0)
                else:
                    self._err(c, f"host low {low_us:.3f} us (1 us or 3 us +/- 10 %)")
                    self.bits.append(int(low_us < 2.0))
            self.hlow = hlow
        if dlow != getattr(self, "_dlow", 0):
            self.edges.append((c, "pad", 1 - dlow))
            self._dlow = dlow
        line = 0 if hlow or dlow else 1
        return 0, line << P_D


def command(data, r):
    return [len(data) | r << 8] + list(data)


SESSION = [([0x00], 3), ([0x01], 4), ([0xFF], 3), ([0x01], 4), ([0x02, 0x80, 0x01], 33)]
EXPECT = [[0x05, 0x00, 0x02], [0x12, 0x34, 0xFE, 0x00], [0x05, 0x00, 0x02], [0x12, 0x34, 0x00, 0x00], [0x00] * 33]


def words_of(session):
    return [w for d, r in session for w in command(d, r)]


def assemble(**equ):
    return ff.assemble("joybus.s", **equ)


def model_run(pad, words, want, cycles=600_000, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners({P_D: 0}), open_drain=ff.open_drain({P_D}))
    m.late = []  # (cycle, lead, address, program line) of late pin events and samples, for the failure message
    event_cycle = m._event_cycle

    def watch(th, c, t):
        d = (t - c) & 0xFFFF
        if not pe_model.LEAD <= d < pe_model.HORIZON and len(m.late) < 5:
            m.late.append((c, d - 0x10000 if d & 0x8000 else d, th.pc, prog.source[th.pc][1].strip()))
        return event_cycle(th, c, t)

    m._event_cycle = watch
    bench = ff.Bench(m, inputs=pad)
    bench.send(0, *words)
    for _ in range(0, cycles, 20_000):
        if len(bench.words(0)) >= want:
            break
        bench.run(20_000)
    bench.run(2_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_joybus_words(dut):
    """Memory words of fw/joybus.s."""
    size = assemble().size
    dut._log.info(f"fw/joybus.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_joybus_model(dut):
    """fw/joybus.s in the reference model against the pad model: the session; the pad corners; a short reply; no pad;
    negative controls for the "1" low time, the bit cell, the stop bit and the host's sample point."""
    log = dut._log
    want = [w for x in EXPECT for w in x]
    pad = JoybusPad()
    bench, m = model_run(pad, words_of(SESSION), len(want))
    assert not m.late, f"session: late events {m.late}"
    assert not pad.errors, f"session: {pad.errors[:3]}; edges {pad.edges[:16]}"
    assert bench.words(0) == want, f"session: {[hex(w) for w in bench.words(0)]}; pad saw {pad.commands}"
    ff.assert_status_clean(m, [0])
    hf = [c for c, n, v in pad.edges if n == "host" and v == 0]
    log.info(f"session: {len(want)} host words as expected; commands {pad.commands}; host cell "
             f"{(hf[1] - hf[0]) / F_MHZ:.3f} us; {m.cycle} cycles ({m.cycle / F_MHZ:.1f} us)")
    for cell, resp in ((3.6, 3.0), (4.4, 10.0), (3.6, 10.0), (4.4, 3.0)):
        pad = JoybusPad(cell_us=cell, resp_us=resp)
        bench, m = model_run(pad, words_of(SESSION), len(want))
        label = f"pad bit time {cell} us, answer {resp} us after the stop-bit fall"
        assert not m.late and not pad.errors and bench.words(0) == want, \
            f"{label}: {m.late} {pad.errors[:3]} {[hex(w) for w in bench.words(0)]}"
        ff.assert_status_clean(m, [0])
        log.info(f"{label}: host words as expected")
    pad = JoybusPad(short=1)
    bench, m = model_run(pad, words_of(SESSION[:2]), 7)
    got = bench.words(0)
    assert not pad.errors and got == [0x05, 0x00, 0x8000, 0x12, 0x34, 0xFE, 0x00], f"short reply: {[hex(w) for w in got]}"
    ff.assert_status_clean(m, [0])
    log.info(f"pad answers 2 of 3 status bytes: {[hex(w) for w in got[:3]]}, next poll correct")
    pad = JoybusPad(present=False)
    bench, m = model_run(pad, words_of(SESSION[:2]), 7)
    got = bench.words(0)
    assert not pad.errors and got == [0x8000] * 7, f"no pad: {[hex(w) for w in got]}"
    ff.assert_status_clean(m, [0])
    log.info(f"no pad: {[hex(w) for w in got]}")
    for equ, text in ((dict(L1=90), "host low"), (dict(CELL=270), "host bit cell"), (dict(STOPL=120), "stop bit"),
                      (dict(TS=200), "words")):
        pad = JoybusPad()
        bench, m = model_run(pad, words_of(SESSION[:2]), 7, cycles=100_000, **equ)
        found = [e for e in pad.errors if text in e]
        if text == "words" and bench.words(0) != want[:7]:
            found = [f"host words {[hex(w) for w in bench.words(0)]} (expected {[hex(w) for w in want[:7]]})"]
        log.info(f"control {equ}: {found[0][:160] if found else 'no fault found'}")
        assert found, f"control {equ}: expected a fault with '{text}', got {pad.errors[:2]} {bench.words(0)}"


@cocotb.test()
async def test_joybus_rtl(dut):
    """fw/joybus.s on the chip at 60 MHz against the pad model (bit time 4.4 us, answer 6 us after the stop-bit fall):
    status and poll, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl joybus"
    session = SESSION[:2]
    want = [w for x in EXPECT[:2] for w in x]
    pad = JoybusPad(cell_us=4.4, resp_us=6.0)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_D: 0}), od=ff.open_drain({P_D}), inputs=pad,
                        label=label)
    bench.send(0, *words_of(session))
    if hasattr(dut, "dump_off"):  # waveform of the status command and its reply (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 12_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; line edges (ps, side, level) "
                 f"{[(round(now - (bench.m.cycle - c) * rf.CLK_PS), n, v) for c, n, v in pad.edges[:24]]}")
    await bench.collect(0, len(want), limit=200_000)
    await bench.run(2000)
    await bench.finish(log)
    assert not pad.errors and bench.words(0) == want, f"{label}: {pad.errors[:3]} {[hex(w) for w in bench.words(0)]}"
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}; commands {pad.commands}; replies {pad.replies}")

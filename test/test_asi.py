# SPDX-License-Identifier: Apache-2.0
"""AS-Interface master firmware (fw/asi.s) against an independent model of an AS-i transceiver IC in Master Mode with
slaves behind it, in the reference model and on the RTL at 60 MHz.

What it does:
  The model (AsiLine) is written from the IDT/Renesas ASI4U data sheet (26 January 2016): the Manchester-II signal at
  IRD and LED1 (sections 3.3.2, 4.3.4, 4.9.3, Tables 4.6 and 4.21, Figure 4.13: idle high with the board wiring
  DI0 = DI1 and DI2 /= DI3, a telegram starts with a falling edge in the middle of the start bit, 0 = falling and 1 =
  rising edge in the middle of each 6 us bit time) and the telegram checks of section 4.12 applied to the master
  request at IRD: start edge falling; an edge (n x 6 us) - 1 us to + 2 us after the start edge for n = 1 to 13
  (No_information_error); CB..PB even (Parity_error); the edge 78 us after the start edge rising (End_bit_error); no
  edge in the bit time after the end bit (Length_error). Section 4.12.1 gives the slave response: 7 bits, end-bit edge
  36 us after the start edge. It does not use the firmware, the assembler or the processor model. The data sheet does
  not give the master pause; the model slaves answer 2, 3, 6 or 10 bit times after the end of the request (stated
  assumption, the test covers the range). A slave answers data exchange requests (CB = 0) to its address with its
  4-bit input value and keeps the 4 low output bits it received. On a response error the IC distorts LED1 so that it
  no longer forms a telegram (section 4.12); the model then holds LED1 low for two bit times inside the telegram.
  The model test sends requests to four slaves and to an absent address, with a planted parity error and a distorted
  response; negative controls for the bit time and the response time-out. The RTL test sends three requests in
  lockstep with the reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  Twenty-eighth row of list P (flex/BLIND.md, record of beacon round 34159012), a replacement row for deferred rows
  (L2c-19, L2c-22, L2c-23). The pinned libsigrokdecode has no AS-i decoder, so the row has no sigrok scenario.

Conditions: logic simulation at 60 MHz at the IC's digital IRD and LED1 pins; the AS-i line (APM pulses, 30 V supply,
cable) is the IC's part and is not simulated.
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
P_IRD = 12  # UO4
PINS = {P_IRD: 0}
BIT_US = 6.0


def request(cb, addr, data):
    return (cb & 1) << 10 | (addr & 31) << 5 | (data & 31)


class AsiLine:
    """ASI4U in Master Mode with slaves on the line (see the module text). slaves: {address: 4-bit input value};
    pause_bits: master pause before a response; rate: response bit time factor; bad_parity / distort: sets of request
    numbers whose response gets a wrong PB / a distorted LED1."""

    def __init__(self, slaves, us=F_MHZ, pause_bits=3.0, rate=1.0, bad_parity=(), distort=()):
        self.us = us
        self.slaves = dict(slaves)
        self.pause = pause_bits * BIT_US
        self.rate = rate
        self.bad_parity, self.distort = set(bad_parity), set(distort)
        self.ird, self.edges = 1, []  # IRD edges (cycle, level) of the current request
        self.t0 = None
        self.requests, self.outputs, self.errors = [], [], []
        self.led = []  # (cycle, level) scheduled LED1 changes
        self.led1 = 1
        self.dev_us = 0.0  # largest IRD edge deviation from n x 3 us after the start edge
        self.log = []  # (cycle, "ird"/"led1", level)

    def _err(self, c, text):
        if len(self.errors) < 20:
            self.errors.append(f"cycle {c}: {text}")

    def _decode(self, c):
        """Checks of section 4.12 on the request edges; returns (cb, addr, data) or None."""
        t0 = self.edges[0][0]
        us = lambda e: (e - t0) / self.us  # noqa: E731
        bits = []
        for n in range(1, 14):
            win = [(e, v) for e, v in self.edges[1:] if n * BIT_US - 1.0 <= us(e) <= n * BIT_US + 2.0]
            if len(win) != 1:
                self._err(c, f"request bit {n}: {len(win)} edges in the window {n * 6 - 1} to {n * 6 + 2} us "
                             f"(No_information_error)")
                return None
            bits.append(win[0][1])
        for e, v in self.edges[1:]:
            k = us(e) / (BIT_US / 2)
            self.dev_us = max(self.dev_us, abs(k - round(k)) * BIT_US / 2)
        if bits[-1] != 1:
            self._err(c, "end bit edge falling (End_bit_error)")
            return None
        if sum(bits[:12]) % 2:
            self._err(c, f"request bits {bits[:12]} odd (Parity_error)")
            return None
        b = bits[:11]
        cb = b[0]
        addr = sum(x << (4 - i) for i, x in enumerate(b[1:6]))
        data = sum(x << (4 - i) for i, x in enumerate(b[6:11]))
        return cb, addr, data

    def _respond(self, t_end, value, k):
        bits = [0] + [(value >> (3 - i)) & 1 for i in range(4)]
        bits.append(sum(bits[1:5]) % 2 ^ (1 if k in self.bad_parity else 0))
        bits.append(1)
        h = BIT_US / 2 * self.rate * self.us
        t = t_end + self.pause * self.us
        ev = []
        for i, b in enumerate(bits):
            ev += [(t + 2 * i * h, 1 - b), (t + (2 * i + 1) * h, b)]
        if k in self.distort:  # IC distorts LED1: low for two bit times from bit 2
            ev = [(x, v) for x, v in ev if not (t + 4 * h <= x < t + 8 * h)] + [(t + 4 * h, 0), (t + 8 * h, 0)]
        ev.append((t + 14 * h, 1))
        self.led += [(int(round(x)), v) for x, v in sorted(ev)]

    def __call__(self, c, uo, uio, oe):
        while self.led and self.led[0][0] <= c:
            _, v = self.led.pop(0)
            if v != self.led1:
                self.log.append((c, "led1", v))
            self.led1 = v
        ird = (uo >> 4) & 1
        if ird != self.ird:
            self.log.append((c, "ird", ird))
            if self.t0 is None:
                if ird:
                    self._err(c, "rising edge after a pause (Start_bit_error)")
                else:
                    self.t0, self.edges = c, [(c, 0)]
            else:
                self.edges.append((c, ird))
                if (c - self.t0) / self.us > 13 * BIT_US + 2.0:
                    self._err(c, f"edge {(c - self.t0) / self.us:.2f} us after the start edge (Length_error)")
            if self.led:
                self._err(c, "IRD changes while a slave responds")
            self.ird = ird
        if self.t0 is not None and (c - self.t0) / self.us > 14 * BIT_US + 3.0:
            k = len(self.requests)
            r = self._decode(c)
            self.requests.append(r)
            t_end = self.t0 + int(round((13 * BIT_US + BIT_US / 2) * self.us))
            if r is not None and r[0] == 0 and r[1] in self.slaves:
                self.outputs.append((r[1], r[2] & 15))
                self._respond(t_end, self.slaves[r[1]], k)
            self.t0, self.edges = None, []
        return self.led1 << 3, 0


SLAVES = {1: 0x5, 7: 0xA, 21: 0xF, 31: 0x0}
REQS = [request(0, 1, 0x13), request(0, 7, 0x0C), request(0, 21, 0x1F), request(0, 31, 0x00), request(0, 9, 0x05),
        request(0, 7, 0x03), request(0, 1, 0x0A), request(0, 21, 0x10)]


def expect(slaves, reqs, bad=()):
    out = []
    for k, r in enumerate(reqs):
        cb, a = r >> 10, (r >> 5) & 31
        if cb or a not in slaves:
            out.append(0x2000)
        elif k in bad:
            out.append(0x4000)
        else:
            out.append(0x8000 | slaves[a])
    return out


def assemble(**equ):
    return ff.assemble("asi.s", **equ)


def model_run(line, reqs, **equ):
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
    bench = ff.Bench(m, inputs=line)
    bench.send(0, *reqs)
    for _ in range(len(reqs) * 3 + 4):
        if len(bench.words(0)) >= len(reqs):
            break
        bench.run(5_000)
    bench.run(2_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_asi_words(dut):
    """Memory words of fw/asi.s."""
    size = assemble().size
    dut._log.info(f"fw/asi.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_asi_model(dut):
    """fw/asi.s in the reference model against the transceiver and slave model: 8 requests (4 slaves, an absent
    address) at master pauses 2, 3, 6 and 10 bit times and response bit times -1 % and +1 %; a planted parity error and
    a distorted response; negative controls for the bit time and the response time-out."""
    log = dut._log
    want = expect(SLAVES, REQS)
    outs = [((r >> 5) & 31, r & 15) for r in REQS if (r >> 5) & 31 in SLAVES]
    for pause, rate in ((3, 1.0), (2, 1.0), (6, 1.0), (10, 1.0), (3, 0.99), (3, 1.01), (10, 1.01)):
        line = AsiLine(SLAVES, pause_bits=pause, rate=rate)
        bench, m = model_run(line, REQS)
        label = f"master pause {pause} bit times, response bit time x {rate}"
        assert not m.late, f"{label}: late events {m.late}"
        assert not line.errors, f"{label}: {line.errors[:3]}"
        assert bench.words(0) == want, f"{label}: {[hex(w) for w in bench.words(0)]}, expected {[hex(w) for w in want]}"
        assert line.outputs == outs, f"{label}: slaves received {line.outputs}"
        ff.assert_status_clean(m, [0])
        ird = [c for c, n, v in line.log if n == "ird"]
        log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}; slaves kept the outputs {outs}; IRD edges "
                 f"within {line.dev_us * 1000:.0f} ns of the 3 us grid; request {(ird[1] - ird[0]) / F_MHZ:.2f} us "
                 f"start to first bit edge" if pause == 3 and rate == 1.0 else f"{label}: host words as expected")
    line = AsiLine(SLAVES, bad_parity={1}, distort={2})
    bench, m = model_run(line, REQS[:4])
    got, want4 = bench.words(0), [0x8005, 0x4000, 0x4000, 0x8000]
    assert not line.errors and got == want4, f"faults: {line.errors[:3]} {[hex(w) for w in got]}"
    ff.assert_status_clean(m, [0])
    log.info(f"planted response parity error (request 2) and distorted LED1 (request 3): {[hex(w) for w in got]}")
    for equ, text in ((dict(HB=170), "No_information_error"), (dict(TRESP=1500), "words")):
        line = AsiLine(SLAVES)
        bench, m = model_run(line, REQS[:3], **equ)
        found = [e for e in line.errors if text in e]
        if text == "words" and bench.words(0) != want[:3]:
            found = [f"host words {[hex(w) for w in bench.words(0)]} (expected {[hex(w) for w in want[:3]]})"]
        log.info(f"control {equ}: {found[0][:160] if found else 'no fault found'}")
        assert found, f"control {equ}: expected '{text}', got {line.errors[:2]} {bench.words(0)}"


@cocotb.test()
async def test_asi_rtl(dut):
    """fw/asi.s on the chip at 60 MHz against the transceiver and slave model: three requests (two slaves, one absent
    address), in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl asi"
    reqs = [REQS[1], REQS[4], REQS[2]]
    want = expect(SLAVES, reqs)
    line = AsiLine(SLAVES)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners(PINS), inputs=line, label=label)
    bench.send(0, *reqs)
    if hasattr(dut, "dump_off"):  # waveform of the first request and its response (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 12_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; edges (ps, line, level) "
                 f"{[(round(now - (bench.m.cycle - c) * rf.CLK_PS), n, v) for c, n, v in line.log[:40]]}")
    await bench.collect(0, len(want), limit=100_000)
    await bench.run(2000)
    await bench.finish(log)
    assert not line.errors and bench.words(0) == want, \
        f"{label}: {line.errors[:3]} {[hex(w) for w in bench.words(0)]}, expected {[hex(w) for w in want]}"
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}; slaves kept {line.outputs}")

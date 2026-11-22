# SPDX-License-Identifier: Apache-2.0
"""IO-Link Device firmware (fw/iolink.s, COM2, M-sequence TYPE_0) against an independent Master model, in the reference
model and on the RTL at 60 MHz.

What it does:
  The Master model (IolMaster) is written from the IO-Link Interface and System Specification V1.1.3 (June 2019,
  io-link.com): wake-up request 5.3.3.3 and Table 10 (TWU 75 to 85 us, TREN <= 500 us), UART frame Figure 21 (start,
  8 data bits LSB first, even parity, stop), Table 9 (COM2 38.4 kbit/s, Master rate +/- 0.1 %, receiver detection
  window: each bit n = 1 .. 11 of a frame shall have its level from (n - 0.65) TBIT to (n - 0.22) TBIT after the start
  of the start bit), M-sequence control A.1.2, check/type A.1.3, checksum/status A.1.5, checksum A.1.6 with seed 52h and
  the 8-to-6 bit equations (A.1), TYPE_0 A.2.2, timing A.3.3 to A.3.5 (t1 <= 1 TBIT, t2 <= 3 TBIT, 1 <= tA <= 10 TBIT),
  and B.1 for the page 1 parameters. It does not use the firmware, the assembler or the processor model. At the
  transceiver's logic pins the Master drives RX (the wake-up request as a low pulse, a stated assumption: the logic
  form depends on the transceiver), and reads TX while EN is 1. It checks every Device frame against the detection
  window, parity and checksum, tA and t2, that EN is 1 for the whole reply and 0 while the Master sends, and that a
  message with a wrong checksum or parity gets no reply.
  The model test runs the start-up session (MinCycleTime, M-sequence capability, RevisionID, VendorID, MasterCycleTime
  and MasterCommand writes, read-back, diagnosis channel read, ISDU channel write, a wrong checksum, a parity error,
  DeviceID) at Master rates -0.1 % and +0.1 %, t1 = 0 and 1 TBIT and wake-up pulses of 75 and 85 us; negative controls
  change tA, the Device bit time and the checksum seed. The RTL test runs the wake-up, a read, a write and the
  read-back in lockstep with the reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  Twenty-second row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode decoder
  at the pinned commit decodes SDCI, so the row has no sigrok scenario.

Conditions: logic simulation at 60 MHz at the transceiver's logic pins with ideal pins; the C/Q line (24 V levels,
slew rate, the wake-up current pulse) and the transceiver are not simulated.
"""

import bisect
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
P_TX, P_EN, P_RX = 12, 13, 11  # UO4, UO5 (output pins), UI3 (input pin)
TBIT = F_MHZ * 1e6 / 38400  # 1562.5 cycles (Table 9: 26.04 us)
R_DET, S_DET = 0.65, 0.22  # Table 9
SEED = 0x52


def cmp6(c8):
    """8-to-6 bit compression, equations (A.1)."""
    d = [(c8 >> k) & 1 for k in range(8)]
    out = [d[1] ^ d[0], d[3] ^ d[2], d[5] ^ d[4], d[7] ^ d[6], d[6] ^ d[4] ^ d[2] ^ d[0], d[7] ^ d[5] ^ d[3] ^ d[1]]
    return sum(b << k for k, b in enumerate(out))


def checksum(octets):
    x = SEED
    for o in octets:
        x ^= o
    return cmp6(x)


def master_msg(mc, od=None, mtype=0, bad_ck=False):
    body = [mc, mtype << 6] + ([] if od is None else [od])
    ck = checksum(body) ^ (1 if bad_ck else 0)
    return [mc, mtype << 6 | ck] + ([] if od is None else [od])


class IolMaster:
    """SDCI Master at the transceiver logic pins (see the module text). rate: Master bit-rate error (Table 9: +/- 0.1 %);
    t1: gap between Master frames in bit times; wurq_us: wake-up pulse; steps: (message octets, parity error octet
    index or None) sent every period_us from first_us on."""

    def __init__(self, steps, us=F_MHZ, rate=0.0, t1=0.0, wurq_at=6000, wurq_us=80.0, first_us=900.0, period_us=2000.0):
        self.us = us
        self.tm = TBIT / (1 + rate)
        self.rx = [(0, 1)]  # (cycle, level) of the Master's drive of RX
        self.msgs = []  # (start cycle, end of last stop bit, octets)
        w0 = wurq_at
        self.rx += [(w0, 0), (w0 + round(wurq_us * us), 1)]
        t = w0 + round(first_us * us)
        for octets, bad_par in steps:
            start = t
            for k, o in enumerate(octets):
                bits = [0] + [(o >> b) & 1 for b in range(8)] + [bin(o).count("1") & 1 ^ (1 if bad_par == k else 0), 1]
                for b, v in enumerate(bits):
                    self.rx.append((round(t + b * self.tm), v))
                t += 11 * self.tm
                if k < len(octets) - 1:
                    t += t1 * self.tm
            self.msgs.append((round(start), round(t), list(octets)))
            t = start + period_us * us
        self.end = round(t)
        self.rx_t = [c for c, _ in self.rx]
        self.tx, self.en = [], []  # (cycle, level) edges of TX and EN
        self.last = None

    def rx_level(self, c):
        return self.rx[bisect.bisect_right(self.rx_t, c) - 1][1]

    def __call__(self, c, uo, uio, oe):
        tx, en = (uo >> 4) & 1, (uo >> 5) & 1
        if self.last != (tx, en):
            if self.last is None or self.last[0] != tx:
                self.tx.append((c, tx))
            if self.last is None or self.last[1] != en:
                self.en.append((c, en))
            self.last = (tx, en)
        return self.rx_level(c) << 3, 0

    def _level(self, edges, c):
        i = bisect.bisect_right([e[0] for e in edges], c) - 1
        return edges[i][1] if i >= 0 else 1

    def analyse(self, wurq_at=6000):
        """Replies to each Master message and the faults found (detection window, parity, checksum, tA, t2, EN)."""
        errors, replies = [], []
        en_on = [(c, v) for c, v in self.en if c > wurq_at]
        for k, (start, end, octets) in enumerate(self.msgs):
            nxt = self.msgs[k + 1][0] if k + 1 < len(self.msgs) else self.end
            for c, v in en_on:  # EN must be 0 while the Master sends
                if start <= c < end:
                    errors.append(f"message {k}: EN changes to {v} at cycle {c} while the Master sends")
            if self._level(self.en, start) == 1 and start > wurq_at:
                errors.append(f"message {k}: EN is 1 at the Master's start bit")
            falls = [c for c, v in self.tx if v == 0 and end <= c < nxt]
            frames, f_end = [], None
            while falls:
                f = falls[0]
                if f_end is not None and f - f_end > 3 * TBIT + 1:
                    errors.append(f"message {k}: t2 {(f - f_end) / TBIT:.2f} TBIT (A.3.4: <= 3)")
                bits = []
                for n in range(1, 12):
                    a, b = f + (n - R_DET) * TBIT, f + (n - S_DET) * TBIT
                    lv = {self._level(self.tx, int(a))} | {self._level(self.tx, int(x[0])) for x in self.tx
                                                             if a <= x[0] <= b}
                    if len(lv) != 1:
                        errors.append(f"message {k}: Device frame at cycle {f}: bit {n} changes inside the detection "
                                      f"window {n - R_DET:.2f} to {n - S_DET:.2f} TBIT (Table 9)")
                    bits.append(lv.pop() if len(lv) == 1 else -1)
                    if any(self._level(self.en, int(x)) != 1 for x in (a, b)):
                        errors.append(f"message {k}: EN is 0 inside Device frame bit {n}")
                if bits[0] != 0 or bits[10] != 1:
                    errors.append(f"message {k}: frame at {f}: start {bits[0]} stop {bits[10]}")
                val = sum(b << i for i, b in enumerate(bits[1:9]) if b > 0)
                if (sum(b for b in bits[1:10] if b > 0)) & 1:
                    errors.append(f"message {k}: Device frame {len(frames)} parity error")
                frames.append(val)
                f_end = f + 11 * TBIT
                falls = [c for c in falls if c > f + 10.5 * TBIT]
            if frames:
                ta = (self.tx[[c for c, v in self.tx].index([c for c, v in self.tx if v == 0 and c >= end][0])][0]
                      - end) / TBIT
                if not 1.0 - 1e-6 <= ta <= 10.0 + 1e-6:
                    errors.append(f"message {k}: tA {ta:.2f} TBIT (A.3.5: 1 to 10)")
                cks = frames[-1]
                if cks & 0x3F != checksum(frames[:-1] + [cks & 0xC0]):
                    errors.append(f"message {k}: Device checksum {cks & 0x3F:02x}, expected "
                                  f"{checksum(frames[:-1] + [cks & 0xC0]):02x}")
                ens = [c for c, v in self.en if v == 0 and c > end and c < nxt]
                if not ens or ens[0] > f_end + TBIT:
                    errors.append(f"message {k}: EN stays 1 after the reply")
            replies.append(frames)
        return replies, errors


# start-up session: (master octets, parity-error octet), expected reply octets without CKS (None = no reply)
CONFIG = [0x02 << 8 | 0x17, 0x04 << 8 | 0x11, 0x07 << 8 | 0x01, 0x08 << 8 | 0x23, 0x09 << 8 | 0x45,
          0x0A << 8 | 0x67, 0x8000 | 0x0B << 8 | 0x89]
SESSION = [
    (master_msg(0xA2), None, [0x17]),  # read MinCycleTime
    (master_msg(0xA3), None, [0x00]),  # read M-sequence capability
    (master_msg(0xA4), None, [0x11]),  # read RevisionID
    (master_msg(0xA7), None, [0x01]),  # VendorID 1
    (master_msg(0xA8), None, [0x23]),  # VendorID 2
    (master_msg(0x21, 0x17), None, []),  # write MasterCycleTime
    (master_msg(0x20, 0x95), None, []),  # write MasterCommand MasterIdent
    (master_msg(0xA1), None, [0x17]),  # read back MasterCycleTime
    (master_msg(0xC0), None, [0x00]),  # read the diagnosis channel: 0
    (master_msg(0x60, 0x12), None, []),  # write to the ISDU channel: ignored, CKS reply
    (master_msg(0xA2, bad_ck=True), None, None),  # wrong checksum: no reply
    (master_msg(0xA2), 0, None),  # parity error in MC: no reply
    (master_msg(0xA9), None, [0x45]),  # DeviceID 1
]
HOST = [0x8117, 0x8095, 0x40A2, 0x2000]


def assemble(**equ):
    return ff.assemble("iolink.s", **equ)


def model_run(master, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners({P_TX: 0, P_EN: 0}))
    m.late = []
    event_cycle = m._event_cycle

    def watch(th, c, t):
        d = (t - c) & 0xFFFF
        if not pe_model.LEAD <= d < pe_model.HORIZON and len(m.late) < 5:
            m.late.append((c, d - 0x10000 if d & 0x8000 else d, th.pc, prog.source[th.pc][1].strip()))
        return event_cycle(th, c, t)

    m._event_cycle = watch
    bench = ff.Bench(m, inputs=master)
    bench.send(0, *CONFIG)
    bench.run(master.end + 2000)
    return bench, m


def expect_check(label, replies, errors, words, session=SESSION, host=HOST):
    want = [r for _, _, r in session]
    got = [None if not f else f[:-1] for f in replies]
    assert not errors, f"{label}: {errors[:3]}"
    assert got == want, f"{label}: replies {got}, expected {want}"
    assert words == host, f"{label}: host words {[hex(w) for w in words]}, expected {[hex(w) for w in host]}"


@cocotb.test(skip=GL)
async def test_iolink_words(dut):
    """Memory words of fw/iolink.s."""
    size = assemble().size
    dut._log.info(f"fw/iolink.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_iolink_model(dut):
    """fw/iolink.s in the reference model against the Master model: the start-up session at the Master corners;
    negative controls for tA, the Device bit time and the checksum seed."""
    log = dut._log
    steps = [(o, p) for o, p, _ in SESSION]
    for rate, t1, wurq in ((0.0, 0.0, 80.0), (-0.001, 1.0, 75.0), (0.001, 0.0, 85.0), (0.001, 1.0, 75.0)):
        master = IolMaster(steps, rate=rate, t1=t1, wurq_us=wurq)
        bench, m = model_run(master)
        replies, errors = master.analyse()
        label = f"Master rate {rate * 100:+.1f} %, t1 {t1} TBIT, wake-up {wurq} us"
        assert not m.late, f"{label}: late events {m.late}"
        expect_check(label, replies, errors, bench.words(0))
        ff.assert_status_clean(m, [0])
        falls = [c for c, v in master.tx if v == 0]
        ta = (falls[0] - master.msgs[0][1]) / TBIT
        log.info(f"{label}: {len(SESSION)} M-sequences as expected (replies {[f for f in replies]}), host words "
                 f"{[hex(w) for w in bench.words(0)]}, first tA {ta:.3f} TBIT")
    for equ, text in ((dict(TAN=12), "tA"), (dict(TBT=1625), "detection window"), (dict(SEED=0x53), "replies")):
        master = IolMaster(steps[:3])
        bench, m = model_run(master, **equ)
        replies, errors = master.analyse()
        found = [e for e in errors if text in e]
        got = [None if not f else f[:-1] for f in replies]
        if text == "replies" and got != [r for _, _, r in SESSION[:3]]:
            found = [f"replies {got}"]
        log.info(f"control {equ}: {found[0][:160] if found else 'no fault found'}")
        assert found, f"control {equ}: expected a fault with '{text}', got {errors[:2]} {got}"


@cocotb.test()
async def test_iolink_rtl(dut):
    """fw/iolink.s on the chip at 60 MHz against the Master model: wake-up, read MinCycleTime, write MasterCycleTime,
    read it back, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl iolink"
    session = [SESSION[0], SESSION[5], SESSION[7]]
    wurq_at = rf.HOST_START + 60_000
    master = IolMaster([(o, p) for o, p, _ in session], wurq_at=wurq_at, period_us=1800.0)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_TX: 0, P_EN: 0}), inputs=master, label=label)
    bench.send(0, *CONFIG)
    if hasattr(dut, "dump_off"):  # waveform of the wake-up and the first M-sequence (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(master.msgs[0][1] + 6 * int(TBIT) + 2000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        to_ps = lambda c: round(now - (bench.m.cycle - c) * rf.CLK_PS)  # noqa: E731
        log.info(f"{label}: wake-up {to_ps(wurq_at)} ps; first Master frame {to_ps(master.msgs[0][0])} ps, end of the "
                 f"message {to_ps(master.msgs[0][1])} ps; TX edges {[(to_ps(c), v) for c, v in master.tx[:12]]}; EN "
                 f"edges {[(to_ps(c), v) for c, v in master.en[:4]]}")
    await bench.run(master.end + 2000 - bench.m.cycle)
    await bench.finish(log)
    replies, errors = master.analyse(wurq_at=wurq_at)
    expect_check(label, replies, errors, bench.words(0), session=session, host=[0x8117])
    log.info(f"{label}: replies {replies}, host words {[hex(w) for w in bench.words(0)]}")

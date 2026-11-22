# SPDX-License-Identifier: Apache-2.0
"""MIL-STD-1553B bus controller transmitter (fw/mil1553_bc.s) and remote terminal receiver (fw/mil1553_rt.s) against
an independent bus model, in the reference model and on the RTL at 60 MHz.

What it does:
  The bus model is written from MIL-STD-1553B with Notice 2 as quoted paragraph by paragraph in the MIL-STD-1553
  Designer's Guide (Data Device Corporation, section "Review and rationale of MIL-STD-1553 A and B" and the Notice 2
  pages): Manchester II bi-phase 4.3.3.2 (a 1 is positive then negative), 1.0 Mbit/s +/- 0.1 % 4.3.3.3, 20-bit words
  4.3.3.4, command/status sync (positive 1.5 us, negative 1.5 us) and data sync (the inverse) 4.3.3.5.1.1 and
  4.3.3.5.2.1, odd parity 4.3.3.5.1.6, bit order MSB first 4.3.2, intermessage gap >= 4 us 4.3.3.7, transmitter zero
  crossing deviation <= 25 ns 4.5.2.1.1.2, receiver input zero crossing deviation +/- 150 ns from the previous crossing
  4.5.2.2.2.1, word validation 4.4.1.1. It does not use the firmware, the assembler or the processor model.
  Transmitter check (Bc1553Monitor): every TX crossing while INH is 0 lies on the 0.5 us grid of the first sync edge
  within 1 cycle, each word decodes from that grid with a valid sync, Manchester bits and odd parity, words are
  contiguous, the transmitter is off outside the message and messages are at least 4 us apart.
  Receiver stimulus (Bus1553): words of messages as RXA/RXB levels with each crossing moved by -4 to +5 cycles from the
  ideal grid (so each interval differs from the ideal by up to 9 cycles = 150 ns), at bit rates 0.999 and 1.001
  Mbit/s, with planted parity, Manchester and sync errors.
  The RTL tests run a short message of each firmware in lockstep with the reference model; the same tests run on the
  gate-level netlist (gl_fw workflow).

Why:
  Twenty-third row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012); flex/candidates.csv marked
  the 1 Mbit/s receive as \"feasibility not verified\", which the receiver test answers. No libsigrokdecode decoder at the
  pinned commit decodes 1553, so the row has no sigrok scenario.

Conditions: logic simulation at 60 MHz at the transceiver's logic pins with ideal pins; the bus (transformer coupling,
stubs, 6 to 9 V levels, 100 to 300 ns rise times) and the transceiver are not simulated; the receiver test models the
+/-150 ns deviation at the logic outputs, where a real receiver adds its own delay skew.
"""

import os
import random
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
HALF = 30  # cycles in 0.5 us
P_TX, P_INH = 12, 13  # UO4, UO5


def parity(v):
    return (bin(v & 0xFFFF).count("1") + 1) & 1  # odd parity bit


def halves(value, cmd, fault=None):
    """Half-bit levels (+1 positive, -1 negative) of one word: 6 sync halves, 16 bits MSB first, parity."""
    out = [1, 1, 1, -1, -1, -1] if cmd else [-1, -1, -1, 1, 1, 1]
    bits = [(value >> (15 - k)) & 1 for k in range(16)] + [parity(value) ^ (1 if fault == "par" else 0)]
    for k, b in enumerate(bits):
        out += [1, -1] if b else [-1, 1]
    if isinstance(fault, tuple) and fault[0] == "man":
        k = 6 + 2 * fault[1]
        out[k + 1] = out[k]
    if fault == "sync":
        out[:6] = [1] * 6
    return out


# ----- transmitter check -----

class Bc1553Monitor:
    """Records TX and INH and decodes the transmitted messages (see the module text)."""

    def __init__(self):
        self.tx, self.inh = [], []
        self.last = None

    def __call__(self, c, uo, uio, oe):
        tx, inh = (uo >> 4) & 1, (uo >> 5) & 1
        if self.last != (tx, inh):
            if self.last is None or self.last[0] != tx:
                self.tx.append((c, tx))
            if self.last is None or self.last[1] != inh:
                self.inh.append((c, inh))
            self.last = (tx, inh)
        return 0, 0

    def decode(self, start_after=0):
        errors, messages = [], []
        on = [(c, v) for c, v in self.inh if c > start_after]
        periods = []
        for i, (c, v) in enumerate(on):
            if v == 0:
                end = next((c2 for c2, v2 in on[i + 1:] if v2 == 1), None)
                periods.append((c, end))
        prev_end = None
        for a, b in periods:
            if b is None:
                errors.append(f"transmitter on at {a} and never off")
                continue
            edges = [(c, v) for c, v in self.tx if a < c <= b]
            if not edges:
                errors.append(f"transmitter on {a}..{b} with no signal")
                continue
            t0 = edges[0][0]
            for c, v in edges:
                dev = (c - t0) % HALF
                dev = dev - HALF if dev > HALF // 2 else dev
                if abs(dev) > 1:
                    errors.append(f"crossing at {c} is {dev} cycles off the 0.5 us grid (4.5.2.1.1.2: 25 ns)")
                    break
            nwords = round((b - t0) / (40 * HALF))
            if abs((b - t0) - nwords * 40 * HALF) > 1:
                errors.append(f"message {a}: transmitter off {b - t0} cycles after the first sync edge, not at the end "
                              f"of a word")
            lv = lambda t: ff.value_at(edges, t) if t >= t0 else 0  # noqa: E731
            words = []
            for w in range(nwords):
                base = t0 + w * 40 * HALF
                h = [lv(base + k * HALF + HALF // 2) for k in range(40)]
                if h[:6] == [1, 1, 1, 0, 0, 0]:
                    cmd = True
                elif h[:6] == [0, 0, 0, 1, 1, 1]:
                    cmd = False
                else:
                    errors.append(f"message {a} word {w}: sync {h[:6]}")
                    cmd = None
                bits = []
                for k in range(17):
                    p = (h[6 + 2 * k], h[7 + 2 * k])
                    if p not in ((1, 0), (0, 1)):
                        errors.append(f"message {a} word {w} bit {k}: halves {p} (Manchester II, 4.3.3.2)")
                    bits.append(p[0])
                val = sum(bv << (15 - k) for k, bv in enumerate(bits[:16]))
                if (sum(bits) & 1) != 1:
                    errors.append(f"message {a} word {w}: even parity (4.3.3.5.1.6: odd)")
                words.append((val, cmd))
            mid_sync = t0 + 3 * HALF
            if prev_end is not None and (mid_sync - prev_end) < 4 * F_MHZ:
                errors.append(f"message gap {(mid_sync - prev_end) / F_MHZ:.2f} us (4.3.3.7: >= 4 us)")
            prev_end = t0 + nwords * 40 * HALF - HALF  # mid-bit crossing of the last parity bit
            messages.append(words)
        return messages, errors


def bc_words(msg, rtrt=False):
    return [len(msg) | (0x100 if rtrt else 0)] + [v for v, _ in msg]


BC_SESSION = [
    ([(0x0843, True), (0x1234, False), (0xABCD, False), (0x0000, False)], False),  # RT 1 receive, SA 2, 3 words
    ([(0x1022, True), (0x1C22, True)], True),  # RT to RT: receive command to RT 2, transmit command to RT 3
    ([(0x0C20, True)] + [((k * 0x1111) ^ 0x8001, False) for k in range(32)], False),  # 32 data words
    ([(0x0FE2, True)], False),  # mode code command (transmit status word)
]


def bc_run(session, **equ):
    prog = ff.assemble("mil1553_bc.s", **equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners({P_TX: 0, P_INH: 0}))
    mon = Bc1553Monitor()
    bench = ff.Bench(m, inputs=mon)
    for msg, rtrt in session:
        bench.send(0, *bc_words(msg, rtrt))
    for _ in range(40):
        bench.run(5000)
        if len(bench.words(0)) >= len(session):
            break
    bench.run(500)
    return bench, m, mon


# ----- receiver stimulus -----

class Bus1553:
    """Drives RXA (ui_in[3]) and RXB (ui_in[4]) for messages [(start cycle, [(value, cmd, fault)])]."""

    def __init__(self, messages, rate=1.0, seed=1, jitter=(-4, 5)):
        rng = random.Random(seed)
        self.changes = [(0, 0)]
        self.ends = []
        half = HALF / rate
        for start, words in messages:
            seq = [h for v, cmd, fault in words for h in halves(v, cmd, fault)] + [0]
            prev = 0
            for k, lv in enumerate(seq):
                if lv != prev:
                    t = round(start + k * half) + rng.randint(*jitter)
                    self.changes.append((max(t, self.changes[-1][0] + 1), lv))
                    prev = lv
            self.ends.append(round(start + (len(seq) - 1) * half))
        self.times = [c for c, _ in self.changes]

    def __call__(self, c, uo, uio, oe):
        lv = ff.value_at(self.changes, c)
        return (1 << 3 if lv == 1 else 0) | (1 << 4 if lv == -1 else 0), 0


def expect(words):
    out = []
    for v, cmd, fault in words:
        flags = 0x8000 | int(cmd)
        if fault == "par":
            flags |= 8
        if isinstance(fault, tuple):
            flags |= 2
        if fault == "sync":
            flags = 0x8004
            v = 0
        out += [flags, v]
    return out


def rt_run(messages, rate=1.0, seed=1, cycles=None):
    prog = ff.assemble("mil1553_rt.s")
    m = pe_model.PeModel(prog.words)
    bus = Bus1553(messages, rate=rate, seed=seed)
    bench = ff.Bench(m, inputs=bus)
    bench.run(cycles or bus.ends[-1] + 20_000)
    return bench, m, bus


def check_rt(label, bench, messages):
    got = bench.words(0)
    want = []
    for _, words in messages:
        want += expect(words)
    # data of a word with a Manchester error is not defined
    mask = [0 if (i % 2 == 1 and want[i - 1] & 2) else 0xFFFF for i in range(len(want))]
    ok = len(got) == len(want) and all((g & k) == (w & k) for g, w, k in zip(got, want, mask))
    assert ok, f"{label}: host words {[hex(w) for w in got]}, expected {[hex(w) for w in want]}"


RT_MSGS = [
    [(0x0843, True, None), (0x1234, False, None), (0xABCD, False, None), (0x0000, False, None)],
    [(0x0800, True, None)],  # a status-like word alone
    [(0x0C20, True, None)] + [((k * 0x1111) ^ 0x8001, False, None) for k in range(32)],
    [(0x5555, True, None), (0xFFFF, False, "par")],  # parity error
    [(0x0F0F, True, None), (0x00FF, False, ("man", 7))],  # Manchester error in the last word
    [(0x1111, True, "sync")],  # positive for 3 us: no mid-sync edge
]


def rt_schedule(msgs, gap_us=150.0, t0=2000):
    out, t = [], t0
    for words in msgs:
        out.append((t, words))
        t += len(words) * 40 * HALF + gap_us * F_MHZ
    return out


@cocotb.test(skip=GL)
async def test_mil1553_words(dut):
    """Memory words of fw/mil1553_bc.s and fw/mil1553_rt.s."""
    for name in ("mil1553_bc.s", "mil1553_rt.s"):
        size = ff.assemble(name).size
        dut._log.info(f"fw/{name}: {size} words of {pe_model.PROG_WORDS}")
        assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_mil1553_model(dut):
    """Both firmware files in the reference model against the bus model: the transmitter session with its checks and
    controls; the receiver at 0.999 and 1.001 Mbit/s with +/-150 ns crossing deviations, planted errors, and the
    measured shortest gap after a message."""
    log = dut._log
    bench, m, mon = bc_run(BC_SESSION)
    msgs, errors = mon.decode()
    want = [msg for msg, _ in BC_SESSION]
    assert not errors, f"transmitter: {errors[:3]}"
    assert msgs == want, f"transmitter: decoded {msgs}"
    assert bench.words(0) == [0x8000 | len(x) for x in want], [hex(w) for w in bench.words(0)]
    ff.assert_status_clean(m, [0])
    gaps = []
    ends = []
    inh = [(c, v) for c, v in mon.inh if c > 0]
    for i, (c, v) in enumerate(inh):
        if v == 0 and ends:
            gaps.append((c + HALF + 3 * HALF - ends[-1]) / F_MHZ)
        if v == 1 and i:
            ends.append(c - HALF)
    log.info(f"transmitter: {len(msgs)} messages decoded as sent ({[len(x) for x in msgs]} words, syncs "
             f"{[[int(cmd) for _, cmd in x] for x in msgs]}), all crossings on the 0.5 us grid, odd parity, contiguous "
             f"words; gaps mid-bit to mid-sync {[round(g, 2) for g in gaps]} us")
    for equ, text in ((dict(HALF=31), "grid"), (dict(SYNC=60), "sync")):
        bench, m, mon = bc_run(BC_SESSION[:1], **equ)
        msgs, errors = mon.decode()
        found = [e for e in errors if text in e]
        log.info(f"control {equ}: {found[0][:160] if found else 'no fault found'}")
        assert found, f"control {equ}: expected '{text}', got {errors[:2]}"
    for rate, seed in ((0.999, 1), (1.001, 2), (1.0, 3)):
        sched = rt_schedule(RT_MSGS)
        bench, m, bus = rt_run(sched, rate=rate, seed=seed)
        check_rt(f"receiver rate {rate}", bench, sched)
        ff.assert_status_clean(m, [0])
        log.info(f"receiver at {rate:.3f} Mbit/s, crossings -4..+5 cycles (seed {seed}): {len(RT_MSGS)} messages, "
                 f"{len(bench.words(0)) // 2} words reported as expected (parity, Manchester and sync errors flagged)")
    # shortest gap: time from the end of a message to the thread's last host word for it, then a next message at
    # that gap + 1 us is received and one 2 us shorter is not
    sched = rt_schedule(RT_MSGS[:3])
    bench, m, bus = rt_run(sched)
    stamps = [c for c, _ in bench.from_thread[0]]
    counts = [len(w) for _, w in sched]
    idx, proc = 0, []
    for k, n in enumerate(counts):
        idx += 2 * n
        proc.append((stamps[idx - 1] - bus.ends[k]) / F_MHZ)
    per_word = (proc[2] - proc[1]) / (counts[2] - counts[1])
    base = proc[1] - per_word * counts[1]
    log.info(f"receiver busy after a message: {[round(p, 2) for p in proc]} us for {counts} words = {base:.2f} us + "
             f"{per_word:.2f} us a word")
    for extra, should in ((1.0, True), (-2.0, False)):
        gap = proc[2] + extra
        msgs2 = [(2000, RT_MSGS[2]), (2000 + 33 * 40 * HALF + round(gap * F_MHZ), RT_MSGS[1])]
        bench, m, bus = rt_run(msgs2)
        got = bench.words(0)
        second = got[66:] == expect(RT_MSGS[1])
        log.info(f"next message {gap:.2f} us after a 33-word message: {'received' if second else 'missed'}")
        assert second == should, f"gap {gap:.2f} us: words {[hex(w) for w in got[66:]]}"


@cocotb.test()
async def test_mil1553_bc_rtl(dut):
    """fw/mil1553_bc.s on the chip at 60 MHz: one 4-word message, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl mil1553 bc"
    mon = Bc1553Monitor()
    bench = rf.RtlBench(dut, ff.assemble("mil1553_bc.s").words, owner=ff.owners({P_TX: 0, P_INH: 0}), inputs=mon,
                        label=label)
    msg, rtrt = BC_SESSION[0]
    bench.send(0, *bc_words(msg, rtrt))
    if hasattr(dut, "dump_off"):  # waveform of the message (tb.fst), for review
        dut.dump_off.value = 0
    await bench.collect(0, 1, limit=200_000)
    await bench.run(500)
    if hasattr(dut, "dump_off"):
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        to_ps = lambda c: round(now - (bench.m.cycle - c) * rf.CLK_PS)  # noqa: E731
        log.info(f"{label}: INH edges {[(to_ps(c), v) for c, v in mon.inh[:4]]}; TX edges "
                 f"{[(to_ps(c), v) for c, v in mon.tx[:14]]}")
    await bench.finish(log)
    msgs, errors = mon.decode(start_after=rf.HOST_START)
    assert not errors and msgs == [msg], f"{label}: {errors[:3]} {msgs}"
    log.info(f"{label}: message decoded {msgs}; host words {[hex(w) for w in bench.words(0)]}")


@cocotb.test()
async def test_mil1553_rt_rtl(dut):
    """fw/mil1553_rt.s on the chip at 60 MHz: a 4-word message and a 2-word message with a parity error, crossings
    -4..+5 cycles, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl mil1553 rt"
    t0 = rf.HOST_START + 2000
    msgs = [RT_MSGS[0], RT_MSGS[3]]
    sched = rt_schedule(msgs, gap_us=60.0, t0=t0)
    bus = Bus1553(sched, seed=5)
    bench = rf.RtlBench(dut, ff.assemble("mil1553_rt.s").words, owner=ff.owners({}), inputs=bus, label=label)
    if hasattr(dut, "dump_off"):  # waveform of the first word (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(t0 + 60 * HALF)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        to_ps = lambda c: round(now - (bench.m.cycle - c) * rf.CLK_PS)  # noqa: E731
        log.info(f"{label}: bus changes (ps, level) {[(to_ps(c), v) for c, v in bus.changes[1:16]]}")
    want = expect(msgs[0]) + expect(msgs[1])
    await bench.collect(0, len(want), limit=200_000)
    await bench.run(500)
    await bench.finish(log)
    check_rt(label, bench, sched)
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}")

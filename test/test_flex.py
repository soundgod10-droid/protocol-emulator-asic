# SPDX-License-Identifier: Apache-2.0
"""Flexibility tests: firmware for protocols that the instruction set was not designed for.

What it does:
  These cocotb tests assemble firmware for CAN, JTAG, SWD and PS/2 (and the timed trigger and the
  I2C device characterization), run it in the reference model (model/pe_model.py) against protocol
  models, and measure the word count and the highest rate that is correct at the declared 50 MHz
  clock. The tests do not use the design under test.

Why:
  docs/isa.md was designed with UART, SPI and I2C firmware in view. A protocol written later from its
  specification alone, with the same instruction set and assembler, shows how far the chip can be
  reprogrammed. Each protocol model in this file is written from the cited specification pages, not
  from the firmware or the processor model; it imports only the generic bench helpers of
  test_fw_full.py (host words, pin watches).
"""

import os
import sys
from collections import deque

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402
from test_fw_full import Bench, level, owners, source  # noqa: E402

GL = os.environ.get("GATES") == "yes"  # the gate-level job also runs this module; it has no DUT use
F_CLK = 50e6  # declared clock (info.yaml)


def assemble(name, **equ):
    return pe_asm.assemble(source(name, **equ), name, 0)


def od(pins):
    return [p in pins for p in range(pe_model.N_OUT)]


# ----- CAN 2.0A -----
# Bosch "CAN Specification Version 2.0" (1991), Part A: frame fields pages 11 to 14, interframe space 19,
# bit stuffing 22, errors 23, bit timing and synchronization 27 to 30.

P_TXA, P_TXB, UI_CRX = 4, 5, 4  # TXA = uio[4] (thread 0), TXB = uio[5] (thread 1), CAN_RX = ui_in[4]


def can_crc(bits):
    """CRC-15 (Part A pages 13, 14): CRC_RG from 0; CRCNXT = bit xor CRC_RG(14); shift left; xor 4599hex."""
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def can_bits(ident, data=(), rtr=False, dlc=None, bad_crc=False):
    """Destuffed bits from SOF to the last CRC bit (Part A pages 11 to 14)."""
    dlc = len(data) if dlc is None else dlc
    bits = [0] + [(ident >> (10 - i)) & 1 for i in range(11)] + [int(rtr), 0, 0]
    bits += [(dlc >> (3 - i)) & 1 for i in range(4)]
    if not rtr:
        for byte in data:
            bits += [(byte >> (7 - i)) & 1 for i in range(8)]
    crc = can_crc(bits) ^ int(bad_crc)
    return bits + [(crc >> (14 - i)) & 1 for i in range(15)]


def can_stuff(bits):
    """Bit stuffing (Part A page 22): after 5 equal bits a complementary bit; the stuff bit starts a new run."""
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            last, run = 1 - b, 1
    return out


def bits_value(bits):
    return int("".join(map(str, bits)), 2) if bits else 0


class CanNode:
    """CAN node model (Part A pages 11 to 14, 19, 22, 23, 27 to 31), written from the specification.

    bt: bit time in cycles (a float: the node's own oscillator), sp: sample point in cycles after the start
    of the bit, sjw: resynchronization jump width in cycles. The node sends the frames of `frames` (dicts for
    can_bits) when the bus is idle and c >= start (or in a SOF of another node while it waits, page 31 [2]),
    receives every frame, sends a dominant ACK slot when the CRC matches, loses arbitration on a recessive
    bit read dominant in the arbitration field, sends a frame again until it is acknowledged, and records
    received frames, its send results and every error it detects.
    """

    def __init__(self, bt, sp, sjw=4, frames=(), start=0, name="node"):
        self.bt, self.sp, self.sjw, self.name = bt, sp, sjw, name
        self.pending = deque(frames)
        self.start = start
        self.tx = 1
        self.prev_bus = 1
        self.state = "idle"
        self.received, self.results, self.errors = [], [], []

    def level(self, c):
        return self.tx

    def _begin(self, start, send):
        self.bitstart, self.shift, self.sampled, self.synced = start, 0, False, False
        self.state = "frame"
        self.dbits, self.run, self.last, self.stuff_next = [], 0, None, False
        self.total, self.tail, self.crc_ok, self.last_sample = None, None, None, 1
        self.sending = send
        if send:
            self.stream = can_stuff(can_bits(**self.pending[0])) + [1] * 13  # delimiters, ACK, EOF, intermission
            self.idx = 0
            self.cur = self.stream[0]
        else:
            self.cur = 1

    def see(self, c, bus):
        fall = self.prev_bus == 1 and bus == 0
        self.prev_bus = bus
        if self.state == "idle":
            if fall:  # hard synchronization on SOF (pages 29, 30 rule 3)
                self._begin(c, bool(self.pending and c >= self.start))
            elif self.pending and c >= self.start:
                self._begin(c + 1, True)
                self.tx = self.cur
                return
            else:
                return
        if fall and not self.synced and self.last_sample == 1 and c > self.bitstart:
            e = c - self.bitstart
            if e < self.sp:  # positive phase error: lengthen PHASE_SEG1 by at most SJW
                if not (self.sending and self.cur == 0):  # page 30 rule 4: not on its own dominant edge
                    self.bitstart += min(e, self.sjw)
                    self.synced = True
            else:  # edge after the sample point: shorten PHASE_SEG2 by at most SJW
                self.shift = min(self.bitstart + self.bt - c, self.sjw)
                self.synced = True
        if not self.sampled and c >= self.bitstart + self.sp:
            self.sampled = True
            self.last_sample = bus
            self._bit(bus)
        if self.state != "idle" and c + 1 >= self.bitstart + self.bt - self.shift:
            self.bitstart += self.bt - self.shift
            self.shift, self.sampled, self.synced = 0, False, False
            self._next()
        self.tx = self.cur if self.state != "idle" else 1

    def _bit(self, v):
        if self.sending and v != self.cur:
            if self.state == "frame" and self.cur == 1 and v == 0 and len(self.dbits) <= 12 and not self.stuff_next:
                self.sending = False  # lost arbitration (page 23, bit error exception)
                self.results.append("lost")
            elif self.tail != 1:  # a dominant ACK slot is not a bit error
                self.errors.append(f"bit error at bit {len(self.dbits)}, tail {self.tail}")
                self.sending = False
        if self.state == "frame":
            if self.stuff_next:
                if v == self.last:
                    self.errors.append(f"stuff error after bit {len(self.dbits)}")
                self.stuff_next, self.last, self.run = False, v, 1
                if self.total is not None and len(self.dbits) == self.total:
                    self.state, self.tail = "tail", -1
                return
            self.dbits.append(v)
            self.run = self.run + 1 if v == self.last else 1
            self.last = v
            if self.run == 5:
                self.stuff_next = True
            n = len(self.dbits)
            if n == 19:
                self.total = 19 + (0 if self.dbits[12] else 8 * min(bits_value(self.dbits[15:19]), 8)) + 15
            if self.total is not None and n == self.total:
                self.crc_ok = can_crc(self.dbits[:-15]) == bits_value(self.dbits[-15:])
                if not self.stuff_next:
                    self.state, self.tail = "tail", -1
            return
        # fixed form (page 14): 0 CRC delimiter, 1 ACK slot, 2 ACK delimiter, 3 to 9 EOF, 10 to 12 intermission
        t = self.tail
        if t == 1:
            if self.sending:
                self.results.append("ack" if v == 0 else "no ack")
        elif v != 1 and t < 10:
            self.errors.append(f"form error at tail bit {t}")
        if t == 2:
            self.received.append(dict(can_decode(self.dbits), crc_ok=self.crc_ok))
            if self.sending and self.results and self.results[-1] == "ack":
                self.pending.popleft()

    def _next(self):
        if self.sending:
            self.idx += 1
        if self.state == "tail":
            self.tail += 1
            if self.tail == 13:
                self.state, self.cur = "idle", 1
                return
        if self.sending:
            self.cur = self.stream[self.idx]
        else:
            self.cur = 0 if (self.state == "tail" and self.tail == 1 and self.crc_ok) else 1


def can_decode(bits):
    n = 0 if bits[12] else min(bits_value(bits[15:19]), 8)
    return dict(ident=bits_value(bits[1:12]), rtr=bits[12], dlc=bits_value(bits[15:19]),
                data=[bits_value(bits[19 + 8 * k:27 + 8 * k]) for k in range(n)])


class CanBus:
    """Wired-AND bus (dominant 0) of the chip pins TXA, TXB (open drain, joined on the board) and the model
    nodes; the chip CAN_RX input gets the bus level `delay` cycles later (transceiver loop delay)."""

    def __init__(self, nodes, delay=0):
        self.nodes, self.delay = nodes, delay
        self.hist = deque([1] * (delay + 1), maxlen=delay + 1)
        self.changes = [(0, 1)]

    def __call__(self, c, uo, uio, oe):
        bus = 0 if 0 in (level(uio, oe, P_TXA), level(uio, oe, P_TXB)) else 1
        for n in self.nodes:
            bus &= n.level(c)
        for n in self.nodes:
            n.see(c, bus)
        if bus != self.changes[-1][1]:
            self.changes.append((c, bus))
        self.hist.append(bus)
        return self.hist[0] << UI_CRX, 0


def can_tx_words(f):
    """Host words of thread 0 for one frame (M, then the stuffed stream with CRC delimiter and ACK slot, filled
    with 1), and the bit of the ACK slot in the status word of the last stream word."""
    stream = can_stuff(can_bits(**f)) + [1, 1]
    ack = len(stream) - 1
    stream += [1] * (-len(stream) % 16)
    words = [bits_value(stream[i:i + 16]) for i in range(0, len(stream), 16)]
    return [len(words) - 1] + words, 15 - ack % 16


def can_rx_parse(words):
    """Frames from the words of thread 1: (destuffed bits, CRC word) for each frame."""
    out, i = [], 0
    while i + 1 < len(words):
        bits = [(words[i] >> (11 - j)) & 1 for j in range(12)] + [(words[i + 1] >> (15 - j)) & 1 for j in range(16)]
        i += 2
        total = 19 + (0 if bits[12] else 8 * min(bits_value(bits[15:19]), 8)) + 15
        while total - len(bits) >= 16:
            bits += [(words[i] >> (15 - j)) & 1 for j in range(16)]
            i += 1
        w = words[i]
        p = w.bit_length() - 1
        bits += [(w >> (p - 1 - j)) & 1 for j in range(p)]
        out.append((bits, words[i + 1] if i + 1 < len(words) else None))
        i += 2
    return out


CAN_FRAMES = [  # chip frames: stuffing in ID and data, DLC 0 to 8, a remote frame
    dict(ident=0x0F0, data=[0x00, 0x00, 0xFF, 0xFF, 0x00, 0x7E, 0x81, 0x55]),
    dict(ident=0x7EF, data=[]),
    dict(ident=0x123, data=[0xA5]),
    dict(ident=0x000, data=[0x1F, 0xE0, 0x3C]),
    dict(ident=0x555, data=[], rtr=True, dlc=4),
]
NODE_FRAMES = [
    dict(ident=0x3C1, data=[0xFF, 0x00, 0x0F]),
    dict(ident=0x001, data=[0x80, 0x01, 0xFE, 0x7F, 0xC0, 0x03, 0xAA]),
]


def can_run(bt, sp, node_frames=(), node_bt=None, node_sp=None, node_start=0, delay=0, sjw=None, equ=None):
    """Chip (thread 0 transmitter, thread 1 receiver) and one model node. Returns (bench, model, node)."""
    prog = pe_asm.assemble(source("can.s", BT=bt, SP=sp, **(equ or {})), "can.s", 0)
    m = pe_model.PeModel(prog.words, start_pc=(0, prog.symbols["rx"][0], 0, 0), run=(True, True, False, False),
                         owner=owners({P_TXA: 0, P_TXB: 1}), open_drain=od((P_TXA, P_TXB)))
    nbt = bt if node_bt is None else node_bt
    node = CanNode(nbt, round(0.75 * nbt) if node_sp is None else node_sp,
                   sjw=max(1, round(nbt / 10)) if sjw is None else sjw,
                   frames=[dict(f) for f in node_frames], start=node_start)
    return Bench(m, inputs=CanBus([node], delay)), m, node


def can_send_all(bench, frames, bt, limit_bits=400):
    """Host of thread 0: sends each frame, waits for its status word, and sends it again after a lost status
    (0xFFFF). Returns [(frame number, status word or None, ACK bit)]."""
    log = []
    for i, f in enumerate(frames):
        words, ackbit = can_tx_words(f)
        for _ in range(4):
            n0 = len(bench.words(0))
            bench.send(0, *words)
            waited = 0
            while len(bench.words(0)) == n0 and waited < limit_bits:
                bench.run(int(10 * bt))
                waited += 10
            if len(bench.words(0)) == n0:
                log.append((i, None, ackbit))
                return log
            s = bench.words(0)[-1]
            log.append((i, s, ackbit))
            if s != 0xFFFF:
                break
    return log


def can_fault(bench, m, node, log=(), chip_frames=(), node_frames=(), expect_lost=0):
    """Text of the first fault, or None. Thread 1 must report every frame on the bus as the node read it, with
    a matching CRC; each chip frame must be on the bus once and acknowledged; each node frame acknowledged."""
    for n in (0, 1):
        th = m.threads[n]
        if th.status or not th.run:
            return f"chip thread {n} status {th.status}, run {th.run}, {th.stop_reason}"
    if node.errors:
        return f"node errors {node.errors[:3]}"
    try:
        rx = can_rx_parse(bench.words(1))
    except IndexError:
        return f"thread 1 words cannot be parsed: {[hex(w) for w in bench.words(1)]}"
    if len(rx) != len(node.received):
        return (f"thread 1 reported {len(rx)} frames, the node saw {len(node.received)}: thread 1 "
                f"{[(hex(can_decode(b)['ident']), can_decode(b)['data'], crcw) for b, crcw in rx]}, node "
                f"{[(hex(r['ident']), r['data']) for r in node.received]}; words {[hex(w) for w in bench.words(1)[:8]]}; "
                f"first frame bits {''.join(map(str, rx[0][0][:40])) if rx else ''}")
    for (bits, crcw), r in zip(rx, node.received):
        d = can_decode(bits)
        want = dict((k, r[k]) for k in ("ident", "rtr", "dlc", "data"))
        if crcw != 0 or not r["crc_ok"] or d != want:
            return f"thread 1 read {d} CRC word {crcw}, the node read {r}"
    seen = [dict((k, r[k]) for k in ("ident", "rtr", "dlc", "data")) for r in node.received]
    for i, f in enumerate(chip_frames):
        d = can_decode(can_bits(**f))
        if seen.count(d) != 1:
            return f"chip frame {d} on the bus {seen.count(d)} times"
        oks = [(s, ab) for j, s, ab in log if j == i and s != 0xFFFF]
        if len(oks) != 1 or oks[0][0] is None or (oks[0][0] >> oks[0][1]) & 1:
            return f"chip frame {d}: status words {[(hex(s) if s is not None else None) for j, s, _ in log if j == i]}"
    lost = sum(1 for _, s, _ in log if s == 0xFFFF)
    if lost != expect_lost:
        return f"chip lost {lost} times, expected {expect_lost}"
    for f in node_frames:
        if can_decode(can_bits(**f)) not in seen:
            return f"node frame {f} not on the bus"
    if node.results.count("ack") != len(node_frames) or node.pending:
        return f"node results {node.results}, {len(node.pending)} frames not sent"
    return None


def first_status(bench, m, cycles, threads, name, equ):
    """Runs up to `cycles` and gives the cycle, thread and source line where a thread first sets LATE or ERR."""
    prog = pe_asm.assemble(source(name, **equ), name, 0)
    for _ in range(cycles):
        bench.run(1)
        for n in threads:
            th = m.threads[n]
            if th.status:
                return f"cycle {m.cycle}: thread {n} status {th.status} at address {th.pc}: {prog.source[th.pc][1].strip()}"
    return "no status change"


def can_trace(bt, sp, frame, n=24):
    """Diagnostic: the first n sample times of thread 1 and the bus changes while thread 0 sends one frame."""
    bench, m, node = can_run(bt, sp)
    words, _ = can_tx_words(frame)
    bench.send(0, *words)
    samples, prev, pcs = [], None, [(0, None)]
    for _ in range(int(60 * bt)):
        bench.run(1)
        if m.threads[1].pc != pcs[-1][1]:
            pcs.append((m.cycle, m.threads[1].pc))
        p = m.threads[1].pend
        at = p["at"] if p is not None and p["op"] == "sample" else None
        if at is not None and at != prev:
            samples.append(at)
        prev = at
    bus = bench.m and bench.inputs.changes
    t0 = bus[1][0] if len(bus) > 1 else 0
    return (f"bus changes from SOF {[(c - t0, v) for c, v in bus[1:n]]}; thread 1 samples from SOF "
            f"{[s - t0 for s in samples[:n]]}; thread 1 pc {m.threads[1].pc}; pc changes from SOF "
            f"{[(c - t0, p) for c, p in pcs if -400 < c - t0 < 900]}")


def can_case(bt, sp, delay=0, node_ppm=0, node_sp=None, offsets=(0,), equ=None, parts=("tx", "rx", "arb")):
    """None if, at each start phase, the chip sends CAN_FRAMES (acknowledged by the node), receives and
    acknowledges NODE_FRAMES, and loses one arbitration, takes the winner's frame and then sends its frame;
    else the first fault."""
    kw = dict(delay=delay, node_bt=bt * (1 + node_ppm * 1e-6), node_sp=node_sp, equ=equ)
    for off in offsets:
        f = None
        if "tx" in parts:
            bench, m, node = can_run(bt, sp, **kw)
            bench.run(100 + off)
            log = can_send_all(bench, CAN_FRAMES, bt)
            bench.run(int(20 * bt))
            f = can_fault(bench, m, node, log, CAN_FRAMES)
        if f and "thread 1 reported" in f:
            f += "; " + can_trace(bt, sp, CAN_FRAMES[0])
        if f and "status" in f:
            bench, m, node = can_run(bt, sp, **kw)
            bench.send(0, *[w for fr in CAN_FRAMES for w in can_tx_words(fr)[0]])
            f += "; " + first_status(bench, m, int(800 * bt), (0, 1), "can.s", dict(BT=bt, SP=sp, **(equ or {})))
        if f:
            return f"chip to node, phase {off}: {f}"
        bench, m, node = can_run(bt, sp, node_frames=NODE_FRAMES, node_start=200 + off, **kw)
        bench.run(int(300 * bt))
        f = can_fault(bench, m, node, node_frames=NODE_FRAMES)
        if f:
            return f"node to chip, phase {off}: {f}"
        # Arbitration: the node sends 0x700; during it the chip and the node get a frame each, so both start
        # after its intermission. The chip ID 0x3C2 loses against 0x3C1 at the last ID bit.
        node_f = [dict(ident=0x700, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
        chip_f = [dict(ident=0x3C2, data=[0x22, 0x33])]
        bench, m, node = can_run(bt, sp, node_frames=node_f, node_start=100 + off, **kw)
        bench.run(100 + off + int(30 * bt))
        log = can_send_all(bench, chip_f, bt)
        bench.run(int(20 * bt))
        f = can_fault(bench, m, node, log, chip_f, node_f, expect_lost=1)
        if f:
            return f"arbitration, phase {off}: {f}"
    return None


@cocotb.test(skip=GL)
async def test_flex_can(dut):
    """fw/can.s (CAN 2.0A node on two threads) against an independent CAN node model (Bosch CAN 2.0 Part A).

    At each bit rate: the chip sends 5 frames (DLC 0 to 8, a remote frame, long equal runs for stuffing) and
    the node acknowledges them; the node sends 2 frames and the chip receives and acknowledges them; one
    arbitration that the chip loses (thread 1 takes the winner's frame, the host sends the frame again).
    """
    log = dut._log
    words = assemble("can.s").size
    log.info(f"fw/can.s: {words} words")
    f = can_case(400, 300)
    log.info(f"125 kbit/s (BT 400, SP 300): {f or 'correct'}")
    assert f is None, f

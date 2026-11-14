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
from test_fw_full import Bench, frame_bits, level, owners, source, uart_line, value_at  # noqa: E402

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
        if fall:
            self.last_fall = c
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
            if self.state == "idle" and bus == 0:
                # the edge came after the sample point of the last intermission bit: the resynchronization
                # (page 30 rule 4) ended that bit at the edge, and the dominant level in bus idle is a SOF (page 19)
                self._begin(self.bitstart, bool(self.pending and c >= self.start))
        self.tx = self.cur if self.state != "idle" else 1

    def _bit(self, v):
        if self.sending and v != self.cur:
            if self.state == "frame" and self.cur == 1 and v == 0 and len(self.dbits) <= 12 and not self.stuff_next:
                self.sending = False  # lost arbitration (page 23, bit error exception)
                self.results.append("lost")
            elif self.tail is None or (self.tail != 1 and self.tail < 10):  # ACK slot and intermission: no bit error
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
        if t == 12 and v == 0:  # dominant at the third intermission bit: a SOF (page 31 [1], [2])
            self._begin(self.last_fall, bool(self.pending))
            return
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


def can_arb_trace(bt, sp, node_ppm=0, delay=0):
    """Diagnostic: bus, chip pin and node levels in the arbitration case, relative to the first node SOF."""
    node_f = [dict(ident=0x700, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
    chip_f = [dict(ident=0x3C2, data=[0x22, 0x33])]
    bench, m, node = can_run(bt, sp, node_frames=node_f, node_start=100, delay=delay, node_bt=bt * (1 + node_ppm * 1e-6))
    bench.watch("txa", lambda uo, uio, oe: level(uio, oe, P_TXA))
    bench.watch("txb", lambda uo, uio, oe: level(uio, oe, P_TXB))
    nodelv = [(0, 1)]
    orig = node.see

    def see(c, bus):
        orig(c, bus)
        if node.tx != nodelv[-1][1]:
            nodelv.append((c, node.tx))
    node.see = see
    bench.run(100 + int(30 * bt))
    bench.send(0, *can_tx_words(chip_f[0])[0])
    bench.run(int(130 * bt))
    t0 = bench.inputs.changes[1][0]

    def rel(ch):
        return [(round((c - t0) / bt, 2), v) for c, v in ch if c >= t0 + 30 * bt][:40]
    return (f"node errors {node.errors[:4]}, results {node.results}; chip words {[hex(w) for w in bench.words(0)]}; "
            f"bus (bits from node SOF) {rel(bench.inputs.changes)}; TXA {rel(bench.changes('txa'))}; "
            f"TXB {rel(bench.changes('txb'))}; node {rel(nodelv)}")


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
        if "rx" in parts:
            bench, m, node = can_run(bt, sp, node_frames=NODE_FRAMES, node_start=200 + off, **kw)
            bench.run(int(300 * bt))
            f = can_fault(bench, m, node, node_frames=NODE_FRAMES)
            if f:
                return f"node to chip, phase {off}: {f}"
        if "arb" not in parts:
            continue
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
    # Smallest bit time at each sample point (SP as a part of BT), for the whole case and for each thread alone.
    smallest = {}
    for frac in (0.625, 0.75, 0.875):
        for parts in (("tx",), ("rx",), ("tx", "rx", "arb")):
            last, why = None, None
            for bt in range(400, 47, -8):
                fault = can_case(bt, round(frac * bt), parts=parts)
                if fault:
                    why = f"BT {bt}: {fault[:160]}"
                    break
                last = bt
            smallest[(frac, parts)] = last
            log.info(f"SP {frac:.3f} BT, parts {'+'.join(parts)}: smallest correct BT {last} "
                     f"({F_CLK / last / 1e3 if last else 0:.1f} kbit/s at 50 MHz, "
                     f"{60e6 / last / 1e3 if last else 0:.1f} kbit/s at 60 MHz by calculation); first fault {why}")
    # Standard rates with node clock error, transceiver delay and 4 start phases.
    results = {}
    for rate, fracs in ((125_000, (0.6, 0.65, 0.7, 0.75)), (200_000, (0.55, 0.6, 0.65)), (250_000, (0.5, 0.55, 0.6))):
        bt = round(F_CLK / rate)
        for frac in fracs:
            sp = round(frac * bt)
            for parts in (("tx",), ("rx",), ("arb",)):
                rows = []
                for ppm, delay in ((0, 0), (5000, 0), (-5000, 0), (0, 10), (5000, 10), (-5000, 10)):
                    rows.append(((ppm, delay), can_case(bt, sp, delay=delay, node_ppm=ppm, offsets=range(4),
                                                        parts=parts)))
                results[(rate, frac, parts)] = all(f is None for _, f in rows)
                log.info(f"{rate / 1e3:.0f} kbit/s (BT {bt}, SP {sp} = {frac:.3f}), part {parts[0]}: " +
                         "; ".join(f"node {p / 1e4:+.1f} %, delay {d}: {(f or 'correct')[:100]}" for (p, d), f in rows))
    for rate, frac in ((125_000, 0.65), (200_000, 0.6), (250_000, 0.55)):
        for parts in (("tx",), ("rx",), ("arb",)):
            assert results[(rate, frac, parts)], f"{rate} bit/s, SP {frac}, part {parts[0]}: not correct in every case"


# ----- JTAG (IEEE Std 1149.1) -----
# TI SSYA002C, "IEEE Std 1149.1 (JTAG) Testability Primer" (1997): TCK edges and TAP state diagram page 3-5
# (Figure 3-3); Test-Logic-Reset after 5 TCK cycles with TMS high page 3-6; instruction register capture value
# ...01 and IDCODE (or BYPASS) after reset page 3-9; bypass register page 3-11; BYPASS code all ones page 3-12;
# 32-bit device identification register with the manufacturer code in bits 1 to 11, pages 3-12, 3-14. The
# primer gives no times: set-up, hold, TCK high and low times and the TDO delay are parameters of the device
# model (values of a device data sheet).

P_TCK, P_TMS, P_TDI, UI_TDO = 8, 9, 10, 0  # uo_out[0], uo_out[1], uo_out[2]; ui_in[0]

TAP_NEXT = {  # state: (next state with TMS 0, with TMS 1) at a rising TCK edge (Figure 3-3)
    "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SELDR"),
    "SELDR": ("CAPDR", "SELIR"), "CAPDR": ("SHDR", "EX1DR"), "SHDR": ("SHDR", "EX1DR"),
    "EX1DR": ("PADR", "UPDR"), "PADR": ("PADR", "EX2DR"), "EX2DR": ("SHDR", "UPDR"), "UPDR": ("RTI", "SELDR"),
    "SELIR": ("CAPIR", "TLR"), "CAPIR": ("SHIR", "EX1IR"), "SHIR": ("SHIR", "EX1IR"),
    "EX1IR": ("PAIR", "UPIR"), "PAIR": ("PAIR", "EX2IR"), "EX2IR": ("SHIR", "UPIR"), "UPIR": ("RTI", "SELDR"),
}


class JtagDevice:
    """One TAP (primer pages 3-5 to 3-14). Instruction register of ir_len bits; after Test-Logic-Reset the
    instruction is IDCODE (32-bit register); USER selects a user_len-bit design-specific register that
    Capture-DR loads from `user` and Update-DR writes; every other code (BYPASS = all ones) selects the 1-bit
    bypass register. td: TDO delay after the falling edge; tsu, th: TMS and TDI set-up and hold at the rising
    edge; tlow, thigh: least TCK low and high times (clock cycles, data-sheet values of the device)."""

    def __init__(self, idcode, ir_len, idcode_ir, user_ir, user_len, user, td=3, tsu=2, th=2, tlow=4, thigh=4):
        self.idcode, self.ir_len, self.idcode_ir, self.user_ir = idcode, ir_len, idcode_ir, user_ir
        self.user_len, self.user = user_len, user
        self.td, self.tsu, self.th, self.tlow, self.thigh = td, tsu, th, tlow, thigh
        self.state, self.ir = "TLR", idcode_ir
        self.sh, self.sh_len = 0, 1
        self.tck, self.tms, self.tdi = 0, 1, 1
        self.t_rise = self.t_fall = self.t_tms = self.t_tdi = -10**9
        self.tdo, self.pend = None, deque()
        self.errors, self.updates = [], []

    def _dr(self):
        if self.ir == self.idcode_ir:
            return self.idcode, 32
        if self.ir == self.user_ir:
            return self.user, self.user_len
        return 0, 1

    def step(self, c, tck, tms, tdi):
        while self.pend and self.pend[0][0] <= c:
            self.tdo = self.pend.popleft()[1]
        for name, v in (("TMS", tms), ("TDI", tdi)):
            if v != getattr(self, name.lower()):
                setattr(self, name.lower(), v)
                setattr(self, "t_" + name.lower(), c)
                if c - self.t_rise < self.th:
                    self.errors.append(f"cycle {c}: {name} hold {c - self.t_rise} < {self.th}")
        if tck == self.tck:
            return
        self.tck = tck
        if tck:  # rising edge: capture or shift in the present state, then the state change (page 3-5)
            if c - self.t_fall < self.tlow:
                self.errors.append(f"cycle {c}: TCK low {c - self.t_fall} < {self.tlow}")
            if c - max(self.t_tms, self.t_tdi) < self.tsu:
                self.errors.append(f"cycle {c}: TMS/TDI set-up {c - max(self.t_tms, self.t_tdi)} < {self.tsu}")
            self.t_rise = c
            st = self.state
            if st == "CAPIR":
                self.sh, self.sh_len = 0b1001 & ((1 << self.ir_len) - 1), self.ir_len  # two LSBs 01 (page 3-9)
            elif st == "CAPDR":
                self.sh, self.sh_len = self._dr()
            elif st in ("SHIR", "SHDR"):
                self.sh = (self.sh >> 1) | (tdi << (self.sh_len - 1))
            self.state = TAP_NEXT[st][tms]
            if self.state == "TLR":
                self.ir = self.idcode_ir  # reset instruction (page 3-9)
        else:  # falling edge: TDO from the shift register in Shift-IR and Shift-DR, else not driven
            if c - self.t_rise < self.thigh:
                self.errors.append(f"cycle {c}: TCK high {c - self.t_rise} < {self.thigh}")
            self.t_fall = c
            st = self.state
            if st == "UPIR":
                self.ir = self.sh
            elif st == "UPDR" and self.ir == self.user_ir:
                self.user = self.sh
                self.updates.append(self.sh)
            self.pend.append((c + self.td, self.sh & 1 if st in ("SHIR", "SHDR") else None))


class JtagChain:
    """Chip TDI -> devs[0] -> devs[1] -> ... -> chip TDO; a TDO line that no device drives reads 1 (pull-up)."""

    def __init__(self, devs):
        self.devs = devs

    def __call__(self, c, uo, uio, oe):
        tck, tms, tdi = uo & 1, (uo >> 1) & 1, (uo >> 2) & 1
        for d in self.devs:
            d.step(c, tck, tms, tdi)
            tdi = 1 if d.tdo is None else d.tdo
        return tdi << UI_TDO, 0


def lsb_word(bits):
    return sum(b << i for i, b in enumerate(bits))


def tap_reset():
    """Host TMS and TDI bits: TMS high for 5 cycles (Test-Logic-Reset, page 3-6), then Run-Test/Idle."""
    return [1] * 5 + [0], [1] * 6, []


def tap_scan(ir, tdi, pause_after=None):
    """Host bits from Run-Test/Idle for one IR or DR scan of the bits tdi (first bit first) and back to
    Run-Test/Idle (Figure 3-3); with pause_after = k the scan goes Exit1, Pause twice, Exit2 after bit k.
    Returns (TMS bits, TDI bits, the cycles in which a bit shifts out on TDO)."""
    tms = [1, 1, 0, 0] if ir else [1, 0, 0]
    bits, idx = [1] * len(tms), []
    for i, b in enumerate(tdi):
        last = i == len(tdi) - 1
        pause = i == pause_after and not last
        idx.append(len(tms))
        tms.append(1 if last or pause else 0)
        bits.append(b)
        if pause:
            tms += [0, 0, 1, 0]
            bits += [1] * 4
    return tms + [1, 0], bits + [1, 1], idx


def seq(*parts):
    tms, tdi, idx = [], [], []
    for t, d, i in parts:
        idx += [len(tms) + k for k in i]
        tms, tdi = tms + t, tdi + d
    return tms, tdi, idx


def value_bits(v, n):
    return [(v >> i) & 1 for i in range(n)]


JTAG_IDS = (0x1A2B3C4D, 0x0BA0E477)  # odd: bit 0 of a device identification register is 1


def jtag_devices(**kw):
    return [JtagDevice(JTAG_IDS[0], ir_len=4, idcode_ir=0b0010, user_ir=0b1000, user_len=8, user=0x5A, **kw),
            JtagDevice(JTAG_IDS[1], ir_len=5, idcode_ir=0b00001, user_ir=0b00100, user_len=12, user=0x3C1, **kw)]


def jtag_steps():
    """(host bits, expected TDO bits of the shifted cycles) for: reset and both IDCODEs (the device next to TDO
    first); IR scan USER (device 1) and BYPASS (device 0); DR scan through Pause-DR writing 0xA53 to device 1;
    DR scan that reads 0xA53 back; reset and the IDCODEs again."""
    ids = value_bits(JTAG_IDS[1], 32) + value_bits(JTAG_IDS[0], 32)
    return [
        (seq(tap_reset(), tap_scan(False, [1] * 64)), ids),
        (seq(tap_scan(True, value_bits(0b00100, 5) + value_bits(0b1111, 4))), value_bits(0b01001, 5) + value_bits(0b1001, 4)),
        (seq(tap_scan(False, value_bits(0xA53, 12) + [0], pause_after=5)), value_bits(0x3C1, 12) + [0]),
        (seq(tap_scan(False, [0] * 13)), value_bits(0xA53, 12) + [0]),
        (seq(tap_reset(), tap_scan(False, [0] * 64)), ids),
    ]


def jtag_tdo(words, n):
    """TDO bit of each cycle from the words of fw/jtag.s (k cycles in a group: the first at bit 16 - k)."""
    out = []
    for g, w in zip(range(0, n, 16), words):
        k = min(16, n - g)
        out += [(w >> (16 - k + i)) & 1 for i in range(k)]
    return out


def jtag_run_a(h1, h2, **kw):
    """fw/jtag.s with the chain; returns (fault text or None, cycles from the first to the last TCK edge of the
    64-cycle IDCODE scan of step 1, TCK cycles of that step)."""
    prog = assemble("jtag.s", H1=h1, H2=h2)
    devs = jtag_devices(**kw)
    m = pe_model.PeModel(prog.words, owner=owners({P_TCK: 0, P_TMS: 0, P_TDI: 0}))
    bench = Bench(m, inputs=JtagChain(devs))
    bench.watch("tck", lambda uo, uio, oe: uo & 1)
    bench.run(50)
    span = None
    for k, ((tms, tdi, idx), want) in enumerate(jtag_steps()):
        n = len(tms)
        words = [n] + [w for g in range(0, n, 16) for w in (lsb_word(tms[g:g + 16]), lsb_word(tdi[g:g + 16]))]
        n0, e0 = len(bench.words(0)), len(bench.changes("tck"))
        bench.send(0, *words)
        need = n0 + (n + 15) // 16
        for _ in range(200):
            if len(bench.words(0)) >= need:
                break
            bench.run(n * (h1 + h2) // 8 + 100)
        got = bench.words(0)[n0:]
        if len(got) != need - n0:
            return f"step {k}: {len(got)} TDO words, expected {need - n0}", None, None
        tdo = jtag_tdo(got, n)
        if [tdo[i] for i in idx] != want:
            return f"step {k}: TDO {[tdo[i] for i in idx]}, expected {want}", None, None
        if k == 0:
            edges = [c for c, _ in bench.changes("tck")[e0:]]
            span = (edges[-1] - edges[0], n)
    bench.run(200)
    for d in devs:
        if d.errors:
            return f"device errors {d.errors[:3]}", None, None
    if devs[1].updates != [0xA53, 0]:
        return f"device 1 updates {devs[1].updates}", None, None
    th = m.threads[0]
    if th.status or not th.run:
        return f"thread status {th.status}, run {th.run}, {th.stop_reason}", None, None
    return None, span[0], span[1]


def jtag_ops():
    """The steps of jtag_steps as reset and scan commands of fw/jtag_tap.s (it has no Pause states):
    lists of (command, TDI bits, expected TDO bits)."""
    ids = value_bits(JTAG_IDS[1], 32) + value_bits(JTAG_IDS[0], 32)
    return [
        [(0, [], []), (64, [1] * 64, ids)],
        [(0x8000 | 9, value_bits(0b00100, 5) + value_bits(0b1111, 4), value_bits(0b01001, 5) + value_bits(0b1001, 4))],
        [(13, value_bits(0xA53, 12) + [0], value_bits(0x3C1, 12) + [0])],
        [(13, [0] * 13, value_bits(0xA53, 12) + [0])],
        [(0, [], []), (64, [0] * 64, ids)],
    ]


def jtag_run_b(h1, h2, **kw):
    """fw/jtag_tap.s with the chain; returns (fault text or None, cycles of the 64-bit IDCODE scan, its bits)."""
    prog = assemble("jtag_tap.s", H1=h1, H2=h2)
    devs = jtag_devices(**kw)
    m = pe_model.PeModel(prog.words, owner=owners({P_TCK: 0, P_TMS: 0, P_TDI: 0}))
    bench = Bench(m, inputs=JtagChain(devs))
    bench.watch("tck", lambda uo, uio, oe: uo & 1)
    bench.run(50)
    span = None
    for k, ops in enumerate(jtag_ops()):
        for cmd, tdi, want in ops:
            n = len(tdi)
            n0, e0 = len(bench.words(0)), len(bench.changes("tck"))
            bench.send(0, cmd, *[lsb_word(tdi[g:g + 16]) for g in range(0, n, 16)])
            need = n0 + (n + 15) // 16
            for _ in range(200):
                if len(bench.words(0)) >= need and not bench.to_thread[0]:
                    break
                bench.run(max(n, 8) * (h1 + h2) // 8 + 100)
            bench.run(12 * (h1 + h2))  # header and tail cycles
            got = bench.words(0)[n0:]
            if len(got) != need - n0:
                return f"step {k}: {len(got)} TDO words, expected {need - n0}", None, None
            tdo = jtag_tdo(got, n)
            if tdo != want:
                return f"step {k}: TDO {tdo}, expected {want}", None, None
            if k == 0 and n:
                edges = [c for c, _ in bench.changes("tck")[e0:]]
                span = (edges[-1] - edges[0], len(edges) // 2)
    bench.run(200)
    for d in devs:
        if d.errors:
            return f"device errors {d.errors[:3]}", None, None
    if devs[1].updates != [0xA53, 0]:
        return f"device 1 updates {devs[1].updates}", None, None
    th = m.threads[0]
    if th.status or not th.run:
        return f"thread status {th.status}, run {th.run}, {th.stop_reason}", None, None
    return None, span[0], span[1]


def jtag_limits(run, td):
    """For each H1, the smallest H2 that is correct (binary search, the value below checked): rows sorted by
    the TCK period."""
    rows = []
    for h1 in range(2, 25):
        if run(h1, 40, td=td)[0]:
            continue
        lo, hi = 1, 40
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (lo, mid) if run(h1, mid, td=td)[0] is None else (mid, hi)
        _, span, n = run(h1, hi, td=td)
        rows.append((h1 + hi, h1, hi, span, n, run(h1, hi - 1, td=td)[0]))
    return sorted(rows)


@cocotb.test(skip=GL)
async def test_flex_jtag(dut):
    """fw/jtag.s against two independent TAP models in a chain (TI SSYA002C primer): both IDCODEs after
    Test-Logic-Reset, an IR scan (USER and BYPASS), a DR scan through Pause-DR with update, a read-back, and
    reset. Measures the smallest TCK low and high times at TDO delays of 3 and 8 cycles."""
    log = dut._log
    log.info(f"fw/jtag.s: {assemble('jtag.s').size} words")
    fault, span, n = jtag_run_a(12, 28)
    assert fault is None, f"H1 12, H2 28: {fault}"
    log.info(f"H1 12, H2 28 (defaults): correct; 64-bit IDCODE step: {n} TCK cycles in {span} clock cycles")
    log.info(f"fw/jtag_tap.s: {assemble('jtag_tap.s').size} words")
    fault, span, n = jtag_run_b(12, 28)
    assert fault is None, f"fw/jtag_tap.s H1 12, H2 28: {fault}"
    best = {}
    for name, run in (("jtag.s", jtag_run_a), ("jtag_tap.s", jtag_run_b)):
        for td in (3, 8):
            rows = jtag_limits(run, td)
            best[(name, td)] = rows[0][0]
            for p, h1, h2, span, n, below in rows[:4]:
                log.info(f"{name}, TDO delay {td}: H1 {h1}, H2 {h2}: TCK period {p} cycles ({F_CLK / p / 1e6:.3f} MHz); "
                         f"64-bit IDCODE step {n} TCK cycles in {span} clock cycles "
                         f"({n * F_CLK / span / 1e6:.3f} MHz mean); H2 {h2 - 1}: {(below or 'correct')[:100]}")
            log.info(f"{name}, TDO delay {td}: smallest H1 that works {min(r[1] for r in rows)}")
    assert best[("jtag.s", 3)] <= 40, f"fw/jtag.s smallest TCK period {best[('jtag.s', 3)]} cycles"


# ----- SWD (Arm Debug Interface v5) -----
# ARM IHI0031H, "Arm Debug Interface Architecture Specification ADIv5.0 to ADIv5.2" (2026): turnaround, idle
# cycles, bit order B4-104; parity B4-105; ACK codes, write and read phases, posted AP reads B4-108, B4-109;
# DP read responses Table B4-2 B4-114; protocol error and WDATA parity B4-116; host responses Table B4-10
# B4-117; line interface (rising-edge sampling and change), pull-up, line reset B4-118, reset state B4-119;
# JTAG-to-SWD sequence B5-125. The specification gives no times: they are parameters of the target model.

P_SWCLK, P_SWDIO = 11, 4  # uo_out[3], uio[4]


def parity(v):
    return bin(v).count("1") & 1


class SwdTarget:
    """SWJ-DP target, SWD side, written from IHI0031H. JTAG is selected at power-up (B4-104 note); 50 or more
    high cycles, then the select sequence 0xE79E (LSB first), select SWD in its reset state (B5-125); a line
    reset (50 or more high cycles, then 2 or more low cycles) enters the reset state, where only a DPIDR read is
    valid (B4-118, B4-119). After the 8-bit request: turnaround, ACK from the rising edge after Park, data and
    parity (B4-108, B4-109); the target samples and changes SWDIO at the rising SWCLK edge, its changes appear
    td cycles later (B4-118). Registers: DPIDR (DP read 0x0), SELECT (DP write 0x8), RDBUFF (DP read 0xC),
    AP registers by (SELECT, A); AP reads are posted (B4-109); the next `waits` AP requests get WAIT; a write
    with a parity error is discarded (B4-105); a protocol error gives no response until a line reset (B4-116)."""

    def __init__(self, dpidr=0x1BA01477, td=3, tsu=2, th=2, tlow=4, thigh=4):
        self.dpidr, self.td, self.tsu, self.th, self.tlow, self.thigh = dpidr, td, tsu, th, tlow, thigh
        self.mode, self.reset_state, self.error = "jtag", False, False
        self.ones, self.zeros, self.sel = 0, 0, None
        self.state, self.bits, self.k = "idle", [], 0
        self.drive, self.pend = None, deque()
        self.select, self.ap, self.posted, self.rdbuff, self.waits = 0, {}, 0, 0, 0
        self.clk, self.t_rise, self.t_fall, self.t_host, self.host = 0, -10**9, -10**9, -10**9, None
        self.log, self.errors, self.protocol_errors = [], [], 0

    def _line_reset(self):
        if self.mode == "swd":
            self.reset_state, self.error, self.state = True, False, "idle"
            self._out(None)

    def _out(self, v):
        self.pend.append((self.c + self.td, v))

    def _request(self, b):
        apndp, rnw, a = b[1], b[2], b[3] << 2 | b[4] << 3
        if b[0] != 1 or b[6] != 0 or b[7] != 1 or parity(b[1] | b[2] << 1 | b[3] << 2 | b[4] << 3) != b[5]:
            return None
        if self.reset_state and not (apndp == 0 and rnw == 1 and a == 0):
            return None  # only a DPIDR read is valid in the reset state (B4-119)
        self.req = (apndp, rnw, a)
        if apndp and self.waits:
            self.waits -= 1
            return "wait"
        return "ok"

    def _read_value(self):
        apndp, _, a = self.req
        if apndp:
            v, self.posted = self.posted, self.ap.get((self.select, a), 0)
            self.rdbuff = self.posted
            return v
        self.reset_state = False if a == 0 else self.reset_state
        return {0x0: self.dpidr, 0xC: self.rdbuff}.get(a, 0)

    def _rise(self, v):
        c = self.c
        if self.mode == "jtag":
            if self.sel is not None:
                self.sel.append(v)
                if len(self.sel) == 16:
                    if lsb_word(self.sel) == 0xE79E:
                        self.mode, self.reset_state, self.state = "swd", True, "idle"
                    self.sel = None
            elif v == 0 and self.ones >= 50:
                self.sel = [0]
            self.ones = self.ones + 1 if v else 0
            return
        if v:  # ones: the present run of high cycles; zeros: the low cycles after it
            self.ones, self.zeros = (1 if self.zeros else self.ones + 1), 0
        else:
            self.zeros += 1
            if self.zeros == 2 and self.ones >= 50:
                self._line_reset()
                return
        if self.error:
            return
        st = self.state
        if st == "idle":
            if v:
                self.state, self.bits = "req", [1]
        elif st == "req":
            self.bits.append(v)
            if len(self.bits) == 8:
                self.ack = self._request(self.bits)
                if self.ack is None:
                    self.error, self.state = True, "idle"
                    self.protocol_errors += 1
                else:
                    self.state, self.k = "resp", 0
        elif st == "resp":
            self.k += 1
            k, ok, (apndp, rnw, a) = self.k, self.ack == "ok", self.req
            if k <= 3:
                self._out((0b001 if ok else 0b010) >> (k - 1) & 1)  # ACK[0:2]
            elif ok and rnw:
                if k == 4:
                    self.rv = self._read_value()
                if k <= 35:
                    self._out(self.rv >> (k - 4) & 1)
                elif k == 36:
                    self._out(parity(self.rv))
                elif k == 37:
                    self._out(None)
                else:  # k == 38: turnaround edge
                    self.log.append(("R", apndp, a, self.rv))
                    self.state = "idle"
            elif ok:
                if k == 4:
                    self._out(None)
                    self.wv = []
                elif k >= 6:
                    self.wv.append(v)
                    if k == 38:
                        w = lsb_word(self.wv[:32])
                        good = parity(w) == self.wv[32]
                        self.log.append(("W", apndp, a, w, good))
                        if good and apndp:
                            self.ap[(self.select, a)] = w
                        elif good and a == 0x8:
                            self.select = w
                        self.state = "idle"
            else:
                if k == 4:
                    self._out(None)
                elif k == 5:
                    self.log.append(("WAIT", apndp, a))
                    self.state = "idle"

    def step(self, c, clk, host_oe, host_v):
        self.c = c
        while self.pend and self.pend[0][0] <= c:
            self.drive = self.pend.popleft()[1]
        host = host_v if host_oe else None
        if host != self.host:
            self.host, self.t_host = host, c
            if host is not None and c - self.t_rise < self.th:
                self.errors.append(f"cycle {c}: SWDIO hold {c - self.t_rise} < {self.th}")
        if host is not None and self.drive is not None:
            self.errors.append(f"cycle {c}: host and target drive SWDIO")
        line = host if host is not None else (self.drive if self.drive is not None else 1)
        if clk != self.clk:
            self.clk = clk
            if clk:
                if c - self.t_fall < self.tlow:
                    self.errors.append(f"cycle {c}: SWCLK low {c - self.t_fall} < {self.tlow}")
                if host is not None and c - self.t_host < self.tsu:
                    self.errors.append(f"cycle {c}: SWDIO set-up {c - self.t_host} < {self.tsu}")
                self.t_rise = c
                self._rise(line)
            else:
                if c - self.t_rise < self.thigh:
                    self.errors.append(f"cycle {c}: SWCLK high {c - self.t_rise} < {self.thigh}")
                self.t_fall = c
        return line


class SwdLine:
    def __init__(self, target):
        self.t = target

    def __call__(self, c, uo, uio, oe):
        line = self.t.step(c, (uo >> 3) & 1, (oe >> P_SWDIO) & 1, (uio >> P_SWDIO) & 1)
        return 0, line << P_SWDIO


def swd_request(apndp, rnw, a):
    """Request byte (B4-108, B4-119 Figure B4-9): Start 1, APnDP, RnW, A[2:3], even parity, Stop 0, Park 1."""
    a2, a3 = (a >> 2) & 1, (a >> 3) & 1
    return 1 | apndp << 1 | rnw << 2 | a2 << 3 | a3 << 4 | parity(apndp | rnw << 1 | a2 << 2 | a3 << 3) << 5 | 1 << 7


def swd_raw(bits):
    """Raw command of fw/swd.s: whole 16-cycle words."""
    assert len(bits) % 16 == 0
    return [0x8000 | len(bits) // 16] + [lsb_word(bits[g:g + 16]) for g in range(0, len(bits), 16)]


SWD_CONNECT = [1] * 64 + value_bits(0xE79E, 16) + [1] * 64 + [0] * 16  # B5-125, then the line reset B4-118
SWD_LINE_RESET = [1] * 64 + [0] * 16


class SwdHost:
    """Host program for fw/swd.s: raw sequences, and transactions that wait for the ACK word and the read data."""

    def __init__(self, bench, period):
        self.b, self.period = bench, period

    def _wait(self, n):
        for _ in range(400):
            if len(self.b.words(0)) >= n:
                return True
            self.b.run(4 * self.period)
        return False

    def raw(self, bits):
        self.b.send(0, *swd_raw(bits))
        self.b.run((len(bits) + 8) * self.period + 400)

    def xfer(self, apndp, rnw, a, value=0, bad_parity=False):
        """Returns (ACK, read value or None, read parity correct or None)."""
        n0 = len(self.b.words(0))
        words = [swd_request(apndp, rnw, a) | rnw << 10]
        if not rnw:
            words += [value & 0xFFFF, value >> 16, parity(value) ^ int(bad_parity)]
        self.b.send(0, *words)
        if not self._wait(n0 + 1):
            return None, None, None
        ack = self.b.words(0)[n0] >> 13
        if not (rnw and ack == 0b001):
            self.b.run(60 * self.period)
            return ack, None, None
        if not self._wait(n0 + 4):
            return ack, None, None
        lo, hi, p = self.b.words(0)[n0 + 1:n0 + 4]
        v = lo | hi << 16
        return ack, v, parity(v) == (p >> 14) & 1


def swd_run(h1, h2, name="swd.s", **kw):
    """Connection, DPIDR, SELECT write, AP write with 2 WAITs and retries, posted AP read and RDBUFF, a write with
    a parity error, a line reset and an access that is not valid in the reset state. Returns (fault or None,
    clock cycles of the DPIDR read transaction)."""
    prog = assemble(name, H1=h1, H2=h2)
    tgt = SwdTarget(**kw)
    m = pe_model.PeModel(prog.words, owner=owners({P_SWCLK: 0, P_SWDIO: 0}))
    bench = Bench(m, inputs=SwdLine(tgt))
    bench.watch("clk", lambda uo, uio, oe: (uo >> 3) & 1)
    host = SwdHost(bench, h1 + h2)
    bench.run(50)
    host.raw(SWD_CONNECT)
    e0 = len(bench.changes("clk"))
    got = host.xfer(0, 1, 0x0)
    edges = [c for c, _ in bench.changes("clk")[e0:]]
    span = edges[-1] - edges[0] if edges else None
    if got != (0b001, tgt.dpidr, True):
        return f"DPIDR read: {got}", None
    if host.xfer(0, 0, 0x8, 0x000000F0)[0] != 0b001:
        return "SELECT write: no OK", None
    tgt.waits = 2
    acks = [host.xfer(1, 0, 0x4, 0xCAFEF00D)[0] for _ in range(3)]
    if acks != [0b010, 0b010, 0b001] or tgt.ap.get((0xF0, 0x4)) != 0xCAFEF00D:
        return f"AP write with WAIT: ACKs {acks}, AP {tgt.ap}", None
    first = host.xfer(1, 1, 0x4)
    last = host.xfer(0, 1, 0xC)
    if first[0] != 0b001 or last != (0b001, 0xCAFEF00D, True):
        return f"AP read, RDBUFF: {first}, {last}", None
    bad = host.xfer(1, 0, 0x4, 0x12345678, bad_parity=True)
    host.xfer(1, 1, 0x4)
    after = host.xfer(0, 1, 0xC)
    if bad[0] != 0b001 or after[1] != 0xCAFEF00D or tgt.log[-3][-1] is not False:
        return f"write with parity error: {bad}, then RDBUFF {after}, log {tgt.log[-3:]}", None
    host.raw(SWD_LINE_RESET)
    e0 = tgt.protocol_errors
    noresp = host.xfer(1, 1, 0x0)
    e1 = tgt.protocol_errors
    host.raw(SWD_LINE_RESET)
    again = host.xfer(0, 1, 0x0)
    if noresp[0] != 0b111 or again != (0b001, tgt.dpidr, True) or e1 != e0 + 1:
        return f"reset state: AP read ACK {noresp}, then DPIDR {again}", None
    if tgt.errors:
        return f"target errors {tgt.errors[:3]}", None
    th = m.threads[0]
    if th.status or not th.run:
        return f"thread status {th.status}, run {th.run}, {th.stop_reason}", None
    return None, span


def swd_limits(name, td):
    rows = []
    for h1 in range(2, 25):
        if swd_run(h1, 40, name, td=td)[0]:
            continue
        lo, hi = 1, 40
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (lo, mid) if swd_run(h1, mid, name, td=td)[0] is None else (mid, hi)
        rows.append((h1 + hi, h1, hi, swd_run(h1, hi, name, td=td)[1], swd_run(h1, hi - 1, name, td=td)[0]))
    return sorted(rows)


@cocotb.test(skip=GL)
async def test_flex_swd(dut):
    """fw/swd.s against an independent SW-DP target model (ARM IHI0031H): JTAG-to-SWD sequence and line reset,
    DPIDR read, DP write, AP write with WAIT and retry, posted AP read with RDBUFF, a write with a parity error,
    and the reset-state rule. Measures the smallest SWCLK times at target delays of 3 and 8 cycles."""
    log = dut._log
    log.info(f"fw/swd.s: {assemble('swd.s').size} words")
    fault, span = swd_run(12, 20)
    assert fault is None, f"H1 12, H2 20: {fault}"
    log.info(f"H1 12, H2 20 (defaults): correct; DPIDR read transaction {span} clock cycles")
    best = {}
    for name in ("swd.s",):
        for td in (3, 8):
            rows = swd_limits(name, td)
            best[(name, td)] = rows[0][0]
            for p, h1, h2, span, below in rows[:4]:
                log.info(f"{name}, target delay {td}: H1 {h1}, H2 {h2}: SWCLK period {p} cycles "
                         f"({F_CLK / p / 1e6:.3f} MHz); DPIDR read {span} clock cycles ({F_CLK / span / 1e3:.1f} k reads/s); "
                         f"H2 {h2 - 1}: {(below or 'correct')[:100]}")
            log.info(f"{name}, target delay {td}: smallest H1 that works {min(r[1] for r in rows)}")
    assert best[("swd.s", 8)] <= 32, f"fw/swd.s smallest SWCLK period {best[('swd.s', 8)]} cycles"


# ----- PS/2 -----
# IBM, "Personal System/2 Hardware Interface Technical Reference" (May 1988), Keyboards (101- and 102-Key): frame
# (Figure 18) page 38; device output, inhibit and line contention page 39; request to send, acknowledge, 20 ms
# response page 40. The clock times are not in the pages read: they are parameters of the models.

P_PCLK, P_PDATA = 4, 5  # uio[4], uio[5], open drain


def ps2_frame(byte):
    """11 line bits, first first: start 0, data LSB first, odd parity, stop 1 (page 38)."""
    data = [(byte >> i) & 1 for i in range(8)]
    return [0] + data + [1 - (sum(data) & 1)] + [1]


def ps2_decode(bits):
    """(byte, frame correct) from 11 line bits."""
    return sum(b << i for i, b in enumerate(bits[1:9])), bits[0] == 0 and bits[10] == 1 and sum(bits[1:10]) % 2 == 1


def ps2_word_bits(w):
    """The 11 bits of a frame word of the firmware (bits 5 to 15)."""
    return [(w >> (5 + i)) & 1 for i in range(11)]


class Ps2Bus:
    """Open-drain CLK and DATA with pull-ups: a line is low when the chip pin drives 0 or the model pulls it low."""

    def __init__(self, model):
        self.m = model

    def __call__(self, c, uo, uio, oe):
        clk = 0 if level(uio, oe, P_PCLK) == 0 or self.m.clk_low else 1
        data = 0 if level(uio, oe, P_PDATA) == 0 or self.m.data_low else 1
        self.m.step(c, clk, data)
        return 0, clk << P_PCLK | data << P_PDATA


class Ps2Device:
    """PS/2 device model (pages 38 to 40), written from the IBM pages. Own clock: `period` cycles, CLK low `low`
    cycles, DATA set `tsu` cycles before each falling edge. It sends the next frame when CLK and DATA have been high
    for `gap` cycles; if CLK is low when it releases CLK before the 10th clock (line contention) it stops and sends
    the frame again later. On a request to send (CLK high, DATA low) it waits `rts_delay` cycles, clocks 11 bits,
    samples DATA at each rising edge, pulls DATA low for a 12th clock if the stop bit is high, records the byte and
    then sends `answer`. It records DATA changes by the host while CLK is high in a receive (errors)."""

    def __init__(self, period, frames=(), low=None, tsu=None, gap=3000, rts_delay=500, answer=0xFA, start=0):
        self.period = period
        self.low = period // 2 if low is None else low
        self.tsu = (period - self.low) // 2 if tsu is None else tsu
        self.queue = deque(frames)
        self.gap, self.rts_delay, self.answer, self.start = gap, rts_delay, answer, start
        self.clk_low = self.data_low = False
        self.state, self.quiet, self.prev = "idle", 0, (1, 1)
        self.received, self.sent, self.stopped, self.errors = [], [], 0, []

    def step(self, c, clk, data):
        st = self.state
        if st == "idle":
            self.quiet = self.quiet + 1 if (clk, data) == (1, 1) else 0
            if (clk, data) == (1, 0) and self.prev != (1, 0):  # request to send: CLK released with DATA low
                self.state, self.t0 = "rts", c + self.rts_delay
            elif self.queue and self.quiet >= self.gap and c >= self.start:
                self.state, self.t0, self.bits = "send", c + 1, ps2_frame(self.queue[0])
        elif st == "rts" and c >= self.t0:
            self.state, self.rbits, self.t0 = "recv", [], c + 1
        elif st == "send":
            k, ph = divmod(c - self.t0, self.period)
            if k > 10:
                self.data_low, self.state, self.quiet = False, "idle", 0
                self.sent.append(self.queue.popleft())
            elif ph == 0:
                self.data_low = self.bits[k] == 0
            elif ph == self.tsu:
                self.clk_low = True
            elif ph == self.tsu + self.low:
                self.clk_low = False
            elif ph == self.tsu + self.low + 2 and clk == 0 and k < 9:  # contention before the 10th clock
                self.data_low, self.state, self.quiet = False, "idle", 0
                self.stopped += 1
        elif st == "recv":
            k, ph = divmod(c - self.t0, self.period)
            if clk == 1 and data != self.prev[1] and k < 10 and ph > 2:
                self.errors.append(f"cycle {c}: host changed DATA while CLK is high (bit {k})")
            if k <= 11 and ph == 0:
                self.clk_low = True
            elif k <= 11 and ph == self.low:
                self.clk_low = False
                if k <= 10:
                    self.rbits.append(data)
                if k == 10:
                    if self.rbits[10] == 1:
                        self.data_low = True  # acknowledge in clock 12
                    else:
                        self.errors.append(f"cycle {c}: stop bit 0")
            elif k == 12:
                self.data_low, self.state, self.quiet = False, "idle", 0
                self.received.append(ps2_decode(self.rbits))
                if self.answer is not None:
                    self.queue.append(self.answer)
        self.prev = (clk, data)


class Ps2Host:
    """PS/2 system model (pages 38 to 40), written from the IBM pages. It samples DATA at each falling CLK edge that
    it does not make, collects 11-bit frames (a frame restarts after 20,000 cycles without a falling edge), checks
    start, odd parity and stop, and records the fall-to-fall clock periods. It can hold CLK low for `inhibit_len`
    cycles from `inhibit_delay` cycles after falling edge `inhibit_at` of frame number `inhibit_frame`. At cycle
    `cmd_at` it sends `cmd`: CLK low for `t_rts` cycles, DATA low, CLK released, the next bit `tchg` cycles after
    falling edges 2 to 11, the acknowledge read after falling edge 12."""

    def __init__(self, cmd=None, cmd_at=None, t_rts=4000, tchg=100, inhibit_frame=None, inhibit_at=0,
                 inhibit_delay=200, inhibit_len=2500):
        self.cmd, self.cmd_at, self.t_rts, self.tchg = cmd, cmd_at, t_rts, tchg
        self.inh = (inhibit_frame, inhibit_at, inhibit_delay, inhibit_len)
        self.clk_low = self.data_low = False
        self.bits, self.frames, self.falls, self.periods = [], [], [], []
        self.prev_clk, self.mode, self.ack, self.pend = 1, "rx", None, []

    def step(self, c, clk, data):
        while self.pend and self.pend[0][0] <= c:
            _, name, v = self.pend.pop(0)
            setattr(self, name, v)
        fall = self.prev_clk == 1 and clk == 0 and not self.clk_low
        self.prev_clk = clk
        if self.cmd is not None and self.mode == "rx" and c == self.cmd_at:
            self.mode, self.n = "tx", 0
            self.tx = ps2_frame(self.cmd)
            self.pend += [(c, "clk_low", True), (c + self.t_rts, "data_low", True), (c + self.t_rts + 100, "clk_low", False)]
            return
        if not fall:
            return
        if self.mode == "tx":
            self.n += 1
            if 2 <= self.n <= 11:
                self.pend.append((c + self.tchg, "data_low", self.tx[self.n - 1] == 0))
            elif self.n == 12:
                self.ack, self.mode = data, "rx"
            return
        if self.falls and c - self.falls[-1] > 8000:  # more than a clock period: a new frame
            self.bits = []
        if self.falls and self.bits:
            self.periods.append(c - self.falls[-1])
        self.falls.append(c)
        self.bits.append(data)
        f, at, delay, length = self.inh
        if f is not None and len(self.frames) == f and len(self.bits) == at:
            self.pend += [(c + delay, "clk_low", True), (c + delay + length, "clk_low", False)]
            self.inh = (None, 0, 0, 0)
        if len(self.bits) == 11:
            self.frames.append(ps2_decode(self.bits))
            self.bits = []


PS2_BYTES = [0xAA, 0x1C, 0xF0, 0x1C, 0x00, 0xFF, 0x80]


def ps2_cmd_word(byte):
    bits = ps2_frame(byte)
    return byte | bits[9] << 8 | 1 << 9


def ps2_host_run(period, offset=0, late="st", cmd=0xED, low=None, frames=PS2_BYTES):
    """fw/ps2_host.s with a device model: the device sends `frames`, then (if cmd is not None) the firmware sends
    `cmd` and the device acknowledges and answers 0xFA. Returns (fault or None, bytes decoded)."""
    late = "st" if late == "rlate" else late  # "rlate": with the late check (label st), "nolate": without
    prog = assemble("ps2_host.s", LATE=late)
    dev = Ps2Device(period, frames, low=low, start=4000 + offset)  # after the 3,000-cycle quiet time: offset = phase
    m = pe_model.PeModel(prog.words, owner=owners({P_PCLK: 0, P_PDATA: 0}), open_drain=od((P_PCLK, P_PDATA)))
    bench = Bench(m, inputs=Ps2Bus(dev))
    bench.run(4000 + offset + len(frames) * (12 * period + 3200))
    words = list(bench.words(0))
    got = [ps2_decode(ps2_word_bits(w)) if w >= 32 else (None, False) for w in words]
    if got != [(b, True) for b in frames]:
        return (f"received {[(hex(b) if b is not None else None, ok) for b, ok in got]}; words "
                f"{[hex(w) for w in words]} at {[c for c, _ in bench.from_thread[0]]}; device sent {dev.sent}"), got
    if cmd is None:
        th = m.threads[0]
        return (None if not th.status and th.run else f"thread status {th.status}"), got
    bench.send(0, ps2_cmd_word(cmd))
    bench.run(500 + 4000 + 13 * period + 600 + 12 * period + 4000)
    rest = bench.words(0)[len(words):]
    if not rest or (rest[0] >> 15) & 1 != 0 or dev.received != [(cmd, True)]:
        return f"command: words {[hex(w) for w in rest]}, device received {dev.received}, errors {dev.errors[:2]}", got
    if len(rest) < 2 or ps2_decode(ps2_word_bits(rest[1])) != (0xFA, True):
        return f"answer after the command: {[hex(w) for w in rest]}", got
    if dev.errors or dev.stopped:
        return f"device errors {dev.errors[:2]}, stopped {dev.stopped}", got
    th = m.threads[0]
    if th.status or not th.run:
        m2 = pe_model.PeModel(prog.words, owner=owners({P_PCLK: 0, P_PDATA: 0}), open_drain=od((P_PCLK, P_PDATA)))
        b2 = Bench(m2, inputs=Ps2Bus(Ps2Device(period, PS2_BYTES, low=low, start=4000 + offset)))
        where = first_status(b2, m2, 4000 + offset + len(PS2_BYTES) * (12 * period + 3200), (0,), "ps2_host.s",
                             dict(LATE=late))
        return f"thread status {th.status}, run {th.run}, {th.stop_reason}; receive part: {where}", got
    return None, got


def ps2_dev_run(equ=None, inhibit=None, cmd=0xF4, frames=(0xAA, 0xFA, 0x12, 0xF0, 0x12)):
    """fw/ps2_dev.s with a system model: the firmware sends `frames` (frame number `inhibit[0]` held off by the host at
    falling edge `inhibit[1]`; the host program sends a frame again after 0xFFFF), then the system sends `cmd` and
    the firmware acknowledges. Returns (fault or None, clock periods)."""
    prog = assemble("ps2_dev.s", **(equ or {}))
    host = Ps2Host(inhibit_frame=None if inhibit is None else inhibit[0], inhibit_at=0 if inhibit is None else inhibit[1])
    m = pe_model.PeModel(prog.words, owner=owners({P_PCLK: 0, P_PDATA: 0}), open_drain=od((P_PCLK, P_PDATA)))
    bench = Bench(m, inputs=Ps2Bus(host))
    bench.run(200)
    stops = 0
    for b in frames:
        word = sum(bit << i for i, bit in enumerate(ps2_frame(b)))
        for _ in range(3):
            n0 = len(bench.words(0))
            bench.send(0, word)
            for _ in range(100):
                bench.run(2000)
                if len(bench.words(0)) > n0:
                    break
            if bench.words(0)[n0:] == [0]:
                break
            stops += 1
            bench.run(10_000)
    if host.frames != [(b, True) for b in frames]:
        return f"system received {[(hex(b), ok) for b, ok in host.frames]}, stops {stops}", host.periods
    host.cmd, host.cmd_at = cmd, m.cycle + 3000
    n0 = len(bench.words(0))
    bench.run(3000 + 4200 + 14 * 6000)
    got = bench.words(0)[n0:]
    if host.ack != 0 or len(got) != 1 or ps2_decode(ps2_word_bits(got[0])) != (cmd, True):
        return f"command: acknowledge {host.ack}, words {[hex(w) for w in got]}", host.periods
    th = m.threads[0]
    if th.status or not th.run:
        return f"thread status {th.status}, run {th.run}, {th.stop_reason}", host.periods
    expect = 0 if inhibit is None or inhibit[1] >= 10 else 1
    if stops != expect:
        return f"{stops} frames stopped by the host, expected {expect}", host.periods
    return None, host.periods


def trigger_case(fire, d, offsets=range(16), b=434, match=0x5A):
    """fw/trigger.s: four 8N1 frames (0x11, match, 0xA5, match) on UART RX, the first start edge `offset` cycles
    late; returns (fault or None, latencies: pulse rising edge minus the start edge of the matching frame, cycles)."""
    lat = []
    prog = assemble("trigger.s", FIRE=fire)
    for off in offsets:
        m = pe_model.PeModel(prog.words, owner=owners({13: 0}))  # TRG = uo_out[5] = output pin 13
        data = [0x11, match, 0xA5, match]
        starts = [2000 + off + i * 12 * b for i in range(4)]
        line = uart_line([(s, frame_bits(x, "8N1")) for s, x in zip(starts, data)], b)
        bench = Bench(m, inputs=lambda c, uo, uio, oe, line=line: (value_at(line, c) << 3, 0))
        bench.watch("trg", lambda uo, uio, oe: (uo >> 5) & 1)
        bench.send(0, b, match, d)
        bench.run(starts[-1] + 12 * b + d + 500)
        rises = [c for i, (c, v) in enumerate(bench.changes("trg")) if v == 1 and i > 0]
        th = m.threads[0]
        if th.status or len(rises) != 2 or bench.words(0) != [match, match]:
            return f"offset {off}: status {th.status}, pulses at {rises}, words {bench.words(0)}", lat
        lat += [rises[0] - starts[1], rises[1] - starts[3]]
    return None, lat


@cocotb.test(skip=GL)
async def test_flex_trigger(dut):
    """fw/trigger.s (option O5): pulse delay from the start edge of a matching UART byte at 16 start phases, for the
    pulse timed from T (fire_t) and for the pulse started after the match (fire_n); the smallest delay D."""
    log = dut._log
    log.info(f"fw/trigger.s: {assemble('trigger.s').size} words")
    b, d = 434, 200
    expect = 2 + b // 2 + 8 * b + d  # edge cycle (pin + 2) + half bit + 8 bits + D
    res = {}
    for fire in ("fire_t", "fire_n"):
        fault, lat = trigger_case(fire, d)
        assert fault is None, f"{fire}: {fault}"
        res[fire] = (min(lat), max(lat))
        log.info(f"{fire}, B {b}, D {d}: latency from the start edge {min(lat)} to {max(lat)} cycles "
                 f"(jitter {max(lat) - min(lat)} cycles) over {len(lat)} matches at 16 start phases; "
                 f"T + D formula {expect}")
    smallest = None
    for dd in range(8, 120, 4):
        if trigger_case("fire_t", dd, offsets=range(4))[0] is None:
            smallest = dd
            break
    log.info(f"fire_t: smallest D without LATE {smallest} cycles "
             f"({trigger_case('fire_t', smallest - 4, offsets=range(4))[0] if smallest else ''})")
    assert res["fire_t"][0] == res["fire_t"][1], "the T-timed pulse must have no jitter"


@cocotb.test(skip=GL)
async def test_flex_ps2(dut):
    """fw/ps2_host.s against an independent PS/2 device model and fw/ps2_dev.s against an independent PS/2 system
    model (IBM PS/2 Hardware Interface Technical Reference, Keyboards, pages 38 to 40): frames both ways, command
    with request to send and acknowledge, line contention, device clock 10 to 16.7 kHz, start edges at many phases
    of the idle window, and the highest device clock that the host firmware decodes."""
    log = dut._log
    log.info(f"fw/ps2_host.s: {assemble('ps2_host.s').size} words; fw/ps2_dev.s: {assemble('ps2_dev.s').size} words")
    for period in (5000, 4000, 3000):  # 10, 12.5, 16.7 kHz device clock
        for late in ("rlate", "nolate"):
            faults = [ps2_host_run(period, offset=o, late=late)[0] for o in range(0, 2100, 150)]
            bad = [f for f in faults if f]
            log.info(f"host firmware, device clock {F_CLK / period / 1e3:.1f} kHz, late check {late == 'rlate'}: "
                     f"{len(faults) - len(bad)} of {len(faults)} start phases correct; first fault {(bad or ['none'])[0][:120]}")
            if late == "rlate":
                assert not bad, f"device clock {period} cycles: {bad[0]}"
    for late in ("rlate", "nolate"):  # one frame, start edge every 8 cycles across one idle window and its gap
        faults = [ps2_host_run(4000, offset=o, late=late, cmd=None, frames=[0x5A])[0] for o in range(0, 2112, 8)]
        bad = [o for o, f in zip(range(0, 2112, 8), faults) if f]
        log.info(f"host firmware, 12.5 kHz, one frame, 264 start phases 8 cycles apart, late check {late == 'rlate'}: "
                 f"{len(faults) - len(bad)} correct; wrong at offsets {bad[:12]}")
        if late == "rlate":
            assert not bad, f"late check: wrong at start offsets {bad[:12]}"
    lo, hi = 20, 3000  # smallest device clock period that the host firmware decodes (binary search, phases 0 and 75)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        ok = all(ps2_host_run(mid, offset=o)[0] is None for o in (0, 75))
        lo, hi = (lo, mid) if ok else (mid, hi)
    log.info(f"host firmware: smallest device clock period {hi} cycles ({F_CLK / hi / 1e3:.0f} kHz); "
             f"{hi - 1}: {(ps2_host_run(hi - 1)[0] or 'correct')[:120]}")
    for name, equ in (("12.5 kHz (defaults)", None), ("10 kHz", dict(TSU=900, TLO=2500, THI=1600)),
                      ("16.7 kHz", dict(TSU=500, TLO=1500, THI=1000))):
        for inhibit in (None, (1, 5), (2, 10)):
            fault, periods = ps2_dev_run(equ, inhibit)
            log.info(f"device firmware {name}, inhibit {inhibit}: {fault or 'correct'}; clock period "
                     f"{min(periods) if periods else '-'} to {max(periods) if periods else '-'} cycles")
            assert fault is None, f"device firmware {name}, inhibit {inhibit}: {fault}"


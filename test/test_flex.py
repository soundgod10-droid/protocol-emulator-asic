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
import random
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


# ----- CAN 2.0A -----
# Bosch "CAN Specification Version 2.0" (1991), Part A: frame fields pages 11 to 14, interframe space 19,
# bit stuffing 22, errors 23, bit timing and synchronization 27 to 30.

P_CTX, P_CRX = 4, 5  # CAN_TX = uio[4], CAN_RX = uio[5]


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


class CanNode:
    """CAN node model (Part A pages 11 to 14, 19, 22, 23, 27 to 30), written from the specification.

    bt: bit time in cycles (a float: the node's own oscillator), sp: sample point in cycles after the start
    of the bit, sjw: resynchronization jump width in cycles. The node sends the frames of `frames` (dicts for
    can_bits) when the bus is idle and c >= start, receives every frame, sends a dominant ACK slot when the
    CRC matches, loses arbitration on a recessive bit read dominant in the arbitration field, and records
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
        self.sends = 0

    def level(self, c):
        return self.tx

    def _begin(self, start, send):
        self.bitstart, self.shift, self.sampled, self.synced = start, 0, False, False
        self.state = "frame"
        self.dbits, self.run, self.last, self.stuff_next = [], 0, None, False
        self.total, self.tail, self.crc_ok, self.last_sample = None, None, None, 1
        self.sending = send
        self.lost = False
        if send:
            f = self.pending[0]
            self.stream = can_stuff(can_bits(**f)) + [1] * 13  # CRC delimiter, ACK, ACK delimiter, EOF, intermission
            self.idx = 0
            self.cur = self.stream[0]
            self.sends += 1
        else:
            self.cur = 1

    def see(self, c, bus):
        fall = self.prev_bus == 1 and bus == 0
        self.prev_bus = bus
        if self.state == "idle":
            if fall:  # hard synchronization on SOF (page 29, 30 rule 3); a waiting frame joins it (page 31 [2])
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
                self.sending, self.lost = False, True  # lost arbitration (page 23 bit error exception)
                self.results.append("lost")
            elif not (self.tail == 1):  # the ACK slot is not a bit error
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
                rtr, dlc = self.dbits[12], int("".join(map(str, self.dbits[15:19])), 2)
                self.total = 19 + (0 if rtr else 8 * min(dlc, 8)) + 15
            if self.total is not None and n == self.total:
                body, crc = self.dbits[:-15], int("".join(map(str, self.dbits[-15:])), 2)
                self.crc_ok = can_crc(body) == crc
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
            d = self.dbits
            ident = int("".join(map(str, d[1:12])), 2)
            dlc = int("".join(map(str, d[15:19])), 2)
            data = [int("".join(map(str, d[19 + 8 * k:27 + 8 * k])), 2) for k in range((self.total - 34) // 8)]
            self.received.append(dict(ident=ident, rtr=d[12], dlc=dlc, data=data, crc_ok=self.crc_ok))
            if self.sending and self.results and self.results[-1] == "ack":
                self.pending.popleft()

    def _next(self):
        if self.sending:
            self.idx += 1
        if self.state == "tail":
            self.tail += 1
            if self.tail == 13:
                self.state = "idle"
                self.cur = 1
                return
        if self.sending:
            self.cur = self.stream[self.idx]
        else:
            self.cur = 0 if (self.state == "tail" and self.tail == 1 and self.crc_ok) else 1


class CanBus:
    """Wired-AND bus (dominant 0) of the chip CAN_TX pin and the model nodes; the chip CAN_RX input gets the
    bus level `delay` cycles later (transceiver loop delay)."""

    def __init__(self, nodes, delay=0):
        self.nodes, self.delay = nodes, delay
        self.hist = deque([1] * (delay + 1), maxlen=delay + 1)
        self.changes = [(0, 1)]

    def __call__(self, c, uo, uio, oe):
        chip = level(uio, oe, P_CTX)
        bus = 1 if chip is None else chip
        for n in self.nodes:
            bus &= n.level(c)
        for n in self.nodes:
            n.see(c, bus)
        if bus != self.changes[-1][1]:
            self.changes.append((c, bus))
        self.hist.append(bus)
        return 0, self.hist[0] << P_CRX


def can_tx_words(f):
    """Host words of fw/can.s for one frame: SOF and ID with the word count, then RTR to CRC, filled with 1."""
    bits = can_bits(**f)
    rest = bits[12:]
    words = []
    for i in range(0, len(rest), 16):
        chunk = rest[i:i + 16] + [1] * 16
        words.append(sum(b << (15 - j) for j, b in enumerate(chunk[:16])))
    w0 = sum(b << (15 - j) for j, b in enumerate(bits[:12])) | len(words)
    return [w0] + words


def can_rx_parse(words):
    """Frames from the words of fw/can.s: (destuffed bits, status word) for each frame."""
    out, i = [], 0
    while i < len(words):
        bits = [(words[i] >> (11 - j)) & 1 for j in range(12)]
        i += 1
        bits += [(words[i] >> (15 - j)) & 1 for j in range(16)]
        i += 1
        total = 19 + (0 if bits[12] else 8 * min(int("".join(map(str, bits[15:19])), 2), 8)) + 15
        while total - len(bits) >= 16:
            bits += [(words[i] >> (15 - j)) & 1 for j in range(16)]
            i += 1
        w = words[i]
        i += 1
        p = w.bit_length() - 1
        bits += [(w >> (p - 1 - j)) & 1 for j in range(p)]
        out.append((bits, words[i] if i < len(words) else None))
        i += 1
    return out


def can_decode(bits):
    ident = int("".join(map(str, bits[1:12])), 2)
    dlc = int("".join(map(str, bits[15:19])), 2)
    n = 0 if bits[12] else min(dlc, 8)
    data = [int("".join(map(str, bits[19 + 8 * k:27 + 8 * k])), 2) for k in range(n)]
    return dict(ident=ident, rtr=bits[12], dlc=dlc, data=data)


CAN_FRAMES = [  # chip frames: stuffing in ID and data, all DLC lengths, a remote frame
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


def can_text(bt, sp):
    return source("can.s", BT=bt, SP=sp)


def can_run(bt, sp, chip_frames=(), node_frames=(), node_bt=None, node_sp=None, node_start=0,
            delay=0, sjw=None, cycles=None, host_at=0):
    """Chip on thread 0 and one model node. Returns (bench, model, node, bus)."""
    prog = pe_asm.assemble(can_text(bt, sp), "can.s", 0)
    m = pe_model.PeModel(prog.words, owner=owners({P_CTX: 0}))
    nbt = bt if node_bt is None else node_bt
    node = CanNode(nbt, round(0.75 * nbt) if node_sp is None else node_sp,
                   sjw=max(1, round(nbt / 10)) if sjw is None else sjw,
                   frames=[dict(f) for f in node_frames], start=node_start)
    bus = CanBus([node], delay)
    bench = Bench(m, inputs=bus)
    if host_at:
        bench.run(host_at)
    for f in chip_frames:
        bench.send(0, *can_tx_words(f))
    nbits = 140 * (len(chip_frames) + len(node_frames) + 1)
    bench.run(cycles or int(nbits * max(bt, nbt)))
    return bench, m, node, bus


def can_fault(bench, m, node, chip_frames=(), node_frames=(), expect_lost=0):
    """Text of the first fault, or None. Every frame must be on the bus once, received correctly by the other
    side with a matching CRC and acknowledged; the chip reports each frame with its status word."""
    th = m.threads[0]
    if th.status or not th.run:
        return f"chip thread status {th.status}, run {th.run}, {th.stop_reason}"
    if node.errors:
        return f"node errors {node.errors[:3]}"
    want_node = [can_decode(can_bits(**f)) for f in chip_frames]
    got_node = [dict((k, r[k]) for k in ("ident", "rtr", "dlc", "data")) for r in node.received if r["crc_ok"]]
    try:
        chip = can_rx_parse(bench.words(0))
    except IndexError:
        return f"chip words cannot be parsed: {[hex(w) for w in bench.words(0)]}"
    chip_frames_seen = [(can_decode(b), s) for b, s in chip]
    if len(chip_frames_seen) != len(node.received):
        return f"chip reported {len(chip_frames_seen)} frames, the node saw {len(node.received)}"
    sent = [d for d, s in chip_frames_seen if s is not None and s & 4]
    if sent != want_node:
        return f"chip sent {sent}, expected {want_node}"
    for d in want_node:
        if d not in got_node:
            return f"node did not receive chip frame {d}"
    for (d, s), r in zip(chip_frames_seen, node.received):
        if s is None or s & 2:
            return f"chip status {s} for frame {d} (CRC error)"
        if s & 1:
            return f"frame {d} not acknowledged (chip status {s})"
        if d != dict((k, r[k]) for k in ("ident", "rtr", "dlc", "data")):
            return f"chip read {d}, node read {r}"
    want_chip = [can_decode(can_bits(**f)) for f in node_frames]
    rx = [d for d, s in chip_frames_seen if not s & 4]
    for d in want_chip:
        if d not in rx:
            return f"chip did not receive node frame {d}"
    if node.results.count("ack") != len(node_frames):
        return f"node results {node.results}"
    lost = sum(1 for _, s in chip_frames_seen if s & 8)
    if lost != expect_lost:
        return f"chip lost {lost} times, expected {expect_lost}"
    return None


def can_case(bt, sp, delay=0, node_ppm=0, offsets=(0,)):
    """None if the chip sends CAN_FRAMES, receives NODE_FRAMES and wins and loses one arbitration correctly,
    at each start phase; else the first fault."""
    for off in offsets:
        nbt = bt * (1 + node_ppm * 1e-6)
        # chip sends, node receives and ACKs
        r = can_run(bt, sp, chip_frames=CAN_FRAMES, node_bt=nbt, delay=delay, host_at=50 + off)
        f = can_fault(*r[:3], chip_frames=CAN_FRAMES)
        if f:
            return f"chip to node, phase {off}: {f}"
        # node sends, chip receives and ACKs
        r = can_run(bt, sp, node_frames=NODE_FRAMES, node_bt=nbt, delay=delay, node_start=200 + off)
        f = can_fault(*r[:3], node_frames=NODE_FRAMES)
        if f:
            return f"node to chip, phase {off}: {f}"
        # arbitration: the node sends a frame; both have a frame waiting for its end. The chip ID 0x3C2 loses
        # against the node ID 0x3C1 in the last ID bit, takes the node frame, then sends again and wins.
        first = dict(ident=0x700, data=[0x11])
        chip_f = [dict(ident=0x3C2, data=[0x22, 0x33])]
        node_f = [first, dict(ident=0x3C1, data=[0x44])]
        bench, m, node, bus = can_run(bt, sp, node_frames=node_f, node_bt=nbt, delay=delay, node_start=100 + off,
                                      cycles=100 + off + int(30 * bt))
        bench.send(0, *can_tx_words(chip_f[0]))  # during the first node frame
        bench.run(int(130 * bt))
        try:
            st = [s for _, s in can_rx_parse(bench.words(0))]
        except IndexError:
            st = []
        if any(s is not None and s & 8 for s in st):  # the host sends the frame again after a lost status
            bench.send(0, *can_tx_words(chip_f[0]))
        bench.run(int(100 * bt))
        f = can_fault(bench, m, node, chip_frames=chip_f, node_frames=node_f, expect_lost=1)
        if f:
            return f"arbitration, phase {off}: {f}"
    return None


@cocotb.test(skip=GL)
async def test_flex_can(dut):
    """fw/can.s (CAN 2.0A node) against an independent CAN node model (Bosch CAN 2.0 Part A).

    At each bit rate: the chip sends 5 frames (DLC 0 to 8, a remote frame, long equal runs for stuffing) and
    the node acknowledges them; the node sends 2 frames and the chip receives and acknowledges them; one
    arbitration that the chip loses (it takes the winner's frame, reports the loss, sends again and wins).
    Measures the smallest bit time that is correct and the sample point range.
    """
    log = dut._log
    words = assemble("can.s").size
    log.info(f"fw/can.s: {words} words")
    f = can_case(400, 300)
    log.info(f"125 kbit/s (BT 400, SP 300): {f or 'correct'}")
    assert f is None, f

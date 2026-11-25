"""A CAN 2.0A node on the general serial line unit (tb.u_slu_ut: pe_slu with SPEC 0, FULL 1) by configuration only,
with the frame-level work of the thread played by the test at the thread's pace (one port access or one
instruction per slot of 4 cycles, docs/isa.md 3.1; a WAIT on the unit's event completes within 4 cycles of the
flag, isa 6.3), against the CAN node model of test/can_node.py (written from the Bosch CAN Specification 2.0 Part
A; the model of the CAN helper study on branch rt1-can). Research test (branch rt1-g1).

Configuration (no CAN constant in the unit; every value below is a configuration word): NRZ at 60 cycles a bit
(1 Mbit/s at 60 MHz), decision point sample 48 of 60 (80 %), resynchronisation on falling (recessive to dominant)
edges only, hard resync inside a window of 12 samples (SJW 4 tq of 3 cycles), stuffing after 5 equal bits with the
start pattern's last bit in the first run (EXT2 = 1), CRC-15 x^15+x^14+x^10+x^8+x^7+x^4+x^3+1 (reflected 0x4CD1,
preset 0, sent as it is), start pattern = the SOF bit (length 1), end of frame by length (written by the thread
from the identifier, RTR and DLC words while the frame runs), bit monitor with the arbitration field of the 12 data
bits after the SOF, tail of 60 cycles (the CRC delimiter), time stamp at EOP, event on every flag the thread uses.

The thread (class Thread) waits on the unit's event (WAIT, 1 slot), reads STATUS and acts: pops the words; computes
the frame length from word 0 (identifier, RTR, IDE, r0, DLC3, DLC2) and word 1 (DLC1, DLC0: a 4-entry table in the
data memory) and writes EXT0 before the frame ends (the tightest case is a DLC 0 frame: word 1 comes at data bit
31, the frame ends at bit 33); acknowledges a frame with a correct CRC by a 1-bit frame at TSTAMP + 68 (the ACK slot
in the receiver's grid); turns the receiver off from the EOP to 200 cycles after it (the ACK edge must not be taken
for a SOF) and on again in the EOF; reads the ACK slot of its own frame through the LINE port 40 cycles after
TXDONE (the tail end = the ACK slot start); re-sends a frame that lost the arbitration after the winner's frame;
starts a frame at TSTAMP + 768 after the last EOP (about 10 cycles before the end of the intermission, inside its
third bit, so that another node with a pending frame joins this SOF and arbitrates) or, after reset, after 700
cycles of idle line.
Not done (as fw/can.s): error frames, overload frames, error counters. A node on the unit does not join a SOF seen
in the intermission with its own identifier (CAN 2.0B page 31): its frames start at a time it computes; when another
SOF comes first, the armed frame is withdrawn and started again after that frame.
"""

import datetime
import os

import cocotb
from cocotb.triggers import FallingEdge

from test_slu import (GL, DIN, P_TXDATA, P_RXDATA, P_STATUS, P_EVMASK, P_TXCTRL, P_TXTIME, P_RXINFO, P_TSTAMP,
                      P_LINE, P_CFGADR, P_CFGDAT, P_TXLAST, slu_reset, slu_reset_again, mode)
from test_rt1_outside import SluRx, EXT0, EXT1, EXT2, NOPIN, F_TXDONE, F_RXSOP, F_RXEOP, F_CRCA, F_STUFF, pack_words, words_to_bits
from can_node import Node, frame_bits, stream_of, crc, to_bits, of_bits

F_TXLATE, F_TXUNDER, F_RXWORD, F_RXOVER, F_ARBLOST, F_BITERR = 1 << 1, 1 << 2, 1 << 3, 1 << 9, 1 << 13, 1 << 14
EVENTS = F_TXDONE | F_TXLATE | F_TXUNDER | F_RXWORD | F_RXSOP | F_RXEOP | F_ARBLOST | F_BITERR
BT, SP, WIN = 60, 48, 12
CRC15_REF = 0x4CD1                   # x^15+x^14+x^10+x^8+x^7+x^4+x^3+1 (0x4599) with its 15 bits reversed
ARB_LEN = 13                         # EXT1[15:10]: a mismatch at data bit k with k + 1 < 13 (k = 0..11) is a lost arbitration
MODE_RX = mode(0, rx=1, stuff=1, ones=0, k=5, eop_len=1, tsel=0)
MODE_RXOFF = mode(0, rx=0, stuff=1, ones=0, k=5, eop_len=1, tsel=0)
SLOT = 4
LEN_TABLE = {0: 0, 1: 16, 2: 8, 3: 24}  # data bits for (DLC0 << 1 | DLC1) of word 1: 8 x (2 DLC1 + DLC0)

CAN_CFG = {
    1: BT,                                            # TXPER: one bit
    2: BT,                                            # LOOP0: 60 samples a bit, hard resync (kp 0)
    3: 0,
    4: WIN | (WIN << 6) | (1 << 13),                  # LOOP2: window 12, acquisition window 12, init on the first edge
    5: 0,                                             # LOOP3: no acquisition count, pattern from the first bit
    6: 0, 7: 0,                                       # PAT: the SOF (one bit, 0)
    8: DIN | NOPIN,                                   # SEL: DIN on pin 0, no AUX
    9: CRC15_REF, 10: 0, 11: 0, 12: 0, 13: 0,         # CRC A reflected polynomial, residue 0; no CRC B
    14: 15,                                           # CRCW: width 15, preset 0, no skipped words or bits
    15: BT,                                           # TAIL: 60 cycles recessive = the CRC delimiter
    16: 0, 17: 0, 18: 0, 19: 0, 20: 0,
    EXT0: 1023 | (SP << 10),                          # length open (the thread writes it), decision point 48
    EXT1: 1 | (1 << 1) | (1 << 2) | (ARB_LEN << 10),  # bit monitor, CRC not complemented, falling edges only
    EXT2: 1,                                          # the SOF is the first bit of the first stuff run
    0: MODE_RX,                                       # written last (SluRx)
}


def crc_ref(bits, poly_ref, width):
    """The unit's CRC register (reflected, shift right, LSB first); it transmits bit 0 first."""
    c, mask = 0, (1 << width) - 1
    for b in bits:
        c = ((c >> 1) ^ (poly_ref if (c ^ b) & 1 else 0)) & mask
    return c


class Stamped(list):
    """A list that records the bus cycle of each append (the node's results with their times)."""

    def __init__(self, bus):
        super().__init__()
        self.bus, self.stamps = bus, []

    def append(self, x):
        super().append(x)
        self.stamps.append(self.bus.c)


class Bus:
    """Wired-AND bus of the unit's TXA (tx_v bit 0 in NRZ: 1 recessive) and the model nodes; the unit's DIN gets the
    bus `delay` cycles later (transceiver loop delay). Runs at each falling clock edge."""

    def __init__(self, dut, nodes, delay):
        self.dut, self.nodes, self.delay = dut, nodes, delay
        self.hist = [1] * (delay + 1)
        self.c = 0
        self.changes = [(0, 1)]
        self.stop = False
        self.task = cocotb.start_soon(self.run())

    async def run(self):
        d = self.dut
        fe = FallingEdge(d.clk)
        while not self.stop:
            await fe
            bus = int(d.slu_txv.value) & 1
            for n in self.nodes:
                bus &= n.level(self.c)
            for n in self.nodes:
                n.see(self.c, bus)
            if bus != self.changes[-1][1]:
                self.changes.append((self.c, bus))
            self.hist.append(bus)
            del self.hist[0]
            d.slu_pins.value = self.hist[0] << DIN
            self.c += 1


class ArbPeek:
    """Test-only view of the unit: the data bit index at which the bit monitor reported a lost arbitration."""

    def __init__(self, dut):
        self.dut, self.hits, self.stop = dut, [], False
        self.task = cocotb.start_soon(self.run())

    async def run(self):
        u = self.dut.u_slu_ut
        fe = FallingEdge(self.dut.clk)
        prev = 0
        while not self.stop:
            await fe
            v = (int(u.sticky.value) >> 13) & 1
            if v and not prev:
                self.hits.append(int(u.rx_bits.value) - 1)
            prev = v


class Thread:
    """The frame-level program at the thread's pace: every port access and every instruction takes one slot; a WAIT
    on the unit's event line costs one slot and completes within 4 cycles of the flag."""

    def __init__(self, dut, s, bus, slot=SLOT, ack=True):
        self.d, self.s, self.bus, self.slot_cycles, self.ack_enabled = dut, s, bus, slot, ack
        self.slots = 0
        self.rx_frames, self.tx_results, self.events = [], [], []
        self.word_slots, self.len_margins, self.ack_samples, self.leads = [], [], [], []
        self.sending, self.own, self.line_busy = None, False, False
        self.base = 33

    def now(self):
        return int(self.d.slu_now.value)

    def ev(self, what, *a):
        self.events.append((self.bus.c, what) + a)

    async def _rest(self, n):
        for _ in range(n):
            await FallingEdge(self.d.clk)

    async def ior(self, a, pop=False):
        v = await self.s.rd(a, pop=pop)                         # 2 falling edges
        await self._rest(self.slot_cycles - 2)
        self.slots += 1
        return v

    async def iow(self, a, v):
        await self.s.wr(a, v & 0xFFFF)
        await self._rest(self.slot_cycles - 2)
        self.slots += 1

    async def work(self, n):
        await self._rest(n * self.slot_cycles)
        self.slots += n

    async def wait_ev(self, deadlines):
        """WAIT on the unit's event line, with a time limit at the earliest of the deadlines (a TADD + WAIT with
        timeout in firmware). Returns when the event line is high or a deadline is due."""
        fe = FallingEdge(self.d.clk)
        while True:
            if int(self.d.slu_ev.value) or any(self.due(t) for t in deadlines if t is not None):
                break
            await fe
        self.slots += 1

    async def set_len(self, n):
        await self.iow(P_CFGADR, EXT0)
        await self.iow(P_CFGDAT, (n & 0x3FF) | (SP << 10))

    async def set_mode(self, m):
        await self.iow(P_CFGADR, 0)
        await self.iow(P_CFGDAT, m)

    def due(self, t):
        return t is not None and ((self.now() - t) & 0xFFFF) < 0x8000

    async def length_from_words(self, words):
        """Frame length in data bits (identifier to the last CRC bit) when the words so far give it, else None.
        Word 0: data bits 0..10 identifier, 11 RTR, 12 IDE, 13 r0, 14 DLC3, 15 DLC2 (first bit in bit 0); word 1:
        bit 0 DLC1, bit 1 DLC0. The instructions of a thread are counted as work."""
        if len(words) == 1:
            w = words[0]
            await self.work(4)                                   # MOV, ANDI (RTR), BNE, SHR 14
            if (w >> 11) & 1:
                return 33                                        # remote frame: no data
            hi = ((w >> 14) & 1) * 2 + ((w >> 15) & 1)           # DLC[3:2]
            if hi >= 2:
                return 97                                        # DLC 8 to 15: 8 bytes
            self.base = 33 + 32 * hi
            await self.work(2)                                   # SHL 5, ADDI 33
            return None
        if len(words) == 2:
            w = words[1]
            await self.work(4)                                   # ANDI 3, LD table (2 slots), ADD
            return self.base + LEN_TABLE[w & 3]
        return None

    async def run(self, frames, until, deferred=(), node=None, first_tx=700):
        queue = list(frames)
        deferred = list(deferred)
        tx_at = (self.now() + first_tx) & 0xFFFF if queue else None
        rx_on_at, last_eop, len_done = None, None, None
        ack_pending = False
        words, length_known = [], False
        feed = []
        L = None
        while self.bus.c < until:
            arm_at = ((tx_at - 100) & 0xFFFF) if (tx_at is not None and self.sending is None and queue) else None
            await self.wait_ev([rx_on_at, arm_at, (self.now() + 2000) & 0xFFFF])
            st = await self.ior(P_STATUS)
            await self.work(3)                                   # the flag tests
            if st & F_RXWORD:
                s0 = self.slots
                w = await self.ior(P_RXDATA, pop=True)
                words.append(w)
                if not self.own and not length_known:
                    L = await self.length_from_words(words)
                    if L is not None:
                        await self.set_len(L)
                        length_known, len_done = True, self.now()
                        self.ev("LEN", L)
                self.word_slots.append(self.slots - s0)
            if st & F_RXSOP:
                await self.iow(P_STATUS, F_RXSOP)
                self.line_busy, words, length_known = True, [], False
                self.ev("SOP")
                if self.sending is not None:
                    txst = (await self.ior(P_TXCTRL)) & 0xF      # 1 armed, 2 data, 3 CRC
                    if txst in (2, 3):
                        self.own, length_known = True, True
                    else:                                        # another SOF came first: withdraw the armed frame
                        await self.iow(P_TXCTRL, (1 << 8) | (1 << 10))   # abort and flush
                        self.ev("WITHDRAWN", self.sending["ident"])
                        self.sending, tx_at, feed = None, None, []
            if st & F_ARBLOST:
                await self.iow(P_STATUS, F_ARBLOST)
                self.tx_results.append(("lost", self.sending["ident"] if self.sending else None))
                self.ev("ARBLOST")
                self.sending, self.own, tx_at, feed = None, False, None, []
                await self.iow(P_TXCTRL, 1 << 10)                # flush the words of the lost frame (the ACK frame would send them)
                await self.set_len(1023)
                length_known, L = False, None
                for k in range(len(words)):                      # the winner's words received so far
                    L = await self.length_from_words(words[:k + 1])
                if L is not None:
                    await self.set_len(L)
                    length_known, len_done = True, self.now()
                    self.ev("LEN", L)
            if st & F_BITERR:
                await self.iow(P_STATUS, F_BITERR)
                self.tx_results.append(("biterr", self.sending["ident"] if self.sending else None))
                self.ev("BITERR")
                self.sending, self.own, tx_at, feed = None, False, None, []
                await self.iow(P_TXCTRL, 1 << 10)                # flush the words of the failed frame
            if st & (F_TXLATE | F_TXUNDER):
                await self.iow(P_STATUS, F_TXLATE | F_TXUNDER)
                self.ev("TXLATE/UNDER", st & (F_TXLATE | F_TXUNDER))
            if st & F_RXEOP:
                t = await self.ior(P_TSTAMP)
                await self.iow(P_STATUS, F_RXEOP | F_STUFF | F_RXOVER)
                crc_ok, stuff_err, over = bool(st & F_CRCA), bool(st & F_STUFF), bool(st & F_RXOVER)
                self.ev("EOP", t, crc_ok, stuff_err, self.own)
                await self.set_mode(MODE_RXOFF)                  # no SOF detection until the ACK delimiter
                if not self.own:
                    acked = False
                    if crc_ok and not stuff_err and self.ack_enabled:
                        await self.iow(P_TXCTRL, 1)              # a 1-bit frame without CRC: the ACK
                        await self.iow(P_TXLAST, 0)
                        await self.iow(P_TXTIME, (t + 68) & 0xFFFF)
                        ack_pending, acked = True, True
                        self.ev("ACKTX", (t + 68) & 0xFFFF)
                info = await self.ior(P_RXINFO)
                while (info >> 5) & 3:
                    words.append(await self.ior(P_RXDATA, pop=True))
                    info = await self.ior(P_RXINFO)
                if not self.own:
                    nb = L if length_known else 16 * len(words)
                    self.rx_frames.append(dict(words=list(words), nbits=nb, crc_ok=crc_ok, stuff_err=stuff_err,
                                               over=over, acked=acked, t=t))
                    if length_known and len_done is not None:
                        self.len_margins.append(((t - len_done) & 0xFFFF, L))
                self.line_busy = False
                last_eop = t
                rx_on_at = (t + 200) & 0xFFFF
                if queue and self.sending is None:
                    tx_at = (t + 768) & 0xFFFF
            if st & F_TXDONE:
                await self.iow(P_STATUS, F_TXDONE)
                if ack_pending:
                    ack_pending = False
                elif self.sending is not None and self.own:
                    t_s = (self.now() + 40) & 0xFFFF             # TADD 40 then the read: the ACK slot's 2nd third
                    while not self.due(t_s):
                        await FallingEdge(self.d.clk)
                    self.slots += 2
                    line = await self.ior(P_LINE)
                    ack = not ((line >> 2) & 1)                  # LINE[2] = DIN: dominant = 0
                    self.ack_samples.append(ack)
                    self.tx_results.append(("done", self.sending["ident"], ack))
                    self.ev("TXDONE", self.sending["ident"], ack)
                    queue.pop(0)
                    self.sending, self.own, feed = None, False, []
                    if queue and last_eop is not None:
                        tx_at = (last_eop + 768) & 0xFFFF
                    if not queue and deferred and node is not None:
                        node.pending.extend(dict(f) for f in deferred)   # the other node's frames come after ours
                        deferred = []
            if self.due(rx_on_at):
                rx_on_at = None
                await self.set_len(1023)
                await self.set_mode(MODE_RX)
                self.ev("RXON")
            if feed:
                free = await self.ior(P_TXDATA)
                if free:
                    w = feed.pop(0)
                    await self.iow(P_TXLAST if not feed else P_TXDATA, w)
            if (self.sending is None and queue and tx_at is not None and not self.line_busy
                    and ((tx_at - self.now()) & 0xFFFF) <= 100):
                f = queue[0]
                bits = frame_bits(f)[0]
                wds, last = pack_words(bits)
                await self.set_len(len(bits) - 1 + 15)           # identifier to the last CRC bit
                await self.iow(P_TXCTRL, (last % 16) | (1 << 5))
                for i, w in enumerate(wds[:2]):
                    await self.iow(P_TXLAST if i == len(wds) - 1 else P_TXDATA, w)
                feed = wds[2:]
                self.leads.append((tx_at - self.now()) & 0xFFFF)
                await self.iow(P_TXTIME, tx_at)
                self.ev("TXTIME", tx_at, f["ident"])
                self.sending, tx_at = f, None


CHIP_FRAMES = [
    dict(ident=0x0F0, data=[0x00, 0x00, 0xFF, 0xFF, 0x00, 0x7E, 0x81, 0x55]),
    dict(ident=0x7EF, data=[]),
    dict(ident=0x123, data=[0xA5]),
    dict(ident=0x000, data=[0x1F, 0xE0, 0x3C]),
    dict(ident=0x555, data=[], rtr=True, dlc=4),
]
NODE_FRAMES = [
    dict(ident=0x3C1, data=[0xFF, 0x00, 0x0F]),
    dict(ident=0x001, data=[0x80, 0x01, 0xFE, 0x7F, 0xC0, 0x03, 0xAA]),
    dict(ident=0x0A5, data=[]),                                  # DLC 0: the tightest length path
    dict(ident=0x2AA, data=[0x0F, 0xF0]),                        # DLC 2: word 1 path
]


async def scenario(dut, chip_frames, node_frames, delay, ppm, node_start=None, bad_crc=False, stuff_err=False,
                   node_rate=None, monitor=True, slot=SLOT, first_tx=700, ack=True, peek=False):
    """One run on a freshly reset unit. node_start None: the node gets its frames after the unit's own frames are
    done (the thread hands them over), so the two sides do not arbitrate; a number: the node has its frames from
    that bus cycle on. Returns (thread, node, bus, peek)."""
    s = SluRx(dut)
    if node_start is not None and node_start < 300:
        node_start = 300                                     # the unit's configuration takes about 100 cycles
    btn = BT * (1 + ppm * 1e-6) if node_rate is None else node_rate
    deferred = node_start is None and bool(chip_frames)
    node = Node(btn, round(0.8 * btn), frames=[] if deferred else [dict(f) for f in node_frames],
                start=node_start if node_start is not None else 0, bad_crc=bad_crc, stuff_err=stuff_err)
    bus = Bus(dut, [node], delay)
    node.results = Stamped(bus)
    pk = ArbPeek(dut) if peek else None
    cfg = dict(CAN_CFG)
    if not monitor:
        cfg[EXT1] &= ~1
    await s.cfg(cfg)
    await s.wr(P_EVMASK, EVENTS)
    th = Thread(dut, s, bus, slot, ack=ack)
    total_bits = sum(len(stream_of(f)[0]) + 3 for f in list(chip_frames) + list(node_frames)) + 60
    until = bus.c + total_bits * 2 * BT + 20 * BT * (2 + len(chip_frames) + len(node_frames))
    await th.run(chip_frames, until, deferred=node_frames if deferred else (), node=node, first_tx=first_tx)
    bus.stop = True
    if pk is not None:
        pk.stop = True
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    return th, node, bus, pk


def fault(th, node, chip_frames, node_frames, expect_lost=None):
    """Text of the first fault, or None. expect_lost None: any number of legitimate arbitration losses."""
    if node.errors:
        return f"node errors {node.errors[:3]}"
    sent = [frame_bits(f)[0] for f in chip_frames]
    got_node = [r["bits"] for r in node.received if r["bits"] in sent]
    for b in sent:
        if got_node.count(b) != 1:
            return f"chip frame {hex(of_bits(b[1:12]))}: on the bus {got_node.count(b)} times (node view)"
    bad = [hex(r["ident"]) for r in node.received if not r["crc_ok"]]
    if bad:
        return f"node read frames with a bad CRC: {bad}"
    done = [r for r in th.tx_results if r[0] == "done"]
    if len(done) != len(chip_frames):
        return f"TXDONE {len(done)} times for {len(chip_frames)} frames: {th.tx_results}"
    if not all(r[2] for r in done):
        return f"own frame without ACK seen: {th.tx_results}"
    lost = [r for r in th.tx_results if r[0] == "lost"]
    if expect_lost is not None and len(lost) != expect_lost:
        return f"ARBLOST {len(lost)} times, expected {expect_lost}: {th.tx_results}"
    if any(r[0] == "biterr" for r in th.tx_results):
        return f"bit error: {th.tx_results}"
    rx_ok = [words_to_bits(r["words"], r["nbits"])[:r["nbits"] - 15] for r in th.rx_frames
             if r["crc_ok"] and not r["stuff_err"]]
    for f in node_frames:
        b = frame_bits(f)[0][1:]
        if rx_ok.count(b) != 1:
            return (f"node frame {hex(f['ident'])}: received {rx_ok.count(b)} times with CRC ok; unit frames "
                    f"{[(hex(of_bits(x[:11])), len(x)) for x in rx_ok]}, all "
                    f"{[(r['nbits'], r['crc_ok'], r['stuff_err']) for r in th.rx_frames]}")
    if any(r["over"] for r in th.rx_frames):
        return "RXOVER"
    if node.results.count("ack") != len(node_frames) or node.pending:
        return f"node results {list(node.results)}, {len(node.pending)} frames pending"
    return None


def summary(th):
    return (f"leads at TXTIME {th.leads}; slots per word event max {max(th.word_slots) if th.word_slots else None}; "
            f"length written before the end by (cycles, length) min {min(th.len_margins) if th.len_margins else None}; "
            f"ACK samples {th.ack_samples}; lost {sum(1 for r in th.tx_results if r[0] == 'lost')}")


class FileLog:
    """Each line goes to the simulator log and to test/output/rt1_can_g1.txt (appended; a header with the test name
    and the wall clock in UTC per test), because the run log is cut when it is long."""

    PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "rt1_can_g1.txt")

    def __init__(self, log, name):
        self.log = log
        os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
        with open(self.PATH, "a", encoding="utf-8") as f:
            f.write(f"== {name} at {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}\n")

    def info(self, text):
        self.log.info(text)
        with open(self.PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def timeline(th, node, bus, n=60):
    """The first n events of the thread, the node's results and the bus edges, in bits from the first SOF."""
    t0 = bus.changes[1][0] if len(bus.changes) > 1 else 0
    ev = [(round((e[0] - t0) / BT, 2),) + e[1:] for e in th.events[:n]]
    nr = [(round((c - t0) / BT, 2), r) for r, c in zip(list(node.results), node.results.stamps)]
    edges = [(round((c - t0) / BT, 2), v) for c, v in bus.changes[1:n]]
    return f"thread {ev}; node {nr}; bus {edges}"


@cocotb.test(skip=GL)
async def test_rt1_can_g1_1m(dut):
    """CAN 2.0A at 1 Mbit/s on the general unit: the unit sends CHIP_FRAMES (DLC 0 to 8, a remote frame, runs that
    need stuffing) and the node acknowledges each (the thread reads the ACK slot); then the node sends NODE_FRAMES
    (DLC 0, 2, 3, 7) and the unit receives them with the CRC correct, the thread acknowledges; node at -0.5 % and
    +0.5 %; loop delays of 10 and 14 cycles. Then the arbitration case: the node has two frames pending from the
    start; the unit's frame (0x7F0) starts in the intermission after each node frame, loses twice at data bit 0
    (ARBLOST), the unit receives the winners, and the frame is sent after them."""
    bits = [int((i * 7919) % 3 == 0) for i in range(100)]
    c = crc_ref(bits, CRC15_REF, 15)
    assert [(c >> i) & 1 for i in range(15)] == to_bits(crc(bits, 0x4599, 15), 15), \
        "test fault: the reflected polynomial does not give the CAN CRC-15 bit sequence"
    await slu_reset(dut)
    log = FileLog(dut._log, "test_rt1_can_g1_1m")
    lines = []
    for ppm in (-5000, 5000):
        for delay in (10, 14):
            th, node, bus, _ = await scenario(dut, CHIP_FRAMES, NODE_FRAMES, delay, ppm)
            f = fault(th, node, CHIP_FRAMES, NODE_FRAMES, expect_lost=0)
            lines.append(f"1 Mbit/s node {ppm:+d} ppm delay {delay}: {'PASS' if f is None else 'FAIL: ' + f}; {summary(th)}")
            log.info(lines[-1])
            if f is not None:
                log.info(timeline(th, node, bus))
                log.info("thread rx %s; node received %s" % ([(r["nbits"], r["crc_ok"], r["acked"]) for r in th.rx_frames],
                                                             [hex(r["ident"]) for r in node.received]))
            assert f is None, lines[-1]
            await slu_reset_again(dut)
    cf = [dict(ident=0x7F0, data=[0x22, 0x33])]
    nf = [dict(ident=0x100, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
    th, node, bus, pk = await scenario(dut, cf, nf, 14, 0, node_start=0, peek=True)
    f = fault(th, node, cf, nf, expect_lost=2)
    lines.append(f"arbitration (node 0x100 and 0x3C1 against the unit's 0x7F0): {'PASS' if f is None else 'FAIL: ' + f}; "
                 f"lost at data bits {pk.hits}; node results {list(node.results)}; {summary(th)}")
    if f is not None or pk.hits != [0, 0]:
        log.info(timeline(th, node, bus))
    for line in lines:
        log.info(line)
    assert f is None, lines[-1]
    assert pk.hits == [0, 0], lines[-1]


@cocotb.test(skip=GL)
async def test_rt1_can_g1_arb_bits(dut):
    """Arbitration lost at each of the 11 identifier bits and at the RTR bit: the unit's frame has identifier 0x7FF
    (remote frame for the RTR case); the node's second frame has the same identifier with bit k dominant (or the data
    frame of the same identifier); both start in the intermission after the node's first frame; the unit must report
    ARBLOST at data bit k, receive the winner and send its frame after it."""
    await slu_reset(dut)
    log = FileLog(dut._log, "test_rt1_can_g1_arb_bits")
    lines = []
    for k in range(12):
        if k < 11:
            cf = [dict(ident=0x7FF, data=[0x11])]
            nf = [dict(ident=0x010, data=[0x01]), dict(ident=0x7FF & ~(1 << (10 - k)), data=[0x22])]
        else:
            cf = [dict(ident=0x7FF, data=[], rtr=True, dlc=1)]
            nf = [dict(ident=0x010, data=[0x01]), dict(ident=0x7FF, data=[0x22])]
        th, node, bus, pk = await scenario(dut, cf, nf, 14, 0, node_start=0, peek=True)
        f = fault(th, node, cf, nf, expect_lost=1)
        lines.append(f"lost at bit {k}: {'PASS' if f is None and pk.hits == [k] else 'FAIL: ' + str(f)}; "
                     f"ARBLOST at data bits {pk.hits}; node results {list(node.results)}")
        log.info(lines[-1])
        if f is not None or pk.hits != [k]:
            log.info(timeline(th, node, bus))
        assert f is None and pk.hits == [k], lines[-1]
        await slu_reset_again(dut)
    for line in lines:
        log.info(line)


@cocotb.test(skip=GL)
async def test_rt1_can_g1_controls(dut):
    """Controls that must fail or be rejected: (a) the node's frame with a corrupted CRC: CRCA_OK 0, no ACK (node:
    no ack); (b) a stuff bit left out: STUFFERR, no ACK; (c) the node at 800 kbit/s: no frame with a correct CRC;
    (d) the bit monitor off in the arbitration case: no ARBLOST, the node sees a bit error; (e) the thread at half
    pace (8-cycle slots): the DLC 0 frame's length is written late (recorded, pass or fail)."""
    await slu_reset(dut)
    log = dut._log
    nf = [dict(ident=0x3C1, data=[0xFF, 0x00, 0x0F])]
    th, node, bus, _ = await scenario(dut, [], nf, 14, 0, node_start=0, bad_crc=True)
    r = [(x["crc_ok"], x["acked"]) for x in th.rx_frames]
    log.info(f"(a) corrupted CRC: unit frames (crc_ok, acked) {r}, node results {list(node.results)}")
    assert r and not any(ok for ok, _ in r) and not any(a for _, a in r), r
    assert "no ack" in node.results and "ack" not in node.results, list(node.results)
    await slu_reset_again(dut)
    th, node, bus, _ = await scenario(dut, [], nf, 14, 0, node_start=0, stuff_err=True)
    r = [(x["crc_ok"], x["stuff_err"], x["acked"]) for x in th.rx_frames]
    log.info(f"(b) stuff error: unit frames (crc_ok, stuff_err, acked) {r}, node results {list(node.results)}")
    assert any(se for _, se, _ in r) and not any(a for _, _, a in r), r
    await slu_reset_again(dut)
    th, node, bus, _ = await scenario(dut, [], nf, 14, 0, node_start=0, node_rate=75.0)
    ok = [x for x in th.rx_frames if x["crc_ok"] and words_to_bits(x["words"], x["nbits"])[:x["nbits"] - 15] == frame_bits(nf[0])[0][1:]]
    log.info(f"(c) node at 800 kbit/s: frames received correctly {len(ok)}; frames {[(x['nbits'], x['crc_ok']) for x in th.rx_frames]}")
    assert not ok
    await slu_reset_again(dut)
    cf = [dict(ident=0x7F0, data=[0x22, 0x33])]
    nf2 = [dict(ident=0x100, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
    th, node, bus, _ = await scenario(dut, cf, nf2, 14, 0, node_start=0, monitor=False)
    log.info(f"(d) bit monitor off: thread tx {th.tx_results}, node results {list(node.results)}, node errors {node.errors[:2]}")
    assert not any(x[0] == "lost" for x in th.tx_results)
    assert node.errors or "bit error" in node.results, (list(node.results), node.errors)
    await slu_reset_again(dut)
    nf3 = [dict(ident=0x0A5, data=[]), dict(ident=0x2AA, data=[0x0F, 0xF0])]
    th, node, bus, _ = await scenario(dut, [], nf3, 14, 0, node_start=0, slot=8)
    f = fault(th, node, [], nf3)
    log.info(f"(e) thread at 8-cycle slots, DLC 0 and 2 frames: {'PASS' if f is None else 'FAIL: ' + f}; {summary(th)}")

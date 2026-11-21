# SPDX-License-Identifier: Apache-2.0
"""MDB/ICP master firmware (fw/mdb.s) against an independent MDB coin changer model, in the reference model and on the
RTL at 60 MHz.

What it does:
  The changer model (MdbChanger) is written from NAMA MDB/ICP Version 4.3 (July 2019): byte format 2.1, block
  format, CHK byte and response codes 2.2, peripheral addresses 2.3, bus reset, timing 3.1 and 3.2, changer
  commands 5.2 and 5.3, the POLL status code "Changer was Reset" (00001011B), and the CHK example of 2.2 (the
  response to a STATUS/SETUP command, CHK 2CH), which the model sends as its SETUP reply. It does not use the
  firmware, the assembler or the processor model. It reads the master's 11-bit bytes (start bit, 8 data bits LSB
  first, mode bit, stop bit) in the middle of each bit and checks: the bit rate inside 9600 +1 %/-2 %, at most
  t-inter-byte = 1.0 ms between the bytes of a block, the mode bit on the address byte only, the CHK, at most 36
  bytes, a bus reset of at least 100 ms, the setup time of at least 200 ms after it, and the master's ACK or NAK
  to a data block within t-response = 5 ms. It answers its own address (08H) after a set delay (up to the 5 ms
  limit), at its own bit rate inside the tolerance and with a set gap between its bytes: ACK to RESET, COIN TYPE
  and DISPENSE; the "Changer was Reset" block to the first POLL after a reset, repeated until the master ACKs it;
  the 2.2 example block to SETUP; NAK to a block with a wrong CHK.
  The model test runs RESET, POLL, SETUP, COIN TYPE, POLL and a block to an absent bill validator (30H) with the
  changer at the bit-rate corners -2 % and +1 %, reply delays 0.2 and 4.9 ms and byte gaps 0 and 0.9 ms; a SETUP
  reply with a wrong CHK (the master must NAK it); a bus reset followed by POLL. Negative controls: a reply after
  5.5 ms (the master must give up) and a master bit rate 2.4 % slow (HB 3200). The RTL test runs RESET, POLL and
  SETUP on the chip with all times divided by 5 (48,000 bit/s), in lockstep with the reference model; the same
  test runs on the gate-level netlist (gl_fw workflow).

Why:
  Eleventh row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode decoder
  at the pinned commit decodes MDB, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins at logic level; the MDB bus interface (optocouplers, 34 V
bus, section 4) is not simulated.
"""

import os
import sys

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
P_TX = 12  # MDB TX = uo_out[4]
UI_RX = 3  # MDB RX = ui_in[3]

# MDB/ICP 4.3 section 3.2 (times in us)
BIT_US = 1e6 / 9600
RATE_TOL = (-0.02, 0.01)
T_INTER_US, T_RESP_US, T_BREAK_US, T_SETUP_US = 1000.0, 5000.0, 100_000.0, 200_000.0
ACK, RET, NAK = 0x00, 0xAA, 0xFF
CHANGER = 0x08
CMD_DATA = {0: 0, 1: 0, 2: 0, 3: 0, 4: 4, 5: 1}  # RESET, SETUP, TUBE STATUS, POLL, COIN TYPE (4), DISPENSE (1)
SETUP_EXAMPLE = [0x02, 0x00, 0x01, 0x05, 0x02, 0x00, 0x07, 0x01, 0x02, 0x05, 0x14, 0xFF]  # 2.2, CHK 2CH
JUST_RESET = 0x0B  # 5.4 POLL status: changer was reset


class MdbChanger:
    """MDB coin changer on the bus (see the module text). scale: all bus times divided by it (RTL test); rate: the
    changer's bit rate as a factor of 9600; resp_us, gap_us: its reply delay and gap between its bytes;
    corrupt: send the SETUP reply with a wrong CHK."""

    def __init__(self, us=F_MHZ, scale=1, rate=1.0, resp_us=200.0, gap_us=0.0, corrupt=False):
        self.cyc = lambda u: u * us / scale
        self.bit = self.cyc(BIT_US)
        self.bit_p = self.bit / rate
        self.resp, self.gap, self.corrupt = self.cyc(resp_us), self.cyc(gap_us), corrupt
        self.prev, self.line = 1, 1
        self.tx_edges = []
        self.rx = None
        self.low_since = None
        self.maybe_break = False
        self.block, self.need, self.last_end = [], 0, None
        self.await_reply = None
        self.sent = None
        self.just_reset = True  # power-up
        self.reset_end = None
        self.blocks, self.replies, self.breaks, self.errors = [], [], [], []

    def _err(self, c, text):
        self.errors.append(f"cycle {c}: {text}")

    def _send(self, t0, values):
        t = t0
        for v in values:
            bits = [0] + [(v >> i) & 1 for i in range(9)] + [1]
            for i, b in enumerate(bits):
                self.tx_edges.append((round(t + i * self.bit_p), b))
            t += 11 * self.bit_p + self.gap
        return t - self.gap

    def _respond(self, c, end, blk):
        cmd = blk[0] & 7
        data = None
        if sum(blk[:-1]) & 0xFF != blk[-1]:
            out = [NAK | 0x100]
            self._err(c, f"block {[hex(b) for b in blk]}: wrong CHK")
        elif cmd == 0:
            self.just_reset, out = True, [ACK | 0x100]
        elif cmd == 3 and self.just_reset:
            data = [JUST_RESET]
        elif cmd == 1:
            data = SETUP_EXAMPLE
        else:
            out = [ACK | 0x100]
        if data is not None:
            chk = (sum(data) + (1 if self.corrupt and cmd == 1 else 0)) & 0xFF
            out = data + [chk | 0x100]
        self.blocks.append((blk, out))
        t_end = self._send(end + self.resp, out)
        if data is not None:
            self.sent = data
            self.await_reply = t_end + self.cyc(T_RESP_US)

    def _byte(self, c, st):
        bits = st["bits"]
        if bits[10] == 0:  # stop bit low: a framing fault or the start of a bus reset
            self.maybe_break = True
            return
        for dt in st["edges"]:
            k = round(dt / self.bit)
            if k and not (-0.0099 * k * self.bit - 1 <= dt - k * self.bit <= 0.0204 * k * self.bit + 1):
                self._err(c, f"master bit rate {100 * (k * self.bit / dt - 1):+.2f} % (9600 +1 %/-2 %)")
                break
        v = sum(bits[1 + i] << i for i in range(9))
        start, end = st["t0"], st["t0"] + 11 * self.bit
        if self.reset_end is not None and start - self.reset_end < self.cyc(T_SETUP_US):
            self._err(c, f"master byte {(start - self.reset_end) / self.cyc(1000):.1f} ms after the bus reset "
                         f"(setup 200 ms)")
        mode, byte = v >> 8, v & 0xFF
        if self.await_reply is not None:
            self.await_reply = None
            if not mode:
                self.replies.append(byte)
                if byte == ACK and self.sent == [JUST_RESET]:
                    self.just_reset = False
                return
            self.replies.append(None)
        if mode:
            if self.block:
                self._err(c, f"address byte 0x{byte:02X} inside a block (mode bit on a data byte)")
            addressed = byte & 0xF8 == CHANGER
            self.block = [byte] if addressed else []
            self.need = 2 + CMD_DATA.get(byte & 7, 0) if addressed else 0
        elif self.block:
            if start - self.last_end > self.cyc(T_INTER_US):
                self._err(c, f"{(start - self.last_end) / self.cyc(1):.0f} us between the master's bytes "
                             f"(t-inter-byte 1.0 ms)")
            self.block.append(byte)
        self.last_end = end
        if len(self.block) > 36:
            self._err(c, "block longer than 36 bytes")
        if self.block and len(self.block) == self.need:
            blk, self.block = self.block, []
            self._respond(c, end, blk)

    def __call__(self, c, uo, uio, oe):
        while self.tx_edges and self.tx_edges[0][0] <= c:
            self.line = self.tx_edges.pop(0)[1]
        tx = (uo >> 4) & 1
        if tx != self.prev:
            if tx == 0:
                self.low_since = c
                if self.rx is None:
                    self.rx = {"t0": c, "bits": [], "k": 0, "edges": []}
            else:
                low = c - self.low_since
                if low >= self.cyc(T_BREAK_US):
                    self.breaks.append(low)
                    self.just_reset, self.reset_end = True, c
                    self.block, self.await_reply = [], None
                elif self.maybe_break:
                    self._err(c, f"TX low {low / self.cyc(1000):.2f} ms: neither a byte nor a bus reset (100 ms)")
                self.maybe_break = False
            if self.rx is not None and c > self.rx["t0"]:
                self.rx["edges"].append(c - self.rx["t0"])
            self.prev = tx
        if self.rx is not None:
            st = self.rx
            while st["k"] < 11 and c >= st["t0"] + (st["k"] + 0.5) * self.bit:
                st["bits"].append(tx)
                st["k"] += 1
            if st["k"] == 11:
                self.rx = None
                self._byte(c, st)
        if self.await_reply is not None and c > self.await_reply:
            self.await_reply = None
            self.replies.append(None)
            self._err(c, "no ACK, NAK or RET from the master within t-response after a data block")
        return self.line << UI_RX, 0


def blk(*b):
    return [len(b)] + list(b)


WORDS = blk(0x08) + blk(0x0B) + blk(0x09) + blk(0x0C, 0x00, 0x07, 0x00, 0x07) + blk(0x0B) + blk(0x30)
WANT = ([0x100, 0] + [JUST_RESET, 0x100 | JUST_RESET, 0] + SETUP_EXAMPLE + [0x12C, 0] + [0x100, 0] + [0x100, 0]
        + [0x8000])


def assemble(**equ):
    return ff.assemble("mdb.s", **equ)


def model_run(dev, words, cycles, want_n=None, **equ):
    """Runs until want_n host words have come (at most `cycles` cycles), or for `cycles` cycles."""
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_TX: 0}))
    bench = ff.Bench(m, inputs=dev)
    bench.send(0, *words)
    step = 100_000
    for _ in range(0, cycles, step):
        if want_n is not None and len(bench.words(0)) >= want_n:
            break
        bench.run(step)
    bench.run(20_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_mdb_words(dut):
    """Memory words of fw/mdb.s."""
    size = assemble().size
    dut._log.info(f"fw/mdb.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_mdb_model(dut):
    """fw/mdb.s in the reference model against the changer model: six exchanges at the bit-rate corners, reply delays
    and byte gaps; a wrong CHK in a reply; a bus reset; negative controls for t-response and the master's bit rate."""
    log = dut._log
    for rate, resp, gap in ((0.98, 200.0, 0.0), (1.01, 4900.0, 900.0), (0.98, 4900.0, 900.0)):
        dev = MdbChanger(rate=rate, resp_us=resp, gap_us=gap)
        bench, m = model_run(dev, WORDS, 9_000_000, want_n=len(WANT))
        label = f"model changer rate {rate}, reply after {resp} us, byte gap {gap} us"
        assert not dev.errors, f"{label}: {dev.errors[:3]}"
        assert bench.words(0) == WANT, f"{label}: {[hex(w) for w in bench.words(0)]}"
        assert dev.replies == [ACK, ACK], f"{label}: master replies {dev.replies}"
        ff.assert_status_clean(m, [0])
        log.info(f"{label}: host words as expected ({len(WANT)}); changer saw {len(dev.blocks)} blocks to 08H "
                 f"{[[hex(b) for b in x[0]] for x in dev.blocks]}; master replies to data blocks {dev.replies}")
    dev = MdbChanger(corrupt=True)
    bench, m = model_run(dev, blk(0x09), 1_200_000)
    log.info(f"model SETUP reply with a wrong CHK: host words {[hex(w) for w in bench.words(0)]}, master reply "
             f"{dev.replies}")
    assert bench.words(0) == SETUP_EXAMPLE + [0x12D, 0x4000] and dev.replies == [NAK], "wrong CHK"
    dev = MdbChanger()
    bench, m = model_run(dev, [0] + blk(0x0B), 19_000_000)
    log.info(f"model bus reset: TX low {[round(b / F_MHZ / 1000, 3) for b in dev.breaks]} ms; host words "
             f"{[hex(w) for w in bench.words(0)]}; errors {dev.errors[:2]}")
    assert not dev.errors and dev.breaks and bench.words(0) == [0, JUST_RESET, 0x100 | JUST_RESET, 0], "bus reset"
    dev = MdbChanger(resp_us=5500.0)
    bench, m = model_run(dev, blk(0x08), 600_000)
    log.info(f"control reply after 5.5 ms: host words {[hex(w) for w in bench.words(0)]}")
    assert bench.words(0)[:1] == [0x8000], "control: the master did not time out"
    dev = MdbChanger()
    model_run(dev, blk(0x08), 300_000, HB=3200)
    found = [e for e in dev.errors if "bit rate" in e]
    log.info(f"control HB 3200: {found[0] if found else 'no fault found'}")
    assert found, f"control HB 3200: {dev.errors[:2]}"


RTL_WORDS = blk(0x08) + blk(0x0B) + blk(0x09)
RTL_WANT = [0x100, 0] + [JUST_RESET, 0x100 | JUST_RESET, 0] + SETUP_EXAMPLE + [0x12C, 0]


@cocotb.test()
async def test_mdb_rtl(dut):
    """fw/mdb.s on the chip at 60 MHz against the changer model with all bus times divided by 5: RESET, POLL and SETUP,
    changer 2 % slow with a 0.9 ms (scaled) byte gap, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl mdb"
    dev = MdbChanger(scale=5, rate=0.98, resp_us=1000.0, gap_us=900.0)
    bench = rf.RtlBench(dut, assemble(HB=625, HBE=613, OUTN=3).words, owner=ff.owners({P_TX: 0}), inputs=dev, label=label)
    bench.send(0, *RTL_WORDS)
    if hasattr(dut, "dump_off"):  # waveform of the RESET exchange (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 40_000)
        dut.dump_off.value = 1
        log.info(f"{label}: waveform from the start; changer blocks {dev.blocks[:1]}")
    await bench.collect(0, len(RTL_WANT))
    await bench.run(2000)
    await bench.finish(log)
    assert not dev.errors and bench.words(0) == RTL_WANT, f"{label}: {dev.errors[:3]} {[hex(w) for w in bench.words(0)]}"
    assert dev.replies == [ACK, ACK], f"{label}: master replies {dev.replies}"
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}; master replies {dev.replies}")

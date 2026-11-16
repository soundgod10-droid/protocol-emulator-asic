"""Unit tests of the serial line unit (SLU) blocks, before they join the chip.

test_slu_loop_equiv: the receive phase loop (src/pe_slu_loop.v, instance tb.u_slu_loop_ut) against
its bit-exact model (test/slu_loop_model.py). The same sample stream goes to both: Manchester frames
at 12 samples per bit with two samples per clock (10BASE-T at 60 MHz on both clock edges) and NRZI
packets at 40 samples per bit (USB low speed at 60 MHz), each with random zero-crossing jitter and
frequency offset. After every clock cycle the decided bit, theta, the integrator and the accepted-edge
count must be equal. It also counts the frames whose data bits the loop decodes correctly, as a
check of the stimulus (the margin study is a separate test).
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from slu_loop_model import TENBASET, USB_LS, Loop


def manchester_edges(bits, bt_ns, ppm, jit_ns, rng, t0=50.0):
    """Line level changes of a Manchester stream: first half of a bit = not b, second half = b.
    Returns (initial level, [(time_ns, level)]) with every zero crossing moved by a uniform
    +/-jit_ns, and the bit time scaled by (1 + ppm/1e6)."""
    bt = bt_ns * (1.0 + ppm * 1e-6)
    level = 1 - bits[0]
    ev = []
    for i, b in enumerate(bits):
        start = t0 + i * bt
        first = 1 - b
        if first != level:
            ev.append((start + rng.uniform(-jit_ns, jit_ns), first))
            level = first
        ev.append((start + bt / 2 + rng.uniform(-jit_ns, jit_ns), b))
        level = b
    return 1 - bits[0], ev


def nrzi_edges(bits, bt_ns, ppm, jit_ns, rng, t0=50.0):
    """NRZI (USB): a 0 bit is a transition at the bit start, a 1 bit none."""
    bt = bt_ns * (1.0 + ppm * 1e-6)
    level = 1
    ev = []
    for i, b in enumerate(bits):
        if b == 0:
            level = 1 - level
            ev.append((t0 + i * bt + rng.uniform(-jit_ns, jit_ns), level))
    return 1, ev


def sample(level0, ev, ts_ns, n):
    """Ideal sampler: n samples at k * ts_ns (k = 0 .. n-1)."""
    ev = sorted(ev)
    out = []
    j = 0
    level = level0
    for k in range(n):
        t = k * ts_ns
        while j < len(ev) and ev[j][0] <= t:
            level = ev[j][1]
            j += 1
        out.append(level)
    return out


async def configure(dut, cfg):
    dut.slu_ut_en.value = 0
    dut.slu_ut_ddr.value = int(cfg.ddr)
    dut.slu_ut_n.value = cfg.n
    dut.slu_ut_manch.value = int(cfg.manch)
    dut.slu_ut_kp0.value, dut.slu_ut_kp1.value, dut.slu_ut_kp2.value = cfg.kp
    dut.slu_ut_gn0.value, dut.slu_ut_gn1.value = cfg.gn
    dut.slu_ut_ki_en.value = int(cfg.ki_en)
    dut.slu_ut_ki_sh.value = cfg.ki_sh
    dut.slu_ut_win.value = cfg.win
    dut.slu_ut_wina.value = cfg.wina
    dut.slu_ut_acq_n.value = cfg.acq_n
    dut.slu_ut_init_edge.value = cfg.init_edge


async def run_stream(dut, cfg, xs):
    """Feed one sample stream (xs[0] = the level before the loop starts) to the RTL and the model.
    Returns (cycles, bits decided, mismatch description or None)."""
    loop = Loop(cfg)
    loop.xprev = xs[0]
    await FallingEdge(dut.clk)
    dut.slu_ut_en.value = 0
    dut.slu_ut_x0.value = xs[0]
    dut.slu_ut_x1.value = xs[0]
    await RisingEdge(dut.clk)        # xprev <= xs[0] while disabled
    await FallingEdge(dut.clk)
    dut.slu_ut_en.value = 1
    per = 2 if cfg.ddr else 1
    bits = []
    cyc = 0
    u = dut.u_slu_loop_ut
    for k in range(1, len(xs) - per + 1, per):
        grp = xs[k:k + per]
        if cfg.ddr:
            dut.slu_ut_x0.value = grp[0]
            dut.slu_ut_x1.value = grp[1]
        else:
            dut.slu_ut_x1.value = grp[0]
        mbit = None
        for x in grp:
            r = loop.step(x)
            if r is not None:
                mbit = r
        await RisingEdge(dut.clk)
        await ReadOnly()
        cyc += 1
        rv = int(dut.slu_ut_bit_v.value)
        if rv != (mbit is not None):
            return cyc, bits, "cycle %d: bit_v %d, model %s" % (cyc, rv, mbit)
        if rv:
            rb = int(dut.slu_ut_bit_d.value)
            if rb != mbit:
                return cyc, bits, "cycle %d: bit %d, model %d" % (cyc, rb, mbit)
            bits.append(rb)
        th = u.th.value.to_signed()
        ii = u.ii.value.to_signed()
        na = int(u.nacc_q.value)
        if (th, ii, na) != (loop.th, loop.ii, loop.nacc):
            return cyc, bits, "cycle %d: th %d ii %d nacc %d, model %d %d %d" % (
                cyc, th, ii, na, loop.th, loop.ii, loop.nacc)
        await FallingEdge(dut.clk)
    dut.slu_ut_en.value = 0
    return cyc, bits, None


def contains(bits, data):
    s = "".join(map(str, bits))
    return "".join(map(str, data)) in s


@cocotb.test()
async def test_slu_loop_equiv(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.slu_ut_rst_n.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 1
    rng = random.Random(20261116)
    summary = []

    # 10BASE-T: 60 MHz both edges = 120 MS/s, bit 100 ns = 12 samples.
    await configure(dut, TENBASET)
    frames, ok_frames, cycles, nbits = 0, 0, 0, 0
    for f in range(50):
        jit = rng.choice([0.0, 5.0, 10.0, 13.5, 15.0])
        ppm = rng.uniform(-200.0, 200.0)
        nbytes = rng.randint(20, 64)
        data = [rng.randint(0, 1) for _ in range(8 * nbytes)]
        bits = [1, 0] * 28 + [1, 0, 1, 0, 1, 0, 1, 1] + data
        lv0, ev = manchester_edges(bits, 100.0, ppm, jit, rng)
        n = int((50.0 + len(bits) * 100.0 * 1.001 + 400.0) / (1000.0 / 120.0))
        xs = sample(lv0, ev, 1000.0 / 120.0, n)
        cyc, got, err = await run_stream(dut, TENBASET, xs)
        assert err is None, "10BASE-T frame %d (jitter %.1f ns, %+.0f ppm): %s" % (f, jit, ppm, err)
        frames += 1
        cycles += cyc
        nbits += len(got)
        ok_frames += contains(got, data)
    summary.append("10BASE-T N=12 ddr: %d frames, %d cycles, %d bits decided, RTL = model in every "
                   "cycle; data decoded in %d of %d frames" % (frames, cycles, nbits, ok_frames, frames))

    # USB low speed: 60 MHz one edge, bit 666.67 ns = 40 samples; host +/-0.05 %, device +/-1.5 %.
    await configure(dut, USB_LS)
    frames, ok_frames, cycles, nbits = 0, 0, 0, 0
    for f in range(50):
        jit = rng.choice([0.0, 20.0, 45.0, 75.0])
        ppm = rng.choice([rng.uniform(-500.0, 500.0), rng.uniform(-15000.0, 15000.0)])
        nbytes = rng.randint(3, 11)
        data = [rng.randint(0, 1) for _ in range(8 * nbytes)]
        # bit stuffing is not the loop's job; keep runs of ones short so NRZI has edges
        for i in range(6, len(data)):
            if all(data[i - 6:i]):
                data[i] = 0
        bits = [0, 0, 0, 0, 0, 0, 0, 1] + data
        lv0, ev = nrzi_edges(bits, 1000.0 / 1.5, ppm, jit, rng)
        n = int((50.0 + len(bits) * 666.67 * 1.02 + 2000.0) / (1000.0 / 60.0))
        xs = sample(lv0, ev, 1000.0 / 60.0, n)
        cyc, got, err = await run_stream(dut, USB_LS, xs)
        assert err is None, "USB packet %d (jitter %.0f ns, %+.0f ppm): %s" % (f, jit, ppm, err)
        frames += 1
        cycles += cyc
        nbits += len(got)
        # NRZI decode of the decided line levels
        dec = [1 if a == b else 0 for a, b in zip(got, got[1:])]
        ok_frames += contains(dec, data)
    summary.append("USB LS N=40: %d packets, %d cycles, %d bits decided, RTL = model in every cycle; "
                   "data decoded in %d of %d packets" % (frames, cycles, nbits, ok_frames, frames))
    for line in summary:
        dut._log.info(line)


# --------------------------------------------------------------------------------------------------
# The whole unit (tb.u_slu_ut, FULL build) through its ports, at 60 MHz.
# --------------------------------------------------------------------------------------------------

from cocotb.triggers import Timer  # noqa: E402

import slu_ref as ref  # noqa: E402

T_NS = 1000.0 / 60.0
P_TXDATA, P_RXDATA, P_STATUS, P_EVMASK, P_TXCTRL, P_TXTIME, P_RXINFO = 0, 1, 2, 3, 4, 5, 6
P_CRCA_LO, P_CRCA_HI, P_CRCB, P_TSTAMP, P_LINE, P_CFGADR, P_CFGDAT, P_TXLAST = 7, 8, 9, 10, 11, 12, 13, 14
DIN, AUX0, AUX1 = 0, 1, 2          # pin numbers used by the tests (ui/uio numbering is the chip's)


class Slu:
    def __init__(self, dut):
        self.dut = dut

    async def wr(self, addr, data):
        d = self.dut
        await FallingEdge(d.clk)
        d.slu_addr.value = addr
        d.slu_wdata.value = data & 0xFFFF
        d.slu_we.value = 1
        await FallingEdge(d.clk)
        d.slu_we.value = 0

    async def rd(self, addr, pop=False):
        d = self.dut
        await FallingEdge(d.clk)
        d.slu_addr.value = addr
        d.slu_re.value = int(pop)
        await Timer(1, unit="ns")
        v = int(d.slu_rdata.value)
        await FallingEdge(d.clk)
        d.slu_re.value = 0
        return v

    async def cfg(self, words):
        """words: {index: value}"""
        for i, v in sorted(words.items()):
            await self.wr(P_CFGADR, i)
            await self.wr(P_CFGDAT, v)

    async def now(self):
        await ReadOnly()
        return int(self.dut.slu_now.value)


async def slu_reset(dut):
    cocotb.start_soon(Clock(dut.clk, 16666, unit="ps").start())   # 60 MHz (even ps period)
    dut.slu_ut_rst_n.value = 0
    dut.slu_we.value = 0
    dut.slu_re.value = 0
    dut.slu_pins.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 1
    await RisingEdge(dut.clk)
    return Slu(dut)


def mode(code, ddr=0, rx=0, stuff=0, ones=0, k=0, usb=0, eop_car=0, eop_len=0, tsel=0, loop=0):
    return (code | ddr << 2 | rx << 3 | stuff << 4 | ones << 5 | k << 6 | usb << 9 | eop_car << 10 |
            eop_len << 11 | tsel << 12 | loop << 15)


USB_CFG_TX = {
    0: mode(1, stuff=1, ones=1, k=6, usb=1, tsel=3),
    1: 40,                                  # one bit = 40 cycles (1.5 Mbit/s at 60 MHz)
    9: 0xA001, 10: 0,                       # CRC16 reflected
    11: 0xB001, 12: 0,                      # CRC16 residue
    13: 0x14 | (0x06 << 8),                 # CRC5 polynomial and residue
    14: 16 | (1 << 6) | (1 << 7) | (8 << 10),   # width 16, preset ones, 1 word (SYNC, PID) skipped
                                            # on TX, 8 bits (PID) skipped on RX
    15: 84 | (40 << 8),                     # EOP: SE0 84 cycles, then J 40 cycles
}


async def capture(dut, ncyc, out=None):
    """tx_v, tx_oe and now in each cycle (after each rising edge), ncyc cycles; runs as a background
    task when out is given."""
    out = [] if out is None else out
    for _ in range(ncyc):
        await RisingEdge(dut.clk)
        await ReadOnly()
        out.append((int(dut.slu_txv.value), int(dut.slu_txoe.value), int(dut.slu_now.value)))
    return out


async def feed_words(s, words, last=True):
    """Push words as the TX buffer frees; the final one through TXLAST."""
    feed = list(words)
    while feed:
        free = await s.rd(P_TXDATA)
        if free:
            w = feed.pop(0)
            await s.wr(P_TXLAST if (last and not feed) else P_TXDATA, w)


@cocotb.test()
async def test_slu_usb_tx(dut):
    """USB low-speed transmit: SYNC, PID, 8 data bytes, CRC16 appended by the unit, bit stuffing,
    NRZI on VPO/VMO, EOP SE0 84 cycles (1.40 us; USB 2.0 7.1.13.2.1 TLEOPT 1.25-1.50 us) and J 40
    cycles, start at TXTIME. Checked against slu_ref from the line levels at each bit centre."""
    s = await slu_reset(dut)
    await s.cfg(USB_CFG_TX)
    pid = 0xC3                                       # DATA0
    data = bytes([0xFF, 0xFF, 0x00, 0x7E, 0x01, 0x80, 0xFE, 0x55])   # runs of ones force stuffing
    words = [0x80 | (pid << 8)] + [data[i] | (data[i + 1] << 8) for i in range(0, 8, 2)]
    await s.wr(P_TXCTRL, (1 << 5))                  # append CRC, last word 16 bits
    for w in words[:2]:
        await s.wr(P_TXDATA, w)
    caps = []
    cocotb.start_soon(capture(dut, 40 * 130, caps))
    t0 = (await s.now() + 200) & 0xFFFF
    await s.wr(P_TXTIME, t0)
    await feed_words(s, words[2:])
    while len(caps) < 40 * 130:
        await RisingEdge(dut.clk)
    # the frame on the pins
    oe = [c[1] for c in caps]
    first = oe.index(1)
    start_now = caps[first][2]
    lines = [c[0] & 3 for c in caps]
    exp_bits = ref.usb_packet_bits(pid, data, crc="16")
    exp_line = ref.nrzi(ref.stuff(exp_bits))
    got = []
    for i in range(len(exp_line)):
        v = lines[first + 40 * i + 20]
        assert v in (1, 2), "bit %d: not J or K (%d)" % (i, v)
        got.append(1 if v == 2 else 0)               # 2 = VMO high = J
    assert got == exp_line, "line levels differ from USB 2.0 NRZI(stuff(SYNC PID DATA CRC16))"
    eop = first + 40 * len(exp_line)
    se0 = 0
    while lines[eop + se0] == 0:
        se0 += 1
    j = 0
    while oe[eop + se0 + j] == 1 and lines[eop + se0 + j] == 2:
        j += 1
    assert se0 == 84, "SE0 %d cycles" % se0
    assert j == 41 or j == 40, "J %d cycles" % j
    assert oe[eop + se0 + j] == 0
    dut._log.info("USB TX: first K/J at now %d for TXTIME %d (offset %d cycles); %d line bits incl. %d "
                  "stuff bits; SE0 %d cycles, J %d cycles, then OE off" %
                  (start_now, t0, (start_now - t0) & 0xFFFF, len(exp_line),
                   len(exp_line) - len(exp_bits), se0, j))
    st = await s.rd(P_STATUS)
    assert st & 1, "TXDONE"
    assert not (st & 6), "no TXLATE / TXUNDER: %04x" % st


async def drive_levels(dut, schedule):
    """schedule: list of (time_ns from now, {pin: level}) applied with Timer."""
    t = 0.0
    for tn, pins in schedule:
        dt = tn - t
        if dt > 0:
            await Timer(round(dt * 1000), unit="ps")
        t = tn
        v = int(dut.slu_pins.value)
        for p, lv in pins.items():
            v = (v & ~(1 << p)) | (lv << p)
        dut.slu_pins.value = v


def usb_host_schedule(line_levels, bt_ns, t0, eop_bits=2.0):
    """Host packet on RCV (DIN), VP (AUX0), VM (AUX1): J = VP0 VM1, K = VP1 VM0, then SE0 for
    eop_bits bit times and J (USB 2.0 7.1.13.2: EOP SE0 2 bit times then J)."""
    sch = [(t0 - 100.0, {DIN: 1, AUX0: 0, AUX1: 1})]
    for i, lv in enumerate(line_levels):
        sch.append((t0 + i * bt_ns, {DIN: lv, AUX0: 1 - lv, AUX1: lv}))
    te = t0 + len(line_levels) * bt_ns
    sch.append((te, {AUX0: 0, AUX1: 0}))
    sch.append((te + eop_bits * bt_ns, {DIN: 1, AUX0: 0, AUX1: 1}))
    return sch, te + eop_bits * bt_ns


USB_CFG_RX = dict(USB_CFG_TX)
USB_CFG_RX.update({
    0: mode(1, rx=1, stuff=1, ones=1, k=6, usb=1, tsel=0),
    2: 40,                                  # N = 40, kp 0 (hard resync)
    3: 0,
    4: 20 | (20 << 6) | (2 << 13),          # window 20 samples, initial sync on the 2nd edge
    5: (2 << 6),                            # pattern after 2 accepted edges
    6: 0x0001, 7: 2 | (20 << 10),           # pattern ..001 (end of SYNC), EOP after 20 SE0 samples
    8: DIN | (AUX0 << 4) | (AUX1 << 8),
})


async def usb_rx_packet(dut, s, pid, data, crc, ppm=0.0, corrupt=None):
    bits = ref.usb_packet_bits(pid, data, crc)
    if corrupt is not None:
        bits[corrupt] ^= 1
    line = ref.nrzi(ref.stuff(bits))
    bt = 1000.0 / 1.5 * (1 + ppm * 1e-6)
    sch, t_end = usb_host_schedule(line, bt, 2000.0)
    await s.wr(P_STATUS, 0xFFFF)
    cocotb.start_soon(drive_levels(dut, sch))
    words = []
    for _ in range(int((t_end + 3000.0) / T_NS / 12)):
        info = await s.rd(P_RXINFO)
        if (info >> 5) & 3:
            words.append(await s.rd(P_RXDATA, pop=True))
    return words, info, await s.rd(P_STATUS), await s.rd(P_TSTAMP)


@cocotb.test()
async def test_slu_usb_rx(dut):
    """USB low-speed receive from a host model: DATA0 with 8 bytes at host +/-0.05 % and at +/-1.5 %,
    a token with CRC5, a corrupted CRC16, a stuffing error. Words, CRC flags, stuff error, EOP."""
    s = await slu_reset(dut)
    await s.cfg(USB_CFG_RX)
    data = bytes([0x12, 0xFF, 0xFF, 0x00, 0xA5, 0x7F, 0xFE, 0x80])
    results = []
    for ppm in (0.0, 500.0, -500.0, 15000.0, -15000.0):
        words, info, st, ts = await usb_rx_packet(dut, s, 0xC3, data, "16", ppm)
        got = b"".join(w.to_bytes(2, "little") for w in words)
        exp = bytes([0xC3]) + data + ref.bytes_of_bits(ref.usb_crc16_bits(ref.bits_of_bytes(data)))
        assert got[:len(exp)] == exp, "ppm %+.0f: %s vs %s" % (ppm, got.hex(), exp.hex())
        assert st & (1 << 6), "CRC16 residue (CRCA_OK) at ppm %+.0f, status %04x" % (ppm, st)
        assert st & (1 << 5), "EOP"
        assert not st & (1 << 8), "no stuff error"
        results.append("%+.0f ppm ok" % ppm)
    # token: OUT to address 0x15, endpoint 1, CRC5
    tok = (0x15 | (1 << 7)).to_bytes(2, "little")
    words, info, st, ts = await usb_rx_packet(dut, s, 0xE1, tok, "5")
    assert st & (1 << 7), "CRC5 residue (CRCB_OK), status %04x" % st
    # corrupted data bit: CRC16 must fail
    words, info, st, ts = await usb_rx_packet(dut, s, 0xC3, data, "16", corrupt=30)
    assert not st & (1 << 6), "CRC16 must fail on a corrupted bit"
    # stuffing error: seven ones on the line
    bits = ref.usb_packet_bits(0xC3, data, "16")
    line = ref.nrzi(bits[:20] + [1] * 7 + bits[20:])
    sch, t_end = usb_host_schedule(line, 1000.0 / 1.5, 2000.0)
    await s.wr(P_STATUS, 0xFFFF)
    cocotb.start_soon(drive_levels(dut, sch))
    await Timer(round((t_end + 2000.0) * 1000), unit="ps")
    st = await s.rd(P_STATUS)
    assert st & (1 << 8), "STUFFERR on 7 ones"
    dut._log.info("USB RX: DATA0 8 bytes %s; CRC5 token ok; corrupted CRC16 rejected; stuff error "
                  "flagged" % ", ".join(results))


ETH_CFG = {
    0: mode(2, ddr=1, rx=1, eop_car=1, tsel=0),
    1: 3,                                   # half bit = 3 cycles (10 Mbit/s at 60 MHz)
    2: 12 | (2 << 6) | (4 << 9) | (6 << 12),    # N 12, kp 2/4/6
    3: 8 | (24 << 6) | (10 << 12),          # gears after 8 and 24 edges, integrator shift 14 - 4
    4: 3 | (5 << 6) | (1 << 12) | (2 << 13),    # window 3, acquisition window 5, integrator on
    5: 24 | (16 << 6),                      # 24 acquisition edges, SFD gate after 16 edges
    6: 0x00AB, 7: 7 | (10 << 10),           # SFD pattern 10101011, carrier off after 10 cycles
    8: DIN | (AUX0 << 4) | (AUX1 << 8),
    9: 0x8320, 10: 0xEDB8,                  # CRC-32 reflected
    11: 0x20E3, 12: 0xDEBB,                 # CRC-32 residue
    14: 32 | (1 << 6) | (4 << 7),           # width 32, preset ones, 4 TX words (preamble, SFD) skipped
    15: 16,                                 # TP_IDL: TD+ high 16 cycles after the last bit
    19: 3 | (16 << 6) | (4 << 12),          # squelch: 4 spacings of 3-16 half cycles
    20: 2 | (12 << 6),                      # link pulse 2-12 cycles
}


@cocotb.test()
async def test_slu_eth_tx(dut):
    """10BASE-Te transmit: preamble and SFD from the thread, 46-byte payload, CRC-32 appended by the
    unit, Manchester at 3 cycles per half bit on TD+ (TXA) and TD- (TXB), tap B one cycle later,
    TP_IDL hold; then a link test pulse at TXTIME."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    payload = bytes(range(46))
    pre = [0x5555, 0x5555, 0x5555, 0xD555]
    words = pre + [payload[i] | (payload[i + 1] << 8) for i in range(0, 46, 2)]
    await s.wr(P_TXCTRL, (1 << 5))
    for w in words[:2]:
        await s.wr(P_TXDATA, w)
    caps = []
    ncap = 6 * 700
    cocotb.start_soon(capture(dut, ncap, caps))
    await s.wr(P_TXTIME, (await s.now() + 100) & 0xFFFF)
    await feed_words(s, words[2:])
    while len(caps) < ncap:
        await RisingEdge(dut.clk)
    td = [c[0] & 1 for c in caps]
    oe = [c[1] for c in caps]
    first = oe.index(1)
    exp = ref.manchester_halves(ref.eth_frame_bits(payload))
    got = [td[first + 3 * i + 1] for i in range(len(exp))]
    assert got == exp, "Manchester halves differ from preamble, SFD, payload, CRC-32"
    tdm = [(c[0] >> 1) & 1 for c in caps]
    assert all(tdm[first + 3 * i + 1] == 1 - exp[i] for i in range(len(exp))), "TD- = not TD+"
    tapb = [(c[0] >> 2) & 1 for c in caps]
    assert all(tapb[first + 3 * i + 2] == exp[i] for i in range(len(exp))), "tap B one cycle later"
    end = first + 3 * len(exp)
    hold = 0
    while td[end + hold] == 1 and oe[end + hold]:
        hold += 1
    last_high = 3 if exp[-1] == 1 else 0       # the last half bit itself was high
    dut._log.info("10BASE-Te TX: %d half bits equal to IEEE 802.3 Manchester of preamble, SFD, 46-byte "
                  "payload and FCS; TD+ high %d cycles after the last half bit (+%d if it was high)"
                  % (len(exp), hold, last_high))
    assert 13 <= hold + last_high <= 27, "TP_IDL hold %d cycles (13.5-27 allowed)" % (hold + last_high)
    # link test pulse at TXTIME
    await s.wr(P_TXCTRL, (1 << 7))
    t1 = (await s.now() + 50) & 0xFFFF
    await s.wr(P_TXTIME, t1)
    caps = await capture(dut, 120)
    w = sum(1 for c in caps if (c[0] & 1) and c[1])
    dut._log.info("link test pulse: TD+ high %d cycles (tail 1 = %d)" % (w, 16))
    assert w == 17 or w == 16


def eth_rx_schedule(bits, jit, ppm, rng, t0):
    """TD+ after the slicer (DIN) with the squelch comparator (AUX0) active during the frame."""
    lv0, ev = manchester_edges(bits, 100.0, ppm, jit, rng, t0=t0)
    sch = [(t0 - 200.0, {DIN: lv0, AUX0: 1})]
    for t, lv in sorted(ev):
        sch.append((t, {DIN: lv}))
    tend = sorted(ev)[-1][0] + 100.0
    sch.append((tend, {DIN: 0}))
    sch.append((tend + 300.0, {AUX0: 0}))
    return sch, tend + 300.0


@cocotb.test()
async def test_slu_eth_rx(dut):
    """10BASE-Te receive through the pins: 20 frames of 64-200 bytes, uniform zero-crossing jitter up to
    +/-13.5 ns (IEEE 802.3 14.3.1.3.1) and +/-100 ppm; carrier by the squelch qualifier, SFD after the
    lock count, CRC-32 residue, carrier-off EOP. Then link pulses: 6 cycles counted, 20 cycles not."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    rng = random.Random(7)
    ok = 0
    for f in range(20):
        payload = bytes(rng.randrange(256) for _ in range(rng.randint(64, 200)))
        jit = rng.choice([0.0, 5.0, 10.0, 13.5])
        ppm = rng.uniform(-100.0, 100.0)
        bits = ref.eth_frame_bits(payload)
        sch, t_end = eth_rx_schedule(bits, jit, ppm, rng, 1000.0)
        await s.wr(P_STATUS, 0xFFFF)
        cocotb.start_soon(drive_levels(dut, sch))
        words = []
        while True:
            info = await s.rd(P_RXINFO)
            if (info >> 5) & 3:
                words.append(await s.rd(P_RXDATA, pop=True))
            st = await s.rd(P_STATUS)
            if st & (1 << 5) and not (info >> 5) & 3:
                break
        got = b"".join(w.to_bytes(2, "little") for w in words)
        exp = payload + (binascii_crc(payload)).to_bytes(4, "little")
        assert got[:len(exp)] == exp, "frame %d (%.1f ns, %+.0f ppm): data differ" % (f, jit, ppm)
        assert st & (1 << 6), "frame %d: CRC-32 residue, status %04x" % (f, st)
        ok += 1
        await Timer(round(t_end * 1000), unit="ps")
    # link pulses
    await s.wr(P_STATUS, 0xFFFF)
    await drive_levels(dut, [(0.0, {DIN: 1, AUX0: 1}), (6 * T_NS, {DIN: 0, AUX0: 0}), (400.0, {})])
    st6 = await s.rd(P_STATUS)
    await s.wr(P_STATUS, 0xFFFF)
    await drive_levels(dut, [(0.0, {DIN: 1, AUX0: 1}), (20 * T_NS, {DIN: 0, AUX0: 0}), (400.0, {})])
    st20 = await s.rd(P_STATUS)
    assert st6 & (1 << 13), "6-cycle pulse is a link test pulse"
    assert not st20 & (1 << 13), "20-cycle pulse is not"
    dut._log.info("10BASE-Te RX: %d of 20 frames with data and CRC-32 residue correct; link pulse "
                  "6 cycles counted, 20 cycles not" % ok)


def binascii_crc(b):
    import binascii
    return binascii.crc32(b) & 0xFFFFFFFF

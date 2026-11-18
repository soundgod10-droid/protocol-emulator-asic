"""Unit tests of the serial line unit (SLU) blocks, before they join the chip.

test_slu_loop_equiv: the receive phase loop (src/pe_slu_loop.v, instance tb.u_slu_loop_ut) against
its bit-exact model (test/slu_loop_model.py). The same sample stream goes to both: Manchester frames
at 12 samples per bit with two samples per clock (10BASE-T at 60 MHz on both clock edges) and NRZI
packets at 40 samples per bit (USB low speed at 60 MHz), each with random zero-crossing jitter and
frequency offset. After every clock cycle the decided bit, theta, the integrator and the accepted-edge
count must be equal. It also counts the frames whose data bits the loop decodes correctly, as a
check of the stimulus (the margin study is a separate test).
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from slu_loop_model import TENBASET, USB_LS, Loop

# The unit tests drive the unit-test instances of test/tb.v, which the gate-level build does not have;
# the chip-level tests at the end run at gate level too.
GL = os.environ.get("GATES") == "yes"


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
        mbit = loop.step_pair(grp[0], grp[1]) if cfg.ddr else loop.step(grp[0])
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


@cocotb.test(skip=GL)
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


def window_limit_hits(cfg, xs):
    """Edges of the stream whose phase error is exactly the lower window limit (-win samples), counted
    with the model's own arithmetic (NRZ form: expected edge at the bit boundary)."""
    lp = Loop(cfg)
    lp.xprev = xs[0]
    none, n = cfg.n * 4096, 0
    for x in xs[1:]:
        if lp.init and x != lp.xprev:
            eps = (lp.th + 4096 - 2048 + none // 2) % none - none // 2
            n += eps == -cfg.win * 4096
        lp.step(x)
    return n


@cocotb.test(skip=GL)
async def test_slu_loop_window_edge(dut):
    """USB loop (N = 40, hard resync, window 20 samples = half a bit): an edge exactly half a bit away
    from the expected bit boundary has the phase error -20 samples, the lower window limit, and is used
    (|eps| <= window; test/slu_loop_model.py). Bits of 0 (an edge every 40 samples) with one edge moved
    by 20 samples later or earlier: that edge and the next one sit on the limit. Edges moved by 19 and
    21 samples (inside the window) as neighbours. RTL = model in every cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.slu_ut_rst_n.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 1
    await configure(dut, USB_LS)
    log = []
    for d in (20, -20, 19, 21):
        edges = [30 + 40 * i for i in range(12)]
        edges[6] += d
        xs, lv = [], 1
        for k in range(30 + 40 * 13):
            if k in edges:
                lv = 1 - lv
            xs.append(lv)
        hits = window_limit_hits(USB_LS, xs)
        assert (hits > 0) == (abs(d) == 20), "edge moved by %d samples: %d edges on the window limit" % (d, hits)
        cyc, got, err = await run_stream(dut, USB_LS, xs)
        assert err is None, "edge moved by %d samples: %s" % (d, err)
        log.append("moved %+d: %d edges on the limit, %d cycles, %d bits, RTL = model" % (d, hits, cyc, len(got)))
    dut._log.info("USB loop window limit: " + "; ".join(log))


# --------------------------------------------------------------------------------------------------
# The whole unit (tb.u_slu_ut, FULL build) through its ports, at 60 MHz.
# --------------------------------------------------------------------------------------------------

from cocotb.triggers import Timer  # noqa: E402
from cocotb.utils import get_sim_time  # noqa: E402

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


@cocotb.test(skip=GL)
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
    rle, prev, cnt = [], None, 0
    for v, o in zip(lines[eop - 80:eop + 200], oe[eop - 80:eop + 200]):
        if (v, o) == prev:
            cnt += 1
        else:
            if prev is not None:
                rle.append("%d/%d x%d" % (prev[0], prev[1], cnt))
            prev, cnt = (v, o), 1
    rle.append("%d/%d x%d" % (prev[0], prev[1], cnt))
    dut._log.info("line/oe runs from 80 cycles before the expected EOP: " + ", ".join(rle))
    se0 = 0
    while lines[eop + se0] == 0:
        se0 += 1
    j = 0
    while oe[eop + se0 + j] == 1 and lines[eop + se0 + j] == 2:
        j += 1
    assert se0 == 84, "SE0 %d cycles" % se0
    assert j == 40, "J %d cycles" % j
    assert (start_now - t0) & 0xFFFF == 0, "first symbol on tx_v in cycle TXTIME"
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
    t_stop = get_sim_time("ns") + t_end + 3000.0
    while get_sim_time("ns") < t_stop:
        info = await s.rd(P_RXINFO)
        if (info >> 5) & 3:
            words.append(await s.rd(P_RXDATA, pop=True))
    return words, info, await s.rd(P_STATUS), await s.rd(P_TSTAMP)


@cocotb.test(skip=GL)
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


@cocotb.test(skip=GL)
async def test_slu_rx_state_codes(dut):
    """LINE[7:4] (the RX state, port 11) in every cycle through one USB packet from the host model:
    hunt 1, data 2 from the end of SYNC, EOP wait 3 from the qualified SE0 to the J, then 4 for exactly
    one cycle (receive loop off) and hunt 1 again."""
    s = await slu_reset(dut)
    await s.cfg(USB_CFG_RX)
    line = ref.nrzi(ref.stuff(ref.usb_packet_bits(0xC3, bytes([0x12, 0x34]), "16")))
    sch, t_end = usb_host_schedule(line, 1000.0 / 1.5, 2000.0)
    await FallingEdge(dut.clk)
    dut.slu_addr.value = P_LINE
    cocotb.start_soon(drive_levels(dut, sch))
    runs = []
    for _ in range(int((t_end + 1000.0) / T_NS)):
        await RisingEdge(dut.clk)
        await ReadOnly()
        st = (int(dut.slu_rdata.value) >> 4) & 0xF
        if runs and runs[-1][0] == st:
            runs[-1][1] += 1
        else:
            runs.append([st, 1])
    dut._log.info("RX state runs (state x cycles): " + ", ".join("%d x%d" % (a, b) for a, b in runs))
    assert [a for a, _ in runs] == [1, 2, 3, 4, 1], runs
    assert runs[3][1] == 1, "state 4 lasts one cycle"


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
    20: 2 | (12 << 6) | (6 << 12),          # received link pulse 2-12 cycles; sent pulse 6 cycles
}


@cocotb.test(skip=GL)
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
    dut._log.info("link test pulse: TD+ high %d cycles (100 ns; IEEE 802.3 Figure 14-13 needs "
                  "0.25-0.85 BT above 585 mV and release well before 2 BT)" % w)
    assert w == 6


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


@cocotb.test(skip=GL)
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
        while (await s.rd(P_RXINFO) >> 5) & 3:        # the last frame's dribble-bit word
            await s.rd(P_RXDATA, pop=True)
        await s.wr(P_STATUS, 0xFFFF)
        cocotb.start_soon(drive_levels(dut, sch))
        words = []
        while True:
            st = await s.rd(P_STATUS)            # status first: words of an EOP seen here are queued
            info = await s.rd(P_RXINFO)
            while (info >> 5) & 3:
                words.append(await s.rd(P_RXDATA, pop=True))
                info = await s.rd(P_RXINFO)
            if st & (1 << 5):
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


@cocotb.test(skip=GL)
async def test_slu_tx_late_underflow(dut):
    """Transmit start rules (docs/helpers.md section 2, TXTIME): a late TXTIME sets TXLATE and starts at
    write + 4, or does not start with TXCTRL bit 6; an empty buffer at the start and a buffer that runs empty
    inside a frame set TXUNDER, and the frame ends with the tail and TXDONE."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    u = dut.u_slu_ut
    await s.wr(P_STATUS, 0xFFFF)
    await s.wr(P_TXCTRL, 0)
    await s.wr(P_TXTIME, (await s.now() + 1) & 0xFFFF)          # lead below 4: late
    await ClockCycles(dut.clk, 20)
    st = await s.rd(P_STATUS)
    assert st & 0x2, "TXLATE after a late TXTIME, status %04x" % st
    assert st & 0x4, "TXUNDER: started with an empty buffer, status %04x" % st
    assert not st & 0x1, "no TXDONE without a frame, status %04x" % st
    await s.wr(P_STATUS, 0xFFFF)
    await s.wr(P_TXCTRL, 1 << 6)                                 # no start when late
    await s.wr(P_TXTIME, (await s.now() - 10) & 0xFFFF)
    await ClockCycles(dut.clk, 20)
    st = await s.rd(P_STATUS)
    state = (await s.rd(P_TXCTRL)) & 0xF
    assert st & 0x2 and not st & 0x4 and state == 0, "late with TXCTRL bit 6: status %04x, state %d" % (st, state)
    await s.wr(P_STATUS, 0xFFFF)
    await s.wr(P_TXCTRL, 0)
    await s.wr(P_TXDATA, 0x5555)                                 # one word, not the last
    await s.wr(P_TXTIME, (await s.now() + 20) & 0xFFFF)
    oe_cycles = 0
    for _ in range(20 + 16 * 6 + 60):
        await RisingEdge(dut.clk)
        await ReadOnly()
        oe_cycles += int(dut.slu_txoe.value)
    st = await s.rd(P_STATUS)
    assert st & 0x4 and st & 0x1, "TXUNDER and TXDONE after a word with no next word, status %04x" % st
    dut._log.info("TX start rules: late TXTIME -> TXLATE and start at write + 4 (TXUNDER with no word); late with "
                  "TXCTRL bit 6 -> TXLATE, no start; underflow after one word -> TXUNDER, tail, TXDONE "
                  "(OE for %d cycles: 16 bits x 6 + TP_IDL)" % oe_cycles)
    assert int(u.tx_st.value) == 0


@cocotb.test(skip=GL)
async def test_slu_eth_collision(dut):
    """10BASE-Te collision: a receive carrier while the helper sends sets COLLISION; the helper sends its 32-bit jam
    (docs/helpers.md section 1), then the TP_IDL tail, and sets TXDONE."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    u = dut.u_slu_ut
    await s.wr(P_STATUS, 0xFFFF)
    await s.wr(P_TXCTRL, 0)
    await s.wr(P_TXDATA, 0x5555)
    await s.wr(P_TXLAST, 0x5555)
    jam = {"bits": 0, "cycles": 0, "done": False}

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            st = int(u.tx_st.value)
            if st == 7:
                jam["cycles"] += 1
                if int(u.bit_start.value) and int(u.t_kind.value) == 5:      # K_JAM
                    jam["bits"] += 1
            elif jam["cycles"]:
                jam["done"] = True
                return
    task = cocotb.start_soon(watch())
    await s.wr(P_TXTIME, (await s.now() + 30) & 0xFFFF)
    sch, _ = eth_rx_schedule([1, 0] * 40, 0.0, 0.0, random.Random(3), 30 * T_NS + 300.0)
    cocotb.start_soon(drive_levels(dut, sch))
    for _ in range(2000):
        await RisingEdge(dut.clk)
        if jam["done"]:
            break
    await ClockCycles(dut.clk, 40)
    st = await s.rd(P_STATUS)
    dut._log.info("collision: %d jam bits in %d cycles of the jam state; status %04x" % (jam["bits"], jam["cycles"], st))
    assert st & (1 << 14), "COLLISION, status %04x" % st
    assert jam["bits"] == 32, "jam of %d bits, specified 32" % jam["bits"]
    assert st & 0x1, "TXDONE after the jam and the tail, status %04x" % st
    task.cancel()


@cocotb.test(skip=GL)
async def test_slu_jabber(dut):
    """10BASE-Te jabber: a transmission longer than 2^22 cycles (69.9 ms at 60 MHz; IEEE 802.3 14.2.1.6 xmit_max
    20-150 ms) stops the helper and sets JABBER; a new frame does not start while JABBER is set. The unjab time
    (2^25 cycles, 0.559 s) is too long to simulate: the test sets the idle tick count to its last value and
    checks that JABBER clears at the next tick."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    u = dut.u_slu_ut
    await s.wr(P_STATUS, 0xFFFF)
    await s.wr(P_TXCTRL, 0)
    await s.wr(P_TXDATA, 0x5555)
    await s.wr(P_TXDATA, 0x5555)
    await s.wr(P_TXTIME, (await s.now() + 20) & 0xFFFF)
    while not int(dut.slu_txoe.value):
        await RisingEdge(dut.clk)
    t0 = get_sim_time("ns")
    words = 2
    while not int(u.jabber.value):
        await Timer(round(120 * T_NS * 1000), unit="ps")      # one word lasts 96 cycles; the buffer holds 2
        free = await s.rd(P_TXDATA)
        for _ in range(free):
            await s.wr(P_TXDATA, 0x5555)
            words += 1
    cycles = (get_sim_time("ns") - t0) / T_NS
    await ClockCycles(dut.clk, 10)
    st = await s.rd(P_STATUS)
    assert st & (1 << 15), "JABBER, status %04x" % st
    assert int(u.tx_st.value) == 0 and not int(dut.slu_txoe.value), "transmitter stopped"
    assert 2 ** 22 - 1024 <= cycles <= 2 ** 22 + 2048, "jabber after %d cycles" % cycles
    await s.wr(P_TXCTRL, 1 << 10)                               # empty the buffer
    await s.wr(P_TXDATA, 0x5555)
    await s.wr(P_TXTIME, (await s.now() + 20) & 0xFFFF)
    oe = 0
    for _ in range(200):
        await RisingEdge(dut.clk)
        oe |= int(dut.slu_txoe.value)
    assert not oe, "no frame while JABBER is set"
    u.jab_cnt.value = 0xFFFF
    await ClockCycles(dut.clk, 600)
    st = await s.rd(P_STATUS)
    assert not st & (1 << 15), "JABBER clears after 65,536 idle ticks, status %04x" % st
    dut._log.info("jabber: set after %d cycles of transmission (%d words), transmitter stopped, no frame while set, "
                  "cleared at the 65,536th idle tick" % (cycles, words))


@cocotb.test(skip=GL)
async def test_slu_usb_line_conditions(dut):
    """USB line timers (docs/helpers.md section 1) with the fixed thresholds of the chip's USB helper: SE0 for 19
    ticks of 16 cycles (304 cycles, 5.07 us) sets LINEA (bus reset), a shorter SE0 does not; J for 15,000 ticks
    (240,000 cycles, 4.0 ms) sets LINEB (suspend), 14,000 ticks do not."""
    s = await slu_reset(dut)
    cfg = dict(USB_CFG_RX)
    cfg.update({16: 19, 17: 15000, 18: 0x0044})                 # the fixed values of the chip's USB helper
    await s.cfg(cfg)
    await drive_levels(dut, [(0.0, {DIN: 1, AUX0: 0, AUX1: 1}), (100.0, {})])
    await s.wr(P_STATUS, 0xFFFF)
    await drive_levels(dut, [(0.0, {AUX0: 0, AUX1: 0}), (17 * 16 * T_NS, {AUX1: 1}), (17 * 16 * T_NS + 100.0, {})])
    st17 = await s.rd(P_STATUS)
    await drive_levels(dut, [(0.0, {AUX0: 0, AUX1: 0}), (21 * 16 * T_NS, {AUX1: 1}), (21 * 16 * T_NS + 100.0, {})])
    st21 = await s.rd(P_STATUS)
    await s.wr(P_STATUS, 0xFFFF)
    await Timer(round((14000 * 16) * T_NS * 1000), unit="ps")
    st_early = await s.rd(P_STATUS)
    await Timer(round((1000 * 16 + 400) * T_NS * 1000), unit="ps")
    st_j = await s.rd(P_STATUS)
    dut._log.info("USB line timers: SE0 272 cycles -> %04x, SE0 336 cycles -> %04x; J 224,000 cycles -> %04x, "
                  "J 240,400 cycles -> %04x" % (st17, st21, st_early, st_j))
    assert not st17 & (1 << 10), "no bus reset after an SE0 of 17 ticks"
    assert st21 & (1 << 10), "bus reset (LINEA) after an SE0 of 21 ticks"
    assert not st_early & (1 << 11), "no suspend after 14,000 ticks of J"
    assert st_j & (1 << 11), "suspend (LINEB) after 15,000 ticks of J"


def binascii_crc(b):
    import binascii
    return binascii.crc32(b) & 0xFFFFFFFF


# --------------------------------------------------------------------------------------------------
# The helpers in the chip: a thread drives them through I/O ports 16-47, the pin configuration
# gives them pins, the frame comes from and goes to the data memory (host WRITE_MEM / READ_MEM).
# --------------------------------------------------------------------------------------------------

import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asm"))
import host_spi as hs  # noqa: E402
import pe_asm  # noqa: E402

CHIP_CLK_PS = 16666                 # 60 MHz
ETH_PORT, USB_PORT = 32, 16
PIN_HDRV, PIN_HETH = 1 << 4, 1 << 5


def helper_pin(eth, signal):
    """Pin configuration word of a helper pin: helper drive, helper select, signal (0 TXA .. 3 TXD,
    4 OE, 5 not OE)."""
    return PIN_HDRV | (PIN_HETH if eth else 0) | (signal << 6)


async def chip_start(dut, prog, mem, pins):
    """Chip reset, program and data words through the host port, pin words, START (thread 0 at 0)."""
    cocotb.start_soon(Clock(dut.clk, CHIP_CLK_PS, unit="ps").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    host = hs.SpiTimed(dut)
    await host.write_mem(0, prog.image()[:prog.size])
    for addr, words in mem:
        await host.write_mem(addr, words)
    head, _ = hs.config_words((0, 0, 0, 0), (True, False, False, False), [None] * 15, [False] * 15)
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    return host


ETH_TX_FW = """
        .equ    E, 32
        LDI     r2, 512         ; frame: count N at 512, then N words
        LDX     r3, [r2]
        ADDI    r2, 1
        LDX     r0, [r2]
        IOW     r0, E+0         ; TXDATA
        ADDI    r2, 1
        LDX     r0, [r2]
        IOW     r0, E+0
        ADDI    r2, 1
        ADDI    r3, -2
        LDI     r0, 544         ; TXCTRL: append CRC (32) + start now (512)
        IOW     r0, E+4
next:   LDX     r0, [r2]
        ADDI    r2, 1
        ADDI    r3, -1
        BEQ     last
w1:     IOR     r1, E+0         ; TX buffer free
        CMPI    r1, 0
        BEQ     w1
        IOW     r0, E+0
        JMP     next
last:   IOR     r1, E+0
        CMPI    r1, 0
        BEQ     last
        IOW     r0, E+14        ; TXLAST
done:   IOR     r1, E+2         ; STATUS: wait for TXDONE
        ANDI    r1, 1
        BEQ     done
        IOR     r1, E+2
        PUSH    r1
        HALT
"""


@cocotb.test()
async def test_slu_chip_eth_tx(dut):
    """10BASE-Te helper in the chip: thread 0 sends a 46-byte frame from the data memory (preamble
    and SFD words from the memory, CRC-32 by the helper); TD+ on uio0 (TXA), TD- on uio1 (TXB), OE on
    uio2, through the pin flip-flops. The pins must carry the IEEE 802.3 Manchester halves of the
    frame with its FCS, then the TP_IDL hold, and the thread must see TXDONE."""
    payload = bytes((7 * i + 3) & 0xFF for i in range(46))
    words = [0x5555, 0x5555, 0x5555, 0xD555] + [payload[i] | (payload[i + 1] << 8) for i in range(0, 46, 2)]
    prog = pe_asm.assemble(ETH_TX_FW, "eth_tx")
    pins = [0] * 15
    pins[0], pins[1], pins[2] = helper_pin(1, 0), helper_pin(1, 1), helper_pin(1, 4)
    host = await chip_start(dut, prog, [(512, [len(words)] + words)], pins)
    caps = []

    async def cap(n):
        for _ in range(n):
            await FallingEdge(dut.clk)
            caps.append((int(dut.uio_out.value), int(dut.uio_oe.value)))
    task = cocotb.start_soon(cap(5000))
    await host.start()
    await task
    assert (caps[0][1] & 0x7) == 0, "helper pins are released while the threads stop"
    assert (caps[-1][1] & 0x7) == 0x7, "helper pins uio0-2 are outputs (OE = 1) while running"
    oe_sig = [(v >> 2) & 1 for v, _ in caps]
    # the helper OE signal on uio2: V = 1 while stopped (reset level), 0 after START, 1 from the frame
    first = next(i for i in range(1, len(oe_sig)) if oe_sig[i] and not oe_sig[i - 1])
    td = [v & 1 for v, _ in caps]
    tdm = [(v >> 1) & 1 for v, _ in caps]
    exp = ref.manchester_halves(ref.eth_frame_bits(payload))
    got = [td[first + 3 * i + 1] for i in range(len(exp))]
    assert got == exp, "uio0 differs from the Manchester halves of preamble, SFD, payload, CRC-32"
    assert all(tdm[first + 3 * i + 1] == 1 - exp[i] for i in range(len(exp))), "uio1 = not uio0"
    end = first + 3 * len(exp)
    hold = 0
    while td[end + hold] == 1 and oe_sig[end + hold]:
        hold += 1
    last_high = 3 if exp[-1] == 1 else 0
    assert 13 <= hold + last_high <= 27, "TP_IDL hold %d cycles" % (hold + last_high)
    words_out, _ = await host.pull(0)
    assert words_out and words_out[0] & 1, "thread saw TXDONE: %s" % words_out
    dut._log.info("chip 10BASE-Te TX: thread 0 sent %d words from the data memory; %d Manchester half "
                  "bits on uio0 equal to the frame with FCS, uio1 inverted, TP_IDL hold %d cycles; "
                  "thread status word %04x" % (len(words), len(exp), hold + last_high, words_out[0]))


ETH_RX_FW = """
        .equ    E, 32
        LDI     r0, 8
        IOW     r0, E+12        ; CFGADR = 8 (SEL)
        LDI     r0, 0x398       ; DIN = input 8 (ui_in[0]), AUX0 = input 9 (ui_in[1]), AUX1 uio3 (unused)
        IOW     r0, E+13
        LDI     r2, 600         ; words go to the data memory from 600
rx:     IOR     r1, E+6         ; RXINFO [6:5]: words in the RX buffer
        ANDI    r1, 0x60
        BEQ     chk
        IOR     r0, E+1         ; RXDATA (pop)
        STX     r0, [r2]
        ADDI    r2, 1
        JMP     rx
chk:    IOR     r1, E+2         ; STATUS: RXEOP?
        ANDI    r1, 0x20
        BEQ     rx
drain:  IOR     r1, E+6
        ANDI    r1, 0x60
        BEQ     fin
        IOR     r0, E+1
        STX     r0, [r2]
        ADDI    r2, 1
        JMP     drain
fin:    IOR     r1, E+2
        PUSH    r1
        PUSH    r2
        HALT
"""


@cocotb.test()
async def test_slu_chip_eth_rx(dut):
    """10BASE-Te helper in the chip: a 64-byte frame on ui_in[0] (slicer output) with the squelch
    comparator on ui_in[1], +/-13.5 ns zero-crossing jitter and +80 ppm; thread 0 copies the received
    words to the data memory and pushes the status; the host reads the words after STOP (READ_MEM)."""
    rng = random.Random(16)
    payload = bytes(rng.randrange(256) for _ in range(64))
    prog = pe_asm.assemble(ETH_RX_FW, "eth_rx")
    host = await chip_start(dut, prog, [], [0] * 15)
    await host.start()
    await ClockCycles(dut.clk, 200)
    bits = ref.eth_frame_bits(payload)
    lv0, ev = manchester_edges(bits, 100.0, 80.0, 13.5, rng, t0=2000.0)
    ui = 1 << hs.CS_N_BIT

    async def drive():
        base = get_sim_time("ns")
        events = [(2000.0 - 300.0, 0, lv0), (2000.0 - 300.0, 1, 1)]
        events += [(t, 0, lv) for t, lv in ev]
        tend = sorted(ev)[-1][0] + 100.0
        events += [(tend, 0, 0), (tend + 300.0, 1, 0)]
        level = {0: 0, 1: 0}
        for t, pin, lv in sorted(events):
            dt = base + t - get_sim_time("ns")
            if dt > 0:
                await Timer(round(dt * 1000), unit="ps")
            level[pin] = lv
            dut.ui_in.value = ui | level[0] | (level[1] << 1)
        return tend
    tend = await drive()
    await Timer(round(5000 * 1000), unit="ps")
    words_out, _ = await host.pull(0)
    assert len(words_out) == 2, "thread pushed status and end address: %s" % words_out
    status, end = words_out
    await host.stop()
    n = end - 600
    got_words = await host.read_mem(600, n)
    got = b"".join(w.to_bytes(2, "little") for w in got_words)
    exp = payload + binascii_crc(payload).to_bytes(4, "little")
    assert got[:len(exp)] == exp, "received bytes differ: %s" % got.hex()
    assert status & (1 << 6), "CRC-32 residue (CRCA_OK), status %04x" % status
    dut._log.info("chip 10BASE-Te RX: 64-byte frame at +80 ppm, 13.5 ns jitter on ui_in[0]: %d words "
                  "in the data memory equal to payload + FCS; status %04x (CRC ok)" % (n, status))


async def drive_ui(dut, schedule):
    """schedule: (time_ns from now, {k: level}) for ui_in[k] (k = DIN 0, AUX0 1, AUX1 2), host idle."""
    t = 0.0
    v = 1 << hs.CS_N_BIT
    for tn, pins in schedule:
        dt = tn - t
        if dt > 0:
            await Timer(round(dt * 1000), unit="ps")
        t = tn
        for p, lv in pins.items():
            v = (v & ~(1 << p)) | (lv << p)
        dut.ui_in.value = v


USB_TX_FW = ETH_TX_FW.replace(".equ    E, 32", ".equ    E, 16")


@cocotb.test()
async def test_slu_chip_usb_tx(dut):
    """USB low-speed helper in the chip: thread 0 sends DATA0 with 8 bytes from the data memory (SYNC
    and PID word from the memory, CRC16 and stuffing by the helper); VPO on uio0 (TXA), VMO on uio1
    (TXB), OE# on uio2 (not OE). Line levels at each bit centre = NRZI(stuff(SYNC PID DATA CRC16)),
    then SE0 84 cycles and J 40 cycles, then OE# high."""
    pid = 0xC3
    data = bytes([0xFF, 0xFF, 0x00, 0x7E, 0x01, 0x80, 0xFE, 0x55])
    words = [0x80 | (pid << 8)] + [data[i] | (data[i + 1] << 8) for i in range(0, 8, 2)]
    prog = pe_asm.assemble(USB_TX_FW, "usb_tx")
    pins = [0] * 15
    pins[0], pins[1], pins[2] = helper_pin(0, 0), helper_pin(0, 1), helper_pin(0, 5)
    host = await chip_start(dut, prog, [(512, [len(words)] + words)], pins)
    caps = []

    async def cap(n):
        for _ in range(n):
            await FallingEdge(dut.clk)
            caps.append(int(dut.uio_out.value))
    task = cocotb.start_soon(cap(40 * 140))
    await host.start()
    await task
    oen = [(v >> 2) & 1 for v in caps]
    first = next(i for i in range(1, len(oen)) if not oen[i] and oen[i - 1])
    lines = [v & 3 for v in caps]                       # bit 1 VMO, bit 0 VPO
    exp_line = ref.nrzi(ref.stuff(ref.usb_packet_bits(pid, data, crc="16")))
    got = []
    for i in range(len(exp_line)):
        v = lines[first + 40 * i + 20]
        assert v in (1, 2), "bit %d: not J or K (%d)" % (i, v)
        got.append(1 if v == 2 else 0)
    assert got == exp_line, "uio0/uio1 differ from NRZI(stuff(SYNC PID DATA CRC16))"
    eop = first + 40 * len(exp_line)
    se0 = 0
    while lines[eop + se0] == 0:
        se0 += 1
    j = 0
    while not oen[eop + se0 + j] and lines[eop + se0 + j] == 2:
        j += 1
    assert (se0, j) == (84, 40), "EOP: SE0 %d, J %d cycles" % (se0, j)
    assert oen[eop + se0 + j] == 1, "OE# high after the EOP"
    dut._log.info("chip USB TX: %d line bits on uio0/uio1 equal to NRZI(stuff(SYNC PID DATA CRC16)); "
                  "SE0 %d cycles, J %d cycles, then OE# high" % (len(exp_line), se0, j))


USB_RX_FW = """
        .equ    E, 16
        LDI     r0, 8
        IOW     r0, E+12        ; CFGADR = 8 (SEL)
        LDI     r0, 0xA9        ; SEL 0xA98: DIN = input 8 (ui_in[0], RCV), AUX0 = input 9 (VP),
        SHL     r0, 4           ;            AUX1 = input 10 (VM)
        ORI     r0, 8
        IOW     r0, E+13
        LDI     r2, 600
rx:     IOR     r1, E+6
        ANDI    r1, 0x60
        BEQ     chk
        IOR     r0, E+1
        STX     r0, [r2]
        ADDI    r2, 1
        JMP     rx
chk:    IOR     r1, E+2
        ANDI    r1, 0x20
        BEQ     rx
drain:  IOR     r1, E+6
        ANDI    r1, 0x60
        BEQ     fin
        IOR     r0, E+1
        STX     r0, [r2]
        ADDI    r2, 1
        JMP     drain
fin:    IOR     r1, E+2
        PUSH    r1
        PUSH    r2
        HALT
"""


@cocotb.test()
async def test_slu_chip_usb_rx(dut):
    """USB low-speed helper in the chip: a host DATA0 packet with 8 bytes at -1.5 % on RCV/VP/VM
    (ui_in[0..2]); thread 0 copies the words to the data memory; bytes and the CRC16 flag checked."""
    data = bytes([0x12, 0xFF, 0xFF, 0x00, 0xA5, 0x7F, 0xFE, 0x80])
    prog = pe_asm.assemble(USB_RX_FW, "usb_rx")
    host = await chip_start(dut, prog, [], [0] * 15)
    await host.start()
    await ClockCycles(dut.clk, 200)
    line = ref.nrzi(ref.stuff(ref.usb_packet_bits(0xC3, data, "16")))
    sch, t_end = usb_host_schedule(line, 1000.0 / 1.5 * (1 - 0.015), 2000.0)
    await drive_ui(dut, sch)
    await Timer(round(5000 * 1000), unit="ps")
    words_out, _ = await host.pull(0)
    assert len(words_out) == 2, "thread pushed status and end address: %s" % words_out
    status, end = words_out
    await host.stop()
    got = b"".join(w.to_bytes(2, "little") for w in await host.read_mem(600, end - 600))
    exp = bytes([0xC3]) + data + ref.bytes_of_bits(ref.usb_crc16_bits(ref.bits_of_bytes(data)))
    assert got[:len(exp)] == exp, "received bytes %s, expected %s" % (got.hex(), exp.hex())
    assert status & (1 << 6), "CRC16 residue (CRCA_OK), status %04x" % status
    dut._log.info("chip USB RX: DATA0 + 8 bytes at -1.5 %% on ui_in[0..2]: %d words in the data memory "
                  "equal to PID, data and CRC16; status %04x" % (end - 600, status))


def tx_event_fw(e, evin):
    """ETH_TX_FW for the helper on ports e..e+15, with the end of the frame taken from the helper's event input
    (EVMASK = TXDONE, then WAIT on input evin) instead of a STATUS poll."""
    fw = ETH_TX_FW.replace(".equ    E, 32", ".equ    E, %d" % e)
    fw = fw.replace("        LDI     r0, 544", "        LDI     r0, 1\n        IOW     r0, E+3         ; EVMASK = TXDONE\n"
                    "        LDI     r0, 544")
    old = ("done:   IOR     r1, E+2         ; STATUS: wait for TXDONE\n"
           "        ANDI    r1, 1\n"
           "        BEQ     done\n")
    assert old in fw
    return fw.replace(old, "        WAIT    %d, RISE, r1     ; helper event input: TXDONE\n" % evin)


@cocotb.test()
async def test_slu_chip_eth_events(dut):
    """10BASE-Te helper in the chip, event input and delayed taps: thread 0 sends a frame and waits for TXDONE on
    input 14 (WAIT; docs/isa.md 5.1), then pushes the status. Pins: TD+ uio0, TD- uio1, OE uio2, TXC (TD+ one cycle
    later) uio3, TXD (TD- one cycle later) uio4. Checks the half bits on uio0, uio3 = uio0 one cycle later,
    uio4 = not uio3, and that the thread saw TXDONE through the event input."""
    payload = bytes((5 * i + 1) & 0xFF for i in range(46))
    words = [0x5555, 0x5555, 0x5555, 0xD555] + [payload[i] | (payload[i + 1] << 8) for i in range(0, 46, 2)]
    prog = pe_asm.assemble(tx_event_fw(ETH_PORT, 14), "eth_tx_ev")
    pins = [0] * 15
    pins[0], pins[1], pins[2] = helper_pin(1, 0), helper_pin(1, 1), helper_pin(1, 4)
    pins[3], pins[4] = helper_pin(1, 2), helper_pin(1, 3)
    host = await chip_start(dut, prog, [(512, [len(words)] + words)], pins)
    caps = []

    async def cap(n):
        for _ in range(n):
            await FallingEdge(dut.clk)
            caps.append(int(dut.uio_out.value))
    task = cocotb.start_soon(cap(5000))
    await host.start()
    await task
    oe_sig = [(v >> 2) & 1 for v in caps]
    first = next(i for i in range(1, len(oe_sig)) if oe_sig[i] and not oe_sig[i - 1])
    exp = ref.manchester_halves(ref.eth_frame_bits(payload))
    td = [v & 1 for v in caps]
    txc = [(v >> 3) & 1 for v in caps]
    txd = [(v >> 4) & 1 for v in caps]
    assert [td[first + 3 * i + 1] for i in range(len(exp))] == exp, "uio0 differs from the Manchester halves"
    assert all(txc[first + 3 * i + 2] == exp[i] for i in range(len(exp))), "uio3 (TXC) = uio0 one cycle later"
    assert all(txd[k] == 1 - txc[k] for k in range(first + 1, first + 3 * len(exp))), "uio4 (TXD) = not uio3"
    words_out, _ = await host.pull(0)
    assert len(words_out) == 1 and words_out[0] & 1, "thread 0 waited for TXDONE on input 14: %s" % words_out
    dut._log.info("chip 10BASE-Te events: %d half bits on uio0, the delayed taps on uio3/uio4; the thread woke on "
                  "event input 14 and pushed status %04x" % (len(exp), words_out[0]))


@cocotb.test()
async def test_slu_chip_usb_events(dut):
    """USB low-speed helper in the chip, event input: thread 0 sends DATA0 with 8 bytes and waits for TXDONE on
    input 13 (WAIT), then pushes the status; VPO uio0, VMO uio1, OE# uio2."""
    pid = 0xC3
    data = bytes([0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80])
    words = [0x80 | (pid << 8)] + [data[i] | (data[i + 1] << 8) for i in range(0, 8, 2)]
    prog = pe_asm.assemble(tx_event_fw(USB_PORT, 13), "usb_tx_ev")
    pins = [0] * 15
    pins[0], pins[1], pins[2] = helper_pin(0, 0), helper_pin(0, 1), helper_pin(0, 5)
    host = await chip_start(dut, prog, [(512, [len(words)] + words)], pins)
    await host.start()
    await ClockCycles(dut.clk, 40 * 140)
    words_out, _ = await host.pull(0)
    assert len(words_out) == 1 and words_out[0] & 1, "thread 0 waited for TXDONE on input 13: %s" % words_out
    dut._log.info("chip USB events: the thread woke on event input 13 after the packet and pushed status %04x"
                  % words_out[0])

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
import sys

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


def loop_nacc(cfg, xs):
    """Accepted-edge count of the model after the stream xs (fed as run_stream feeds it)."""
    lp = Loop(cfg)
    lp.xprev = xs[0]
    per = 2 if cfg.ddr else 1
    for k in range(1, len(xs) - per + 1, per):
        if cfg.ddr:
            lp.step_pair(xs[k], xs[k + 1])
        else:
            lp.step(xs[k])
    return lp.nacc


@cocotb.test(skip=GL)
async def test_slu_loop_acq_window(dut):
    """10BASE-T loop (N = 12 samples a bit, both clock edges): for the first 24 accepted edges after the initial
    sync the window is the acquisition window of 5 samples, then 3 (LOOP2, LOOP3). Manchester of alternating bits
    (one edge a bit, at mid-bit) with the 7th edge, in acquisition, moved by 4 samples (inside 5, outside 3) or by 6
    (half a bit, outside both; a larger move is a smaller one toward the neighbouring edge, as the phase error wraps
    at half a bit): the model accepts the 4-sample edges and refuses the 6-sample one, and RTL = model in every
    cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.slu_ut_rst_n.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 1
    assert (TENBASET.wina, TENBASET.win, TENBASET.acq_n) == (5, 3, 24), "test fault: loop configuration"
    await configure(dut, TENBASET)

    def stream(d):
        edges = [31 + 12 * i for i in range(40)]
        edges[6] += d
        xs, lv = [], 1
        for k in range(525):                         # xs[0], then 262 sample pairs
            if k in edges:
                lv = 1 - lv
            xs.append(lv)
        return xs

    base = loop_nacc(TENBASET, stream(0))
    log = [f"no move: {base} edges accepted"]
    for d in (4, -4, 6):
        xs = stream(d)
        na = loop_nacc(TENBASET, xs)
        assert na == base - (1 if abs(d) > 5 else 0), f"edge moved by {d}: model accepted {na} edges, {base} unmoved"
        cyc, got, err = await run_stream(dut, TENBASET, xs)
        assert err is None, f"edge moved by {d} samples: {err}"
        log.append(f"moved {d:+d}: {na} accepted, {cyc} cycles, {len(got)} bits, RTL = model")
    dut._log.info("10BASE-T loop acquisition window: " + "; ".join(log))


def loop_nacc_before(cfg, xs, e):
    """Accepted-edge count of the model before the sample pair that starts at sample e (ddr streams)."""
    lp = Loop(cfg)
    lp.xprev = xs[0]
    k = 1
    while k + 1 < e:
        lp.step_pair(xs[k], xs[k + 1])
        k += 2
    return lp.nacc


@cocotb.test(skip=GL)
async def test_slu_loop_acq_end(dut):
    """10BASE-T loop: the acquisition window of 5 samples holds for exactly the first 24 accepted edges after the
    initial sync, the window of 3 samples from the 25th on (LOOP2, LOOP3: acq_n = 24). Manchester of alternating
    bits (one edge a bit, at mid-bit). The 24th accepted edge moved by 4 samples is accepted, the 25th moved by 4
    (either way) is refused; the model count before each moved edge is checked first. After acquisition an edge moved
    by exactly 3 samples (the window limit) is accepted, by 4 refused. RTL = model in every cycle."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.slu_ut_rst_n.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 1
    assert (TENBASET.wina, TENBASET.win, TENBASET.acq_n) == (5, 3, 24), "test fault: loop configuration"
    await configure(dut, TENBASET)
    edges0 = [31 + 12 * i for i in range(40)]        # odd sample numbers: each edge starts a sample pair

    def stream(j, d):
        edges = list(edges0)
        edges[j] += d
        xs, lv = [], 1
        for k in range(525):
            if k in edges:
                lv = 1 - lv
            xs.append(lv)
        return xs

    base_xs = stream(0, 0)
    base = loop_nacc(TENBASET, base_xs)
    before = {j: loop_nacc_before(TENBASET, base_xs, edges0[j]) for j in (25, 26, 30)}
    assert before == {25: 23, 26: 24, 30: 28}, f"test fault: accepted edges before edges 25, 26, 30: {before}"
    log = [f"no move: {base} edges accepted"]
    # After acquisition every edge of this stream has phase error 0 (no correction, integrator 0), so an edge moved by
    # exactly 3 samples has the error +3 or -3 samples: the window limit, accepted (|eps| <= window); 4: refused.
    for j, d, refused in ((25, 4, False), (25, -4, False), (26, 4, True), (26, -4, True),
                          (30, 3, False), (30, -3, False), (30, 4, True)):
        xs = stream(j, d)
        na = loop_nacc(TENBASET, xs)
        assert na == base - (1 if refused else 0), f"edge {j} moved by {d}: model accepted {na}, {base} unmoved"
        cyc, got, err = await run_stream(dut, TENBASET, xs)
        assert err is None, f"edge {j} moved by {d} samples: {err}"
        log.append(f"accepted edge {before[j] + 1} moved {d:+d}: {na} accepted, {cyc} cycles, {len(got)} bits, RTL = model")
    dut._log.info("10BASE-T loop end of acquisition: " + "; ".join(log))


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
    # While idle (OE off) the outputs stay at J, before and after the frame (docs/helpers.md: idle level J).
    idle_after = lines[eop + se0 + j:eop + se0 + j + 20]
    assert first > 0 and set(lines[:first]) == {2} and set(idle_after) == {2}, \
        f"idle level before / after the frame not J (VMO 1, VPO 0): {set(lines[:first])}, {set(idle_after)}"
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
    # The EOP SE0 lasts 2 bit times (80 cycles); the receiver qualifies it after 20 SE0 samples (PAT2[15:10]), so
    # the EOP wait state lasts the rest of the SE0 up to the J: 60 cycles.
    assert runs[2][1] == 60, f"EOP wait {runs[2][1]} cycles"


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
async def test_slu_tx_buffer_full(dut):
    """A TXDATA write while both buffer words wait and no word is taken is dropped: free (TXDATA read)
    stays 0, the frame carries the first two words only, and afterwards both words are free."""
    s = await slu_reset(dut)
    await s.cfg({0: mode(0), 1: 4})                  # NRZ, 4 cycles a bit, no CRC
    await s.wr(P_TXCTRL, 0)                          # last word 16 bits
    await s.wr(P_TXDATA, 0x1234)
    assert await s.rd(P_TXDATA) == 1, "one word free"
    await s.wr(P_TXLAST, 0xA5C3)
    assert await s.rd(P_TXDATA) == 0, "no word free"
    await s.wr(P_TXDATA, 0xFFFF)                     # full: dropped
    free = await s.rd(P_TXDATA)
    assert free == 0, "a write to the full buffer changed the free count to %d" % free
    caps = []
    cocotb.start_soon(capture(dut, 4 * 40, caps))
    await s.wr(P_TXCTRL, 1 << 9)                     # start now (TXTIME = now + 4)
    while len(caps) < 4 * 40:
        await RisingEdge(dut.clk)
    oe = [c[1] for c in caps]
    first = oe.index(1)
    got = [caps[first + 4 * i + 2][0] & 1 for i in range(32)]
    exp = [(0x1234 >> i) & 1 for i in range(16)] + [(0xA5C3 >> i) & 1 for i in range(16)]
    assert got == exp, "frame bits %s" % "".join(map(str, got))
    assert oe[first + 4 * 32 + 8] == 0, "the frame ends after the two words"
    free = await s.rd(P_TXDATA)
    assert free == 2, "after the frame %d words free" % free
    dut._log.info("TX buffer full: third write dropped (free 0), frame of 32 bits = 1234 A5C3, then free 2")


@cocotb.test(skip=GL)
async def test_slu_line_after_reset(dut):
    """LINE (port 11) during the reset and in the first cycles after its release: no pin is selected
    (SEL resets to all ones), the input synchronizers and the RX state reset to 0, so LINE reads 0,
    also with every pin high."""
    cocotb.start_soon(Clock(dut.clk, 16666, unit="ps").start())
    dut.slu_ut_rst_n.value = 0
    dut.slu_we.value = 0
    dut.slu_re.value = 0
    dut.slu_addr.value = P_LINE
    dut.slu_pins.value = (1 << 13) - 1
    reads = []
    for _ in range(3):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        reads.append(int(dut.slu_rdata.value))
    dut.slu_ut_rst_n.value = 1
    await Timer(1, unit="ns")
    reads.append(int(dut.slu_rdata.value))           # after the release, before the next clock edge
    for _ in range(4):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        reads.append(int(dut.slu_rdata.value))
    dut._log.info("LINE in reset (3 cycles), after the release, then 4 cycles: " +
                  " ".join("%04x" % r for r in reads))
    assert reads == [0] * len(reads), reads


@cocotb.test(skip=GL)
async def test_slu_squelch_after_idle(dut):
    """10BASE-Te squelch (SQ: 4 spacings in a row of 3 to 16 half cycles): after an idle gap the first
    edge does not end a valid spacing, so carrier needs 4 more edges. Gaps of 60 to 80 cycles (the gap
    counter saturates at 127 half cycles), each edge on a whole cycle (even half-cycle count), train
    spacing 6 cycles; LINE[3] (carrier) read before each train edge."""
    s = await slu_reset(dut)
    cfg = dict(ETH_CFG)
    cfg[0] = mode(2, ddr=1, rx=0, eop_car=1)         # receiver off: only the line conditions run
    await s.cfg(cfg)
    await FallingEdge(dut.clk)
    dut.slu_addr.value = P_LINE
    din = 0
    pins = 1 << AUX0                                 # squelch comparator: signal present

    async def cycle(level):
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")                    # first half of the cycle: x0 and x1 see the edge
        dut.slu_pins.value = pins | (level << DIN)

    async def carrier():
        await ReadOnly()
        return (int(dut.slu_rdata.value) >> 3) & 1

    log = []
    for gap in range(60, 81):
        for _ in range(gap - 1):
            await cycle(din)
        seen = []
        for e in range(6):
            din ^= 1
            await cycle(din)
            for _ in range(5):
                await cycle(din)
            seen.append(await carrier())
        n = seen.index(1) + 1 if 1 in seen else 0
        log.append("%d:%d" % (gap, n))
        assert n == 5, "gap %d cycles: carrier after edge %d of the train (expected 5): %s" % (gap, n, seen)
    dut._log.info("squelch after idle, gap cycles:train edges to carrier " + " ".join(log))


@cocotb.test(skip=GL)
async def test_slu_ports_after_reset(dut):
    """Every readable port right after reset: TXDATA 2 words free, every other port 0 (STATUS flags, EVMASK, TX
    state, TXTIME, RXINFO, the CRC registers, TSTAMP, LINE)."""
    s = await slu_reset(dut)
    want = {P_TXDATA: 2, P_RXDATA: 0, P_STATUS: 0, P_EVMASK: 0, P_TXCTRL: 0, P_TXTIME: 0, P_RXINFO: 0,
            P_CRCA_LO: 0, P_CRCA_HI: 0, P_CRCB: 0, P_TSTAMP: 0, P_LINE: 0}
    got = {}
    for a in want:
        await FallingEdge(dut.clk)
        dut.slu_addr.value = a
        await Timer(1, unit="ns")
        v = dut.slu_rdata.value
        assert v.is_resolvable, f"port {a} after reset: {v}"
        got[a] = int(v)
    dut._log.info("ports after reset: " + " ".join(f"{a}:{v:04x}" for a, v in got.items()))
    assert got == want, got


@cocotb.test(skip=GL)
async def test_slu_rxinfo_words(dut):
    """RXINFO[15:7] counts the full words of the last frame only: two USB packets (DATA0 with 8 bytes, then a
    token); for each, RXINFO words = RXDATA words popped less the part word (RXINFO[4:0] bits, if not 0)."""
    s = await slu_reset(dut)
    await s.cfg(USB_CFG_RX)
    data = bytes([0x12, 0xFF, 0xFF, 0x00, 0xA5, 0x7F, 0xFE, 0x80])
    tok = (0x15 | (1 << 7)).to_bytes(2, "little")
    log = []
    for pid, payload, crc in ((0xC3, data, "16"), (0xE1, tok, "5")):
        words, info, st, _ = await usb_rx_packet(dut, s, pid, payload, crc)
        nw, last = info >> 7, info & 0x1F
        log.append(f"PID {pid:02X}: {len(words)} words popped, RXINFO words {nw}, last bits {last}")
        assert nw == len(words) - (1 if last else 0), "; ".join(log)
    dut._log.info("RXINFO: " + "; ".join(log))


@cocotb.test(skip=GL)
async def test_slu_txtime_lead_limit(dut):
    """TXTIME lead 4 (the smallest on-time lead, docs: lead 4 to 32,767) starts the frame at TXTIME with no TXLATE;
    lead 3 is late: TXLATE, start at write + 4."""
    s = await slu_reset(dut)
    await s.cfg({0: mode(0), 1: 4})                  # NRZ, 4 cycles a bit
    await s.wr(P_TXCTRL, 0)
    log = []
    for lead in (4, 3):
        await s.wr(P_STATUS, 0xFFFF)
        await s.wr(P_TXLAST, 0x00F0)
        caps = []
        cocotb.start_soon(capture(dut, 4 * 30, caps))
        await FallingEdge(dut.clk)
        t_wr = int(dut.slu_now.value)
        dut.slu_addr.value = P_TXTIME
        dut.slu_wdata.value = (t_wr + lead) & 0xFFFF
        dut.slu_we.value = 1
        await FallingEdge(dut.clk)
        dut.slu_we.value = 0
        while len(caps) < 4 * 30:
            await RisingEdge(dut.clk)
        first = [c[2] for c in caps if c[1]][0]
        st = await s.rd(P_STATUS)
        log.append(f"lead {lead}: write in cycle {t_wr}, first symbol at now {first}, TXLATE {(st >> 1) & 1}")
        assert (first - t_wr) & 0xFFFF == 4, "; ".join(log)
        assert ((st >> 1) & 1) == (1 if lead < 4 else 0), "; ".join(log)
    dut._log.info("TXTIME lead limit: " + "; ".join(log))


@cocotb.test(skip=GL)
async def test_slu_eth_frame_end(dut):
    """10BASE-Te end of a frame: after the last bit, TP_IDL (TD+ high, tx_v 01) for 16 cycles (TAIL), then the
    outputs are released at once (tx_oe 0, tx_v 00): no other symbol after TP_IDL."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    await s.wr(P_TXCTRL, 0)                          # last word 16 bits, no CRC
    await s.wr(P_TXLAST, 0x255A)                     # last bit (bit 15) 0: its second half is TD+ low, not TP_IDL
    caps = []
    cocotb.start_soon(capture(dut, 300, caps))
    await s.wr(P_TXCTRL, 1 << 9)
    while len(caps) < 300:
        await RisingEdge(dut.clk)
    oe = [c[1] for c in caps]
    last = max(i for i, o in enumerate(oe) if o)
    tail = 0
    while caps[last - tail][0] & 3 == 1 and oe[last - tail]:
        tail += 1
    dut._log.info(f"10BASE-Te frame end: {tail} cycles of tx_v 01 (TP_IDL) to the last OE cycle, then tx_v "
                  f"{caps[last + 1][0] & 3:02b} OE {oe[last + 1]}")
    assert tail == 16 and caps[last][0] & 3 == 1, f"TP_IDL {tail} cycles, last OE cycle tx_v {caps[last][0] & 3:02b}"
    assert oe[last + 1] == 0 and caps[last + 1][0] & 3 == 0, "outputs not released after TP_IDL"


@cocotb.test(skip=GL)
async def test_slu_tx_write_at_take(dut):
    """A TXDATA/TXLAST write in the cycle in which the transmitter takes a word from the buffer (the frame start, or
    the load of the next word at a bit start) keeps the order: with one word waiting it becomes the next word, with
    two waiting it goes behind them. NRZ, 4 cycles a bit: A, then B written at the start take; A, B, then C at the
    start take; A, B, then C at the take of B inside the frame. Each frame carries its words in the order written
    and ends after them."""
    s = await slu_reset(dut)
    await s.cfg({0: mode(0), 1: 4})                  # NRZ, 4 cycles a bit, no CRC
    await s.wr(P_TXCTRL, 0)                          # last word 16 bits
    u = dut.u_slu_ut
    A, B, C = 0x1234, 0xA5C3, 0x0FF0

    async def write_at_take(addr, data, skip):
        # tx_take is 1 for the cycle whose closing clock edge takes the word; the write is driven for that cycle
        while True:
            await FallingEdge(dut.clk)
            if int(u.tx_take.value):
                if skip == 0:
                    break
                skip -= 1
        t, n = int(dut.slu_now.value), int(u.tx_cnt.value)
        dut.slu_addr.value = addr
        dut.slu_wdata.value = data
        dut.slu_we.value = 1
        await FallingEdge(dut.clk)
        dut.slu_we.value = 0
        return t, n

    log = []
    for label, pre, late, skip in (("start take", [A], B, 0), ("start take", [A, B], C, 0),
                                   ("load take", [A, B], C, 1)):
        for w in pre:
            await s.wr(P_TXDATA, w)
        caps = []
        cocotb.start_soon(capture(dut, 4 * 70, caps))
        await s.wr(P_TXTIME, (await s.now() + 20) & 0xFFFF)
        t, n = await write_at_take(P_TXLAST, late, skip)
        while len(caps) < 4 * 70:
            await RisingEdge(dut.clk)
        words = pre + [late]
        nb = 16 * len(words)
        oe = [c[1] for c in caps]
        first = oe.index(1)
        got = [caps[first + 4 * i + 2][0] & 1 for i in range(nb)]
        exp = [(w >> i) & 1 for w in words for i in range(16)]
        desc = f"{label} at now {t} with {n} word(s) in the buffer, words " + " ".join(f"{w:04X}" for w in words)
        assert got == exp, f"{desc}: frame bits {''.join(map(str, got))}"
        assert oe[first + 4 * nb + 8] == 0, f"{desc}: the frame does not end after its words"
        log.append(desc)
    dut._log.info("TX write in the take cycle: " + "; ".join(log) + ": each frame in the order written")


async def eth_rx_one(dut, s, payload):
    """One 10BASE-Te frame through the pins (no jitter, no offset); returns (words popped, RXINFO, STATUS) after
    its carrier-off EOP and the end of the line schedule."""
    sch, t_end = eth_rx_schedule(ref.eth_frame_bits(payload), 0.0, 0.0, random.Random(1), 1000.0)
    await s.wr(P_STATUS, 0xFFFF)
    cocotb.start_soon(drive_levels(dut, sch))
    words = []
    while True:
        st = await s.rd(P_STATUS)                    # status first: words of an EOP seen here are queued
        info = await s.rd(P_RXINFO)
        while (info >> 5) & 3:
            words.append(await s.rd(P_RXDATA, pop=True))
            info = await s.rd(P_RXINFO)
        if st & (1 << 5):
            break
    await Timer(round(t_end * 1000), unit="ps")
    return words, info, st


@cocotb.test(skip=GL)
async def test_slu_tstamp_hold(dut):
    """TSTAMP with MODE[13:12] = 0 holds now[15:0] of the last EOP (10BASE-Te: the carrier-off end of a received
    frame) until the next EOP: a frame that the unit sends and a received link test pulse leave it unchanged."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    await eth_rx_one(dut, s, bytes(range(46)))
    ts0 = await s.rd(P_TSTAMP)
    await s.wr(P_STATUS, 0xFFFF)
    await s.wr(P_TXCTRL, 0)
    await s.wr(P_TXLAST, 0x255A)
    await s.wr(P_TXCTRL, 1 << 9)                     # start now
    await ClockCycles(dut.clk, 16 * 6 + 60)
    st_tx = await s.rd(P_STATUS)
    ts1 = await s.rd(P_TSTAMP)
    await s.wr(P_STATUS, 0xFFFF)
    await drive_levels(dut, [(0.0, {DIN: 1, AUX0: 1}), (6 * T_NS, {DIN: 0, AUX0: 0}), (400.0, {})])
    st_lp = await s.rd(P_STATUS)
    ts2 = await s.rd(P_TSTAMP)
    dut._log.info(f"TSTAMP after the received frame {ts0:04x}, after a sent frame {ts1:04x} (status {st_tx:04x}), "
                  f"after a link pulse {ts2:04x} (status {st_lp:04x})")
    assert st_tx & 1 and st_lp & (1 << 13), "test fault: no TXDONE or no link pulse"
    assert ts0 != 0 and ts1 == ts0 and ts2 == ts0, "TSTAMP changed without an EOP"


@cocotb.test(skip=GL)
async def test_slu_crca_after_frame(dut):
    """CRCA_LO and CRCA_HI (ports 7, 8) after a received 10BASE-Te frame are the CRC A register: CRC-32 (reflected
    EDB88320, preset ones) over every data bit received after the SFD, computed here from the popped words (full
    words, then RXINFO[4:0] bits of the last one). The register over payload and FCS alone is the residue
    DEBB20E3 (IEEE 802.3 3.2.9)."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    payload = bytes((7 * i + 3) & 0xFF for i in range(64))
    words, info, st = await eth_rx_one(dut, s, payload)
    lastn = info & 0x1F
    bits = []
    for k, w in enumerate(words):
        n = lastn if (k == len(words) - 1 and lastn) else 16
        bits += [(w >> i) & 1 for i in range(n)]

    def crc32(bs):
        c = 0xFFFFFFFF
        for b in bs:
            c = (c >> 1) ^ (0xEDB88320 if (c ^ b) & 1 else 0)
        return c

    nfcs = 8 * (len(payload) + 4)
    assert crc32(bits[:nfcs]) == 0xDEBB20E3, "test fault: payload and FCS bits do not give the residue"
    lo, hi = await s.rd(P_CRCA_LO), await s.rd(P_CRCA_HI)
    dut._log.info(f"CRCA {hi:04x}{lo:04x} after {len(bits)} data bits ({len(bits) - nfcs} after the FCS); model "
                  f"{crc32(bits):08x}; status {st:04x}")
    assert st & (1 << 6), "CRCA_OK"
    assert (hi << 16 | lo) == crc32(bits), "CRCA_HI:CRCA_LO is not the CRC register of the received bits"


@cocotb.test(skip=GL)
async def test_slu_link_pulse_width(dut):
    """10BASE-Te link test pulse classifier (LP: 2 to 12 cycles): a high on TD+ (DIN) while the squelch comparator
    (AUX0) is active is a link pulse (STATUS 13) when it lasts 2 to 12 cycles; 1 and 13 cycles are not. Each high is
    driven from just after a rising clock edge for a whole number of cycles, so the input register sees it exactly
    that many times; AUX0 is active from 20 cycles before to 10 cycles after."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)

    async def level(pins, cycles):
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")
        dut.slu_pins.value = pins
        for _ in range(cycles - 1):
            await RisingEdge(dut.clk)

    res = {}
    for w in (1, 2, 12, 13):
        await s.wr(P_STATUS, 0xFFFF)
        await level(1 << AUX0, 20)
        await level((1 << AUX0) | (1 << DIN), w)
        await level(1 << AUX0, 10)
        await level(0, 30)
        res[w] = ((await s.rd(P_STATUS)) >> 13) & 1
    dut._log.info("link pulse width (cycles): LINKPULSE " + ", ".join(f"{w}: {v}" for w, v in res.items()))
    assert res == {1: 0, 2: 1, 12: 1, 13: 0}, res


async def slu_reset_again(dut):
    """Reset of the unit inside a test whose clock already runs (slu_reset starts the clock)."""
    await FallingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 0
    dut.slu_we.value = 0
    dut.slu_re.value = 0
    dut.slu_pins.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 1
    await RisingEdge(dut.clk)
    return Slu(dut)


@cocotb.test(skip=GL)
async def test_slu_sel_last_pin(dut):
    """SEL[3:0] chooses DIN among inputs 0 to 12; 13 to 15 are none (docs/helpers.md section 2). Input 12, the last
    one, reaches LINE[2] (DIN): 1 with input 12 high, 0 with input 12 low and every other input high; with SEL 13
    LINE[2] stays 0 while every input is high."""
    s = await slu_reset(dut)
    every = (1 << 13) - 1
    res = {}
    for sel, pins in ((12, every), (12, every & ~(1 << 12)), (13, every)):
        await s.cfg({8: sel | (15 << 4) | (15 << 8)})
        dut.slu_pins.value = pins
        await ClockCycles(dut.clk, 4)
        res[(sel, pins == every)] = ((await s.rd(P_LINE)) >> 2) & 1
    dut._log.info(f"LINE[2] (DIN) for (SEL, all inputs high): {res}")
    assert res == {(12, True): 1, (12, False): 0, (13, True): 0}, res


@cocotb.test(skip=GL)
async def test_slu_port_rules(dut):
    """Port rules of docs/helpers.md section 2 that no other test reads: EVMASK reads back what was written; CFGADR is
    0 after reset, so a first CFGDAT write goes to MODE; only a write to CFGADR sets the index (a TXDATA write between
    CFGADR and CFGDAT leaves it); RXDATA reads 0 when the receive buffer is empty, also after a frame. A MODE word
    with receive on (NRZ) moves the receive state from 0 (off) to 1 (hunt) in LINE[7:4]."""
    s = await slu_reset(dut)
    await s.wr(P_EVMASK, 0x5A5A)
    evm = await s.rd(P_EVMASK)
    await s.wr(P_EVMASK, 0)
    await s.wr(P_CFGDAT, mode(0, rx=1))              # no CFGADR write since reset: index 0 = MODE
    await ClockCycles(dut.clk, 2)
    st_first = (await s.rd(P_LINE)) >> 4
    s = await slu_reset_again(dut)
    await s.wr(P_CFGADR, 0)
    await s.wr(P_TXDATA, 0x0001)                     # not a CFGADR write: the index stays 0
    await s.wr(P_CFGDAT, mode(0, rx=1))
    await ClockCycles(dut.clk, 2)
    st_after_tx = (await s.rd(P_LINE)) >> 4
    s = await slu_reset_again(dut)
    await s.cfg(USB_CFG_RX)
    words, info, st, _ = await usb_rx_packet(dut, s, 0xC3, bytes([0x12, 0x34]), "16")
    empty = await s.rd(P_RXDATA, pop=True)
    dut._log.info(f"EVMASK read back {evm:04x}; RX state after a first CFGDAT (MODE, RX on) {st_first}, after "
                  f"CFGADR 0, TXDATA, CFGDAT {st_after_tx}; {len(words)} words popped, then RXDATA {empty:04x}")
    assert evm == 0x5A5A, f"EVMASK read back {evm:04x}"
    assert st_first == 1 and st_after_tx == 1, f"MODE write did not reach index 0: RX state {st_first}, {st_after_tx}"
    assert words and empty == 0, f"RXDATA with an empty buffer read {empty:04x}"


@cocotb.test(skip=GL)
async def test_slu_line_cond_exact(dut):
    """Line condition B (LTC[7:6] = 1, J) sets LINEB when its timer reaches LTB ticks: with one tick per cycle (LTC
    prescale 0) and LTB 7, STATUS bit 11 is set from exactly LTB + 1 = 8 cycles after the first cycle in which LINE
    shows J. Two runs from reset with the same stimulus: one reads LINE in every cycle, one reads STATUS."""
    res = {}
    for port in (P_LINE, P_STATUS):
        s = await (slu_reset(dut) if port == P_LINE else slu_reset_again(dut))
        await s.cfg({18: (1 << 6)})                  # LTC first: prescale 0, condition A SE0, condition B J
        await s.cfg({17: 7})                         # then LTB 7 (before LTC, condition B is SE0, the input now)
        await s.wr(P_STATUS, 0xFFFF)
        dut.slu_pins.value = 1 << AUX1               # J: VP (AUX0) 0, VM (AUX1) 1
        await s.wr(P_CFGADR, 8)
        await FallingEdge(dut.clk)
        dut.slu_addr.value = P_CFGDAT
        dut.slu_wdata.value = DIN | (AUX0 << 4) | (AUX1 << 8)
        dut.slu_we.value = 1
        await FallingEdge(dut.clk)
        dut.slu_we.value = 0
        dut.slu_addr.value = port
        t0 = int(dut.slu_now.value)                  # cycles counted from the SEL write in each run
        await Timer(1, unit="ns")                    # the first cycle after the write edge is read too
        v = int(dut.slu_rdata.value)
        first = 0 if ((v & 3) == 2 if port == P_LINE else bool(v & (1 << 11))) else None
        for _ in range(0 if first == 0 else 40):
            await RisingEdge(dut.clk)
            await ReadOnly()
            v = int(dut.slu_rdata.value)
            hit = (v & 3) == 2 if port == P_LINE else bool(v & (1 << 11))
            if hit:
                first = (int(dut.slu_now.value) - t0) & 0xFFFF
                break
        res[port] = first
        await FallingEdge(dut.clk)
    dut._log.info(f"cycles from the SEL write to the first LINE = J: {res[P_LINE]}; to the first LINEB: {res[P_STATUS]}")
    assert None not in res.values(), res
    assert res[P_STATUS] - res[P_LINE] == 8, f"LINEB {res[P_STATUS] - res[P_LINE]} cycles after J, expected 8"


@cocotb.test(skip=GL)
async def test_slu_runt_crc(dut):
    """CRCA_OK (STATUS bit 6) belongs to the last frame: after a good 10BASE-Te frame (CRCA_OK 1), a runt of SFD and
    5 data bits, which ends before its first octet boundary, gives CRCA_OK 0."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    _, _, st_good = await eth_rx_one(dut, s, bytes(range(46)))
    bits = [1, 0] * 28 + [1, 0, 1, 0, 1, 0, 1, 1] + [1, 0, 1, 1, 0]
    sch, t_end = eth_rx_schedule(bits, 0.0, 0.0, random.Random(2), 1000.0)
    await s.wr(P_STATUS, 0xFFFF)
    cocotb.start_soon(drive_levels(dut, sch))
    await Timer(round((t_end + 2000.0) * 1000), unit="ps")
    st_runt = await s.rd(P_STATUS)
    info = await s.rd(P_RXINFO)
    dut._log.info(f"status after the good frame {st_good:04x}, after the runt {st_runt:04x} (RXINFO {info:04x})")
    assert st_good & (1 << 6), "test fault: the good frame has no CRCA_OK"
    assert st_runt & (1 << 4) and st_runt & (1 << 5), "test fault: the runt gave no SOP or EOP"
    assert not st_runt & (1 << 6), "CRCA_OK of the runt shows the previous frame"


@cocotb.test(skip=GL)
async def test_slu_rx_overflow(dut):
    """The receive buffer holds 2 words (docs/helpers.md section 2): a USB DATA0 packet with 8 bytes (5 words with the
    PID and CRC16) that nobody reads keeps its first two words; the third word sets RXOVER (STATUS bit 9, word
    lost) and RXINFO[6:5] stays 2."""
    s = await slu_reset(dut)
    await s.cfg(USB_CFG_RX)
    data = bytes([0x12, 0xFF, 0xFF, 0x00, 0xA5, 0x7F, 0xFE, 0x80])
    line = ref.nrzi(ref.stuff(ref.usb_packet_bits(0xC3, data, "16")))
    sch, t_end = usb_host_schedule(line, 1000.0 / 1.5, 2000.0)
    await s.wr(P_STATUS, 0xFFFF)
    cocotb.start_soon(drive_levels(dut, sch))
    await Timer(round((t_end + 2000.0) * 1000), unit="ps")
    st = await s.rd(P_STATUS)
    info = await s.rd(P_RXINFO)
    w0 = await s.rd(P_RXDATA, pop=True)
    w1 = await s.rd(P_RXDATA, pop=True)
    empty = await s.rd(P_RXDATA, pop=True)           # buffer empty (its second slot still holds FFFF): reads 0
    exp = bytes([0xC3]) + data
    dut._log.info(f"unread packet: status {st:04x}, RXINFO {info:04x}, words {w0:04x} {w1:04x}, then {empty:04x}")
    assert empty == 0, f"RXDATA with an empty buffer read {empty:04x}"
    assert st & (1 << 5), "test fault: no EOP"
    assert st & (1 << 9) and (info >> 5) & 3 == 2, f"RXOVER / buffer count: status {st:04x}, RXINFO {info:04x}"
    assert [w0, w1] == [exp[0] | exp[1] << 8, exp[2] | exp[3] << 8], "the buffer does not keep the first two words"


@cocotb.test(skip=GL)
async def test_slu_txctrl_after_reset(dut):
    """TXCTRL after reset is 0 (docs/helpers.md section 5: a word can be put in the buffer with TXLAST while TXCTRL is
    0: 16 bits, no CRC). With no TXCTRL write since reset, a frame of one TXLAST word started by TXTIME has 16 bits.
    NRZ, 4 cycles a bit."""
    s = await slu_reset(dut)
    await s.cfg({0: mode(0), 1: 4})
    await s.wr(P_TXLAST, 0x8001)
    caps = []
    cocotb.start_soon(capture(dut, 4 * 30, caps))
    await s.wr(P_TXTIME, (await s.now() + 20) & 0xFFFF)
    while len(caps) < 4 * 30:
        await RisingEdge(dut.clk)
    oe = [c[1] for c in caps]
    first = oe.index(1)
    nbits = (sum(oe) + 2) // 4
    got = [caps[first + 4 * i + 2][0] & 1 for i in range(16)]
    dut._log.info(f"frame with no TXCTRL write: OE for {sum(oe)} cycles, bits {''.join(map(str, got))}")
    assert nbits == 16 and got == [(0x8001 >> i) & 1 for i in range(16)], f"{sum(oe)} OE cycles, bits {got}"


@cocotb.test(skip=GL)
async def test_slu_reset_in_frame(dut):
    """A reset in a frame (the processor reset that STOP holds) releases the outputs at once: OE 0 and every TX
    output 0 while the reset is held, and still after its release, until the next frame. 10BASE-Te frame of four
    words; the reset comes 20 bits into it."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    await s.wr(P_TXCTRL, 0)
    for w in (0x5555, 0x5555, 0xA5A5):
        await s.wr(P_TXDATA, w)
    await s.wr(P_TXCTRL, 1 << 9)                     # start now
    while not int(dut.slu_txoe.value):
        await RisingEdge(dut.clk)
    await ClockCycles(dut.clk, 20 * 6)
    in_frame = (int(dut.slu_txv.value), int(dut.slu_txoe.value))
    await FallingEdge(dut.clk)
    dut.slu_ut_rst_n.value = 0
    await Timer(1, unit="ns")
    held = [(int(dut.slu_txv.value), int(dut.slu_txoe.value))]
    for _ in range(3):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        held.append((int(dut.slu_txv.value), int(dut.slu_txoe.value)))
    dut.slu_ut_rst_n.value = 1
    after = []
    for _ in range(4):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        after.append(int(dut.slu_txoe.value))
    dut._log.info(f"in the frame (tx_v, OE) {in_frame}; reset held {held}; OE after the release {after}")
    assert in_frame[1] == 1, "test fault: the frame is not on the outputs"
    assert held == [(0, 0)] * 4, f"outputs while the reset is held: {held}"
    assert after == [0] * 4, f"OE after the release: {after}"


@cocotb.test(skip=GL)
async def test_slu_clk_part_word(dut):
    """Clocked mode receive (docs/helpers.md section 6): the last word of a frame holds its bits from bit 0 up and 0
    above them. Frames of 12 and 4 bits sent with TXPER 2 while DIN is high: the received word is 0FFF and 000F."""
    s = await slu_reset(dut)
    await s.cfg({0: mode(3, rx=1), 1: 2, 8: DIN | (15 << 4) | (15 << 8) | (2 << 12)})
    dut.slu_pins.value = 1 << DIN
    got = {}
    for n in (12, 4):
        await s.wr(P_STATUS, 0xFFFF)
        await s.wr(P_TXCTRL, n)                      # bits in the last word
        await s.wr(P_TXLAST, 0x0000)
        await s.wr(P_TXCTRL, n | (1 << 9))           # start now
        await ClockCycles(dut.clk, 4 * n + 120)
        info = await s.rd(P_RXINFO)
        got[n] = (await s.rd(P_RXDATA, pop=True), info & 0x1F, await s.rd(P_STATUS))
    dut._log.info("clocked receive with DIN high: " + "; ".join(
        f"{n} bits: word {w:04x}, RXINFO bits {b}, status {st:04x}" for n, (w, b, st) in got.items()))
    assert got[12][:2] == (0x0FFF, 12) and got[4][:2] == (0x000F, 4), got


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


@cocotb.test(skip=GL)
async def test_slu_link_pulse_start(dut):
    """Start cycle of a link test pulse against a frame (docs/helpers.md section 2, transmit timing), 10BASE-Te
    configuration: a frame armed with TXTIME = t has its first symbol on the helper output in cycle t; a link test
    pulse (TXCTRL bit 7) armed with TXTIME = t starts in cycle t - 1, because the output stage holds only the first
    cycle of a frame (the cycle that loads its first bit). The pulse is 6 cycles wide. Capture convention as
    test_slu_usb_tx: tx_v, tx_oe and now after each rising edge."""
    s = await slu_reset(dut)
    await s.cfg(ETH_CFG)
    res = {}
    for kind in ("frame", "pulse"):
        for _ in range(300):
            if (await s.rd(P_TXCTRL)) & 0xF == 0:
                break
        await s.wr(P_STATUS, 0xFFFF)
        if kind == "frame":
            await s.wr(P_TXCTRL, 0)                              # last word 16 bits, no CRC
            await s.wr(P_TXLAST, 0x5555)
        else:
            await s.wr(P_TXCTRL, 1 << 7)                         # link test pulse instead of a frame
        t = (await s.now() + 50) & 0xFFFF
        await s.wr(P_TXTIME, t)
        caps = []
        for _ in range(200):
            await RisingEdge(dut.clk)
            await ReadOnly()
            caps.append((int(dut.slu_txv.value), int(dut.slu_txoe.value), int(dut.slu_now.value),
                         get_sim_time("ns")))
        first = next(i for i, c in enumerate(caps) if c[1])
        off = ((caps[first][2] - t + 0x8000) & 0xFFFF) - 0x8000
        width = 0
        while caps[first + width][1] and caps[first + width][0] & 1:
            width += 1
        res[kind] = (t, caps[first][2], off, width, caps[first][3])
        dut._log.info("%s: TXTIME %d, first helper output cycle now %d (offset %+d), %.3f ns; TD+ high with OE for "
                      "%d cycles from there" % (kind, t, caps[first][2], off, caps[first][3], width))
    assert res["frame"][2] == 0, "frame: first symbol in cycle TXTIME %+d" % res["frame"][2]
    assert res["pulse"][2] == -1, "link test pulse: start in cycle TXTIME %+d, documented TXTIME - 1" % res["pulse"][2]
    assert res["pulse"][3] == 6, "link test pulse %d cycles wide" % res["pulse"][3]


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
    # the underflow ends the frame with the full tail: 16 bits of 6 cycles, then TP_IDL for TAIL = 16 cycles
    assert oe_cycles == 16 * 6 + 16, "OE for %d cycles after an underflow, expected 112" % oe_cycles
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


# The helper firmware of the chip tests below is the example code of fw/helper/ (C2-4), with its host calls
# (fw/helper/host_calls.py): the tests run exactly those files, at RTL and at gate level.
HELPER_FW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fw", "helper")
if HELPER_FW_DIR not in sys.path:
    sys.path.insert(0, HELPER_FW_DIR)
import host_calls as hc  # noqa: E402


def helper_fw(name):
    """Source text of an example program of fw/helper/."""
    with open(os.path.join(HELPER_FW_DIR, name), encoding="utf-8") as f:
        return f.read()


SPI_CLK_FW = helper_fw("spi_flash.s")


class ChipSpiPins:
    """The flash model of test_slu_clk.py on the chip pins: SCK uio0, MOSI uio1, /CS uio2 after the output delay;
    MISO back on ui_in[0] after the input delay (host port idle)."""

    def __init__(self, dut, dev, d_out):
        self.dut, self.dev, self.d_out = dut, dev, d_out
        self.last = None
        self.dump = getattr(dut, "dump_off", None)           # not in the coverage build (test/tb.v)
        self.review = self.dump is not None
        if self.review:
            self.dump.value = 1
        self.task = cocotb.start_soon(self.run())

    def dump_stop(self):
        self.dump.value = 1

    def set_pin(self, pin, level):
        self.dut.ui_in.value = (1 << hs.CS_N_BIT) | (level & 1)

    async def run(self):
        from cocotb.triggers import Edge
        import test_slu_clk as sc
        while True:
            await Edge(self.dut.uio_out)
            s = str(self.dut.uio_out.value)[-3:]                # uio2..uio0; the unused pins may be X or Z
            if any(c not in "01" for c in s):
                continue
            v = int(s, 2)
            ch = v ^ (self.last if self.last is not None else v ^ 7)
            self.last = v
            if ch & 4:
                cocotb.start_soon(sc.later(self.d_out, self.dev.set_cs, (v >> 2) & 1))
                if not (v >> 2) & 1 and self.review:
                    # waveform only for the first 5 us after /CS falls: command, address, first data bits
                    self.review = False
                    self.dump.value = 0
                    cocotb.start_soon(sc.later(5000.0, self.dump_stop))
            if ch & 2:
                cocotb.start_soon(sc.later(self.d_out, self.dev.on_di, (v >> 1) & 1))
            if ch & 1:
                cocotb.start_soon(sc.later(self.d_out, self.dev.on_clk, v & 1))


@cocotb.test()
async def test_slu_chip_spi_flash(dut):
    """USB helper in the chip in its clocked mode (SPI mode 0, 15 MHz SCK): thread 0 configures the helper, drives
    /CS (uio2) with OUT events and feeds a 03h read of 256 bytes from the data memory; thread 1 stores the received
    words; SCK on uio0 (helper TXB = clock), MOSI on uio1 (TXA = data), MISO on ui_in[0]. The flash model of
    test_slu_clk.py (W25Q128JV times) on the pins, with the slow pad and 10 ns multiplexer delays both ways."""
    import test_slu_clk as sc
    rng = random.Random(77)
    flash = [rng.randrange(256) for _ in range(512)]
    addr = 0x2C
    frame = hc.spi_flash_read_frame(addr, 256)[hc.FRAME_ADDR]          # host call: bit count, then the words
    bits = sc.bytes_to_bits([0x03, 0, 0, addr]) + [0] * (8 * 256)
    words = sc.words_of(bits)
    assert frame == [len(bits)] + words, "host call spi_flash_read_frame differs from the bench's own frame"
    prog = pe_asm.assemble(SPI_CLK_FW, "spi_clk")
    t1 = 120
    pins = [0] * 15
    pins[0], pins[1] = helper_pin(0, 1), helper_pin(0, 0)
    cocotb.start_soon(Clock(dut.clk, CHIP_CLK_PS, unit="ps").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    host = hs.SpiTimed(dut)
    await host.write_mem(0, prog.image()[:max(prog.words) + 1])     # up to the thread-1 code at .org 120
    await host.write_mem(512, [len(bits)] + words)
    owner = [None] * 15
    owner[2] = 0
    head, own = hs.config_words((0, t1, 0, 0), (True, True, False, False), owner, [False] * 15)
    pins[2] = own[2]                                                # /CS: a pin of thread 0
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    d_out = sc.OUT_PAD[1] + sc.MUX_MAX
    d_in = sc.IN_PAD[1] + sc.MUX_MAX
    dev = sc.SpiFlash(None, 0, 0, flash, d_in)
    io = ChipSpiPins(dut, dev, d_out)
    dev.u = io
    await host.start()
    await ClockCycles(dut.clk, 4 * len(bits) + 3000)
    words_out, _ = await host.pull(0)
    assert len(words_out) == 1, "thread 0 pushed its status: %s" % words_out
    status = words_out[0]
    await host.stop()
    end = (await host.read_mem(500, 1))[0]
    dut._log.info("thread 0 status %04x; thread 1 stored words up to address %d (%d words, %d expected); the flash "
                  "saw %d bits, %d timing errors %s" % (status, end, end - 768, len(words), len(dev.bits),
                                                        len(dev.errors), dev.errors[:3]))
    assert end - 768 == len(words), "thread 1 stored %d words, expected %d" % (end - 768, len(words))
    got_words = await host.read_mem(768, len(words))
    got = sc.bits_to_bytes(sc.bits_of(got_words, len(bits))[32:])
    assert hc.spi_flash_read_bytes(got_words, 256) == got, "host call spi_flash_read_bytes differs from the bench"
    want = [flash[(addr + k) % len(flash)] for k in range(256)]
    io.task.cancel()
    assert not status & 0x204, "status %04x: TXUNDER or RXOVER" % status
    assert not dev.errors, "flash timing at its pins: %s" % dev.errors[:3]
    assert got == want, "%d of 256 bytes differ (first %s)" % (sum(a != b for a, b in zip(got, want)),
                                                              [(i, got[i], want[i]) for i in range(256) if got[i] != want[i]][:3])
    dut._log.info("chip SPI flash read: 03h of 256 bytes at 15 MHz SCK in mode 0 through uio0/uio1/ui_in[0], /CS on "
                  "uio2 by thread 0, words stored by thread 1; slow pads + 10 ns mux both ways; equal to the flash, "
                  "0 timing errors at the flash pins; status %04x" % status)


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


UART_HTX_FW = helper_fw("uart_tx.s")


@cocotb.test()
async def test_slu_chip_uart_tx(dut):
    """USB helper in the chip as a UART transmitter (NRZ code, TXPER 4 = 15 Mbaud at 60 MHz; docs/helpers.md section
    6): thread 0 configures the helper and feeds 64 back-to-back 8N1 bytes from the data memory (the host packed the
    start and stop bits into the words); TXA on uio0. An independent receiver decodes uio0 at each bit middle, every
    edge must lie on the 4-cycle grid of the first start bit, and the thread must see TXDONE without TXUNDER. Logic
    simulation at the pin flip-flop (the pad adds a constant delay to every edge)."""
    import test_slu_clk as sc
    rng = random.Random(1119)
    data = [0x55, 0x00, 0xFF, 0x0F] + [rng.randrange(256) for _ in range(60)]
    bits = sc.uart_bits(data)
    words = sc.words_of(bits)
    assert hc.uart_tx_frame(data) == {hc.FRAME_ADDR: [len(bits)] + words}, "host call uart_tx_frame differs"
    prog = pe_asm.assemble(UART_HTX_FW, "uart_htx")
    pins = [0] * 15
    pins[0] = helper_pin(0, 0)                                       # uio0 = USB helper TXA (data)
    host = await chip_start(dut, prog, list(hc.uart_tx_frame(data).items()), pins)
    caps = []

    async def cap(n):
        for _ in range(n):
            await FallingEdge(dut.clk)
            caps.append(int(dut.uio_out.value) & 1)
    task = cocotb.start_soon(cap(4 * len(bits) + 1500))
    dump = getattr(dut, "dump_off", None)                            # not in the coverage build (test/tb.v)
    if dump is not None:                                             # waveform: START and the first 6 us
        dump.value = 1
    await host.start()
    if dump is not None:
        dump.value = 0
        dut._log.info("waveform: review window from %.1f ns (START) for 6 us" % get_sim_time("ns"))
        import test_slu_clk as sc2
        cocotb.start_soon(sc2.later(6000.0, setattr, dump, "value", 1))
    await task
    # after START the helper shows the USB idle (TXA 0) until MODE is written (docs/helpers.md section 6, After START)
    low0 = next(i for i in range(len(caps)) if caps[i] == 0)
    low_n = next(i for i in range(low0, len(caps)) if caps[i] == 1) - low0
    start = next(i for i in range(low0 + low_n + 40, len(caps)) if caps[i] == 0 and all(caps[i - 40:i]))
    got, nedges, off = sc.uart_decode(caps[start:], 4, len(data))
    words_out, _ = await host.pull(0)
    assert len(words_out) == 1, "thread 0 pushed its status: %s" % words_out
    status = words_out[0]
    dut._log.info("chip UART TX on the USB helper at TXPER 4 (15 Mbaud): %d bytes back to back, %d line bits, %d edges, "
                  "%d off the 4-cycle grid, decoded %s; START level: TXA low for %d cycles before MODE; status %04x" % (
                      len(data), len(bits), nedges, len(off), "equal" if got == data else "DIFFERENT", low_n, status))
    assert got == data, "decoded %s" % ["%02X" % b for b in got[:8]]
    assert not off, "edges off the bit grid at cycles %s" % off[:8]
    assert status & 1 and not status & 4, "status %04x: TXDONE without TXUNDER expected" % status


UART_HRX_FW = helper_fw("uart_rx.s")


@cocotb.test()
async def test_slu_chip_uart_rx(dut):
    """USB helper in the chip as a UART receiver (edge-started frames, LTC[10], 4 cycles a bit = 15 Mbaud at 60 MHz;
    docs/helpers.md section 6): a sender drives ui_in[0] with back-to-back 8N1 bytes at -2.5 % and +2.5 % of the bit
    time, with the fast input pad and with the slow input pad plus 10 ns of board multiplexer; thread 0 configures the
    helper and stores every received word in the data memory (one word a byte: data in bits 7 to 14, start bit 6, stop
    bit 15); the host reads them after STOP. Logic simulation with the input path as a delay."""
    import test_slu_clk as sc
    rng = random.Random(1120)
    runs = [(err, d_in) for err in (-0.025, 0.025) for d_in in (sc.IN_PAD[0], sc.IN_PAD[1] + sc.MUX_MAX)]
    sent = []
    plan = []
    for err, d_in in runs:
        data = [rng.randrange(256) for _ in range(32)]
        sent += data
        plan.append((err, d_in, data))
    prog = pe_asm.assemble(UART_HRX_FW, "uart_hrx")
    host = await chip_start(dut, prog, list(hc.uart_rx_buffer(len(sent)).items()), [0] * 15)
    if getattr(dut, "dump_off", None) is not None:
        dut.dump_off.value = 1                                       # no waveform until the review window
    ui = 1 << hs.CS_N_BIT
    await host.start()
    dut.ui_in.value = ui | 1                                         # line idle high (the host port drives all of ui_in)
    await ClockCycles(dut.clk, 300)
    dump = getattr(dut, "dump_off", None)                            # not in the coverage build (test/tb.v)
    if dump is not None:                                             # waveform: the first 4 us of the first run
        dump.value = 0
        t_rev = get_sim_time("ns")
        cocotb.start_soon(sc.later(4000.0, setattr, dump, "value", 1))
        dut._log.info("waveform: review window %.1f to %.1f ns, first bytes %s" % (
            t_rev, t_rev + 4000.0, ["%02X" % b for b in sent[:6]]))
    for err, d_in, data in plan:
        bt = 4 * sc.T_NS * (1 + err)
        sch = []
        t = 100.0 + d_in + rng.random() * sc.T_NS
        for b in data:
            for k, lv in enumerate([0] + [(b >> i) & 1 for i in range(8)] + [1]):
                sch.append((t + k * bt, lv))
            t += 10 * bt
        base = get_sim_time("ns")
        for tt, lv in sch:
            dt = base + tt - get_sim_time("ns")
            if dt > 0:
                await Timer(max(1, round(dt * 1000)), unit="ps")
            dut.ui_in.value = ui | lv
        await Timer(round(600 * 1000), unit="ps")                    # idle between the runs
    await host.stop()
    got_words = await host.read_mem(768, len(sent) + 8)
    n = next((i for i, w in enumerate(got_words) if w == 0xFFFF), len(got_words))
    rx = [((w >> 7) & 0xFF, (w >> 6) & 1, (w >> 15) & 1) for w in got_words[:n]]
    good = sum(1 for (b, s, p), d in zip(rx, sent) if b == d and s == 0 and p == 1)
    hc_bytes, hc_bad = hc.uart_rx_bytes(got_words)                  # the host call reads the same words
    assert hc_bytes == [b for b, _, _ in rx] and hc_bad == [i for i, (_, s, p) in enumerate(rx) if s or not p], \
        "host call uart_rx_bytes differs from the bench"
    dut._log.info("chip UART RX on the USB helper, 15 Mbaud: %d words stored for %d bytes sent (sender -2.5 %% and "
                  "+2.5 %%, input %.2f and %.2f ns); %d equal with start 0 and stop 1; first words %s, first bytes %s" % (
                      n, len(sent), sc.IN_PAD[0], sc.IN_PAD[1] + sc.MUX_MAX, good,
                      ["%04x" % w for w in got_words[:6]], ["%02x" % b for b in sent[:6]]))
    assert n == len(sent), "%d words stored, %d bytes sent" % (n, len(sent))
    assert good == len(sent), "first bad byte %s" % next(
        (i, rx[i], sent[i]) for i in range(n) if rx[i] != (sent[i], 0, 1))


# ----- JTAG and SWD on the USB helper at chip level (docs/helpers.md section 6) -----

async def chip_start_owned(dut, prog, data, pins, owned, t1, t2=None):
    """Chip start as test_slu_chip_spi_flash: program up to the end, data words, threads 0 and 1 (and 2 at t2) run,
    and the pins in `owned` belong to thread 0 (driven with OUT events); the other pins as given."""
    cocotb.start_soon(Clock(dut.clk, CHIP_CLK_PS, unit="ps").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    host = hs.SpiTimed(dut)
    await host.write_mem(0, prog.image()[:max(prog.words) + 1])
    for addr, words in data:
        await host.write_mem(addr, words)
    owner = [None] * 15
    for p in owned:
        owner[p] = 0
    head, own = hs.config_words((0, t1, t2 or 0, 0), (True, True, t2 is not None, False), owner, [False] * 15)
    pins = list(pins)
    for p in owned:
        pins[p] = own[p]
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    return host


class ChipPinWatch:
    """Calls the device callbacks at their pin times: on[i](level) for uio pin i after delay d_out (pad and
    multiplexer); a pin whose output enable is 0 is passed as None (released)."""

    def __init__(self, dut, on, d_out):
        self.dut, self.on, self.d_out = dut, on, d_out
        self.last = {}
        self.task = cocotb.start_soon(self.run())

    async def run(self):
        from cocotb.triggers import Edge
        import test_slu_clk as sc
        while True:
            await Edge(self.dut.uio_out)
            out, oe = str(self.dut.uio_out.value), str(self.dut.uio_oe.value)
            for i, fn in self.on.items():
                o, e = out[len(out) - 1 - i], oe[len(oe) - 1 - i]
                if e != "1":
                    v = None
                elif o in "01":
                    v = int(o)
                else:
                    continue
                if self.last.get(i, "x") != v:
                    self.last[i] = v
                    cocotb.start_soon(sc.later(self.d_out, fn, v))

    def stop(self):
        self.task.cancel()


JTAG_HELPER_FW = helper_fw("jtag_idcode.s")


@cocotb.test()
async def test_slu_chip_jtag_idcode(dut):
    """USB helper in the chip as a JTAG controller (docs/helpers.md section 6): TCK = helper clock on uio0, TDI = helper
    data on uio1, TMS from thread 0 on uio2 (OUT events at the bit starts, times from TXTIME), TDO on ui_in[0]. Frame A
    (9 bits, 7.5 MHz) moves two TAPs from Test-Logic-Reset to Shift-DR; frame B (64 bits, 15 MHz TCK) shifts out both
    IDCODEs; thread 1 stores the received words. Device model: the two-TAP chain of test_flex.py (TI SSYA002C state
    machine) at its pins with TDO 7 ns after the falling edge, set-up 1 ns, hold 2 ns; pin path at the slow pads with
    10 ns of board multiplexer each way, then at the fast pads with none. Logic simulation with the pin path as delays."""
    import test_slu_clk as sc
    import test_flex as fx
    prog = pe_asm.assemble(JTAG_HELPER_FW, "jtag_helper")
    want = fx.value_bits(fx.JTAG_IDS[1], 32) + fx.value_bits(fx.JTAG_IDS[0], 32)
    lines = []
    for corner in ("slow, mux 10 ns", "fast, no mux"):
        d = sc.CORNERS[corner]
        pins = [0] * 15
        pins[0], pins[1] = helper_pin(0, 1), helper_pin(0, 0)          # uio0 TCK (TXB), uio1 TDI (TXA)
        host = await chip_start_owned(dut, prog, [(500, [768])], pins, [2], 120)
        devs = fx.jtag_devices(td=sc.JTAG_TD, tsu=sc.JTAG_TSU, th=sc.JTAG_TH, tlow=sc.JTAG_TCKMIN, thigh=sc.JTAG_TCKMIN)

        class Tdo:
            def set_pin(self, pin, level):
                dut.ui_in.value = (1 << hs.CS_N_BIT) | (level & 1)
        chain = sc.JtagAtPins(Tdo(), devs, d[2])
        tms_changes = []

        def tms(v):
            tms_changes.append(get_sim_time("ns"))
            chain.on_tms(1 if v is None else v)                       # released: board pull-up on TMS
        watch = ChipPinWatch(dut, {0: lambda v: chain.on_tck(v or 0), 1: lambda v: chain.on_tdi(1 if v is None else v),
                                   2: tms}, d[0])
        dump = getattr(dut, "dump_off", None)
        if dump is not None:
            dump.value = 0 if corner.startswith("slow") else 1        # waveform: the slow-corner session
        await host.start()
        # the host port's START transaction drives all of ui_in; give TDO back its level (the pull-up or the device)
        dut.ui_in.value = (1 << hs.CS_N_BIT) | (1 if chain.out is None else chain.out)
        await ClockCycles(dut.clk, 3000)
        if dump is not None:
            dump.value = 1
        watch.stop()                                                  # the session is over: thread 0 has halted
        words_out, _ = await host.pull(0)
        release = await stop_release(dut, host, (0, 1, 2))
        end = (await host.read_mem(500, 1))[0]
        got_words = await host.read_mem(768, end - 768)
        got = sc.bits_of(got_words[1:5], 64)                          # word 0: the TDO samples of frame A
        assert hc.jtag_idcode_values(got_words, 2) == [fx.lsb_word(got[:32]), fx.lsb_word(got[32:])], \
            "host call jtag_idcode_values differs from the bench"
        dev_errs = [e for dv in devs for e in dv.errors]
        gaps = [b - a for a, b in zip(tms_changes, tms_changes[1:])]
        lines.append("%s: thread 0 status %s, %d words stored, IDCODEs %08x %08x (want %08x %08x), device timing "
                     "errors %d, smallest TMS change spacing %.1f ns" % (
                         corner, ["%04x" % w for w in words_out], end - 768, fx.lsb_word(got[:32]),
                         fx.lsb_word(got[32:]), fx.JTAG_IDS[1], fx.JTAG_IDS[0], len(dev_errs),
                         min(gaps) if gaps else 0.0))
        assert len(words_out) == 1 and not words_out[0] & 0x204, "thread 0 status %s" % words_out
        assert end - 768 == 5, "%d words stored, want 5 (1 + 4)" % (end - 768)
        assert got == want, "IDCODE scan %08x %08x" % (fx.lsb_word(got[:32]), fx.lsb_word(got[32:]))
        assert not dev_errs, "device timing at its pins: %s" % dev_errs[:3]
    for line in lines:
        dut._log.info("chip JTAG on the USB helper, " + line)


SWD_HELPER_FW = """
        .equ    E, 16           ; USB helper ports 16-31
        .equ    SWD, 3          ; uio3: SWDIO, a thread-0 pin; the helper reads it back as input 3
t0:     TNOW    8
        OUT     SWD, 1
        LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 0x00B
        IOW     r0, E+13        ; MODE: clocked code, RX on
        LDI     r0, 16
        IOW     r0, E+13        ; TXPER 16: 32 cycles a bit (1.875 MHz) for the connection sequence
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0x23F
        SHL     r0, 6
        ORI     r0, 0x33
        IOW     r0, E+13        ; SEL 0x8FF3: DIN = input 3 (uio3, SWDIO), sample delay 8
        LDI     r0, 18
        IOW     r0, E+12
        LDI     r0, 0
        IOW     r0, E+13        ; LTC 0: SWCLK idle low
        LDI     r2, 512         ; connection frame: bit count N at 512, then the SWDIO words (first bit in bit 0)
        LDX     r3, [r2]
        MOV     r0, r3
        ANDI    r0, 15
        IOW     r0, E+4         ; TXCTRL: bits in the last word; thread 2 feeds the (unused) helper data words
        ADDI    r3, 15
        SHR     r3, 4           ; words of SWDIO bits
        ADDI    r2, 1
        TNOW    120
        TGET    r1
        IOW     r1, E+5         ; TXTIME = T
cw:     LDX     r0, [r2]        ; next 16 SWDIO bits
        ADDI    r2, 1
        LDI     r1, 16
cb:     OUT     SWD, r0>>       ; bit k at T + 32 k (the bit start, the falling SWCLK edge); the bits after N are 0
        TADD    32
        DJNZ    r1, cb
        DJNZ    r3, cw
wc:     IOR     r0, E+4
        ANDI    r0, 15
        BNE     wc
        LDI     r0, 1
        IOW     r0, E+12
        LDI     r0, 4
        IOW     r0, E+13        ; TXPER 4: 8 cycles a bit, 7.5 MHz SWCLK for the transfer
        LDI     r0, 14
        IOW     r0, E+4         ; TXCTRL: 46 bits = 2 words + 14 (request 8, turnaround, ACK 3, data 32, parity,
        LDI     r2, 600         ; turnaround)
        LDX     r0, [r2]        ; the 8 request bits (first bit in bit 0)
        TNOW    100
        TGET    r1
        IOW     r1, E+5         ; TXTIME = T; the request bits one per 8 cycles (unrolled: 2 slots a bit)
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, r0>>
        TADD    8
        OUT     SWD, Z          ; bit 8: turnaround, then the target drives ACK, data and parity
wt:     IOR     r0, E+4
        ANDI    r0, 15
        BNE     wt
        IOR     r1, E+2
        PUSH    r1
        HALT
        .org    150
t1:     LDI     r1, 768         ; thread 1: received words to the data memory from 768
rx:     IOR     r0, E+6
        ANDI    r0, 0x60
        BEQ     rx
        IOR     r0, E+1
        STX     r0, [r1]
        ADDI    r1, 1
        ST      r1, 500
        JMP     rx
        .org    170
t2:     LDI     r2, 508         ; thread 2: the helper data words (0) of both frames; [508] = words of frame 1
        LDX     r3, [r2]
f1:     IOR     r0, E+0
        CMPI    r0, 0
        BEQ     f1
        LDI     r0, 0
        CMPI    r3, 1
        BEQ     f1l
        IOW     r0, E+0
        ADDI    r3, -1
        JMP     f1
f1l:    IOW     r0, E+14        ; TXLAST of the connection frame
        LDI     r3, 3           ; frame 2: 46 bits = 3 words
f2:     IOR     r0, E+0
        CMPI    r0, 0
        BEQ     f2
        LDI     r0, 0
        CMPI    r3, 1
        BEQ     f2l
        IOW     r0, E+0
        ADDI    r3, -1
        JMP     f2
f2l:    IOW     r0, E+14
        HALT
"""


@cocotb.test()
async def test_slu_chip_swd_dpidr(dut):
    """USB helper in the chip as an SWD host (docs/helpers.md section 6): SWCLK = helper clock on uio0, SWDIO on uio3
    from thread 0 (OUT events at the bit starts; released for the turnaround, ACK, data and parity) and read back by
    the helper as input 3. Connection (JTAG-to-SWD sequence and line resets of test_flex.py) at 3.75 MHz, then a DPIDR
    read at 7.5 MHz in one frame: request, turnaround, ACK, 32 data bits, parity; thread 1 stores the samples. Target:
    the SW-DP model of test_flex.py (Arm IHI0031H) with output 25 ns after the rising edge, set-up and hold 2 ns, at the
    slow pads with 10 ns of board multiplexer each way, and output 2 ns at the fast pads. Logic simulation."""
    import test_slu_clk as sc
    import test_flex as fx
    prog = pe_asm.assemble(SWD_HELPER_FW, "swd_helper")
    conn = list(fx.SWD_CONNECT)
    cwords = sc.words_of(conn)
    req = fx.swd_request(0, 1, 0x0)                                   # DP read, address 0: DPIDR
    lines = []
    for corner, td in (("slow, mux 10 ns", 25.0), ("fast, no mux", 2.0)):
        d = sc.CORNERS[corner]
        pins = [0] * 15
        pins[0] = helper_pin(0, 1)                                    # uio0 SWCLK (helper TXB)
        host = await chip_start_owned(dut, prog, [(500, [768]), (508, [len(cwords)]), (512, [len(conn)] + cwords),
                                                  (600, [req])], pins, [3], 150, 170)
        tgt = fx.SwdTarget(td=td, tsu=2.0, th=2.0, tlow=20.0, thigh=20.0)

        class Line:
            def set_pin(self, pin, level):
                dut.uio_in.value = (level & 1) << 3
        swd = sc.SwdAtPins(Line(), tgt, d[2])
        watch = ChipPinWatch(dut, {0: lambda v: swd.on_clk(v or 0),
                                   3: lambda v: swd.host((0, 1) if v is None else (1, v))}, d[0])
        dump = getattr(dut, "dump_off", None)
        if dump is not None:
            dump.value = 0 if corner.startswith("slow") else 1
        await host.start()
        await ClockCycles(dut.clk, 32 * (16 * len(cwords)) + 2000)
        if dump is not None:
            dump.value = 1
        words_out, _ = await host.pull(0)
        await host.stop()
        watch.stop()
        end = (await host.read_mem(500, 1))[0]
        got_words = await host.read_mem(768, end - 768)
        nc = (len(conn) + 15) // 16
        xfer = sc.bits_of(got_words[nc:nc + 3], 46)
        # the sample of bit k reads the line the target drives after the rising edge of bit k (as test_slc_swd):
        # ACK in samples 8-10, data 11-42, parity 43
        ack = xfer[8] | xfer[9] << 1 | xfer[10] << 2
        val = fx.lsb_word(xfer[11:43])
        par = xfer[43]
        lines.append("%s, target output %.0f ns: thread 0 status %s, %d words stored, ACK %s, DPIDR %08x (target "
                     "%08x), parity %s, target timing errors %d" % (
                         corner, td, ["%04x" % w for w in words_out], end - 768, bin(ack), val, tgt.dpidr,
                         "ok" if fx.parity(val) == par else "BAD", len(tgt.errors)))
        assert len(words_out) == 1 and not words_out[0] & 0x204, "thread 0 status %s" % words_out
        assert ack == 0b001, "ACK %s (bits %s)" % (bin(ack), xfer[:16])
        assert val == tgt.dpidr and fx.parity(val) == par, "DPIDR %08x parity %d" % (val, par)
        assert not tgt.errors, "target timing at its pins: %s" % tgt.errors[:3]
    for line in lines:
        dut._log.info("chip SWD on the USB helper, " + line)

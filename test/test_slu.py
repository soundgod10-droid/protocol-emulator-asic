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

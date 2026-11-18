"""Clocked and NRZ modes of the USB helper (pe_slu SPEC 1, the form in the chip; unit-test instance tb.u_slc_ut).

What it does:
  The test plays the thread: it writes the configuration words that SPEC 1 accepts (MODE bits 0, 1, 3, 4, 9,
  TXPER[7:0], LTC[12:8], SEL), pushes and pops words through the ports and starts frames at TXTIME. The
  device on the pins is a model written from its data sheet or standard, not from the RTL. Between the unit and
  the device the test adds the chip's pin path: one pin flip-flop after tx_v (pe_proc.v), then an output pad
  and the shuttle's multiplexer; on the way back the multiplexer and an input pad. The pad delays are the pad
  SPICE results of docs/verification.md section 9 (output 1.13 to 3.32 ns, input 0.26 to 0.45 ns); the
  multiplexer delay is a parameter (0 to 10 ns each way), because it is not measured on this process.

Why:
  The clocked and NRZ code of the helper was never run by a test before (A1). These tests give the rates the
  clocked mode reaches with a stated pin path, and controls that must fail, so a pass is not a weak check.
  Logic simulation with delay parameters; the pads and the multiplexer are not simulated electrically.
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, FallingEdge, ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time

GL = os.environ.get("GATES") == "yes"
T_PS = 16666                        # 60 MHz (even ps period, as test_slu.py)
T_NS = T_PS / 1000.0
P_TXDATA, P_RXDATA, P_STATUS, P_TXCTRL, P_TXTIME, P_RXINFO = 0, 1, 2, 4, 5, 6
P_CFGADR, P_CFGDAT, P_TXLAST = 12, 13, 14
CFG_MODE, CFG_TXPER, CFG_SEL, CFG_LTC = 0, 1, 8, 18
C_NRZ, C_CLK = 0, 3
DIN = 0                             # unit pin number of the data input in these tests
OUT_PAD = (1.13, 3.32)              # ns, docs/verification.md section 9 (pad SPICE, fast and slow)
IN_PAD = (0.26, 0.45)
MUX_MAX = 10.0                      # ns each way (parameter)


def now_ns():
    return get_sim_time("ns")


async def later(ns, fn, *args):
    await Timer(max(1, round(ns * 1000)), unit="ps")
    fn(*args)


class Unit:
    """Port access to tb.u_slc_ut, one access per two falling edges (as test_slu.Slu)."""

    def __init__(self, dut):
        self.d = dut
        self.pins = 0

    async def wr(self, addr, data):
        d = self.d
        await FallingEdge(d.clk)
        d.slc_addr.value = addr
        d.slc_wdata.value = data & 0xFFFF
        d.slc_we.value = 1
        await FallingEdge(d.clk)
        d.slc_we.value = 0

    async def rd(self, addr, pop=False):
        d = self.d
        await FallingEdge(d.clk)
        d.slc_addr.value = addr
        d.slc_re.value = int(pop)
        await Timer(1, unit="ns")
        v = int(d.slc_rdata.value)
        await FallingEdge(d.clk)
        d.slc_re.value = 0
        return v

    async def cfg(self, idx, value):
        await self.wr(P_CFGADR, idx)
        await self.wr(P_CFGDAT, value)

    async def now(self):
        await ReadOnly()
        return int(self.d.slu_now.value)

    def set_pin(self, pin, level):
        self.pins = (self.pins & ~(1 << pin)) | ((level & 1) << pin)
        self.d.slc_pins.value = self.pins


async def unit_reset(dut):
    cocotb.start_soon(Clock(dut.clk, T_PS, unit="ps").start())
    dut.slc_rst_n.value = 0
    dut.slc_we.value = 0
    dut.slc_re.value = 0
    dut.slc_pins.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.slc_rst_n.value = 1
    await RisingEdge(dut.clk)
    return Unit(dut)


async def clocked_config(u, txper, cpol=0, lead=0, estart=0, fall=0, delay=0, code=C_CLK, rx=1):
    """The four configuration words that the USB helper accepts for the clocked and NRZ modes."""
    await u.cfg(CFG_MODE, code | (rx << 3))                       # code, RX on; stuffing 0, USB line 0
    await u.cfg(CFG_TXPER, txper)
    await u.cfg(CFG_LTC, (cpol << 8) | (lead << 9) | (estart << 10) | (fall << 12))
    await u.cfg(CFG_SEL, DIN | (0xF << 4) | (0xF << 8) | ((delay & 0xF) << 12))


def words_of(bits):
    return [sum(b << i for i, b in enumerate(bits[k:k + 16])) for k in range(0, len(bits), 16)]


def bits_of(words, n):
    out = []
    for w in words:
        out += [(w >> i) & 1 for i in range(16)]
    return out[:n]


async def clocked_frame(u, bits_out, lead=40, before_start=None, after_end=None):
    """One clocked frame of len(bits_out) bits (first bit first on the line), started at TXTIME = now + lead.
    Returns (received bits, status at the end, TXTIME). before_start/after_end: callbacks of the bench (CS)."""
    n = len(bits_out)
    words = words_of(bits_out)
    # A TXTIME write starts a frame only in the idle state; after a frame the unit keeps tail 1 for
    # c_tail[7:0] = 84 cycles (the fixed USB value of SPEC 1), so wait for the idle state first.
    for _ in range(300):
        if (await u.rd(P_TXCTRL)) & 0xF == 0:
            break
    else:
        raise AssertionError("transmitter not idle 600 cycles after the last frame")
    await u.wr(P_TXCTRL, 1 << 10)                                 # empty the TX buffer
    while (await u.rd(P_RXINFO) >> 5) & 3:
        await u.rd(P_RXDATA, pop=True)
    await u.wr(P_STATUS, 0xFFFF)
    await u.wr(P_TXCTRL, n % 16)                                  # bits in the last word (0 = 16)
    feed = list(words)
    while feed and len(words) - len(feed) < 2:
        w = feed.pop(0)
        await u.wr(P_TXDATA if feed else P_TXLAST, w)
    if before_start:
        before_start()
    t0 = (await u.now() + lead) & 0xFFFF
    await u.wr(P_TXTIME, t0)
    rx = []
    st = 0
    while True:
        if feed and await u.rd(P_TXDATA):
            w = feed.pop(0)
            await u.wr(P_TXDATA if feed else P_TXLAST, w)
        info = await u.rd(P_RXINFO)
        if (info >> 5) & 3:
            rx.append(await u.rd(P_RXDATA, pop=True))
            continue
        st = await u.rd(P_STATUS)
        if (st >> 5) & 1 and not feed:                            # RXEOP: the frame and its last sample are done
            while (await u.rd(P_RXINFO) >> 5) & 3:
                rx.append(await u.rd(P_RXDATA, pop=True))
            break
    if after_end:
        after_end()
    return bits_of(rx, n), st, t0


class PinPath:
    """The chip's pin path for the helper outputs tx_v[1] (clock) and tx_v[0] (data): the pin flip-flop
    (one cycle after tx_v), then out pad + multiplexer; calls the device at its pin time."""

    def __init__(self, dut, on_clk, on_data, d_clk, d_data):
        self.dut, self.on_clk, self.on_data = dut, on_clk, on_data
        self.d_clk, self.d_data = d_clk, d_data
        self.task = cocotb.start_soon(self.run())

    async def run(self):
        last = int(self.dut.slc_txv.value) & 3
        while True:
            await Edge(self.dut.slc_txv)
            v = int(self.dut.slc_txv.value) & 3
            ch, last = v ^ last, v
            if ch & 2 and self.on_clk:
                cocotb.start_soon(later(T_NS + self.d_clk, self.on_clk, (v >> 1) & 1))
            if ch & 1 and self.on_data:
                cocotb.start_soon(later(T_NS + self.d_data, self.on_data, v & 1))

    def stop(self):
        self.task.cancel()


# ----- T1: SPI controller against a serial NOR flash -----

# W25Q128JV data sheet, Rev. F (Winbond, 2018-03-27), section 9.6 AC Electrical Characteristics, page 64
# (PDF page 65): fR (Read Data 03h) <= 50 MHz; clock high and low time >= 45 % of the period; tSLCH 3 ns;
# tCHSH 3 ns; tSHSL1 10 ns; tDVCH (data in set-up) 1 ns; tCHDX (data in hold) 2 ns; tCLQV (clock low to
# output valid) <= 6 ns; tCLQX (output hold) >= 1.5 ns. The flash works in SPI modes 0 and 3; the model takes
# all four modes with the same times (as a generic SPI target), so modes 1 and 2 are checked as well.
F_R_MHZ, T_CLH_FRAC, T_SLCH, T_CHSH, T_DVCH, T_CHDX, T_CLQV, T_CLQX = 50.0, 0.45, 3.0, 3.0, 1.0, 2.0, 6.0, 1.5
JEDEC_ID = (0xEF, 0x40, 0x18)


class SpiFlash:
    """Serial flash at its pins: /CS from the bench, CLK and DI from the chip's pin path, DO back through the
    input path. Commands 9Fh (JEDEC ID) and 03h (read, 24-bit address), MSB first. In the window from tCLQX to
    tCLQV after the shift edge DO carries the inverse of the new bit (data not valid), then the new bit."""

    def __init__(self, unit, cpol, cpha, mem, d_in):
        self.u, self.cpol, self.cpha, self.mem, self.d_in = unit, cpol, cpha, mem, d_in
        self.cs, self.clk, self.di, self.t_di = 1, cpol, 0, -1e9
        self.bits, self.errors, self.edges = [], [], []
        self.t_cs_low = self.t_hold = self.t_last = None

    def err(self, text):
        if len(self.errors) < 20:
            self.errors.append("%.3f ns: %s" % (now_ns(), text))

    def set_cs(self, v):
        t = now_ns()
        if v == 0:
            self.bits, self.edges, self.t_cs_low, self.t_last = [], [], t, None
        elif self.t_last is not None and t - self.t_last < T_CHSH:
            self.err("tCHSH %.2f ns" % (t - self.t_last))
        self.cs = v

    def on_di(self, v):
        t = now_ns()
        if self.t_hold is not None and t < self.t_hold and v != self.di:
            self.err("tCHDX: DI changed %.2f ns after the sampling edge" % (t - (self.t_hold - T_CHDX)))
        self.di, self.t_di = v, t

    def on_clk(self, v):
        t = now_ns()
        if v == self.clk:
            return
        leading = self.clk == self.cpol
        self.clk = v
        if self.cs:
            return
        if not self.edges and t - self.t_cs_low < T_SLCH:
            self.err("tSLCH %.2f ns" % (t - self.t_cs_low))
        self.edges.append(t)
        self.t_last = t
        if leading == (self.cpha == 0):                          # sampling edge
            if t - self.t_di < T_DVCH:
                self.err("tDVCH %.2f ns" % (t - self.t_di))
            self.t_hold = t + T_CHDX
            self.bits.append(self.di)
        else:                                                    # shift edge: next DO bit
            b = self.out_bit(len(self.bits))
            cocotb.start_soon(later(T_CLQX + self.d_in, self.u.set_pin, DIN, 1 - b))
            cocotb.start_soon(later(T_CLQV + self.d_in, self.u.set_pin, DIN, b))

    def out_bit(self, i):
        b = self.bits
        if len(b) < 8:
            return 0
        cmd = int("".join(map(str, b[:8])), 2)
        if cmd == 0x9F:
            start, data = 8, JEDEC_ID
            k = (i - start) // 8
            return (data[k] >> (7 - (i - start) % 8)) & 1 if start <= i < start + 24 else 0
        if cmd == 0x03 and len(b) >= 32:
            addr = int("".join(map(str, b[8:32])), 2)
            k = (i - 32) // 8
            return (self.mem[(addr + k) % len(self.mem)] >> (7 - (i - 32) % 8)) & 1 if i >= 32 else 0
        return 0

    def duty_check(self, period_ns):
        """Clock high and low times at the device pin against 45 % of the period (tCRLH, tCRLL)."""
        e = self.edges
        if len(e) < 3:
            return
        worst = min(b - a for a, b in zip(e, e[1:]))
        if worst < T_CLH_FRAC * period_ns:
            self.err("clock high or low %.2f ns < 45 %% of %.2f ns" % (worst, period_ns))


def bytes_to_bits(bs):
    return [(b >> (7 - i)) & 1 for b in bs for i in range(8)]


def bits_to_bytes(bits):
    return [int("".join(map(str, bits[k:k + 8])), 2) for k in range(0, len(bits) - 7, 8)]


async def spi_transfer(dut, u, mode, txper, delay, fall, d_out, d_in, cmd_bytes, n_read, mem):
    """One /CS transaction: cmd_bytes, then n_read bytes read. Returns (read bytes, device errors)."""
    cpol, cpha = mode >> 1, mode & 1
    await clocked_config(u, txper, cpol=cpol, lead=cpha, fall=fall, delay=delay)
    dev = SpiFlash(u, cpol, cpha, mem, d_in)
    path = PinPath(dut, dev.on_clk, dev.on_di, d_out[0], d_out[1])
    bits = bytes_to_bits(cmd_bytes) + [0] * (8 * n_read)
    got, st, _ = await clocked_frame(u, bits, lead=40, before_start=lambda: dev.set_cs(0))
    await Timer(round((T_NS * 2 + max(d_out) + T_CHSH + 1) * 1000), unit="ps")
    dev.set_cs(1)
    dev.duty_check(2 * txper * T_NS)
    await Timer(round(20 * 1000), unit="ps")                      # tSHSL1 10 ns before the next transaction
    path.stop()
    return bits_to_bytes(got[8 * len(cmd_bytes):]), dev.errors


CORNERS = {
    # name: (clock out delay, data out delay, input delay) in ns: pad + multiplexer each way
    "fast, no mux": (OUT_PAD[0], OUT_PAD[0], IN_PAD[0]),
    "slow, mux 10 ns": (OUT_PAD[1] + MUX_MAX, OUT_PAD[1] + MUX_MAX, IN_PAD[1] + MUX_MAX),
    "skew: clock slow, data fast": (OUT_PAD[1] + MUX_MAX, OUT_PAD[0], IN_PAD[0]),
    "skew: clock fast, data slow": (OUT_PAD[0], OUT_PAD[1] + MUX_MAX, IN_PAD[1] + MUX_MAX),
}


async def id_ok(dut, u, mode, txper, delay, fall, corner):
    d = CORNERS[corner]
    got, errs = await spi_transfer(dut, u, mode, txper, delay, fall, d[:2], d[2], [0x9F], 3, [0] * 16)
    return tuple(got) == JEDEC_ID and not errs, got, errs


@cocotb.test(skip=GL)
async def test_slc_spi_flash(dut):
    """T1: SPI controller on the USB helper's clocked mode, against the flash model, at TXPER 2 (4 cycles a bit,
    15 MHz) and TXPER 4 (8 cycles, 7.5 MHz) in modes 0 to 3, and at TXPER 1 (30 MHz) in mode 0: the read-back
    settings (sample delay 0 to 15 cycles, rising or falling-edge copy of DIN) that read the JEDEC ID at every
    pin-path corner; then a 256-byte 03h read at the rated settings. Control: at TXPER 1 with sample delay 0
    and the 10 ns multiplexer the ID must be read wrong."""
    u = await unit_reset(dut)
    log = dut._log
    rng = random.Random(1118)
    mem = [rng.randrange(256) for _ in range(512)]
    summary = []
    rated = {}
    for txper, modes in ((2, (0, 1, 2, 3)), (4, (0, 1, 2, 3)), (1, (0,))):
        for mode in modes:
            window = []
            for fall in (0, 1):
                for delay in range(16):
                    if all([(await id_ok(dut, u, mode, txper, delay, fall, c))[0] for c in CORNERS]):
                        window.append((delay, fall))
            summary.append("TXPER %d (%.1f MHz) mode %d: settings that read the ID at all %d corners: %s" % (
                txper, 60.0 / (2 * txper), mode, len(CORNERS), window or "none"))
            if window:
                rated[(txper, mode)] = window[len(window) // 2]
    for line in summary:
        log.info(line)
    for txper in (2, 4):
        for mode in range(4):
            assert (txper, mode) in rated, "TXPER %d mode %d: no read-back setting works at all corners" % (txper, mode)
    # 256-byte reads at the middle of each window, at the two extreme corners
    for (txper, mode), (delay, fall) in sorted(rated.items()):
        for corner in ("fast, no mux", "slow, mux 10 ns"):
            d = CORNERS[corner]
            addr = rng.randrange(256)
            got, errs = await spi_transfer(dut, u, mode, txper, delay, fall, d[:2], d[2],
                                           [0x03, 0, 0, addr], 256, mem)
            want = [mem[(addr + k) % len(mem)] for k in range(256)]
            assert got == want and not errs, "03h read, TXPER %d mode %d delay %d fall %d %s: %d bytes wrong, %s" % (
                txper, mode, delay, fall, corner, sum(a != b for a, b in zip(got, want)), errs[:3])
            log.info("03h read of 256 bytes at address %d: TXPER %d (%.1f MHz) mode %d, sample delay %d, %s edge "
                     "copy, %s: equal to the flash, 0 timing errors at the device pins" % (
                         addr, txper, 60.0 / (2 * txper), mode, delay, "falling" if fall else "rising", corner))
    # control: TXPER 1, sample delay 0, 10 ns multiplexer: the ID must be read wrong
    ok, got, errs = await id_ok(dut, u, 0, 1, 0, 0, "slow, mux 10 ns")
    log.info("control TXPER 1, delay 0, slow pads + 10 ns mux: ID %s (must not be %s)" % (
        ["%02X" % b for b in got], ["%02X" % b for b in JEDEC_ID]))
    assert not ok, "control: TXPER 1 with sample delay 0 and a 10 ns multiplexer read the ID correctly"

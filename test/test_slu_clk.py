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


def dump_on(dut, on):
    """Waveform on or off (test/tb.v dump_off; the coverage build has none): the tests dump only review frames."""
    d = getattr(dut, "dump_off", None)
    if d is not None:
        d.value = 0 if on else 1


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
    budget = 4 * n * 256 // 16 + 2000                             # port accesses; a frame bit is at most 512 cycles
    while True:
        budget -= 1
        assert budget > 0, "clocked frame of %d bits: no RXEOP (status %04x, %d words received)" % (n, st, len(rx))
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


async def spi_flash_run(dut, plan):
    """T1: SPI controller on the USB helper's clocked mode, against the flash model, at TXPER 2 (4 cycles a bit,
    15 MHz) and TXPER 4 (8 cycles, 7.5 MHz) in modes 0 to 3, and at TXPER 1 (30 MHz) in mode 0: the read-back
    settings (sample delay 0 to 15 cycles, rising or falling-edge copy of DIN) that read the JEDEC ID at every
    pin-path corner; then a 256-byte 03h read at the rated settings. Control: at TXPER 1 with sample delay 0
    and the 10 ns multiplexer the ID must be read wrong."""
    u = await unit_reset(dut)
    dump = getattr(dut, "dump_off", None)     # waveform only for the review frames below (test/tb.v dump_off;
    if dump is not None:                      # not in the coverage build)
        dump.value = 1
    log = dut._log
    rng = random.Random(1118)
    mem = [rng.randrange(256) for _ in range(512)]
    summary = []
    rated = {}
    for txper, modes in plan:
        for mode in modes:
            window = []
            for fall in (0, 1):
                for delay in range(12):
                    good = True
                    for c in CORNERS:
                        if not (await id_ok(dut, u, mode, txper, delay, fall, c))[0]:
                            good = False
                            break
                    if good:
                        window.append((delay, fall))
            summary.append("TXPER %d (%.1f MHz) mode %d: settings that read the ID at all %d corners: %s" % (
                txper, 60.0 / (2 * txper), mode, len(CORNERS), window or "none"))
            if window:
                rated[(txper, mode)] = window[len(window) // 2]
    for line in summary:
        log.info(line)
    for txper, modes in plan:
        for mode in modes:
            if txper >= 2:
                assert (txper, mode) in rated, "TXPER %d mode %d: no read-back setting works at all corners" % (
                    txper, mode)
    # review frames for the waveform: one JEDEC ID read in mode 0 at the rated setting of each rate, slow corner
    for (txper, mode), (delay, fall) in sorted(rated.items()):
        if mode == 0:
            if dump is not None:
                dump.value = 0
            ok, got, _ = await id_ok(dut, u, 0, txper, delay, fall, "slow, mux 10 ns")
            if dump is not None:
                dump.value = 1
            log.info("waveform: JEDEC ID read at TXPER %d, sample delay %d, slow pads + 10 ns mux, ends at %.1f ns: %s" % (
                txper, delay, now_ns(), ["%02X" % b for b in got]))
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
    if not any(txper == 1 for txper, _ in plan):
        return
    # control: TXPER 1, sample delay 0, 10 ns multiplexer: the ID must be read wrong
    ok, got, errs = await id_ok(dut, u, 0, 1, 0, 0, "slow, mux 10 ns")
    log.info("control TXPER 1, delay 0, slow pads + 10 ns mux: ID %s (must not be %s)" % (
        ["%02X" % b for b in got], ["%02X" % b for b in JEDEC_ID]))
    assert not ok, "control: TXPER 1 with sample delay 0 and a 10 ns multiplexer read the ID correctly"


# ----- T4: UART transmitter (NRZ code) -----

def uart_bits(data, nbits=8, stop=1):
    """Line bits of back-to-back 8N1 frames (start 0, LSB first, stop 1)."""
    out = []
    for b in data:
        out += [0] + [(b >> i) & 1 for i in range(nbits)] + [1] * stop
    return out


async def uart_tx_frame(dut, u, txper, data):
    """Send data as one NRZ frame (the host packs start and stop bits into the words); record the line (tx_v[0],
    TXA) at every clock edge from the frame start. Returns (line levels per cycle from the first start bit, cycles)."""
    await u.cfg(CFG_MODE, C_NRZ)                                  # NRZ, RX off
    await u.cfg(CFG_TXPER, txper)
    bits = uart_bits(data)
    caps = []
    done = {"stop": False}

    async def cap():
        while not done["stop"]:
            await RisingEdge(dut.clk)
            await ReadOnly()
            caps.append(int(dut.slc_txv.value) & 1)
    task = cocotb.start_soon(cap())
    await clocked_frame_tx(u, bits)
    await ClockCycles(dut.clk, 4 * txper)
    done["stop"] = True
    await task
    first = caps.index(0)
    return caps[first:], len(bits)


async def clocked_frame_tx(u, bits_out, lead=40):
    """Transmit-only frame (RX off): push the words, start at TXTIME, feed until the last word, wait for TXDONE."""
    n = len(bits_out)
    words = words_of(bits_out)
    for _ in range(300):
        if (await u.rd(P_TXCTRL)) & 0xF == 0:
            break
    await u.wr(P_TXCTRL, 1 << 10)
    await u.wr(P_STATUS, 0xFFFF)
    await u.wr(P_TXCTRL, n % 16)
    feed = list(words)
    while feed and len(words) - len(feed) < 2:
        w = feed.pop(0)
        await u.wr(P_TXDATA if feed else P_TXLAST, w)
    await u.wr(P_TXTIME, (await u.now() + lead) & 0xFFFF)
    budget = 4 * n * 256 // 16 + 2000
    while True:
        budget -= 1
        assert budget > 0, "NRZ frame of %d bits did not end" % n
        if feed:
            if await u.rd(P_TXDATA):
                w = feed.pop(0)
                await u.wr(P_TXDATA if feed else P_TXLAST, w)
            continue
        st = await u.rd(P_STATUS)
        if st & 1:                                                # TXDONE
            assert not st & 4, "TXUNDER in an NRZ frame (status %04x)" % st
            return st


def uart_decode(line, b_cycles, nbytes):
    """Independent receiver: find each start edge on the cycle grid, sample every bit at its middle, check the stop
    bit, and check that every edge of the line falls on a multiple of the bit time from the first start edge."""
    out, i = [], 0
    for _ in range(nbytes):
        while i < len(line) and line[i] != 0:
            i += 1
        assert i < len(line), "start bit %d missing" % len(out)
        bits = [line[i + b_cycles * k + b_cycles // 2] for k in range(10)]
        assert bits[0] == 0 and bits[9] == 1, "byte %d: start %d stop %d" % (len(out), bits[0], bits[9])
        out.append(sum(bits[1 + k] << k for k in range(8)))
        i += 10 * b_cycles - b_cycles // 2                        # into the stop bit
    edges = [k for k in range(1, len(line)) if line[k] != line[k - 1]]
    off_grid = [k for k in edges if k % b_cycles]
    return out, len(edges), off_grid


@cocotb.test(skip=GL)
async def test_slc_uart_tx(dut):
    """T4: UART transmitter on the USB helper's NRZ code at TXPER 4 (15 Mbaud) and TXPER 5 (12 Mbaud): 64 back-to-back
    8N1 bytes in one frame (start and stop bits packed by the host into the words), decoded by an independent receiver
    at each bit middle; every edge of the line on the bit grid of the first start edge."""
    u = await unit_reset(dut)
    log = dut._log
    rng = random.Random(4)
    for txper in (4, 5):
        data = [0x55, 0x00, 0xFF, 0x0F] + [rng.randrange(256) for _ in range(60)]
        line, nbits = await uart_tx_frame(dut, u, txper, data)
        got, nedges, off = uart_decode(line, txper, len(data))
        log.info("UART TX TXPER %d (%.1f Mbaud): %d bytes back to back, %d line bits, %d edges, %d off the %d-cycle grid; "
                 "decoded %s" % (txper, 60.0 / txper, len(data), nbits, nedges, len(off), txper,
                                 "equal" if got == data else "DIFFERENT"))
        assert got == data, "TXPER %d: decoded %s" % (txper, got[:8])
        assert not off, "TXPER %d: edges off the bit grid at cycles %s" % (txper, off[:8])


# ----- T3: UART receiver, frames started by the DIN falling edge (LTC[10]) -----

async def uart_send(u, data, bt_ns, phase_ns, d_in, idle_bits=0):
    """Sender at its own bit time bt_ns (rate error included), first start edge phase_ns after the call; the line reaches
    the unit pin d_in ns later. Back-to-back 8N1 frames with idle_bits between them."""
    t = phase_ns + d_in
    sch = []
    for b in data:
        for k, lv in enumerate([0] + [(b >> i) & 1 for i in range(8)] + [1]):
            sch.append((t + k * bt_ns, lv))
        t += (10 + idle_bits) * bt_ns
    t0 = now_ns()
    for tt, lv in sch:
        dt = t0 + tt - now_ns()
        if dt > 0:
            await Timer(max(1, round(dt * 1000)), unit="ps")
        u.set_pin(DIN, lv)
    return t


async def uart_rx_run(dut, u, b_cycles, err, delay, fall, phase_ns, data, d_in, framing_at=None):
    """Receive data with the edge-started frames. Returns the received (byte, start, stop) list."""
    txper = b_cycles // 2
    await clocked_config(u, txper, estart=1, fall=fall, delay=delay)
    await u.wr(P_TXCTRL, 10)                                      # 10 samples a frame: start, 8 data, stop
    while (await u.rd(P_RXINFO) >> 5) & 3:
        await u.rd(P_RXDATA, pop=True)
    await u.wr(P_STATUS, 0xFFFF)
    u.set_pin(DIN, 1)
    await ClockCycles(dut.clk, 8)
    if framing_at is not None:
        data = list(data)
    sender = cocotb.start_soon(uart_send(u, data, b_cycles * T_NS * (1 + err), phase_ns, d_in))
    words = []
    while len(words) < len(data):
        info = await u.rd(P_RXINFO)
        if (info >> 5) & 3:
            words.append(await u.rd(P_RXDATA, pop=True))
        elif sender.done():
            await ClockCycles(dut.clk, 4 * b_cycles)
            if not (await u.rd(P_RXINFO) >> 5) & 3:
                break
    return [((w >> 7) & 0xFF, (w >> 6) & 1, (w >> 15) & 1) for w in words]


def rx_ok(rx, data):
    return len(rx) == len(data) and all(b == d and s == 0 and p == 1 for (b, s, p), d in zip(rx, data))


@cocotb.test(skip=GL)
async def test_slc_uart_rx(dut):
    """T3: UART receiver on the edge-started clocked frames (LTC[10]): 15 Mbaud (4 cycles a bit) and 7.5 Mbaud
    (8 cycles), sender error -2.5 % and +2.5 %, 16 start phases inside a clock period, back-to-back 8N1 bytes, input
    path from no multiplexer to 10 ns; the stop bit (sample 10) gives the framing check. Control: +/-2.8 % at 15 Mbaud
    must lose or corrupt a byte."""
    u = await unit_reset(dut)
    dump_on(dut, False)
    log = dut._log
    rng = random.Random(3)
    d_ins = (IN_PAD[0], IN_PAD[1] + MUX_MAX)
    chosen = {}
    for b_cycles in (4, 8):
        # read-back setting: the first that passes both rate errors at a few phases and both input delays
        for fall in (0, 1):
            for delay in range(4):
                good = True
                for err in (-0.025, 0.025):
                    for ph in (0.0, T_NS / 2):
                        for d_in in d_ins:
                            data = [rng.randrange(256) for _ in range(12)]
                            if not rx_ok(await uart_rx_run(dut, u, b_cycles, err, delay, fall, ph, data, d_in), data):
                                good = False
                                break
                        if not good:
                            break
                    if not good:
                        break
                if good:
                    chosen[b_cycles] = (delay, fall)
                    break
            if b_cycles in chosen:
                break
        assert b_cycles in chosen, "%d cycles a bit: no read-back setting passes +/-2.5 %%" % b_cycles
        delay, fall = chosen[b_cycles]
        # full run: 16 phases x 2 errors x 2 input delays, 16 bytes each, and one run of 256 back-to-back bytes
        nbytes = 0
        for k in range(16):
            for err in (-0.025, 0.025):
                for d_in in d_ins:
                    data = [rng.randrange(256) for _ in range(16)]
                    rx = await uart_rx_run(dut, u, b_cycles, err, delay, fall, k * T_NS / 16, data, d_in)
                    assert rx_ok(rx, data), "%d cycles, err %+.1f %%, phase %d/16, input %.2f ns: %s" % (
                        b_cycles, err * 100, k, d_in, rx[:4])
                    nbytes += len(data)
        data = [rng.randrange(256) for _ in range(256)]
        rx = await uart_rx_run(dut, u, b_cycles, 0.025, delay, fall, 0.3, data, d_ins[1])
        assert rx_ok(rx, data), "256 back-to-back bytes at +2.5 %%: %d received" % len(rx)
        log.info("UART RX %d cycles a bit (%.1f Mbaud): setting delay %d, %s copy; %d bytes at -2.5 %% and +2.5 %%, 16 "
                 "start phases, input %.2f and %.2f ns, and 256 back-to-back bytes at +2.5 %%: all equal, start 0, stop 1" % (
                     b_cycles, 60.0 / b_cycles, delay, "falling" if fall else "rising", nbytes, d_ins[0], d_ins[1]))
        if b_cycles == 4:                                         # review frame for the waveform
            data = [0x55, 0xA3, 0x0F, 0xF0]
            dump_on(dut, True)
            t0 = now_ns()
            rx = await uart_rx_run(dut, u, 4, 0.025, delay, fall, 0.3, data, d_ins[1])
            dump_on(dut, False)
            log.info("waveform: bytes 55 A3 0F F0 at 15 Mbaud +2.5 %%, input %.2f ns, delay %d %s copy, %.1f to %.1f ns: "
                     "received %s" % (d_ins[1], delay, "falling" if fall else "rising", t0, now_ns(),
                                      ["%02X" % b for b, _, _ in rx]))
            assert rx_ok(rx, data), "review frame: %s" % rx
    # framing error: a frame with a 0 stop bit is reported (stop flag 0)
    delay, fall = chosen[4]
    await clocked_config(u, 2, estart=1, fall=fall, delay=delay)
    await u.wr(P_TXCTRL, 10)
    u.set_pin(DIN, 1)
    await ClockCycles(dut.clk, 8)
    t = now_ns() + 20.0
    for k, lv in enumerate([0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 1]):        # 0x55 with stop 0, then idle
        await Timer(max(1, round((t + k * 4 * T_NS - now_ns()) * 1000)), unit="ps")
        u.set_pin(DIN, lv)
    await ClockCycles(dut.clk, 40)
    w = await u.rd(P_RXDATA, pop=True)
    log.info("framing check: word %04x, data %02x, stop bit %d (0 = framing error)" % (w, (w >> 7) & 0xFF, (w >> 15) & 1))
    assert (w >> 15) & 1 == 0 and (w >> 7) & 0xFF == 0x55
    # control: +/-2.8 % at 15 Mbaud over the 16 phases must fail somewhere
    delay, fall = chosen[4]
    failed = 0
    for err in (-0.028, 0.028):
        for k in range(16):
            data = [rng.randrange(256) for _ in range(16)]
            if not rx_ok(await uart_rx_run(dut, u, 4, err, delay, fall, k * T_NS / 16, data, d_ins[1]), data):
                failed += 1
    log.info("control +/-2.8 %% at 15 Mbaud: %d of 32 runs lost or corrupted a byte" % failed)
    assert failed, "control: +/-2.8 % at 15 Mbaud passed every run"


# ----- T2 (JTAG) and T6 (SWD): the device models of test/test_flex.py, run at their pins in ns -----

import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_flex as fx  # noqa: E402  JtagDevice (TI SSYA002C) and SwdTarget (Arm IHI0031H), unchanged


class ThreadPin:
    """A pin that a thread drives with OUT events at the helper's bit starts (the falling clock edges of a
    clocked frame, read from tx_v as a thread knows them from TXTIME): pin flip-flop, out pad and multiplexer.
    One event slot per pin: a change less than 8 cycles after the previous one is an error (docs/isa.md 4.4)."""

    def __init__(self, d_out, apply):
        self.d_out, self.apply = d_out, apply
        self.t_last, self.errors = -1e9, []

    def change(self, v):
        t = now_ns()
        if t - self.t_last < 8 * T_NS - 0.5:
            self.errors.append("pin change %.1f ns after the previous one" % (t - self.t_last))
        self.t_last = t
        cocotb.start_soon(later(T_NS + self.d_out, self.apply, v))


class ClockedHost:
    """Runs one clocked frame and gives the bench the bit starts: plan[k] is applied by the thread pin at the
    start of bit k (bit 0 before the frame, the others at the falling edges of tx_v[1])."""

    def __init__(self, dut, plan, pin, on_clk, d_clk):
        self.dut, self.plan, self.pin, self.on_clk, self.d_clk = dut, plan, pin, on_clk, d_clk
        self.k = 0
        self.task = cocotb.start_soon(self.run())

    async def run(self):
        last = int(self.dut.slc_txv.value) & 2
        while True:
            await Edge(self.dut.slc_txv)
            v = int(self.dut.slc_txv.value) & 2
            if v == last:
                continue
            last = v
            cocotb.start_soon(later(T_NS + self.d_clk, self.on_clk, v >> 1))
            if not v:                                             # falling edge: start of the next bit
                self.k += 1
                if self.k < len(self.plan) and self.plan[self.k] is not None and self.plan[self.k] != self.plan[self.k - 1]:
                    self.pin.change(self.plan[self.k])

    def stop(self):
        self.task.cancel()


class JtagAtPins:
    """The chain of fx.JtagDevice at its pins, times in ns; TDO (or the pull-up) reaches DIN after d_in."""

    def __init__(self, u, devs, d_in):
        self.u, self.devs, self.d_in = u, devs, d_in
        self.tck, self.tms, self.tdi, self.out = 0, 1, 1, None

    def eval(self):
        t = now_ns()
        tdi = self.tdi
        for d in self.devs:
            d.step(t, self.tck, self.tms, tdi)
            tdi = 1 if d.tdo is None else d.tdo
        if tdi != self.out:
            self.out = tdi
            cocotb.start_soon(later(self.d_in, self.u.set_pin, DIN, tdi))
        nxt = [d.pend[0][0] for d in self.devs if d.pend]
        if nxt and min(nxt) > t:
            cocotb.start_soon(later(min(nxt) - t + 0.001, self.eval))

    def on_tck(self, v):
        self.tck = v
        self.eval()

    def on_tdi(self, v):
        self.tdi = v
        self.eval()

    def on_tms(self, v):
        self.tms = v
        self.eval()


JTAG_TD, JTAG_TSU, JTAG_TH, JTAG_TCKMIN = 7.0, 1.0, 2.0, 10.0   # ns, device parameters (the primer gives no times)


async def jtag_frame(dut, u, chain, txper, tms, tdi, delay, fall, corner):
    """One clocked frame with TMS from a thread pin and TDI from the helper; returns the TDO samples."""
    d = CORNERS[corner]
    await clocked_config(u, txper, cpol=0, lead=0, fall=fall, delay=delay)
    tms_pin = ThreadPin(d[1], chain.on_tms)
    path = PinPath(dut, None, chain.on_tdi, d[0], d[1])
    host = ClockedHost(dut, tms, tms_pin, chain.on_tck, d[0])
    chain.on_tms(tms[0])
    got, st, _ = await clocked_frame(u, tdi)
    await ClockCycles(dut.clk, 4)
    host.stop()
    path.stop()
    return got, tms_pin.errors


async def jtag_session(dut, u, shift_txper, delay, fall, corner):
    """IDCODEs, BYPASS in both devices, a 1,000-bit DR scan through Pause-DR. Returns (fault or None, notes)."""
    devs = fx.jtag_devices(td=JTAG_TD, tsu=JTAG_TSU, th=JTAG_TH, tlow=JTAG_TCKMIN, thigh=JTAG_TCKMIN)
    chain = JtagAtPins(u, devs, CORNERS[corner][2])
    errs = []

    async def nav(tms):
        _, e = await jtag_frame(dut, u, chain, 4, tms, [1] * len(tms), delay, fall, corner)
        errs.extend(e)

    async def shift(tdi):
        tms = [0] * (len(tdi) - 1) + [1]
        got, e = await jtag_frame(dut, u, chain, shift_txper, tms, tdi, delay, fall, corner)
        errs.extend(e)
        return got

    await nav([1] * 5 + [0, 1, 0, 0])                            # Test-Logic-Reset, Run-Test/Idle, to Shift-DR
    ids = await shift([1] * 64)
    want = fx.value_bits(fx.JTAG_IDS[1], 32) + fx.value_bits(fx.JTAG_IDS[0], 32)
    if ids != want:
        return "IDCODE scan: %08x %08x" % (fx.lsb_word(ids[:32]), fx.lsb_word(ids[32:64])), None
    await nav([1, 0, 1, 1, 0, 0])                                 # Update-DR, RTI, Select-DR, Select-IR, Capture-IR, Shift-IR
    cap = await shift([1] * 9)                                    # BYPASS (all ones) in both instruction registers
    if cap != fx.value_bits(0b01001, 5) + fx.value_bits(0b1001, 4):
        return "IR capture %s" % cap, None
    await nav([1, 0, 1, 0, 0])                                    # Update-IR, RTI, Select-DR, Capture-DR, Shift-DR
    rng = random.Random(9)
    pat = [rng.randrange(2) for _ in range(1000)]
    a = await shift(pat[:500])
    await nav([0, 0, 1, 0])                                       # Exit1-DR -> Pause-DR (twice) -> Exit2-DR -> Shift-DR
    b = await shift(pat[500:])
    await nav([1, 0])                                             # Update-DR, RTI
    if a + b != [0, 0] + pat[:998]:
        bad = [i for i, (x, y) in enumerate(zip(a + b, [0, 0] + pat[:998])) if x != y]
        return "1,000-bit bypass scan: %d bits wrong, first at %s" % (len(bad), bad[:5]), None
    dev_errs = [e for dv in devs for e in dv.errors]
    if dev_errs or errs:
        return "device timing %s, thread pin %s" % (dev_errs[:3], errs[:3]), None
    return None, "IDCODEs %08x %08x" % (fx.JTAG_IDS[1], fx.JTAG_IDS[0])


@cocotb.test(skip=GL)
async def test_slc_jtag(dut):
    """T2: JTAG on the USB helper's clocked mode: TCK = helper clock, TDI = helper data, TDO = DIN, TMS from a thread
    pin with OUT events at the bit starts (at most one change per 8 cycles: navigation frames at TXPER 4, shift frames
    with TMS only at the last bit). Two TAPs of test_flex.py (TI SSYA002C) with TDO 7 ns after the falling edge,
    set-up 1 ns, hold 2 ns, TCK high and low 10 ns or more: both IDCODEs, BYPASS in both, a 1,000-bit DR scan through
    Pause-DR, at shift rates 15 MHz (TXPER 2) and 30 MHz (TXPER 1), at the extreme pin-path corners."""
    u = await unit_reset(dut)
    dump_on(dut, False)
    log = dut._log
    for shift_txper in (2, 1):
        good = []
        for fall in (0, 1):
            for delay in range(8):
                ok = True
                for corner in ("fast, no mux", "slow, mux 10 ns"):
                    f, _ = await jtag_session(dut, u, shift_txper, delay, fall, corner)
                    if f:
                        ok = False
                        break
                if ok:
                    good.append((delay, fall))
        log.info("JTAG shift at TXPER %d (%.0f MHz TCK), navigation at TXPER 4: read-back settings correct at both "
                 "extreme corners: %s" % (shift_txper, 60.0 / (2 * shift_txper), good or "none"))
        assert good, "JTAG at TXPER %d: no setting works at both corners" % shift_txper
        delay, fall = good[len(good) // 2]
        for corner in CORNERS:
            f, note = await jtag_session(dut, u, shift_txper, delay, fall, corner)
            assert f is None, "JTAG TXPER %d %s: %s" % (shift_txper, corner, f)
        log.info("JTAG TXPER %d, delay %d %s copy: %s, IR BYPASS, 1,000-bit scan through Pause-DR correct at all %d "
                 "corners, 0 device timing errors, TMS changes 8 or more cycles apart" % (
                     shift_txper, delay, "falling" if fall else "rising", note, len(CORNERS)))


class SwdAtPins:
    """fx.SwdTarget at its pins, times in ns: SWCLK from the helper, host SWDIO from a thread pin (OE and value),
    the line (target, host or pull-up) reaches DIN after d_in."""

    def __init__(self, u, tgt, d_in):
        self.u, self.t, self.d_in = u, tgt, d_in
        self.clk, self.oe, self.v, self.out = 0, 0, 1, None

    def eval(self):
        now = now_ns()
        line = self.t.step(now, self.clk, self.oe, self.v)
        if line != self.out:
            self.out = line
            cocotb.start_soon(later(self.d_in, self.u.set_pin, DIN, line))
        if self.t.pend and self.t.pend[0][0] > now:
            cocotb.start_soon(later(self.t.pend[0][0] - now + 0.001, self.eval))

    def on_clk(self, v):
        self.clk = v
        self.eval()

    def host(self, ov):
        self.oe, self.v = ov
        self.eval()


async def swd_frame(dut, u, swd, plan, delay, fall, corner):
    """One clocked frame at TXPER 4: plan[k] = (OE, value) of the host SWDIO in bit k (None = unchanged)."""
    d = CORNERS[corner]
    await clocked_config(u, 4, cpol=0, lead=0, fall=fall, delay=delay)
    pin = ThreadPin(d[1], swd.host)
    host = ClockedHost(dut, plan, pin, swd.on_clk, d[0])
    swd.host(plan[0])
    got, st, _ = await clocked_frame(u, [0] * len(plan))
    await ClockCycles(dut.clk, 4)
    host.stop()
    return got, pin.errors


async def swd_session(dut, u, delay, fall, corner, td):
    """Connection, DPIDR read, SELECT write, AP write, posted AP read and RDBUFF. Returns fault or None."""
    tgt = fx.SwdTarget(td=td, tsu=2.0, th=2.0, tlow=20.0, thigh=20.0)
    swd = SwdAtPins(u, tgt, CORNERS[corner][2])
    errs = []

    async def drive(bits):
        _, e = await swd_frame(dut, u, swd, [(1, b) for b in bits], delay, fall, corner)
        errs.extend(e)

    async def xfer(apndp, rnw, a, value=0):
        req = fx.swd_request(apndp, rnw, a)
        plan = [(1, (req >> i) & 1) for i in range(8)] + [(0, 1)] * 3        # request, release, ACK bits 8-10
        got, e = await swd_frame(dut, u, swd, plan, delay, fall, corner)
        errs.extend(e)
        ack = got[8] | got[9] << 1 | got[10] << 2
        if ack != 0b001:
            return ack, None
        if rnw:                                                   # data 11-42, parity 43, turnaround 44, end 45
            got, e = await swd_frame(dut, u, swd, [(0, 1)] * 34 + [(1, 0)], delay, fall, corner)
            errs.extend(e)
            v = fx.lsb_word(got[:32])
            return ack, v if fx.parity(v) == got[32] else ("parity", v)
        wbits = fx.value_bits(value, 32) + [fx.parity(value)]     # release 11, turnaround 12, data 13-45
        _, e = await swd_frame(dut, u, swd, [(0, 1), (0, 1)] + [(1, b) for b in wbits], delay, fall, corner)
        errs.extend(e)
        return ack, None

    await drive(fx.SWD_CONNECT)
    r = await xfer(0, 1, 0x0)
    if r != (0b001, tgt.dpidr):
        return "DPIDR read %s" % (r,)
    if (await xfer(0, 0, 0x8, 0x000000F0))[0] != 0b001:
        return "SELECT write"
    if (await xfer(1, 0, 0x4, 0xCAFEF00D))[0] != 0b001 or tgt.ap.get((0xF0, 0x4)) != 0xCAFEF00D:
        return "AP write: AP %s" % tgt.ap
    await xfer(1, 1, 0x4)
    r = await xfer(0, 1, 0xC)
    if r != (0b001, 0xCAFEF00D):
        return "RDBUFF %s" % (r,)
    if tgt.errors or errs:
        return "target timing %s, thread pin %s" % (tgt.errors[:3], errs[:3])
    return None


@cocotb.test(skip=GL)
async def test_slc_swd(dut):
    """T6: SWD at 7.5 MHz SWCLK (TXPER 4, 8 cycles a bit): SWCLK = helper clock, SWDIO driven by a thread pin (OUT
    events at the bit starts, OE off for the turnaround and the target's bits) and sampled by the helper on DIN. The
    request and ACK are one frame and the data phase the next, so the host has the ACK before it drives data. Target:
    fx.SwdTarget (Arm IHI0031H) with output delay 2 or 25 ns after the rising edge, set-up and hold 2 ns, SWCLK high
    and low 20 ns or more: JTAG-to-SWD and line reset, DPIDR, SELECT, AP write, posted AP read with RDBUFF."""
    u = await unit_reset(dut)
    dut.dump_off.value = 1
    log = dut._log
    good = []
    for fall in (0, 1):
        for delay in range(8):
            ok = True
            for corner in ("fast, no mux", "slow, mux 10 ns"):
                for td in (2.0, 25.0):
                    if await swd_session(dut, u, delay, fall, corner, td):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                good.append((delay, fall))
    log.info("SWD 7.5 MHz: read-back settings correct at both extreme corners and target delays 2 and 25 ns: %s" % (
        good or "none"))
    assert good, "SWD: no read-back setting works"
    delay, fall = good[len(good) // 2]
    for corner in CORNERS:
        for td in (2.0, 25.0):
            f = await swd_session(dut, u, delay, fall, corner, td)
            assert f is None, "SWD %s, target delay %.0f ns: %s" % (corner, td, f)
    log.info("SWD 7.5 MHz, delay %d %s copy: connection, DPIDR, SELECT, AP write, posted AP read and RDBUFF correct "
             "at all %d corners and target delays 2 and 25 ns; 0 target timing errors; SWDIO changes 8 or more cycles "
             "apart" % (delay, "falling" if fall else "rising", len(CORNERS)))


@cocotb.test(skip=GL)
async def test_slc_spi_flash_15mhz(dut):
    """T1 at TXPER 2 (4 cycles a bit, 15 MHz), SPI modes 0 to 3 (see spi_flash_run)."""
    await spi_flash_run(dut, ((2, (0, 1, 2, 3)),))


@cocotb.test(skip=GL)
async def test_slc_spi_flash_7m5_30mhz(dut):
    """T1 at TXPER 4 (7.5 MHz) in modes 0 to 3 and at TXPER 1 (30 MHz) in mode 0, with the control that must fail
    (see spi_flash_run)."""
    await spi_flash_run(dut, ((4, (0, 1, 2, 3)), (1, (0,))))

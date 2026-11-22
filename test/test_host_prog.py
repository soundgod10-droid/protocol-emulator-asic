"""Host program (host/pe_host) in CPython against the simulated chip: the code that runs on the RP2350.

The library calls block (as on the board); here they run in a thread (cocotb bridge) and each SPI transaction
or wait is done by the simulator (cocotb resume), so the library code is the same as on the board. What is
the test bench, not the board code: the pin-level transport (test/host_spi.py SpiTimed for the protocol
tests; the PIO interpreter host/pio_sim.py running the PIO program text of host/pe_host/pio_spi.py for the
PIO tests), the line models and the checks.

Conditions: logic simulation of the RTL at 60 MHz (and 25 MHz for the bench clock case) with ideal pins;
board, pad and multiplexer delays only where a test sets them; no transceiver, magnetics or cable. Nothing
here has run on an RP2350.

  test_host_eth_ping   ARP request and 10 ICMP echo requests from the 10BASE-T partner model
                       (test/linemodel/tenbaset.py) through fw/eth_10bt.s; the host program answers.
  test_host_usb_keys   100 keystrokes typed by the host program through fw/usb_dev.s, read by the USB host
                       model (test/linemodel/usb_host.py) after enumeration.
  test_host_pio_link   the PIO program in the interpreter: READ_ID and a memory pattern through
                       pe_host.core at 60 MHz (10 MHz SCK) and at the 25 MHz bench clock (4.17 MHz SCK).
  test_host_pio_window MISO sample delay d = 0 to 4 against the board delay each way and both PIO clock
                       phases: the window table and the first failing delay (GC-4).
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer
from cocotb.utils import get_sim_time

try:
    from cocotb.task import bridge, resume          # cocotb 2.0
except ImportError:                                  # cocotb 1.x names
    from cocotb import external as bridge, function as resume
try:
    from cocotb.triggers import ValueChange
except ImportError:
    from cocotb.triggers import Edge as ValueChange

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "host"))
import build_image  # noqa: E402
import host_spi as hs  # noqa: E402
import pio_sim  # noqa: E402
from linemodel import tenbaset as T  # noqa: E402
from linemodel import usb_host as H  # noqa: E402
from pe_host.core import CHIP_ID, Chip  # noqa: E402
from pe_host.eth import Station  # noqa: E402
from pe_host.kbd import Keyboard  # noqa: E402
from pe_host.pio_spi import PioSpi  # noqa: E402
from test_eth_dev import Line, partner_wave  # noqa: E402
from test_usb_dev import RECOVERY_PS, RESET_PS, Bus, Glue, check_clean, summary  # noqa: E402

CHIP_HZ = 60_000_000
MAC_DUT = bytes([0x02, 0, 0, 0, 0, 0x01])
MAC_P = bytes([0x02, 0, 0, 0, 0, 0x02])
IP_DUT = bytes([192, 168, 7, 2])
IP_P = bytes([192, 168, 7, 1])


class SimTransport:
    """xfer / sleep_us / ticks_us of the library, done by the simulator through the test host model."""

    def __init__(self, spi):
        self.spi = spi
        self.now_us = get_sim_time("us")
        self.xfers = 0
        self._x = resume(self._axfer)
        self._s = resume(self._asleep)

    async def _axfer(self, out):
        got = await self.spi.transaction(list(out))
        self.now_us = get_sim_time("us")
        self.xfers += 1
        return bytes(got)

    async def _asleep(self, us):
        await Timer(max(1, round(us * 1000)), unit="ns")
        self.now_us = get_sim_time("us")

    def xfer(self, out):
        return self._x(bytes(out))

    def sleep_us(self, us):
        self._s(us)

    def ticks_us(self):
        return int(self.now_us)


def dump(dut, on):
    """Waveform dump on or off (test/tb.v dump_off, Icarus only): each test dumps only its review window."""
    if hasattr(dut, "dump_off"):
        dut.dump_off.value = 0 if on else 1


async def board(dut, clk_ps=16666):
    dump(dut, False)
    cocotb.start_soon(Clock(dut.clk, clk_ps, unit="ps").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    async def rst(level):
        dut.rst_n.value = level
        await ClockCycles(dut.clk, 2)
    return resume(rst)


# ---------------------------------------------------------------------------------------------------------
# Ethernet: ARP + 10 ICMP echoes
# ---------------------------------------------------------------------------------------------------------

def ones16(b):
    """The checker's own Internet checksum sum (RFC 1071), independent of pe_host.eth."""
    if len(b) % 2:
        b = bytes(b) + b"\0"
    s = sum(int.from_bytes(b[i:i + 2], "big") for i in range(0, len(b), 2))
    while s > 0xFFFF:
        s = (s & 0xFFFF) + (s >> 16)
    return s


def arp_request():
    p = (bytes([0, 1, 8, 0, 6, 4, 0, 1]) + MAC_P + IP_P + bytes(6) + IP_DUT)
    return T.build_frame(b"\xff" * 6, MAC_P, 0x0806, p)


def echo_request(seq, data):
    icmp = bytearray([8, 0, 0, 0, 0x12, 0x34, seq >> 8, seq & 0xFF]) + data
    c = (~ones16(icmp)) & 0xFFFF
    icmp[2:4] = c.to_bytes(2, "big")
    ip = bytearray([0x45, 0, 0, 0, 0x40 | 0, seq & 0xFF, 0, 0, 64, 1, 0, 0]) + IP_P + IP_DUT
    ip[2:4] = (20 + len(icmp)).to_bytes(2, "big")
    ip[4:6] = (0x4000 + seq).to_bytes(2, "big")
    c = (~ones16(ip)) & 0xFFFF
    ip[10:12] = c.to_bytes(2, "big")
    return T.build_frame(MAC_DUT, MAC_P, 0x0800, bytes(ip) + bytes(icmp)), bytes(icmp)


def check_arp_reply(f):
    """RFC 826 reply to arp_request(): who-has IP_DUT answered with MAC_DUT, to MAC_P."""
    errs = []
    if f[0:6] != MAC_P or f[6:12] != MAC_DUT or f[12:14] != b"\x08\x06":
        errs.append("Ethernet header %s" % f[:14].hex())
    a = f[14:42]
    if a[0:8] != bytes([0, 1, 8, 0, 6, 4, 0, 2]):
        errs.append("ARP fixed fields %s" % a[0:8].hex())
    if a[8:14] != MAC_DUT or a[14:18] != IP_DUT or a[18:24] != MAC_P or a[24:28] != IP_P:
        errs.append("ARP addresses %s" % a[8:28].hex())
    return errs


def check_echo_reply(f, icmp_req):
    """RFC 792/791: echo reply with the request's identifier, sequence number and data; valid checksums."""
    errs = []
    if f[0:6] != MAC_P or f[6:12] != MAC_DUT or f[12:14] != b"\x08\x00":
        errs.append("Ethernet header %s" % f[:14].hex())
    ip = f[14:]
    ihl = (ip[0] & 15) * 4
    total = int.from_bytes(ip[2:4], "big")
    if ip[0] >> 4 != 4 or ihl != 20 or ones16(ip[:20]) != 0xFFFF:
        errs.append("IPv4 header %s" % ip[:20].hex())
    if ip[9] != 1 or ip[12:16] != IP_DUT or ip[16:20] != IP_P or ip[8] == 0:
        errs.append("IPv4 protocol, addresses or TTL %s" % ip[8:20].hex())
    icmp = ip[ihl:total]
    if icmp[0] != 0 or icmp[1] != 0 or ones16(icmp) != 0xFFFF:
        errs.append("ICMP type %d code %d sum %04x" % (icmp[0], icmp[1], ones16(icmp)))
    if icmp[4:] != icmp_req[4:]:
        errs.append("identifier, sequence or data differ")
    return errs


async def wait_packets(line, t0, n, timeout_us=20000):
    """The first n chip packets, each ended 10 us before now (the decoder closes an open burst at the end of
    the window, so a packet still on the line would count; a partner frame sent then collides with it)."""
    t_end = get_sim_time("us") + timeout_us
    while True:
        now = get_sim_time("ps")
        evs = T.decode_tx(line.tx_wave(t0, now))
        pk = [e for e in evs if e.kind == "packet" and e.t1 < now - t0 - 10e6]
        if len(pk) >= n:
            return pk
        assert get_sim_time("us") < t_end, "the chip sent %d packets, waiting for %d" % (len(pk), n)
        await Timer(20, unit="us")


@cocotb.test()
async def test_host_eth_ping(dut):
    """ARP + 10 ICMP echo requests (32 data octets each) from the partner model; the host program
    (pe_host.eth.Station) answers through fw/eth_10bt.s. Every chip packet is decoded by the partner model
    (Manchester, preamble, SFD, FCS, rate, jitter, TP_IDL); the replies are checked field by field against
    RFC 826 and RFC 792 by this test's own checker."""
    rst = await board(dut)
    spi = hs.SpiTimed(dut)
    line = Line(dut, spi)
    t = SimTransport(spi)
    chip = Chip(t, reset_pin=rst)
    image = build_image.build("eth_10bt", CHIP_HZ)
    await bridge(chip.load)(image, CHIP_HZ)
    dut._log.info("loaded %s: %d words in %d regions, %d transactions" % (
        image["name"], sum(len(w) for _, w in image["mem"]), len(image["mem"]), t.xfers))
    st = Station(chip, MAC_DUT, IP_DUT)
    t0 = get_sim_time("ps")
    rng = random.Random(5)
    results = {}

    async def partner():
        await Timer(50, unit="us")
        await line.send(partner_wave(arp_request()))
        pk = await wait_packets(line, t0, 1)
        results["arp"] = pk[0]
        reqs = []
        for seq in range(1, 11):
            frame, icmp = echo_request(seq, bytes(rng.randrange(256) for _ in range(32)))
            reqs.append(icmp)
            await Timer(20, unit="us")
            await line.send(partner_wave(frame))
            pk = await wait_packets(line, t0, 1 + seq)
        results["pk"] = pk
        results["reqs"] = reqs

    task = cocotb.start_soon(partner())
    n_icmp = lambda s: sum(1 for w, _ in s.log if w == "icmp")       # noqa: E731
    n_arp = lambda s: sum(1 for w, _ in s.log if w == "arp")         # noqa: E731
    try:
        await bridge(st.serve)(lambda s: n_arp(s) >= 1 and n_icmp(s) >= 10, 60000)
    except Exception:
        evs = T.decode_tx(line.tx_wave(t0, get_sim_time("ps")))
        stat = await bridge(chip.status)()
        dut._log.info("diagnosis: station log %s; events %s; frames waiting %d; partner %s; chip line %s; "
                      "partner bursts %s; %r" % (
                          st.log, [hex(e) for e in st.events], len(st.frames), sorted(results),
                          [(e.kind, round(e.t0 / 1e6, 2), len(e.frame)) for e in evs],
                          [(round((a - t0) / 1e6, 2), round((b - t0) / 1e6, 2)) for a, b in line.rx_log], stat))
        raise
    await task
    pk = results["pk"]
    assert len(pk) == 11, "chip packets %d" % len(pk)
    for e in pk:
        assert not e.violations, "packet violations: %s" % e.violations
    errs = check_arp_reply(pk[0].frame[:-4])
    assert not errs, "ARP reply: %s" % errs
    for k, (e, req) in enumerate(zip(pk[1:], results["reqs"])):
        errs = check_echo_reply(e.frame[:-4], req)
        assert not errs, "echo reply %d: %s" % (k + 1, errs)
    dut._log.info("waveform marks: chip packets (start, end) ps %s" % [(t0 + e.t0, t0 + e.t1) for e in pk[:3]])
    dut._log.info("ETH: ARP reply + 10 echo replies correct; station log %s; events %s; %d SPI transactions; "
                  "first reply rate %+.1f ppm jitter %.0f ps; simulated %.3f ms" % (
                      [w for w, _ in st.log], [hex(e) for e in st.events], t.xfers, pk[0].measures["rate_ppm"],
                      pk[0].measures["jitter_ps"], (get_sim_time("ps") - t0) / 1e9))


# ---------------------------------------------------------------------------------------------------------
# USB: 100 keystrokes
# ---------------------------------------------------------------------------------------------------------

USAGE = {}
for _i in range(26):
    USAGE[(0, 0x04 + _i)] = chr(0x61 + _i)
    USAGE[(2, 0x04 + _i)] = chr(0x41 + _i)
for _i, _c in enumerate("123456789"):
    USAGE[(0, 0x1E + _i)] = _c
USAGE.update({(0, 0x27): "0", (0, 0x2C): " ", (0, 0x37): ".", (0, 0x36): ",", (2, 0x1E): "!", (2, 0x38): "?"})


def decode_keys(reports):
    """The checker's own reading of boot keyboard reports (HID 1.11 B.1; usage page 0x07): a key-down report
    followed by an all-up report is one keystroke."""
    out, errs = [], []
    for k in range(0, len(reports) - 1, 2):
        down, up = reports[k], reports[k + 1]
        if up != bytes(8) or down[1] != 0 or any(down[3:]):
            errs.append("reports %d, %d: %s %s" % (k, k + 1, down.hex(), up.hex()))
            continue
        out.append(USAGE.get((down[0], down[2]), "?"))
    return "".join(out), errs


@cocotb.test()
async def test_host_usb_keys(dut):
    """The USB host model enumerates the chip (address 5, configuration 1), then polls endpoint 1 back to back
    (faster than the 10 ms bInterval, for simulation time). The host program waits for the configuration
    event and types 100 characters (each a key-down and an all-up report, 200 reports). The host model must
    read the 200 reports in order with alternating DATA0/DATA1, every device packet within the USB 2.0 limits.
    Bus reset 20 us and recovery 100 us (simulation speed, as test_usb_dev)."""
    rst = await board(dut)
    spi = hs.SpiTimed(dut)
    bus = Bus(dut, spi)
    t = SimTransport(spi)
    chip = Chip(t, reset_pin=rst)
    image = build_image.build("usb_dev", CHIP_HZ)
    await bridge(chip.load)(image, CHIP_HZ)
    rng = random.Random(9)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,!?"
    text = "".join(rng.choice(alphabet) for _ in range(100))
    glue = Glue(bus)
    s = H.UsbHostSession()
    got = []
    t0 = get_sim_time("ps")

    async def usb_host():
        rep = await glue.run(s.enumerate(addr=5, reset_ps=RESET_PS, recovery_ps=RECOVERY_PS))
        assert rep["device"]["idVendor"] == 0x1209, rep["device"]

        def polls():
            naks = 0
            while len(got) < 2 * len(text):
                yield from s.gap()
                pid, data = yield from s.in_(5, 1)
                if pid in ("DATA0", "DATA1"):
                    got.append((pid, bytes(data)))
                else:
                    naks += 1
            return naks
        return await glue.run(polls())

    task = cocotb.start_soon(usb_host())
    kb = Keyboard(chip)

    def host_main():
        kb.wait_configured(100_000)
        return kb.type_text(text, timeout_us=20_000)
    n = await bridge(host_main)()
    naks = await task
    check_clean(s, bus)
    pids = [p for p, _ in got]
    assert all(pids[k] == ("DATA0" if k % 2 == 0 else "DATA1") for k in range(len(pids))), pids[:8]
    typed, errs = decode_keys([d for _, d in got])
    assert not errs, errs[:4]
    assert typed == text, "typed %r, sent %r" % (typed, text)
    dut._log.info("USB: %d keystrokes (%d reports) read in order by the host model, DATA0/DATA1 alternating, "
                  "%d NAKs, %d SPI transactions; text %r; %s; simulated %.3f ms" % (
                      n, len(got), naks, t.xfers, text[:24], summary(s), (get_sim_time("ps") - t0) / 1e9))


# ---------------------------------------------------------------------------------------------------------
# PIO program in the interpreter
# ---------------------------------------------------------------------------------------------------------

PINS_DEMO = dict(sck=24, cs_n=23, mosi=22, miso=40, gpio_base=16)


class _NoTime:
    def sleep_us(self, us):
        raise RuntimeError("not used in the bench")

    def ticks_us(self):
        return 0


class PioSimTransport:
    """The PIO program of pe_host.pio_spi (built by PioSpi with the rp2 stand-in) clocked at 2 x the chip clock
    against the chip pins. Output pins change at the clock edge after the instruction (output register), plus
    the board delay d_ps; MISO reaches the PIO d_ps after the chip pin and passes a 2-flip-flop input
    synchronizer (RP2350 default, not bypassed). The CPU side (FIFO feed and drain) is instant."""

    def __init__(self, dut, chip_hz, d, delay_ps, phase_cycles=0):
        self.dut = dut
        self.cyc_ps = round(1e12 / (2 * chip_hz))
        self.delay_ps = delay_ps
        self.phase_cycles = phase_cycles
        self.lvl = {PINS_DEMO["sck"]: 0, PINS_DEMO["cs_n"]: 1, PINS_DEMO["mosi"]: 0}
        self.nxt = dict(self.lvl)
        self.hist = [(0, 0)]
        self.s1 = self.s2 = 0
        rp = pio_sim.Rp2Module(drive=self._drive, read=self._read)
        self.spi = PioSpi(chip_hz, d=d, rp2_mod=rp, pin_cls=pio_sim.Pin, time_mod=_NoTime(), **PINS_DEMO)
        self.sm = self.spi.sm
        self.pull_pc = 0
        self.now_us = get_sim_time("us")
        self.xfers = 0
        self._apply(self.lvl)
        self.task = cocotb.start_soon(self._watch())
        self._x = resume(self._axfer)
        self._s = resume(self._asleep)

    def close(self):
        (getattr(self.task, "cancel", None) or self.task.kill)()

    def _drive(self, pin, v):
        self.nxt[pin] = v

    def _read(self, pin):
        return self.s2

    def _apply(self, lv):
        ui = (lv[PINS_DEMO["sck"]] << hs.SCK_BIT) | (lv[PINS_DEMO["cs_n"]] << hs.CS_N_BIT) | \
             (lv[PINS_DEMO["mosi"]] << hs.MOSI_BIT)
        self.dut.ui_in.value = ui

    async def _later(self, lv):
        await Timer(self.delay_ps, unit="ps")
        self._apply(lv)

    async def _watch(self):
        while True:
            await ValueChange(self.dut.uo_out)
            v = self.dut.uo_out.value
            b = (v.to_unsigned() >> hs.MISO_BIT) & 1 if v.is_resolvable else 0
            self.hist.append((get_sim_time("ps"), b))

    def _miso_at(self, t):
        for tt, b in reversed(self.hist):
            if tt <= t:
                return b
        return 0

    async def _cycle(self):
        """One PIO clock edge: outputs of the last instruction, synchronizer, next instruction."""
        if self.nxt != self.lvl:
            self.lvl = dict(self.nxt)
            if self.delay_ps:
                cocotb.start_soon(self._later(dict(self.lvl)))
            else:
                self._apply(self.lvl)
        now = get_sim_time("ps")
        self.s2, self.s1 = self.s1, self._miso_at(now - self.delay_ps)
        self.sm.step()
        await Timer(self.cyc_ps, unit="ps")

    async def _axfer(self, out):
        await RisingEdge(self.dut.clk)
        for _ in range(self.phase_cycles):
            await Timer(self.cyc_ps, unit="ps")
        n = len(out)
        got = []
        self.sm.put(8 * n - 1)
        i = 0
        idle = 0
        while idle < 4:
            if i < n and self.sm.tx_fifo() < 4:
                self.sm.put(out[i] << 24)
                i += 1
            while self.sm.rx_fifo():
                got.append(self.sm.get() & 0xFF)
            await self._cycle()
            done = len(got) == n and self.sm.pc == self.pull_pc and self.sm.stalled
            idle = idle + 1 if done else 0
        await Timer(self.delay_ps + 2 * self.cyc_ps, unit="ps")
        self.now_us = get_sim_time("us")
        self.xfers += 1
        return bytes(got)

    async def _asleep(self, us):
        await Timer(max(1, round(us * 1000)), unit="ns")
        self.now_us = get_sim_time("us")

    def xfer(self, out):
        return self._x(bytes(out))

    def sleep_us(self, us):
        self._s(us)

    def ticks_us(self):
        return int(self.now_us)


def link_check(chip, words=64, seed=1):
    """READ_ID, then WRITE_MEM / READ_MEM of a pseudo-random pattern; returns the number of errors."""
    if chip.read_id() != CHIP_ID:
        return -1
    rng = random.Random(seed)
    pat = [rng.randrange(65536) for _ in range(words)]
    chip.write_mem(512, pat)
    got = chip.read_mem(512, words)
    return sum(1 for a, b in zip(pat, got) if a != b)


@cocotb.test()
async def test_host_pio_link(dut):
    """The PIO program text of pe_host.pio_spi, assembled by the rp2 stand-in, in the interpreter: its words
    equal the RP2350 data sheet encodings listed here; READ_ID and a 64-word memory pattern through
    pe_host.core at 60 MHz (PIO 120 MHz, SCK 10 MHz) with 5 ns board delay each way (the shuttle's 10 ns
    round-trip estimate), at both PIO clock phases."""
    rst = await board(dut)
    from pe_host.pio_spi import make_program
    prog = make_program(pio_sim.asm_pio, pio_sim.PIO, 2)
    # RP2350 data sheet PIO instruction encoding: PULL block 0x80A0, MOV X, OSR 0xA027, OUT NULL, 32 0x6060,
    # NOP (MOV Y, Y) 0xA042, OUT PINS, 1 0x6001, IN PINS, 1 0x4001, JMP X-- 0x0040 | address;
    # side-set (2 bits) in bits 12:11, delay in bits 10:8.
    expect = [0x80A0 | (1 << 11) | (3 << 8), 0xA027 | (1 << 11), 0x6060 | (1 << 11), 0xA042,
              0x6001 | (4 << 8), 0xA042 | (2 << 11) | (2 << 8), 0x4001 | (2 << 11) | (2 << 8), 0x0044,
              0xA042 | (3 << 8)]
    assert prog.words == expect, "PIO words %s, expected %s" % ([hex(w) for w in prog.words],
                                                                [hex(w) for w in expect])
    assert (prog.wrap_bottom, prog.wrap_top) == (0, 8)
    res = []
    for phase in (0, 1):
        t = PioSimTransport(dut, CHIP_HZ, d=2, delay_ps=5000, phase_cycles=phase)
        chip = Chip(t, reset_pin=rst)
        e = await bridge(link_check)(chip)
        st = await bridge(chip.status)()
        t.close()
        res.append((phase, e, st.cmd_err))
        assert e == 0 and not st.cmd_err, "phase %d: errors %d, %r" % (phase, e, st)
    dut._log.info("PIO 60 MHz: words %s; phases (phase, errors, CMD_ERR) %s" % (
        [hex(w) for w in prog.words], res))


@cocotb.test()
async def test_host_pio_link_25mhz(dut):
    """The same program at the 25 MHz bench clock: chip clock 40 ns, PIO 50 MHz, SCK 4.17 MHz; READ_ID and
    the memory pattern with 0 and 5 ns board delay each way (FPGA LVCMOS33 pins and jumpers, not the chip
    pads). Logic simulation of the RTL at 25 MHz."""
    rst = await board(dut, clk_ps=40000)
    res = []
    for dly in (0, 5000):
        t = PioSimTransport(dut, 25_000_000, d=2, delay_ps=dly)
        chip = Chip(t, reset_pin=rst)
        e = await bridge(link_check)(chip)
        t.close()
        res.append((dly, e))
        assert e == 0, "25 MHz, delay %d ps: %d errors" % (dly, e)
    dut._log.info("PIO 25 MHz: (delay ps, errors) %s" % res)


@cocotb.test()
async def test_host_pio_window(dut):
    """Sample delay d = 0 to 4 against the board delay each way (0 to 60 ns: RP2350 pad, board, multiplexer,
    chip pad; the same each way) and both PIO clock phases, 60 MHz. A case passes when READ_ID and a 16-word
    memory pattern are correct. Logged: the passing delays per d and the first failing delay (GC-4)."""
    rst = await board(dut)
    delays = list(range(0, 61, 5))
    table = {}
    for d in range(5):
        row = []
        for dly in delays:
            ok = True
            for phase in (0, 1):
                t = PioSimTransport(dut, CHIP_HZ, d=d, delay_ps=dly * 1000, phase_cycles=phase)
                chip = Chip(t, reset_pin=rst)
                e = await bridge(link_check)(chip, 16, d * 100 + dly)
                t.close()
                ok = ok and e == 0
            row.append(ok)
        table[d] = row
    lines = []
    first_fail = {}
    for d, row in table.items():
        passing = [dl for dl, ok in zip(delays, row) if ok]
        fails = [dl for dl, ok in zip(delays, row) if not ok]
        first_fail[d] = min(fails) if fails else None
        lines.append("d=%d: pass at %s ns each way; first fail %s" % (d, passing, first_fail[d]))
    dut._log.info("PIO window (60 MHz, SCK 10 MHz, both phases):\n" + "\n".join(lines))
    assert table[2][delays.index(5)], "the default d = 2 must pass at the 5 ns estimate each way"
    assert any(not ok for row in table.values() for ok in row), "control: some delay must fail"

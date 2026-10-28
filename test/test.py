# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

"""Milestone M2c test: host SPI port, and the UART transmitter as firmware.

What it does:
  The chip has no fixed UART any more. The tests load fw/uart_tx.s through
  the host SPI port (docs/host_port.md) with an independent host model
  (test/host_spi.py), read it back and compare, write the configuration
  (thread 0 owns pin 12 = uo_out[4]), start the threads and give the bytes
  through the host FIFO of thread 0. Then UART_TX is decoded with the
  receiver model of M1, which does not use the processor.
  Other tests check the protocol: chip ID, status, command rules, codes
  that are not valid, the SCK period limit, STOP and START, and the reset.

  The tests decide pass or fail only from chip pins:
  - uo_out[4] UART_TX (tb wire "uart_tx")
  - ui_in[7:5], uo_out[7]: host SPI port (HOST_SCK, HOST_CS_N, HOST_MOSI,
    HOST_MISO, A1 section 2.2)
  A UART receiver model decodes UART_TX. It finds the start edge, then
  samples each bit at its middle (start edge + (k + 0.5) bit times).
  A line monitor records each change of UART_TX with its simulation time.
  The test uses these records to measure the bit time and to find
  glitches, X and Z values.

Values from A1_architecture.md:
  - clock 50 MHz (section 2.3), 115,200 bit/s, 434 cycles for each bit
    (fw/uart_tx.s), our part of the clock-error budget +/-1 % (section 3).
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotb.utils import get_sim_time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import host_spi as hs  # noqa: E402
import pe_asm  # noqa: E402

CLK_NS = 20  # 50 MHz
CLK_PS = CLK_NS * 1000
BAUD = 115_200
T_NOM_PS = 1e12 / BAUD  # 8,680,555.6 ps: bit time that the receiver expects
BIT_CYCLES = 434  # design value: round(50e6 / 115200)
T_DUT_PS = BIT_CYCLES * CLK_PS  # 8,680,000 ps: bit time that the design must make
RATE_TOL = 0.01  # +/-1 % (A1 section 3)
EDGE_TOL_PS = 1_000  # an edge must be within 1 ns of a bit boundary
FRAME_BITS = 10  # start + 8 data + stop
P_TX = 12  # UART_TX = uo_out[4] = output pin 12 (docs/isa.md 5.1)
RESET_UO = 0x7F  # isa 2.4: V = 1 on uo_out[6:0]; uo_out[7] = HOST_MISO = 0 with no transaction


def now_ps():
    return get_sim_time(unit="ps")


class LineMonitor:
    """Records each change of one pin as (time in ps, value as text)."""

    def __init__(self, signal):
        self.signal = signal
        self.changes = []

    async def run(self):
        while True:
            await self.signal.value_change
            self.changes.append((now_ps(), str(self.signal.value)))

    def after(self, t_ps):
        return [c for c in self.changes if c[0] > t_ps]

    def not_01(self):
        return [c for c in self.changes if c[1] not in ("0", "1")]


class UartRx:
    """UART receiver model: 8 data bits, LSB first, 1 start bit, 1 stop bit.

    It samples at the middle of each bit, from the measured start edge,
    with the nominal bit time of 115,200 bit/s (not the design value).
    """

    def __init__(self, signal):
        self.signal = signal
        self.frames = []  # (start-edge time in ps, list of 10 samples as text)

    async def run(self):
        while True:
            await FallingEdge(self.signal)
            t0 = now_ps()
            samples = []
            for k in range(FRAME_BITS):
                t_sample = t0 + round((k + 0.5) * T_NOM_PS)
                await Timer(t_sample - now_ps(), unit="ps")
                samples.append(str(self.signal.value))
            self.frames.append((t0, samples))


async def start(dut):
    """Starts the clock and the line monitor, then resets the chip. Host pins idle: CS_N high, SCK low."""
    mon = LineMonitor(dut.uart_tx)
    cocotb.start_soon(mon.run())
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)  # reset synchronizer: 2 cycles
    return mon, hs.SpiTimed(dut)


def uart_tx_image():
    """fw/uart_tx.s as a 128-word memory image (words after the program are 0 = HALT)."""
    prog = pe_asm.assemble_file(os.path.join(ROOT, "fw", "uart_tx.s"))
    image = list(prog.image())
    return prog, image + [0] * (128 - len(image))


async def load_uart_tx(dut, host):
    """Loads fw/uart_tx.s through the host port, reads it back, configures pin 12 for thread 0, starts."""
    prog, image = uart_tx_image()
    await host.write_mem(0, image)
    back = await host.read_mem(0, 128)
    bad = [a for a in range(128) if back[a] != image[a]]
    assert not bad, f"read-back differs at {len(bad)} addresses, first {bad[0]}: {back[bad[0]]:04X} not {image[bad[0]]:04X}"
    owner = [None] * 15
    owner[P_TX] = 0
    head, pins = hs.config_words((0, 0, 0, 0), (True, False, False, False), owner, [False] * 15)
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    await host.start()
    st = await host.read_status()
    assert st["running"] == 1 and st["run"] == [1, 0, 0, 0] and not st["cmd_err"], f"status after START: {st}"
    dut._log.info(f"fw/uart_tx.s: {prog.size} words loaded and read back through the host port; status {st}")
    return prog


async def send_bytes(host, data):
    """Gives the bytes to thread 0: reads the free space of its in-FIFO (status), then pushes that many."""
    i = 0
    while i < len(data):
        st = await host.read_status(4)
        assert not st["push_drop"] and not st["cmd_err"], f"status {st}"
        free = 4 - st["in_cnt"][0]
        if free > 0:
            n = min(free, len(data) - i)
            await host.push(0, data[i:i + n])
            i += n


def edge_offsets(frames, changes):
    """Returns (offset in ps, bit number k) for each edge after the start edge.

    k is the nearest bit boundary with the nominal bit time.
    """
    result = []
    for t0, _ in frames:
        for t, _ in changes:
            if t0 < t < t0 + (FRAME_BITS - 0.25) * T_NOM_PS:
                k = round((t - t0) / T_NOM_PS)
                result.append((t - t0, k))
    return result


def measure_bit_time(log, frames, changes, label):
    """Measures the bit time from the edges. Returns it in ps."""
    offsets = [(off, k) for off, k in edge_offsets(frames, changes) if k >= 1]
    assert offsets, f"{label}: no edges to measure the bit time"
    estimates = [off / k for off, k in offsets]
    t_bit = sum(off for off, _ in offsets) / sum(k for _, k in offsets)
    spread = max(estimates) - min(estimates)
    rate = 1e12 / t_bit
    err = rate / BAUD - 1
    log.info(
        f"{label}: measured bit time {t_bit / 1000:.3f} ns "
        f"({t_bit / CLK_PS:.3f} clock cycles) from {len(offsets)} edges, "
        f"spread {spread:.1f} ps; bit rate {rate:.1f} bit/s, error {err * 100:+.4f} %"
    )
    assert abs(err) < RATE_TOL, f"{label}: bit-rate error {err * 100:+.4f} % is more than 1 %"
    assert abs(t_bit - T_DUT_PS) < 1, f"{label}: bit time {t_bit} ps is not {BIT_CYCLES} cycles"
    assert spread < 2 * EDGE_TOL_PS / FRAME_BITS, f"{label}: bit time is not constant"
    return t_bit


def check_frame(n, t0, samples, expected, changes, t_bit):
    """Checks start bit, data, stop bit and the position of each edge of one frame."""
    assert all(s in ("0", "1") for s in samples), f"frame {n}: X or Z in samples {samples}"
    assert samples[0] == "0", f"frame {n}: start bit is {samples[0]}, not 0"
    assert samples[9] == "1", f"frame {n}: stop bit is {samples[9]}, not 1"
    value = sum(int(samples[1 + i]) << i for i in range(8))
    assert value == expected, f"frame {n}: data 0x{value:02X}, expected 0x{expected:02X}"
    # Each change in the frame must be at a bit boundary and agree with the bits.
    # An extra pair of changes (a glitch) or a change between boundaries fails here.
    # The end is 10 bit times: the line must stay high for the full stop bit.
    got = []
    for t, v in changes:
        if t0 <= t < t0 + FRAME_BITS * t_bit - EDGE_TOL_PS:
            k = round((t - t0) / t_bit)
            err = abs(t - t0 - k * t_bit)
            assert err <= EDGE_TOL_PS, f"frame {n}: edge at {t} ps is {err:.0f} ps from boundary {k}"
            got.append((k, v))
    want = [(0, "0")] + [(k, samples[k]) for k in range(1, FRAME_BITS) if samples[k] != samples[k - 1]]
    assert got == want, f"frame {n}: edges {got}, expected {want}"


def check_stream(log, mon, rx, expected, label, back_to_back):
    """Checks all frames of one test and the time between them."""
    assert not mon.not_01(), f"{label}: X or Z on UART_TX: {mon.not_01()}"
    got = [f"{sum(int(s[1 + i]) << i for i in range(8)):02X}" if all(c in '01' for c in s) else "??" for _, s in rx.frames]
    assert len(rx.frames) == len(expected), f"{label}: {len(rx.frames)} frames, expected {len(expected)}: {got}"
    t_bit = measure_bit_time(log, rx.frames, mon.changes, label)
    for n, ((t0, samples), byte) in enumerate(zip(rx.frames, expected)):
        check_frame(n, t0, samples, byte, mon.changes, t_bit)
    starts = [t0 for t0, _ in rx.frames]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    if gaps:
        log.info(
            f"{label}: start-to-start time min {min(gaps) / 1000:.3f} ns, max {max(gaps) / 1000:.3f} ns "
            f"(10 bit times = {FRAME_BITS * t_bit / 1000:.3f} ns)"
        )
        assert min(gaps) >= FRAME_BITS * t_bit - EDGE_TOL_PS, f"{label}: a frame starts before the stop bit ends"
        if back_to_back:
            assert max(gaps) - min(gaps) <= EDGE_TOL_PS and abs(min(gaps) - FRAME_BITS * t_bit) <= EDGE_TOL_PS, (
                f"{label}: frames are not back-to-back (10 bit times)"
            )
    log.info(f"{label}: {len(rx.frames)} frames correct")


@cocotb.test()
async def test_reset_keeps_line_idle(dut):
    """UART_TX is high during and after reset, before any program is loaded, with no change and no X or Z."""
    mon = LineMonitor(dut.uart_tx)
    cocotb.start_soon(mon.run())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT  # host port idle
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    t_reset = now_ps()
    await Timer(1, unit="ns")
    assert str(dut.uart_tx.value) == "1", "UART_TX is not high 1 ns after reset"
    for _ in range(50):
        await FallingEdge(dut.clk)
        assert str(dut.uart_tx.value) == "1", "UART_TX is not high during reset"
    dut.rst_n.value = 1
    # Two frame times with no request: the line must stay high.
    await Timer(2 * FRAME_BITS * T_DUT_PS, unit="ps")
    assert str(dut.uart_tx.value) == "1", "UART_TX is not high after reset"
    dut._log.info(f"UART_TX changes from reset to {now_ps() / 1e6:.3f} us: {mon.changes}")
    assert mon.after(t_reset) == [], f"UART_TX changed during or after reset: {mon.after(t_reset)}"
    assert not mon.not_01(), f"X or Z on UART_TX: {mon.not_01()}"
    assert dut.uo_out.value.is_resolvable, f"uo_out has X or Z: {dut.uo_out.value}"
    assert dut.uo_out.value == RESET_UO, f"uo_out is {dut.uo_out.value}, expected 01111111 (isa 2.4)"
    assert dut.uio_oe.value == 0, f"uio_oe is {dut.uio_oe.value}, expected 0"


@cocotb.test()
async def test_reset_during_frame(dut):
    """Chip reset in a frame: UART_TX high at once and stays high; the memory keeps the program; threads stop."""
    mon, host = await start(dut)
    await load_uart_tx(dut, host)
    _, image = uart_tx_image()
    await send_bytes(host, [0x00])  # line is low for 9 bit times
    await FallingEdge(dut.uart_tx)
    # 3.3 bit times into the frame, not on a clock edge (+7 ns).
    await Timer(int(3.3 * T_DUT_PS) + 7_000, unit="ps")
    assert str(dut.uart_tx.value) == "0", "UART_TX is not low inside the 0x00 frame"
    t_assert = now_ps()
    dut.rst_n.value = 0
    await Timer(1, unit="ns")
    assert str(dut.uart_tx.value) == "1", "UART_TX is not high 1 ns after reset"
    for _ in range(50):
        await FallingEdge(dut.clk)
        assert str(dut.uart_tx.value) == "1", "UART_TX is not high during reset"
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    st = await host.read_status()
    assert st["running"] == 0 and st["run"] == [0, 0, 0, 0], f"status after reset: {st}"
    back = await host.read_mem(0, 128)
    assert back == image, "the memory changed at the chip reset (docs/isa.md 2.4)"
    await Timer(2 * FRAME_BITS * T_DUT_PS, unit="ps")
    changes = [c for c in mon.changes if c[0] >= t_assert]
    dut._log.info(f"UART_TX changes from reset assert at {t_assert} ps: {changes}; memory unchanged after reset")
    # Exactly one change: to 1, at the reset assert time (asynchronous reset).
    assert changes == [(t_assert, "1")], f"UART_TX changes after reset: {changes}"
    assert not mon.not_01(), f"X or Z on UART_TX: {mon.not_01()}"


@cocotb.test()
async def test_fixed_patterns(dut):
    """fw/uart_tx.s: 0x00, 0xFF, 0x55, 0xAA, each as a single frame with idle time between."""
    mon, host = await start(dut)
    await load_uart_tx(dut, host)
    rx = UartRx(dut.uart_tx)
    cocotb.start_soon(rx.run())
    patterns = [0x00, 0xFF, 0x55, 0xAA]
    for byte in patterns:
        await send_bytes(host, [byte])
        await Timer(12 * T_DUT_PS, unit="ps")  # frame plus idle time
    check_stream(dut._log, mon, rx, patterns, "fixed patterns (firmware)", back_to_back=False)
    st = await host.read_status()
    assert st["late"] == [0, 0, 0, 0] and st["err"] == [0, 0, 0, 0], f"status {st}"


@cocotb.test()
async def test_counter_sequence(dut):
    """fw/uart_tx.s: 256 bytes 0x00 to 0xFF back-to-back: each frame exactly 10 bit times."""
    mon, host = await start(dut)
    await load_uart_tx(dut, host)
    rx = UartRx(dut.uart_tx)
    cocotb.start_soon(rx.run())
    data = list(range(256))
    await send_bytes(host, data)
    await Timer(5 * FRAME_BITS * T_DUT_PS, unit="ps")  # the last 4 bytes in the FIFO, plus idle
    check_stream(dut._log, mon, rx, data, "counter sequence (firmware)", back_to_back=True)
    st = await host.read_status()
    assert st["late"] == [0, 0, 0, 0] and st["err"] == [0, 0, 0, 0] and not st["push_drop"], f"status {st}"


@cocotb.test()
async def test_host_id_status_rules(dut):
    """READ_ID, status after reset, command codes that are not valid, and the command rules."""
    mon, host = await start(dut)
    log = dut._log
    chip_id = await host.read_id()
    log.info(f"READ_ID: {[f'{b:02X}' for b in chip_id]}")
    assert chip_id == hs.CHIP_ID, f"chip ID {chip_id}, expected {hs.CHIP_ID}"
    st = await host.read_status()
    assert st == hs.parse_status([0] * hs.STATUS_BYTES), f"status after reset: {st}"

    # A known word at address 5, to check that no invalid byte writes the memory.
    await host.write_mem(5, [0x1234])
    # Not valid: stuck MOSI (0x00, 0xFF), one wrong bit in each valid code, and
    # each valid code with the WRITE_MEM data after it (it must do nothing).
    bad = [0x00, 0xFF] + [c ^ (1 << b) for c in hs.ALL_COMMANDS for b in range(8)]
    assert not any(hs.code_ok(b) for b in bad), "test fault: a code in the bad list is valid"
    for b in bad:
        await host.transaction([b, 5, 0xAB, 0xCD])
        st = await host.read_status()
        assert st["cmd_err"] == 1 and st["running"] == 0, f"code {b:02X}: status {st}"
        await host.clear()
    assert await host.read_mem(5, 1) == [0x1234], "a code that is not valid changed the memory"
    log.info(f"{len(bad)} codes that are not valid (0x00, 0xFF, every code with 1 wrong bit): CMD_ERR, no effect")

    # Rules while stopped: PUSH and PULL are refused.
    for t in ([hs.PUSH, 0, 0x00, 0x41], [hs.PULL, 0, 0, 0, 0]):
        got = await host.transaction(t)
        st = await host.read_status()
        assert st["cmd_err"] == 1 and st["in_cnt"] == [0, 0, 0, 0], f"{t[0]:02X} while stopped: {st}, MISO {got}"
        await host.clear()

    # Start with no thread running (RUN mask 0): the threads "run" for the port.
    await host.write_cfg(4, [0])
    await host.start()
    st = await host.read_status()
    assert st["running"] == 1 and st["run"] == [0, 0, 0, 0] and not st["cmd_err"], f"after START: {st}"
    # Rules while running: WRITE_MEM, READ_MEM, WRITE_CFG and START are refused.
    await host.write_mem(5, [0xBEEF])
    assert (await host.read_status())["cmd_err"] == 1
    await host.clear()
    got = await host.transaction(hs.t_read_mem(5, 1))
    assert got[2:] == [0, 0] and (await host.read_status())["cmd_err"] == 1, f"READ_MEM while running: {got}"
    await host.clear()
    await host.start()
    assert (await host.read_status())["cmd_err"] == 1
    await host.clear()
    # PUSH 5 words into the 4-word FIFO of thread 0 (no program pulls): the 5th is dropped.
    await host.push(0, [1, 2, 3, 4, 5])
    st = await host.read_status()
    assert st["in_cnt"][0] == 4 and st["push_drop"] == 1 and not st["cmd_err"], f"PUSH 5 words: {st}"
    await host.stop()
    st = await host.read_status()
    assert st["running"] == 0 and st["in_cnt"] == [0, 0, 0, 0], f"after STOP: {st}"
    assert await host.read_mem(5, 1) == [0x1234], "WRITE_MEM while running changed the memory"
    await host.clear()
    assert await host.read_status() == hs.parse_status([0] * hs.STATUS_BYTES)
    # The only change is the reset level at time 0 (X to 1).
    assert mon.after(0) == [], f"UART_TX changed with no program: {mon.after(0)}"
    log.info("command rules: refused commands set CMD_ERR and change nothing; the 5th PUSH word sets PUSH_DROP")


@cocotb.test()
async def test_host_load_readback(dut):
    """WRITE_MEM of 128 random words, READ_MEM in one transaction and with the address wrap 127 -> 0."""
    _, host = await start(dut)
    rng = random.Random(2610)
    words = [rng.randrange(0x10000) for _ in range(128)]
    await host.write_mem(0, words)
    back = await host.read_mem(0, 128)
    assert back == words, f"read-back differs at {[a for a in range(128) if back[a] != words[a]][:8]}"
    assert await host.read_mem(126, 4) == words[126:] + words[:2], "address wrap 127 -> 0"
    # Two words written across the wrap.
    await host.write_mem(127, [0xA5A5, 0x5A5A])
    assert await host.read_mem(127, 2) == [0xA5A5, 0x5A5A]
    dut._log.info("128 words written and read back; the address wraps from 127 to 0")


@cocotb.test()
async def test_host_sck_limit(dut):
    """READ_ID and READ_MEM at SCK periods of 100, 90 and 80 ns with the MISO allowance (docs/host_port.md).

    At 100 ns (10 MHz) the read must be correct. The shorter periods show where
    the 3-cycle MISO delay plus the 27.4 ns allowance no longer fits (logged).
    """
    _, host = await start(dut)
    await host.write_mem(0, [0x8001, 0x7FFE, 0xC3C3, 0x3C3C])
    results = {}
    for period in (100, 90, 80):
        h = hs.SpiTimed(dut, period_ns=period)
        ok = (await h.read_id()) == hs.CHIP_ID and (await h.read_mem(0, 4)) == [0x8001, 0x7FFE, 0xC3C3, 0x3C3C]
        results[period] = ok
    dut._log.info(f"SCK period (ns) -> READ_ID and READ_MEM correct with a 27.4 ns MISO allowance: {results}")
    assert results[100], "10 MHz host SCK does not work"


@cocotb.test()
async def test_stop_start(dut):
    """STOP in a frame releases UART_TX at once; START after STOP runs the program as after a reset."""
    mon, host = await start(dut)
    await load_uart_tx(dut, host)
    rx = UartRx(dut.uart_tx)
    await send_bytes(host, [0x00])
    await FallingEdge(dut.uart_tx)
    await Timer(int(4.5 * T_DUT_PS), unit="ps")
    assert str(dut.uart_tx.value) == "0", "UART_TX is not low inside the 0x00 frame"
    t_stop = now_ps()
    await host.stop()
    st = await host.read_status()
    assert st["running"] == 0 and st["run"] == [0, 0, 0, 0], f"status after STOP: {st}"
    changes = [c for c in mon.changes if c[0] >= t_stop]
    assert len(changes) == 1 and changes[0][1] == "1", f"UART_TX after STOP: {changes}"
    dut._log.info(f"STOP: UART_TX high {(changes[0][0] - t_stop) / 1000:.1f} ns after the STOP transaction began")
    await Timer(3 * FRAME_BITS * T_DUT_PS, unit="ps")
    assert [c for c in mon.changes if c[0] > changes[0][0]] == [], "UART_TX changed while the threads stop"
    # START again with the same memory and configuration: frames as after a reset.
    cocotb.start_soon(rx.run())
    t_restart = now_ps()
    await host.start()
    data = [0x55, 0xAA, 0x0F]
    await send_bytes(host, data)
    await Timer(5 * FRAME_BITS * T_DUT_PS, unit="ps")
    mon_after = LineMonitor(dut.uart_tx)
    mon_after.changes = [c for c in mon.changes if c[0] > t_restart]
    check_stream(dut._log, mon_after, rx, data, "after STOP and START", back_to_back=True)

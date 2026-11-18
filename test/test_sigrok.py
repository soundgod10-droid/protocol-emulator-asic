# SPDX-License-Identifier: Apache-2.0
"""Scenarios for the outside oracle: the protocol programs of fw/ on the chip, decoded by sigrok.

What it does:
  Each test runs one protocol program on the chip at 60 MHz against its independent protocol model, with the
  host on the SPI port and the reference model in lockstep (test_rtl_fw.RtlBench), and asserts the model's checks
  as test_rtl_fw.py does. It also writes one record per scenario to scenario.json (or $SIGROK_SCENARIO): the time
  window in the simulation, the chip lines, the sigrok decoder stack, and the content that the scenario's own data
  gives, as texts in each decoder's output format at libsigrokdecode 71f4514 (the format strings of its pd.py,
  cited per scenario). flex/sigrok_judge.py then decodes the pin dump of the same run (make PIN_VCD=1 FST=) with
  sigrok-cli and compares (.github/workflows/sigrok.yaml).

Why:
  flex/PREREG.md section 6 item 3 and docs/counting.md: an outside decoder, written by other people from the
  standards, must read the scenario's content from the chip pins with no warning or error.

Conditions: RTL simulation (Icarus Verilog) at 60 MHz, ideal pins; at gate level the same tests run on the netlist
(functional cell models, no delays).
"""

import json
import os

import cocotb
from cocotb.utils import get_sim_time

import test_flex as fx
import test_fw_full as ff
import test_holdout as ho
import test_rtl_fw as rf

OUT = os.environ.get("SIGROK_SCENARIO", "scenario.json")
LEVEL = "gate level" if os.environ.get("GATES") == "yes" else "RTL"
_scenarios = []


def now_ps():
    return int(get_sim_time(unit="ps"))


def record(test, name, lines, stack, content, t0, t1, tamper, note):
    """Adds one scenario and rewrites the scenario file (each test of the module adds its records)."""
    _scenarios.append(dict(test=test, name=name, lines=lines, stack=stack, content=content, window_ps=[t0, t1],
                           tamper=tamper, note=note))
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"level": LEVEL, "commit": os.environ.get("GITHUB_SHA", ""), "clock_ps": rf.CLK_PS,
                   "scenarios": _scenarios}, fh, indent=1)


def hx(values):
    return [f"{v:02X}" for v in values]


# ----- UART -----
# uart/pd.py: annotation class rx-data, text format "{:02X}" for the default format "hex".


@cocotb.test()
async def test_sigrok_uart_tx(dut):
    """fw/uart_tx_full.s at 115,200 bit/s 8N1 (B 521 at 60 MHz), 6 back-to-back frames, check_tx on the chip pin."""
    rf.start_clock(dut)
    rate, data = 115_200, [0x55, 0x00, 0xFF, 0xA5, 0x3C, 0x81]
    k, c, b = ff.uart_cfg(rf.F_CLK, rate)
    label = f"sigrok uart_tx_full {rate} bit/s 8N1"
    bench = rf.RtlBench(dut, ff.assemble("uart_tx_full.s").words, owner=ff.owners({ff.P_TX: 0}), label=label)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    frames = [ff.frame_bits(d, "8N1") for d in data]
    await bench.run(100)
    t0 = now_ps()
    bench.send(0, c, k, *[ff.frame_word(f) for f in frames])
    await bench.run(rf.HOST_START + (len(frames) * 10 + 4) * b)
    t1 = now_ps()
    ff.check_tx(dut._log, bench.changes("tx"), b, rf.F_CLK / rate, frames, label)
    await bench.finish(dut._log)
    record("test_sigrok_uart_tx", "uart_tx", [["uart_tx", "uo:4"]], f"uart:rx=uart_tx:baudrate={rate}",
           {"uart:rx-data": hx(data)}, t0, t1, "uart_tx", "chip UART TX (OUT12) at 115,200 bit/s 8N1, 6 frames")


@cocotb.test()
async def test_sigrok_uart_rx(dut):
    """fw/uart_rx_full.s at 115,200 bit/s 8N1, a sender at the nominal bit time, 6 frames; the host words hold
    the sent bits (test_rtl_fw.rtl_rx without the glitch)."""
    rf.start_clock(dut)
    rate, data = 115_200, [0x0F, 0xF0, 0x5A, 0xC3, 0x00, 0x7E]
    _, _, b = ff.uart_cfg(rf.F_CLK, rate)
    b_tx = rf.F_CLK / rate
    frames, t = [], rf.HOST_START
    for d in data:
        bits = ff.frame_bits(d, "8N1")
        frames.append((t, bits))
        t += round(len(bits) * b_tx)
    line = ff.uart_line(frames, b_tx)
    label = f"sigrok uart_rx_full {rate} bit/s 8N1"
    bench = rf.RtlBench(dut, ff.assemble("uart_rx_full.s", ND1=9).words,
                        inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << ff.UI_RX, 0), label=label)
    bench.send(0, b, ff.rx_h(b))
    await bench.run(100)
    t0 = now_ps()
    await bench.run(t + 3 * b - 100)
    await bench.collect(0, len(frames))
    t1 = now_ps()
    words = bench.words(0)
    assert len(words) == len(frames) and all(ff.rx_bits(w, 9) == bits[:10] for w, (_, bits) in zip(words, frames)), (
        f"{label}: words {[hex(w) for w in words]}")
    await bench.finish(dut._log)
    record("test_sigrok_uart_rx", "uart_rx", [["uart_rx", "ui:3"]], f"uart:rx=uart_rx:baudrate={rate}",
           {"uart:rx-data": hx(data)}, t0, t1, "uart_rx",
           "UART sender model on IN11 (ui_in[3]) at 115,200 bit/s 8N1, 6 frames, received by the chip")


# ----- SPI -----
# spi/pd.py: annotation classes mosi-data and miso-data, text "{:02X}" (8-bit words, MSB first).

SPI_CTL_LINES = [["spi_cs", "drvh:0"], ["spi_sck", "drvl:3"], ["spi_mosi", "drvl:1"], ["spi_miso", "in:2"]]
SPI_TGT_LINES = [["spi_cs", "in:0"], ["spi_sck", "in:3"], ["spi_mosi", "in:1"], ["spi_miso", "drvh:2"]]


@cocotb.test()
async def test_sigrok_spi_ctl(dut):
    """fw/spi_ctl.s mode 0 and mode 3 (D 0) at the smallest half period against the SPI device model."""
    rf.start_clock(dut)
    for cpol, cpha in ((0, 0), (1, 1)):
        h = ff.spi_h_min(cpol, cpha, 0)
        cl = cpol if cpha == 0 else 1 - cpol
        dev = ff.SpiDevice(cpol, cpha, ff.SPI_DEV)
        label = f"sigrok spi_ctl CPOL {cpol} CPHA {cpha} H {h}"
        bench = rf.RtlBench(dut, ff.assemble("spi_ctl.s", CPOL=cpol, CL=cl, D=0).words,
                            owner=ff.owners({ff.P_CS: 0, ff.P_MOSI: 0, ff.P_SCK: 0}), inputs=dev, label=label)
        await bench.run(100)
        t0 = now_ps()
        bench.send(0, h, *ff.SPI_WORDS)
        await bench.run(rf.HOST_START + len(ff.SPI_WORDS) * (20 * h + 120) + 2000)
        await bench.collect(0, len(ff.SPI_WORDS))
        t1 = now_ps()
        fault = ff.spi_fault(bench, bench.m, dev)
        assert fault is None, f"{label}: {fault}"
        await bench.finish(dut._log)
        mosi = [w & 0xFF for w in ff.SPI_WORDS]
        record("test_sigrok_spi_ctl", f"spi_ctl_mode{2 * cpol + cpha}", SPI_CTL_LINES,
               f"spi:clk=spi_sck:mosi=spi_mosi:miso=spi_miso:cs=spi_cs:cpol={cpol}:cpha={cpha}",
               {"spi:mosi-data": hx(mosi), "spi:miso-data": hx(ff.SPI_DEV)}, t0, t1, "spi_mosi",
               f"chip SPI controller mode {2 * cpol + cpha}, 4 bytes each way; undriven CS read high, SCK and MOSI "
               "read low (board resistors)")


@cocotb.test()
async def test_sigrok_spi_tgt(dut):
    """fw/spi_tgt.s mode 0 and mode 3 at 1 MHz against the SPI controller model (test_rtl_fw.test_rtl_spi_tgt)."""
    rf.start_clock(dut)
    for mode in (0, 3):
        ctl = ff.SpiController(mode, 30, [mosi for mosi, _ in ff.SPI_TGT_XFERS], lead=30, gap=3000,
                               t0=rf.HOST_START + 1000)
        label = f"sigrok spi_tgt mode {mode}"
        bench = rf.RtlBench(dut, rf.pe_asm.assemble(ff.spi_tgt_text(mode), "spi_tgt.s", 0).words,
                            owner=ff.owners({ff.P_MISO: 0}), inputs=ctl, label=label)
        for _, miso in ff.SPI_TGT_XFERS:
            bench.send(0, *[byte << 2 for byte in miso], 0)
        await bench.run(rf.HOST_START // 2)
        t0 = now_ps()
        await bench.run(ctl.end + 1000 - rf.HOST_START // 2)
        await bench.collect(0, sum(len(mosi) for mosi, _ in ff.SPI_TGT_XFERS))
        t1 = now_ps()
        fault = ff.spi_tgt_fault(bench, bench.m, ctl)
        assert fault is None, f"{label}: {fault}"
        await bench.finish(dut._log)
        mosi = [b for x, _ in ff.SPI_TGT_XFERS for b in x]
        miso = [b for _, x in ff.SPI_TGT_XFERS for b in x]
        cpol, cpha = mode >> 1, mode & 1
        record("test_sigrok_spi_tgt", f"spi_tgt_mode{mode}", SPI_TGT_LINES,
               f"spi:clk=spi_sck:mosi=spi_mosi:miso=spi_miso:cs=spi_cs:cpol={cpol}:cpha={cpha}",
               {"spi:miso-data": hx(miso), "spi:mosi-data": hx(mosi)}, t0, t1, "spi_miso",
               f"SPI controller model, chip as target, mode {mode}, 1 MHz, 2 transfers; undriven MISO read high")


# ----- I2C -----
# i2c/pd.py: classes start ("Start"), repeat-start ("Start repeat"), stop ("Stop"), ack ("ACK"), nack ("NACK"),
# address-read/-write ("Address read: {b:02X}", 7-bit address with the default address_format "shifted"),
# data-read/-write ("Data read: {b:02X}", "Data write: {b:02X}").
I2C_CLASSES = "i2c:start|repeat-start|stop|ack|nack|address-read|address-write|data-read|data-write"
I2C_LINES = [["i2c_scl", "in:6"], ["i2c_sda", "in:7"]]


def i2c_texts(events):
    """sigrok texts of test_fw_full.i2c_script() events: a START after a byte without STOP is a repeated START."""
    out, in_xfer = [], False
    for e in events:
        if e == "START":
            out.append("Start repeat" if in_xfer else "Start")
            in_xfer = True
        elif e == "STOP":
            out.append("Stop")
            in_xfer = False
        elif e[0] == "ADDR":
            _, addr, rd, ack = e
            out += [f"Address {'read' if rd else 'write'}: {addr:02X}", "ACK" if ack else "NACK"]
        elif e[0] == "W":
            out += [f"Data write: {e[1]:02X}", "ACK"]
        elif e[0] == "R":
            out += [f"Data read: {e[1]:02X}", "NACK" if e[2] else "ACK"]
    return out


@cocotb.test()
async def test_sigrok_i2c_ctl(dut):
    """fw/i2c_ctl.s Fast-mode and Standard-mode without stretching against the I2C target model: write, repeated
    START, read with ACK and NACK, STOP, NACKed address (test_rtl_fw.test_rtl_i2c)."""
    rf.start_clock(dut)
    words, replies, events = ff.i2c_script()
    for mode in ("Fm", "Sm"):
        label = f"sigrok i2c_ctl {mode}"
        bench, bus = rf.i2c_bench(dut, mode, 0, label)
        await bench.run(100)
        t0 = now_ps()
        rf.send_pairs(bench, words)
        await bench.run(rf.HOST_START + (36_000 if mode == "Fm" else 100_000))
        await bench.collect(0, len(replies))
        t1 = now_ps()
        assert bench.words(0) == replies, f"{label}: replies {[hex(w) for w in bench.words(0)]}"
        assert ff.i2c_events(bus) == events, f"{label}: bus events {ff.i2c_events(bus)}"
        ff.check_i2c_times(dut._log, bus.changes, mode, rf.F_MHZ, label)
        await bench.finish(dut._log)
        record("test_sigrok_i2c_ctl", f"i2c_ctl_{mode.lower()}", I2C_LINES, "i2c:scl=i2c_scl:sda=i2c_sda",
               {I2C_CLASSES: i2c_texts(events)}, t0, t1, "i2c_sda",
               f"chip I2C controller {mode} with the target model: bus levels (open drain, wired AND)")

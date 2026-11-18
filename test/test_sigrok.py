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


def record(test, name, lines, stack, content, t0, t1, tamper, note, forbid=()):
    """Adds one scenario and rewrites the scenario file (each test of the module adds its records). forbid: classes
    that a decoder uses for errors only."""
    _scenarios.append(dict(test=test, name=name, lines=lines, stack=stack, content=content, window_ps=[t0, t1],
                           tamper=tamper, note=note, forbid=list(forbid)))
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


# ----- PS/2 -----
# ps2/pd.py: it samples DATA at the falling CLK edges (device-to-host frames only) and gives class word
# "Data: {:02x}" and class parity-ok "Parity OK" (parity-err "Parity error" is a failure class).
PS2_LINES = [["ps2_clk", f"in:{fx.P_PCLK}"], ["ps2_data", f"in:{fx.P_PDATA}"]]


def ps2_texts(values):
    return [t for b in values for t in (f"Data: {b:02x}", "Parity OK")]


@cocotb.test()
async def test_sigrok_ps2_host(dut):
    """fw/ps2_host.s receives 3 device frames from the PS/2 device model at 12.5 kHz (test_rtl_fw.test_rtl_ps2,
    first part, without the host command: sigrok's ps2 decoder reads device-to-host frames only)."""
    rf.start_clock(dut)
    frames = fx.PS2_BYTES[:3]
    dev = fx.Ps2Device(rf.PS2_PERIOD, frames, gap=3600, rts_delay=600, start=rf.HOST_START + 1000)
    label = "sigrok ps2_host.s 12.5 kHz device clock"
    bench = rf.RtlBench(dut, fx.assemble("ps2_host.s", LATE="st").words, owner=ff.owners({fx.P_PCLK: 0, fx.P_PDATA: 0}),
                        od=fx.od((fx.P_PCLK, fx.P_PDATA)), inputs=fx.Ps2Bus(dev), label=label)
    await bench.run(100)
    t0 = now_ps()
    await bench.run(rf.HOST_START + 1000 + len(frames) * (12 * rf.PS2_PERIOD + 3800) - 100)
    t1 = now_ps()
    got = [fx.ps2_decode(fx.ps2_word_bits(w)) if w >= 32 else (None, False) for w in bench.words(0)]
    assert got == [(b, True) for b in frames], f"{label}: received {got}, device sent {dev.sent}"
    assert not dev.errors, f"{label}: device errors {dev.errors[:2]}"
    await bench.finish(dut._log)
    record("test_sigrok_ps2_host", "ps2_host", PS2_LINES, "ps2:clk=ps2_clk:data=ps2_data",
           {"ps2:word|parity-ok": ps2_texts(frames)}, t0, t1, "ps2_data",
           "PS/2 device model sends 3 frames to the chip (host), bus levels of CLK and DATA (open drain)")


@cocotb.test()
async def test_sigrok_ps2_dev(dut):
    """fw/ps2_dev.s (12.5 kHz) sends 5 frames to the PS/2 system model, one frame per host word, with no inhibit
    (test_rtl_fw.test_rtl_ps2, second part, without the inhibited frame and the system command)."""
    rf.start_clock(dut)
    host = fx.Ps2Host(t_rts=4800, tchg=120, inhibit_frame=-1)
    label = "sigrok ps2_dev.s 12.5 kHz"
    bench = rf.RtlBench(dut, fx.assemble("ps2_dev.s", **rf.PS2_DEV_EQU).words,
                        owner=ff.owners({fx.P_PCLK: 0, fx.P_PDATA: 0}), od=fx.od((fx.P_PCLK, fx.P_PDATA)),
                        inputs=fx.Ps2Bus(host), label=label)
    await bench.run(200)
    t0 = now_ps()
    sent = (0xAA, 0xFA, 0x12, 0xF0, 0x12)
    for b in sent:
        n0 = len(bench.words(0))
        bench.send(0, sum(bit << i for i, bit in enumerate(fx.ps2_frame(b))))
        for _ in range(100):
            await bench.run(2400)
            if len(bench.words(0)) > n0:
                break
        assert bench.words(0)[n0:] == [0], f"{label}: frame 0x{b:02X} status {bench.words(0)[n0:]}"
    await bench.run(4800)
    t1 = now_ps()
    assert host.frames == [(b, True) for b in sent], f"{label}: system received {host.frames}"
    await bench.finish(dut._log)
    record("test_sigrok_ps2_dev", "ps2_dev", PS2_LINES, "ps2:clk=ps2_clk:data=ps2_data",
           {"ps2:word|parity-ok": ps2_texts(sent)}, t0, t1, "ps2_data",
           "chip as PS/2 device sends 5 frames to the system model, bus levels of CLK and DATA (open drain)")


# ----- CAN -----
# can/pd.py: classes id "Identifier: {id} (0x{id:x})", rtr "Remote transmission request: data frame",
# dlc "Data length code: {n}", data "Data byte {i}: 0x{b:02x}", crc-sequence "CRC-15 sequence: 0x{crc:04x}",
# ack-slot "ACK slot: ACK".
CAN_CLASSES = "can:id|rtr|dlc|data|crc-sequence|ack-slot"


def can_texts(frames):
    out = []
    for f in frames:
        bits = fx.can_bits(**f)
        ident, data = f["ident"], f.get("data", [])
        out += [f"Identifier: {ident} (0x{ident:x})", "Remote transmission request: data frame",
                f"Data length code: {len(data)}"]
        out += [f"Data byte {i}: 0x{b:02x}" for i, b in enumerate(data)]
        out += [f"CRC-15 sequence: 0x{fx.bits_value(bits[-15:]):04x}", "ACK slot: ACK"]
    return out


@cocotb.test()
async def test_sigrok_can(dut):
    """fw/can.s at 250 kbit/s sends 4 data frames (stuffing in identifier and data, DLC 0 to 8) to the CAN node model
    at +0.5 % clock with a 10-cycle loop delay; the node acknowledges each (test_rtl_fw.test_rtl_can, first part,
    without the remote frame: sigrok's can decoder reads DLC data bytes after a remote frame too)."""
    rf.start_clock(dut)
    bt = round(rf.F_CLK / 250_000)
    sp = round(0.55 * bt)
    frames = [f for f in fx.CAN_FRAMES if not f.get("rtr")]
    label = f"sigrok can 250 kbit/s BT {bt} SP {sp}"
    bench, node = rf.can_bench(dut, bt, sp, node_ppm=5000, delay=10, label=label)
    await bench.run(rf.HOST_START)
    t0 = now_ps()
    sent = await rf.can_send_all(bench, frames, bt)
    await bench.run(int(20 * bt))
    t1 = now_ps()
    fault = fx.can_fault(bench, bench.m, node, sent, frames)
    assert fault is None, f"{label}: {fault}"
    await bench.finish(dut._log)
    record("test_sigrok_can", "can", [["can_rx", "ui:4"]], "can:can_rx=can_rx:nominal_bitrate=250000:sample_point=75",
           {CAN_CLASSES: can_texts(frames)}, t0, t1, "can_rx",
           "chip sends 4 data frames, CAN_RX (IN12) = the bus as the transceiver returns it, 10 cycles loop delay")


# ----- AM2302 -----
# am230x/pd.py: classes humidity "Humidity: {h:.1f} %", temperature "Temperature: {t:.1f} °C", checksum
# "Checksum: OK". Its bit windows (bit 1 high 65 to 80 us) are the data sheet's typical times, so the sensor
# model runs at the data sheet times (scale 1.0), not at the x 1.2 corner of test_rtl_fw.test_rtl_am2302.


@cocotb.test()
async def test_sigrok_am2302(dut):
    """fw/am2302.s reads both data sheet examples (65.2 % / 35.1 C and 40.0 % / -10.1 C) from the AM2302 sensor
    model at the data sheet times."""
    rf.start_clock(dut)
    prog = ho.assemble("am2302.s", **ho.AM_EQU[rf.F_MHZ])
    for k, (hum, tmp) in enumerate(ho.AM_DATA):
        dev = ho.Am2302Device(rf.F_MHZ, hum, tmp, scale=1.0, t_go=30)
        label = f"sigrok am2302.s example {k}"
        bench = rf.RtlBench(dut, prog.words, owner=ff.owners({ho.P_AM: 0}), od=ff.open_drain({ho.P_AM}), inputs=dev,
                            label=label)
        await bench.run(100)
        t0 = now_ps()
        bench.send(0, 1)
        for _ in range(12000 * rf.F_MHZ // 10_000 + 2):
            await bench.run(10_000)
            got = bench.words(0)
            if got and (got[0] != 0 or len(got) == 4):
                break
        await bench.run(1000)
        t1 = now_ps()
        assert bench.words(0) == [0, hum, tmp, ho.am_sum(hum, tmp)], f"{label}: replies {bench.words(0)}"
        await bench.finish(dut._log)
        t = (tmp & 0x7FFF) / 10 * (-1 if tmp & 0x8000 else 1)
        record("test_sigrok_am2302", f"am2302_{k}", [["am_sda", f"in:{ho.P_AM}"]], "am230x:sda=am_sda",
               {"am230x:humidity|temperature|checksum": [f"Humidity: {hum / 10:.1f} %", f"Temperature: {t:.1f} °C",
                                                          "Checksum: OK"]}, t0, t1, "am_sda",
               f"chip reads the AM2302 model, data sheet example {k}, bus level of the data line (open drain)")

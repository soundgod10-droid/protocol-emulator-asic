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

import test_dmx512 as dx
import test_flex as fx
import test_fw_full as ff
import test_holdout as ho
import test_i2c_tgt as it
import test_lin as ln
import test_rtl_fw as rf
import test_sbus as sb
import test_usb_dev as ud

OUT = os.environ.get("SIGROK_SCENARIO", "scenario.json")
LEVEL = "gate level" if os.environ.get("GATES") == "yes" else "RTL"
_scenarios = []


def now_ps():
    return int(get_sim_time(unit="ps"))


def record(test, name, lines, stack, content, t0, t1, tamper, note, forbid=(), allow=None):
    """Adds one scenario and rewrites the scenario file (each test of the module adds its records). forbid: classes
    that a decoder uses for errors only. allow: classes that the judge's fault pattern matches but that are part of
    the protocol, each with the annotation class inside whose span it is allowed (flex/sigrok_judge.py allowed)."""
    _scenarios.append(dict(test=test, name=name, lines=lines, stack=stack, content=content, window_ps=[t0, t1],
                           tamper=tamper, note=note, forbid=list(forbid), allow=dict(allow or {})))
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
# address-read/-write ("Address read: {b:02X}", 7-bit address with the default address_format "shifted", and before
# it the R/W bit "Read" or "Write" in the same class),
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
            out += ["Read" if rd else "Write", f"Address {'read' if rd else 'write'}: {addr:02X}",
                    "ACK" if ack else "NACK"]
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


# ----- I2C target -----
# The 24C02-type transfers of test_i2c_tgt.SCRIPT that a 24xx EEPROM decoder reads without a warning: a page write
# at 0x10, a sequential random read of it, a current-address read (the NACKed other address and the write that
# ends with a repeated START are left out: eeprom24xx/pd.py gives "Warning: No reply from slave!" and does not
# take a write ended by a repeated START). eeprom24xx/pd.py (chip generic: 8-byte pages, 1 address byte): classes
# page-write "Page write (addr={a:02X}, {n} bytes): {bytes}", seq-random-read "Sequential random read (addr=...,
# ...): ...", cur-addr-read "Current address read: {b:02X}".
I2C_TGT_SCRIPT = ([it.S, it.w(it.ADDR << 1), it.w(0x10)] + [it.w(b) for b in it.DATA] + [it.P]
                  + [it.S, it.w(it.ADDR << 1), it.w(0x10), it.S, it.w(it.ADDR << 1 | 1)] + [it.r(True)] * 4
                  + [it.r(False), it.P] + [it.S, it.w(it.ADDR << 1 | 1), it.r(False), it.P])


def i2c_script_texts(script, results):
    """i2c texts of a controller script and its results: the byte after a START is the address byte."""
    out, res, prev, in_xfer = [], iter(results), None, False
    for op in script:
        if op[0] == "start":
            out.append("Start repeat" if in_xfer else "Start")
            in_xfer = True
        elif op[0] == "stop":
            out.append("Stop")
            in_xfer = False
        elif op[0] == "w":
            _, byte, nack = next(res)
            if prev == "start":
                out += ["Read" if byte & 1 else "Write", f"Address {'read' if byte & 1 else 'write'}: {byte >> 1:02X}"]
            else:
                out.append(f"Data write: {byte:02X}")
            out.append("NACK" if nack else "ACK")
        else:
            _, byte = next(res)
            out += [f"Data read: {byte:02X}", "ACK" if op[1] else "NACK"]
        prev = op[0]
    return out


@cocotb.test()
async def test_sigrok_i2c_tgt(dut):
    """fw/i2c_tgt.s in Fast-mode (tLOW 1.3 us, tHIGH 1.2 us, with clock stretching) against the I2C controller model
    of test_i2c_tgt.py: page write, sequential random read, current-address read."""
    rf.start_clock(dut)
    want = ([("w", op[1], 0) for op in I2C_TGT_SCRIPT if op[0] == "w"])
    reads = iter(it.DATA + [0x00])
    exp = [("w", op[1], 0) if op[0] == "w" else ("r", next(reads)) for op in I2C_TGT_SCRIPT if op[0] in ("w", "r")]
    ctl = it.I2cController(rf.F_MHZ, "Fm", 1.3, 1.2, hd=1, start=rf.HOST_START + it.START, script=I2C_TGT_SCRIPT)
    label = "sigrok i2c_tgt Fm"
    bench = rf.RtlBench(dut, it.assemble().words, owner=ff.owners({it.P_SCL: 0, it.P_SDA: 0}),
                        od=ff.open_drain({it.P_SCL, it.P_SDA}), inputs=ctl, label=label)
    await bench.run(rf.HOST_START + it.START - 500)
    t0 = now_ps()
    for _ in range(400):
        if ctl.done:
            break
        await bench.run(5000)
    await bench.run(1000)
    t1 = now_ps()
    assert ctl.done and not ctl.errors, f"{label}: done {ctl.done}, errors {ctl.errors[:3]}"
    assert ctl.results == exp and len(want) == sum(1 for r in exp if r[0] == "w"), f"{label}: results {ctl.results}"
    await bench.finish(dut._log)
    data = " ".join(f"{b:02X}" for b in it.DATA)
    record("test_sigrok_i2c_tgt", "i2c_tgt_fm", I2C_LINES, "i2c:scl=i2c_scl:sda=i2c_sda,eeprom24xx:chip=generic",
           {I2C_CLASSES: i2c_script_texts(I2C_TGT_SCRIPT, exp),
            "eeprom24xx:page-write|seq-random-read|cur-addr-read": [
                f"Page write (addr=10, 5 bytes): {data}", f"Sequential random read (addr=10, 5 bytes): {data}",
                "Current address read: 00"]}, t0, t1, "i2c_sda",
           "I2C controller model (UM10204 Fm corner), chip as a 24C02-type target with clock stretching; bus levels")


# ----- 1-Wire -----
# onewire_network/pd.py on onewire_link: class text "Reset/presence: true", "ROM command: 0x{c:02x} '{name}'"
# (0x33 'Read ROM', 0xcc 'Skip ROM'), "ROM: 0x{rom:016x}" (64 bits, first bit = bit 0), "Data: 0x{b:02x}".


async def onewire_case(dut, test, name, t_rd, note):
    """Read ROM, Write Scratchpad, Read Scratchpad with a device that holds a read 0 for t_rd us."""
    rf.start_clock(dut)
    dev = ho.OneWireDevice(rf.F_MHZ, t_rd=t_rd)
    label = f"sigrok onewire.s, read 0 held {t_rd} us"
    bench = rf.RtlBench(dut, ho.assemble("onewire.s", **ho.OW_EQU[rf.F_MHZ]).words, owner=ff.owners({ho.P_OW: 0}),
                        od=ff.open_drain({ho.P_OW}), inputs=dev, label=label)
    await bench.run(100)
    t0 = now_ps()
    bench.send(0, *ho.OW_SCRIPT)
    resets = sum(1 for w in ho.OW_SCRIPT if w & 0x100)
    await bench.run(rf.HOST_START + resets * 1000 * rf.F_MHZ + (len(ho.OW_SCRIPT) - resets) * 8 * 72 * rf.F_MHZ)
    await bench.collect(0, len(ho.ow_expected()))
    t1 = now_ps()
    assert bench.words(0) == ho.ow_expected(), f"{label}: replies {[hex(w) for w in bench.words(0)]}"
    await bench.finish(dut._log)
    rom = sum(b << (8 * k) for k, b in enumerate(ho.ROM))
    scratch = list(ho.SCRATCH)
    scratch[2:5] = [0x19, 0xE6, 0x3F]
    texts = (["Reset/presence: true", "ROM command: 0x33 'Read ROM'", f"ROM: 0x{rom:016x}",
              "Reset/presence: true", "ROM command: 0xcc 'Skip ROM'"] + [f"Data: 0x{b:02x}" for b in (0x4E, 0x19, 0xE6, 0x3F)]
             + ["Reset/presence: true", "ROM command: 0xcc 'Skip ROM'", "Data: 0xbe"] + [f"Data: 0x{b:02x}" for b in scratch])
    record(test, name, [["ow_bus", f"in:{ho.P_OW}"]], "onewire_link:owr=ow_bus,onewire_network",
           {"onewire_network:text": texts}, t0, t1, "ow_bus", note)


@cocotb.test()
async def test_sigrok_onewire(dut):
    """fw/onewire.s against the DS18B20 device model at its default times: Read ROM, Write Scratchpad, Read
    Scratchpad (test_rtl_fw.test_rtl_onewire with the device's typical times)."""
    await onewire_case(dut, "test_sigrok_onewire", "onewire", 15,
                       "chip as 1-Wire controller with the DS18B20 model: bus level (open drain)")


@cocotb.test()
async def test_sigrok_onewire_hold30(dut):
    """The same transfers with a device that holds a read 0 for 30 us (a case of test_holdout.test_holdout_onewire).
    The data sheet (page 16) gives 15 us as the time the data is valid at least; the device releases the bus by the
    end of the 60 us slot. 15 us is also the limit of onewire_link/pd.py (LOWR max), so in a capture of the first
    scenario the sample phase decides a read 0; 30 us is away from the limit (bench/judges/capture.py)."""
    await onewire_case(dut, "test_sigrok_onewire_hold30", "onewire_hold30", 30,
                       "chip as 1-Wire controller with the DS18B20 model, read 0 held 30 us: bus level (open drain)")


# ----- JTAG -----
# jtag/pd.py: at Update-DR/IR, class bitstring-tdi "{IR|DR} TDI: {bits} (0x{v:x}), {n} bits" and bitstring-tdo
# the same with TDO; the bits are written last shifted bit first. fw/jtag_tap.s has no Pause state (a scan through
# Pause-DR gets one more bit in jtag/pd.py, which also takes a bit at Exit1-DR to Pause-DR).


def jtag_texts():
    out = []
    for ops in fx.jtag_ops():
        for cmd, tdi, tdo in ops:
            if cmd == 0:
                continue
            kind = "IR" if cmd & 0x8000 else "DR"
            for name, bits in (("TDI", tdi), ("TDO", tdo)):
                b = "".join(str(x) for x in reversed(bits))
                out.append(f"{kind} {name}: {b} (0x{int('0' + b, 2):x}), {len(bits)} bits")
    return out


@cocotb.test()
async def test_sigrok_jtag(dut):
    """fw/jtag_tap.s (TCK 1.5 MHz) against the two-TAP chain: both IDCODEs, IR scan, a DR write of 0xA53 and its
    read-back, reset and IDCODEs again (test_rtl_fw.rtl_jtag with fw/jtag_tap.s)."""
    rf.start_clock(dut)
    t0 = now_ps()
    await rf.rtl_jtag(dut, "jtag_tap.s", 12, 28)
    t1 = now_ps()
    record("test_sigrok_jtag", "jtag_tap", [["jtag_tck", "uo:0"], ["jtag_tms", "uo:1"], ["jtag_tdi", "uo:2"],
                                            ["jtag_tdo", "ui:0"]],
           "jtag:tdi=jtag_tdi:tdo=jtag_tdo:tck=jtag_tck:tms=jtag_tms", {"jtag:bitstring-tdi|bitstring-tdo": jtag_texts()},
           t0, t1, "jtag_tdi", "chip as JTAG controller (fw/jtag_tap.s) with the two-TAP chain model; TDO from the chain")


# ----- SWD -----
# swd/pd.py: classes reset "LINERESET", enable "JTAG->SWD", read/write "IDCODE", "W SELECT", "W AP4", "R AP4",
# "RDBUFF", ack "OK"/"WAIT", data "0x{v:08x}"; class parity is given only for a parity error (forbidden here).


@cocotb.test()
async def test_sigrok_swd(dut):
    """fw/swd.s (SWCLK 1.875 MHz) against the SW-DP target model: JTAG-to-SWD and line reset, DPIDR read, SELECT
    write, AP write with two WAITs, posted AP read and RDBUFF (test_rtl_fw.test_rtl_swd without the parity-error
    write and the reset-state part)."""
    rf.start_clock(dut)
    h1, h2 = 12, 20
    tgt = fx.SwdTarget()
    label = "sigrok swd.s"
    bench = rf.RtlBench(dut, fx.assemble("swd.s", H1=h1, H2=h2).words, owner=ff.owners({fx.P_SWCLK: 0, fx.P_SWDIO: 0}),
                        inputs=fx.SwdLine(tgt), label=label)
    host = rf.RtlSwdHost(bench, h1 + h2)
    await bench.run(50)
    t0 = now_ps()
    await host.raw(fx.SWD_CONNECT)
    got = await host.xfer(0, 1, 0x0)
    assert got == (0b001, tgt.dpidr, True), f"{label}: DPIDR read {got}"
    assert (await host.xfer(0, 0, 0x8, 0x000000F0))[0] == 0b001, f"{label}: SELECT write"
    tgt.waits = 2
    acks = [(await host.xfer(1, 0, 0x4, 0xCAFEF00D))[0] for _ in range(3)]
    assert acks == [0b010, 0b010, 0b001], f"{label}: ACKs {acks}"
    first = await host.xfer(1, 1, 0x4)
    last = await host.xfer(0, 1, 0xC)
    assert first[0] == 0b001 and first[2] and last == (0b001, 0xCAFEF00D, True), f"{label}: {first}, {last}"
    await bench.run(60 * (h1 + h2))
    t1 = now_ps()
    assert not tgt.errors, f"{label}: target errors {tgt.errors[:3]}"
    await bench.finish(dut._log)
    texts = (["LINERESET", "JTAG->SWD", "LINERESET", "IDCODE", "OK", f"0x{tgt.dpidr:08x}", "W SELECT", "OK",
              "0x000000f0", "W AP4", "WAIT", "W AP4", "WAIT", "W AP4", "OK", "0xcafef00d", "R AP4", "OK",
              f"0x{first[1]:08x}", "RDBUFF", "OK", "0xcafef00d"])
    record("test_sigrok_swd", "swd", [["swd_swclk", f"uo:{fx.P_SWCLK - 8}"], ["swd_swdio", f"in:{fx.P_SWDIO}"]],
           "swd:swclk=swd_swclk:swdio=swd_swdio", {"swd:reset|enable|read|write|ack|data": texts}, t0, t1, "swd_swdio",
           "chip as SWD host with the SW-DP model; SWDIO bus level", forbid=["swd:parity"])


# ----- AVR PDI -----
# avr_pdi/pd.py: classes data-prog and data-dev "Data: 0x{bytes}", a multi-byte item little endian written high
# byte first. The first 17 words of test_holdout.PDI_SCRIPT (before the planted parity error and BREAK): LDCS
# status, KEY with the 8-byte NVM key, LDCS status, STCS ctrl 0x05, LDCS ctrl.


@cocotb.test()
async def test_sigrok_pdi(dut):
    """fw/pdi.s (PDI_CLK 1 MHz) against the XMEGA PDI device model: enable, LDCS, KEY, STCS, LDCS; replies 0x00,
    0x02 (NVMEN), 0x05."""
    rf.start_clock(dut)
    dev = ho.PdiDevice(rf.F_MHZ, t_rst=1.0, d_out=1, undriven=0, mem=ho.PDI_MEM)
    label = "sigrok pdi.s"
    bench = rf.RtlBench(dut, ho.assemble("pdi.s", **ho.PDI_EQU[rf.F_MHZ]).words,
                        owner=ff.owners({ho.P_CLK: 0, ho.P_DAT: 0}), inputs=dev, label=label)
    script = list(ho.PDI_SCRIPT[:17])
    i = 0
    while i < len(script):
        if i + 1 < len(script) and script[i + 1] & 0x8000:
            bench.send_group(0, script[i], script[i + 1])
            i += 2
        else:
            bench.send(0, script[i])
            i += 1
    await bench.run(100)
    t0 = now_ps()
    for _ in range(100):
        await bench.run(5000)
        if len(bench.words(0)) >= 3 and not any(bench.to_thread):
            break
    await bench.run(2000)
    t1 = now_ps()
    assert not dev.faults, f"{label}: {dev.faults}"
    assert ho.pdi_result(bench.words(0)) == [0x00, 0x02, 0x05], f"{label}: replies {bench.words(0)}"
    await bench.finish(dut._log)
    key = [(w >> 1) & 0xFF for w in script[3:11]]
    texts = ["Data: 0x00", "Data: 0x" + "".join(f"{b:02x}" for b in reversed(key)), "Data: 0x02", "Data: 0x05",
             "Data: 0x05"]
    record_pdi(t0, t1, texts)


# ----- USB low speed -----
# usb_packet/pd.py on usb_signalling (low-speed): at the end of each packet a class packet-<pid> with the summary
# "SETUP ADDR {a} EP {e}" or "DATA0 [ {bytes} ]" (" %02X" per byte); SYNC and CRC errors are classes *-err, a short
# packet is packet-invalid (forbidden here). The content is the SETUP token and the data packets: the device may NAK
# an IN before its data (legal), so the handshakes are checked by the host model, not listed here.


@cocotb.test()
async def test_sigrok_usb(dut):
    """fw/usb_dev.s with the USB 2.0 host model of test/linemodel: after a bus reset (20 us, simulation speed) and the
    recovery, GET_DESCRIPTOR device (18 bytes, 3 data packets of the 8-byte low-speed maximum) and the status stage
    (test_usb_dev.py)."""
    host, bus, _, _ = await ud.usb_chip(dut)
    glue = ud.Glue(bus)
    s = ud.H.UsbHostSession()
    gd = ud.C.get_descriptor(ud.C.DT_DEVICE, 0, 18)
    out = {}

    def reset():
        yield from s.reset(ud.RESET_PS, ud.RECOVERY_PS)

    def read():
        out["dev"], out["stall"] = yield from s.control_read(0, gd)
        yield from s.gap(20)
    # usb_signalling/pd.py seeds its state from the first sample of the window, so the window starts before the bus
    # reset: the decoder sees the reset (SE0 longer than 2.5 us) and the idle J before the first SYNC.
    t0 = now_ps()
    await glue.run(reset())
    await glue.run(read())
    t1 = now_ps()
    ud.check_clean(s, bus)
    desc = bytes(ud.usb_dev_data.DEVICE)
    assert out["dev"] == desc and not out["stall"], out
    hexs = lambda b: "".join(f" {x:02X}" for x in b)  # noqa: E731
    texts = (["SETUP ADDR 0 EP 0", f"DATA0 [{hexs(bytes(gd))} ]"]
             + [f"DATA{(k // 8 + 1) % 2} [{hexs(desc[k:k + 8])} ]" for k in range(0, len(desc), 8)] + ["DATA1 [ ]"])
    record("test_sigrok_usb", "usb_ls", [["usb_dp", "ui:1"], ["usb_dm", "ui:2"]],
           "usb_signalling:dp=usb_dp:dm=usb_dm:signalling=low-speed,usb_packet:signalling=low-speed",
           {"usb_packet:packet-setup|packet-data0|packet-data1": texts}, t0, t1, "usb_dp",
           "USB 2.0 host model and the chip as low-speed device: D+ and D- of the bus (VP, VM of the transceiver "
           "model), logic levels only", forbid=["usb_packet:packet-invalid"])


# ----- DMX512 -----
# dmx512/pd.py on uart (250,000 bit/s, 2 stop bits), option format hex: class startcode "STARTCODE {v:02X}" for slot 0
# and databyte "DATABYTE {n}: {v:02X}" for slot n. Its class break is the BREAK of every packet, and the uart decoder
# below it gives rx-break and a frame-error rx-warning for the same low (a frame of zeros without stop bits): these
# are allowed inside the span of a dmx512 break annotation only; dmx512's warning and error classes stay faults.


@cocotb.test()
async def test_sigrok_dmx512(dut):
    """fw/dmx512.s sends four packets (25, 4, 1 and 3 slots; NULL and alternate START Code), checked by the E1.11
    receiver model of test_dmx512.py."""
    rf.start_clock(dut)
    label = "sigrok dmx512"
    bench = rf.RtlBench(dut, dx.assemble().words, owner=ff.owners({dx.P_TX: 0}), label=label)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    await bench.run(100)
    t0 = now_ps()
    bench.send(0, *dx.host_words(dx.PACKETS))
    await bench.run(rf.HOST_START + dx.run_cycles(dx.PACKETS))
    t1 = now_ps()
    await bench.finish(dut._log)
    dx.check(bench.changes("tx"), bench.m.cycle, dx.PACKETS, label)
    texts = []
    for p in dx.PACKETS:
        texts += [f"STARTCODE {p[0]:02X}"] + [f"DATABYTE {n}: {v:02X}" for n, v in enumerate(p[1:], 1)]
    record("test_sigrok_dmx512", "dmx512", [["dmx_tx", "uo:4"]],
           "uart:rx=dmx_tx:baudrate=250000:stop_bits=2.0,dmx512:format=hex", {"dmx512:startcode|databyte": texts},
           t0, t1, "dmx_tx", "chip DMX512 controller (DMX_TX = OUT12, the transceiver's DI), 4 packets",
           allow={"dmx512:break": "", "uart:rx-break": "dmx512:break", "uart:rx-warning": "dmx512:break"})


# ----- S.BUS -----
# sbus_futaba/pd.py on uart (100,000 bit/s, even parity, 2 stop bits, inverted): classes header "0x{:02x}", proportional
# "{:d}" (channels 1 to 16), digital "{:d}" (channels 17, 18), framelost and failsafe "{:d}", footer "0x{:02x}"; it ends
# a message at the uart decoder's IDLE (one frame time of idle line).
SBUS_CLASSES = "sbus_futaba:header|proportional|digital|framelost|failsafe|footer"


@cocotb.test()
async def test_sigrok_sbus(dut):
    """fw/sbus_tx.s sends two frames (values over the 11-bit range, flags 0001 and 1100), checked by the S.BUS receiver
    model of test_sbus.py."""
    rf.start_clock(dut)
    frames = sb.FRAMES[:2]
    label = "sigrok sbus_tx"
    bench = rf.RtlBench(dut, sb.assemble("sbus_tx.s").words, owner=ff.owners({sb.P_TX: 0}), label=label)
    bench.watch("tx", sb.tx_level)
    await bench.run(100)
    t0 = now_ps()
    bench.send(0, *sb.tx_words(frames))
    await bench.run(rf.HOST_START + sb.tx_cycles(frames))
    t1 = now_ps()
    await bench.finish(dut._log)
    sb.tx_check(bench.changes("tx"), bench.m.cycle, frames, label)
    texts = []
    for ch, fl in frames:
        texts += (["0x0f"] + [f"{v:d}" for v in ch] + [f"{(fl >> k) & 1:d}" for k in range(4)] + ["0x00"])
    record("test_sigrok_sbus", "sbus_tx", [["sbus_tx", f"drvl:{sb.P_TX}"]],
           "uart:rx=sbus_tx:baudrate=100000:parity=even:stop_bits=2.0:invert_rx=yes,sbus_futaba",
           {SBUS_CLASSES: texts}, t0, t1, "sbus_tx",
           "chip S.BUS transmitter (SBUS_TX = uio[4]; released pin read low through the board pull-down), 2 frames")


# ----- LIN -----
# lin/pd.py on uart (19,200 bit/s), option version 2 (enhanced checksum except for identifiers 60, 61): at the end of a
# frame (next BREAK or two idle byte times) class data "Sync", "ID: {id:02X} Parity: {p:d} (ok)", "Data: 0x{b:02X}" for
# each data byte, "Checksum: 0x{c:02X}"; class control "Break condition" for the BREAK, and the uart decoder's rx-break
# and frame-error warning for the same low are allowed inside that annotation only; lin's error classes stay faults.


@cocotb.test()
async def test_sigrok_lin(dut):
    """fw/lin_cmd.s runs two frames with the LIN responder node of test_lin.py: one the chip publishes (0x22) and one the
    node publishes (0x10, the data of LIN 2.2A Table 2.5); the bus level (LIN_RX) is decoded."""
    rf.start_clock(dut)
    label = "sigrok lin_cmd"
    sched, words, publish, subscribe, want = ln.cmd_case(short=True)
    node = ln.LinNode("responder", publish=publish, subscribe=subscribe)
    bench = rf.RtlBench(dut, ln.assemble("lin_cmd.s").words, owner=ff.owners({ln.P_TX: 0}), inputs=node, label=label)
    await bench.run(100)
    t0 = now_ps()
    bench.send(0, *words)
    for _ in range(100):
        if len(node.frames) >= len(sched) and len(bench.words(0)) >= 5:
            break
        await bench.run(20_000)
    await bench.run(int(30 * ln.BIT))  # two idle byte times end the last frame in lin/pd.py
    t1 = now_ps()
    await bench.finish(dut._log)
    ln.cmd_check(node, bench.words(0), sched, want, label)
    texts = []
    for fid, kind, x in sched:
        data = list(x) if kind == "pub" else list(publish[fid])
        texts += (["Sync", f"ID: {fid:02X} Parity: {ln.lin_pid(fid) >> 6:d} (ok)"] + [f"Data: 0x{b:02X}" for b in data]
                  + [f"Checksum: 0x{ln.lin_checksum(fid, data):02X}"])
    record("test_sigrok_lin", "lin_cmd", [["lin_bus", f"ui:{ln.UI_RX}"]], "uart:rx=lin_bus:baudrate=19200,lin",
           {"lin:data": texts}, t0, t1, "lin_bus",
           "chip as LIN commander with the responder node; LIN_RX = the bus (wired AND of both nodes), 2 frames",
           allow={"uart:rx-break": "lin:control", "uart:rx-warning": "lin:control"})


def record_pdi(t0, t1, texts):
    record("test_sigrok_pdi", "pdi", [["pdi_clk", f"drvh:{ho.P_CLK}"], ["pdi_data", f"bus:{ho.P_DAT}"]],
           "avr_pdi:reset=pdi_clk:data=pdi_data", {"avr_pdi:data-prog|data-dev": texts}, t0, t1, "pdi_data",
           "chip as PDI programmer with the XMEGA model; PDI_DATA = chip level when driven, device level when released")

# SPDX-License-Identifier: Apache-2.0
"""Waveform samples of the simulator site, from the pin dumps of the protocol tests in test/test_rtl_fw.py and of the
USB and 10BASE-T procedures in test/test_helpers_rtl.py.

Use:
  python -I sim/tools/wave_samples.py tests rtl|gl
      the tests that give samples at that level, as a JSON list (the CI matrix)
  python -I sim/tools/wave_samples.py module TEST
      the test module of TEST
  python -I sim/tools/wave_samples.py trim rtl|gl TEST RAW.vcd OUT_DIR SIMULATOR COMMIT RUN_URL
      writes the samples of TEST at that level and OUT_DIR/part_<level>_<test>.json
  python -I sim/tools/wave_samples.py manifest PARTS_DIR OUT_DIR [--require-gl-equal]
      collects the samples of all parts into OUT_DIR, compares each gate-level dump with the RTL dump of the same test
      value for value, and writes OUT_DIR/manifest.json in the order of SAMPLES

What it does:
  A raw dump is the pin VCD of one whole test (make PIN_VCD=1 FST=, test/tb.v): rst_n, ui_in, uo_out, uio_in, uio_out,
  uio_oe. Each sample is a time window of one configuration of the test with named lines (sim/tools/trim_vcd.py), the
  decoders that the waveform page runs on it, and for each main protocol decoder the number of records ("expect") that
  the test's traffic gives; the decoder check (sim/tools/sigrok_compare.py) holds the decoders to that number. The
  windows are in microseconds of the test (60 MHz, 16,668 ps per cycle), chosen so that each one starts after the bus
  model has set its bus to idle and ends 2 us before the next chip reset of the test.
  The gate-level check compares the whole raw dumps: every pin vector, every change time and value, and the end time.

Why:
  The page replays waveforms of the chip's own tests, of the same commit as the page (gate level: the netlist of the
  newest successful gds run), so that a reader sees the RTL and the layout netlist do what the model does.
"""

import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trim_vcd  # noqa: E402

US = 1_000_000  # ps per us
CLK_PS = trim_vcd.CLK_PS

HOST = {"name": "host", "lines": [["host_sck", "ui:7"], ["host_cs_n", "ui:6"], ["host_mosi", "ui:5"], ["host_miso", "uo:7"]]}
UART = {"name": "uart", "lines": [["uart_tx", "uo:4"]]}
SPI = {"name": "spi", "lines": [["spi_cs", "drv:0"], ["spi_sck", "drv:3"], ["spi_mosi", "drv:1"], ["spi_miso", "in:2"]]}
I2C = {"name": "i2c", "lines": [["i2c_scl", "in:6"], ["i2c_sda", "in:7"]]}
CAN = {"name": "can", "lines": [["can_rx", "ui:4"], ["can_txa", "pull:4"], ["can_txb", "pull:5"]]}
JTAG = {"name": "jtag", "lines": [["jtag_tck", "uo:0"], ["jtag_tms", "uo:1"], ["jtag_tdi", "uo:2"], ["jtag_tdo", "ui:0"]]}
SWD = {"name": "swd", "lines": [["swd_swclk", "uo:3"], ["swd_swdio", "in:4"]]}
PS2 = {"name": "ps2", "lines": [["ps2_clk", "in:4"], ["ps2_data", "in:5"]]}
OW = {"name": "onewire", "lines": [["ow_bus", "in:4"], ["ow_drive", "pull:4"]]}
# USB low-speed bus as the transceiver gives it to the chip (D+ = VP on ui_in[1], D- = VM on ui_in[2], RCV on ui_in[0])
# and the chip's transceiver controls (VPO, VMO, OE#); 10BASE-T transmit pair with OE, receive slicer and squelch.
USB = {"name": "usb", "lines": [["usb_dp", "ui:1"], ["usb_dm", "ui:2"], ["usb_rcv", "ui:0"], ["usb_vpo", "drv:0"],
                                ["usb_vmo", "drv:1"], ["usb_oen", "drv:2"]]}
ETH = {"name": "eth", "lines": [["eth_tdp", "drv:0"], ["eth_tdm", "drv:1"], ["eth_oe", "drv:2"], ["eth_rx", "ui:0"],
                                ["eth_sq", "ui:1"]]}


def dec(name, protocol, pins, options=None, expect=None):
    d = {"name": name, "protocol": protocol, "pins": pins}
    if options:
        d["options"] = options
    if expect is not None:
        d["expect"] = expect
    return d


HOST_DEC = dec("host port", "spi", {"clk": "host_sck", "mosi": "host_mosi", "miso": "host_miso", "cs": "host_cs_n"},
               {"cpol": 0, "cpha": 0})
SPI_PINS = {"clk": "spi_sck", "mosi": "spi_mosi", "miso": "spi_miso", "cs": "spi_cs"}
I2C_DEC = dec("I2C", "i2c", {"scl": "i2c_scl", "sda": "i2c_sda"}, expect=17)
JTAG_DEC = dec("JTAG", "jtag", {"tck": "jtag_tck", "tms": "jtag_tms", "tdi": "jtag_tdi", "tdo": "jtag_tdo"}, expect=5)
SPI0_DEC = dec("SPI", "spi", SPI_PINS, {"cpol": 0, "cpha": 0}, expect=4)


def sample(key, test, title, groups, decoders, window_us, note, timing=None, levels=("rtl",), module="test_rtl_fw"):
    return {"key": key, "test": test, "title": title, "groups": groups, "decoders": decoders,
            "window_ps": [round(window_us[0] * US), None if window_us[1] is None else round(window_us[1] * US)],
            "note": note, "timing": timing, "levels": levels, "module": module}


BOTH = ("rtl", "gl")
SAMPLES = [
    sample("uart_tx_1M", "test_rtl_uart_tx", "UART TX 1 Mbit/s 8N1", [UART, HOST],
           [dec("UART TX", "uart", {"rx": "uart_tx"}, {"baud": 1_000_000}, expect=8), HOST_DEC], (0, 205.0),
           "first configuration: 1,000,000 bit/s 8N1, 8 back-to-back frames, from reset to 2 us before the next reset",
           "UART TX", BOTH),
    sample("spi_ctl_mode0", "test_rtl_spi_ctl", "SPI controller mode 0", [SPI, HOST], [SPI0_DEC, HOST_DEC], (0, 215.3),
           "first configuration: CPOL 0, CPHA 0, D 0, from reset to 2 us before the next reset", None, BOTH),
    sample("i2c_fm", "test_rtl_i2c", "I2C controller Fast-mode", [I2C], [I2C_DEC], (117, 777.5),
           "first configuration, from just after the bus model has set the bus to idle (the inputs are 0 before it): "
           "Fast-mode, no clock stretching (write, repeated START, read with ACK and NACK, STOP, NACKed address)",
           "I2C", BOTH),
    sample("can", "test_rtl_can", "CAN 250 kbit/s, chip sends 5 frames", [CAN],
           [dec("CAN", "can", {"rx": "can_rx"}, {"bitrate": 250_000, "samplePoint": 0.75}, expect=5)], (236, 2171.7),
           "first configuration, from just after the bus model has set the bus to idle (the inputs are 0 before it): "
           "the chip sends 5 frames to a node at +0.5 % clock, loop delay 10 cycles", None, BOTH),
    sample("jtag", "test_rtl_jtag", "JTAG two-TAP chain, fw/jtag.s", [JTAG, HOST], [JTAG_DEC, HOST_DEC], (0, 413.5),
           "first part: fw/jtag.s, TCK 1.5 MHz, IDCODEs, IR scan, DR scan through Pause-DR, read-back, reset", None, BOTH),
    sample("four_protocols", "test_rtl_four_protocols", "Four protocols at once: UART, SPI, I2C, JTAG",
           [UART, SPI, I2C, JTAG, HOST],
           [dec("UART TX", "uart", {"rx": "uart_tx"}, {"baud": 460_800}, expect=12), SPI0_DEC, I2C_DEC, JTAG_DEC, HOST_DEC],
           (225, 620), "from just before the first protocol edge to after the last JTAG edge (the whole protocol "
           "activity of the test)", "I2C", BOTH),
    sample("uart_tx_115k", "test_rtl_uart_tx", "UART TX 115,200 bit/s 8N1", [UART, HOST],
           [dec("UART TX", "uart", {"rx": "uart_tx"}, {"baud": 115_200}, expect=6), HOST_DEC], (207.0, 864.1),
           "second configuration: 115,200 bit/s 8N1 (bit time 521 cycles against 520.833 nominal), 6 frames", "UART TX"),
    sample("spi_ctl_mode3", "test_rtl_spi_ctl", "SPI controller mode 3", [SPI, HOST],
           [dec("SPI", "spi", SPI_PINS, {"cpol": 1, "cpha": 1}, expect=4), HOST_DEC], (651.7, 866.9),
           "fourth configuration: CPOL 1, CPHA 1, D 0"),
    sample("swd", "test_rtl_swd", "SWD host, DPIDR read, WAIT, parity error", [SWD],
           [dec("SWD", "swd", {"swclk": "swd_swclk", "swdio": "swd_swdio"}, expect=17)], (138, None),
           "whole test from just after the line model has set the line to idle (the inputs are 0 before it)"),
    sample("ps2_host", "test_rtl_ps2", "PS/2 host: 3 device frames, command 0xED with ACK and answer", [PS2],
           [dec("PS/2", "ps2", {"clk": "ps2_clk", "data": "ps2_data"})], (138, 5507.3),
           "first part, from just after the bus model has set the bus to idle: fw/ps2_host.s with the PS/2 device "
           "model, 12.5 kHz"),
    sample("onewire", "test_rtl_onewire", "1-Wire: Read ROM, Write and Read Scratchpad", [OW],
           [dec("1-Wire", "onewire", {"owr": "ow_bus", "drive": "ow_drive"}, expect=206)], (103, 17560.1),
           "first part, from just after the bus model has set the bus to idle: DS18B20 model at its slowest sample time",
           "1-Wire"),
    # The procedures of the simulator's USB and 10BASE-T examples on the chip (test/test_helpers_rtl.py, the reference
    # model with the helper model in lockstep). Windows: test time = 158,346 ps + (load cycles + model cycle) x 16,668
    # ps; the host load takes 129,337 cycles (USB image) and 56,473 cycles (10BASE-T image).
    sample("usb_enum", "test_helpers_rtl_usb_enum", "USB low speed: GET_DESCRIPTOR and SET_ADDRESS at address 0", [USB],
           [dec("USB", "usb", {"dp": "usb_dp", "dm": "usb_dm"}, {"speed": "low"}, expect=21)], (2280.95, 2931.0),
           "model cycles 7,500 to 46,500 of the enumeration: the first 21 bus packets (GET_DESCRIPTOR(device) with "
           "its data and status stages, then SET_ADDRESS 5), host and chip at 1.5 Mbit/s, the chip's replies 5.675 bit "
           "times after the host EOP", None, BOTH, "test_helpers_rtl"),
    sample("eth_rxtx", "test_helpers_rtl_eth", "10BASE-T: a frame received, then a frame sent", [ETH],
           [dec("10BASE-T RX", "eth10", {"rx": "eth_rx", "sq": "eth_sq"}, expect=1),
            dec("10BASE-T TX", "eth10", {"p": "eth_tdp", "n": "eth_tdm", "oe": "eth_oe"}, expect=1)], (1039.79, 1604.3),
           "model cycles 5,900 to 39,768: the partner's 64-octet frame at cycle 6,000 on the slicer and squelch "
           "inputs, then the 60-octet frame that the host gives, sent on TD+ and TD- with preamble, SFD, FCS and TP_IDL",
           None, BOTH, "test_helpers_rtl"),
]
LEVEL = {"rtl": "RTL", "gl": "gate level"}
GL_EQUAL = "the gate-level pin dump of this test equals the RTL pin dump value for value"


def tests(level):
    return sorted({s["test"] for s in SAMPLES if level in s["levels"]})


def module(test):
    return next(s["module"] for s in SAMPLES if s["test"] == test)


def digest(raw, end):
    return hashlib.sha256(json.dumps([end, sorted(raw.items())]).encode()).hexdigest()


def trim(level, test, raw_path, out, simulator, commit, run_url):
    os.makedirs(out, exist_ok=True)
    raw, end = trim_vcd.read_raw(raw_path)
    part = {"level": level, "test": test, "commit": commit, "run": run_url, "simulator": simulator, "end_ps": end,
            "raw_sha256": hashlib.sha256(open(raw_path, "rb").read()).hexdigest(), "raw_digest": digest(raw, end),
            "samples": []}
    for s in SAMPLES:
        if s["test"] != test or level not in s["levels"]:
            continue
        lv = "RTL" if level == "rtl" else "GL"
        f = f"{s['key']}_{level}.vcd"
        comments = [f"test {test} (test/{s['module']}.py), commit {commit}", f"{LEVEL[level]}, {simulator}",
                    f"window {s['note']}", f"run {run_url}"]
        t0, t1, size = trim_vcd.write_sample(raw, end, {"window_ps": s["window_ps"], "groups": s["groups"],
                                                        "comments": comments}, os.path.join(out, f))
        m = {"file": f, "title": f"{s['title']} ({lv})", "test": test, "level": LEVEL[level], "simulator": simulator,
             "decoders": s["decoders"]}
        if s["timing"]:
            m["timing"] = s["timing"]
        m.update(window_ps=[t0, t1], window_cycles=[t0 // CLK_PS, t1 // CLK_PS], window_note=s["note"], bytes=size,
                 raw_sha256=part["raw_sha256"], commit=commit, run=run_url)
        part["samples"].append(m)
        print(f"{f}: {size:,} bytes, window {t0}..{t1} ps ({(t1 - t0) / CLK_PS:,.0f} cycles)")
    with open(os.path.join(out, f"part_{level}_{test}.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(part, fh, indent=1)
        fh.write("\n")
    return part


def manifest(parts_dir, out, require_gl_equal):
    os.makedirs(out, exist_ok=True)
    parts, files = {}, {}
    for d, _, names in os.walk(parts_dir):
        for n in names:
            if n.startswith("part_") and n.endswith(".json"):
                p = json.load(open(os.path.join(d, n), encoding="utf-8"))
                parts[(p["level"], p["test"])] = p
            elif n.endswith(".vcd"):
                files[n] = os.path.join(d, n)
    rtl = [p for (lv, _), p in parts.items() if lv == "rtl"]
    if not rtl:
        raise SystemExit("no RTL samples")
    bad = []
    by_file = {}
    for (lv, test), p in parts.items():
        for m in p["samples"]:
            if lv == "gl":
                r = parts.get(("rtl", test))
                equal = r is not None and r["raw_digest"] == p["raw_digest"]
                m["gl_equal_to_rtl"] = GL_EQUAL if equal else (
                    f"the gate-level pin dump of this test (netlist of commit {p['commit'][:10]}) differs from the RTL "
                    f"pin dump of commit {r['commit'][:10] if r else '?'}")
                if not equal:
                    bad.append(test)
            by_file[m["file"]] = m
    order = [f"{s['key']}_{lv}.vcd" for lv in ("rtl", "gl") for s in SAMPLES if lv in s["levels"]]
    samples = [by_file[f] for f in order if f in by_file]
    for m in samples:
        shutil.copyfile(files[m["file"]], os.path.join(out, m["file"]))
    man = {"commit": rtl[0]["commit"], "clock_ps": CLK_PS, "ns_per_cycle": CLK_PS / 1000, "samples": samples}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(man, fh, indent=1)
        fh.write("\n")
    print(f"{len(samples)} samples ({sum(1 for m in samples if m['level'] == 'RTL')} RTL, "
          f"{sum(1 for m in samples if m['level'] != 'RTL')} gate level); gate level differs from RTL: {sorted(set(bad))}")
    return 1 if bad and require_gl_equal else 0


def main(argv):
    if len(argv) >= 3 and argv[1] == "tests":
        print(json.dumps(tests(argv[2])))
        return 0
    if len(argv) == 3 and argv[1] == "module":
        print(module(argv[2]))
        return 0
    if len(argv) == 9 and argv[1] == "trim":
        trim(*argv[2:9])
        return 0
    if len(argv) in (4, 5) and argv[1] == "manifest":
        return manifest(argv[2], argv[3], "--require-gl-equal" in argv[4:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

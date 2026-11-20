# SPDX-License-Identifier: Apache-2.0
"""Sweep points of the model tests on the chip RTL: one cocotb testcase per point, at 60 MHz.

What it does:
  test_fw_full.py, test_flex.py and test_holdout.py sweep rates, modes, timing parameters and clock errors in the
  reference model only; test_rtl_fw.py runs the standard point and a few worst cases of each on the RTL. This module
  runs the other sweep points on the RTL, each as its own testcase, on the RtlBench of test_rtl_fw.py: the firmware is
  loaded through the host SPI port, the protocol model of the model test drives the pins in closed loop, the reference
  model runs in lockstep (chip pins = model pins in every cycle, host words = model words), and the protocol checks of
  the model test judge the result. Each case names the rule it checks (CASES, column rule).

Which cases run:
  The cases are many (about 160, about 1 min each), so they run in their own sharded job: SWEEP_SHARDS=N and
  SWEEP_SHARD=k define the cases with index k, k + N, ... A run with a test filter (COCOTB_TEST_FILTER, or COCOTB_TESTCASE
  of a local single-test run) defines all of them, so one case can be run by name. Without either, the module defines no
  test, so the regression of test.yaml and the coverage job do not run them.

Outside the simulator:
  python test/test_rtl_sweep.py --list    the trace table (case, rule, parameters)
  python test/test_rtl_sweep.py --model   each case on the reference model alone (the model-test function of its
                                          family); a case kept here must pass there first

Clock: 60 MHz (16.668 ns), as test_rtl_fw.py. RTL only: the gate-level jobs run test_rtl_fw.py.
"""

import os
import random
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in ("model", "asm", "test"):
    if os.path.join(ROOT, p) not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, p))

import test_flex as fx  # noqa: E402
import test_fw_full as ff  # noqa: E402

F_CLK = 60e6
TOL = ff.TOL

# ----- the cases -----


def _rng(name):
    return random.Random(zlib.crc32(name.encode()))


def _uart_n(fmt, b):
    """Frames per case: as many as fit in about 240,000 cycles, 2 to 6."""
    return max(2, min(6, 240_000 // (len(ff.frame_bits(0, fmt)) * b)))


def build_cases():
    cases = []

    def add(name, family, rule, what, **args):
        cases.append(dict(name="test_sweep_" + name, family=family, rule=rule, what=what, args=args))

    # UART transmitter: rates x formats (the points of test_rtl_uart_tx are left out)
    tx_done = {(1_000_000, "8N1"), (115_200, "8N1"), (9_600, "8E1")}
    for rate in (2_000_000, 1_500_000, 1_000_000, 921_600, 500_000, 460_800, 250_000, 230_400, 115_200, 57_600,
                 38_400, 19_200, 9_600, 4_800, 2_400):
        for fmt in ("8N1", "8E1", "8N2"):
            if (rate, fmt) not in tx_done:
                add(f"uart_tx_{rate}_{fmt}", "uart_tx", "fw/uart_tx_full.s header; isa 4.2 (K x C bit time)",
                    f"UART TX {rate} bit/s {fmt}: every bit at its middle, edges at bit boundaries, back to back, "
                    f"divider error within 1 %", rate=rate, fmt=fmt)
    # UART receiver: rates x sender bit-time error x formats (the points of test_rtl_uart_rx are left out)
    rx_done = {(1_000_000, "8N1", -TOL), (1_000_000, "8N1", TOL), (115_200, "8E1", -TOL), (115_200, "8E1", TOL)}
    pts = [(r, "8N1", e) for r in (2_000_000, 1_500_000, 1_000_000, 500_000, 460_800, 230_400, 115_200, 57_600,
                                   19_200, 9_600) for e in (-TOL, 0.0, TOL)]
    pts += [(r, f, e) for r in (1_000_000, 230_400, 115_200, 57_600) for f in ("8E1", "8N2") for e in (-TOL, TOL)]
    pts += [(r, "8N1", e) for r in (1_000_000, 115_200, 9_600) for e in (-0.02, 0.02)]
    for rate, fmt, err in pts:
        if (rate, fmt, err) not in rx_done:
            sign = "m" if err < 0 else "p"
            add(f"uart_rx_{rate}_{fmt}_{sign}{abs(err) * 1000:.0f}", "uart_rx",
                "fw/uart_rx_full.s header (sender bit-time error up to +/-3.3 %); a 3-cycle glitch is ignored",
                f"UART RX {rate} bit/s {fmt}, sender {err * 100:+.1f} %: each word holds the sent bits",
                rate=rate, fmt=fmt, err=err)
    # SPI controller: 4 modes x D x H (smallest H of the model and H + 3; the D 0 / D 7 smallest-H points of
    # test_rtl_spi_ctl are left out)
    for d in (0, 1, 2, 3, 5, 7):
        for cpol in (0, 1):
            for cpha in (0, 1):
                for extra in (0, 3):
                    if d in (0, 7) and extra == 0:
                        continue
                    add(f"spi_ctl_m{cpol}{cpha}_d{d}_h{'min' if extra == 0 else 'min+3'}", "spi_ctl",
                        "fw/spi_ctl.s header; SPI mode (CPOL, CPHA) sample and out edges",
                        f"SPI controller CPOL {cpol} CPHA {cpha} D {d}, half period = smallest H of the model + {extra}: "
                        f"data both ways, CS periods, every half period H", cpol=cpol, cpha=cpha, d=d, extra=extra)
    # SPI target: 4 modes x slower half periods (test_rtl_spi_tgt runs half period 30)
    for mode in range(4):
        for half in (45, 60, 90):
            add(f"spi_tgt_m{mode}_h{half}", "spi_tgt", "fw/spi_tgt.s header; SPI mode sample and out edges",
                f"SPI target mode {mode}, half period {half} cycles: MISO bytes at the sample edges, MOSI bytes in the "
                f"host words, MISO released while CS is high", mode=mode, half=half)
    # I2C controller: clock stretching lengths (test_rtl_i2c runs 0 and 360) and arbitration in Standard-mode
    for mode in ("Fm", "Sm"):
        for stretch in (60, 1200, 3000):
            add(f"i2c_{mode}_stretch{stretch}", "i2c", "NXP UM10204 Rev. 7.0 3.1.9 (clock stretching), Table 11",
                f"I2C {mode} with clock stretching of {stretch} cycles: write, repeated START, read with ACK and NACK, "
                f"STOP, NACKed address; every bus time at or above its Table 11 minimum", mode=mode, stretch=stretch)
    for bit in (1, 3):
        add(f"i2c_Sm_arbitration_bit{bit}", "i2c_arb", "NXP UM10204 Rev. 7.0 3.1.8 (arbitration)",
            f"I2C Sm arbitration lost in address bit {bit}: the controller stops driving and reports it", bit=bit)
    # JTAG: TCK low / high times (test_rtl_jtag runs H1 12, H2 28)
    for name in ("jtag.s", "jtag_tap.s"):
        for h1, h2 in ((6, 14), (8, 20), (10, 24), (16, 32), (20, 40), (24, 24)):
            add(f"jtag_{name[:-2]}_h{h1}_{h2}", "jtag", "IEEE 1149.1 TAP state diagram (TI SSYA002C Figure 3-3)",
                f"{name} with H1 {h1}, H2 {h2}: IDCODEs after reset, IR scan, DR scan, read-back; TAP models check "
                f"set-up, hold and TCK times", name=name, h1=h1, h2=h2)
    # CAN: the asserted rates and sample points of test_flex_can, each part with a node at +/-0.5 % and 10 cycles of
    # transceiver loop delay (the three points of test_rtl_can are left out)
    can_done = {(250_000, "tx", 5000), (250_000, "rx", -5000), (250_000, "arb", 5000)}
    for rate, frac in ((125_000, 0.65), (200_000, 0.6), (250_000, 0.55)):
        for part in ("tx", "rx", "arb"):
            for ppm in (5000, -5000):
                if (rate, part, ppm) not in can_done:
                    add(f"can_{rate // 1000}k_{part}_{'p' if ppm > 0 else 'm'}{abs(ppm) // 100}", "can",
                        "Bosch CAN 2.0 Part A (bit timing, arbitration, ACK); fw/can.s header",
                        f"CAN {rate // 1000} kbit/s, sample point {frac:.2f}, part {part}, node {ppm / 1e4:+.1f} %, "
                        f"delay 10 cycles", rate=rate, frac=frac, part=part, ppm=ppm)
    return cases


CASES = build_cases()


# ----- model check of a case (outside the simulator) -----


def model_fault(case):
    """None if the case passes on the reference model alone (the model-test function of its family), else the fault."""
    import logging
    log = logging.getLogger("sweep")
    a, fam, name = case["args"], case["family"], case["name"]
    try:
        if fam == "uart_tx":
            data = [_rng(name).randrange(256) for _ in range(_uart_n(a["fmt"], ff.uart_cfg(F_CLK, a["rate"])[2]))]
            bench, m, frames, b = ff.tx_run(F_CLK, a["rate"], a["fmt"], data)
            ff.check_tx(log, bench.changes("tx"), b, F_CLK / a["rate"], frames, name)
            return None if ff.status_clean(m, [0]) else "thread status"
        if fam == "uart_rx":
            k, c, b = ff.uart_cfg(F_CLK, a["rate"])
            data = [_rng(name).randrange(256) for _ in range(_uart_n(a["fmt"], b))]
            ok = ff.rx_run(b, ff.rx_h(b), a["fmt"], (F_CLK / a["rate"]) * (1 + a["err"]), data, glitch=True)[0]
            return None if (k == 1 and ok) else "words differ or thread status (K %d)" % k
        if fam == "spi_ctl":
            h = ff.spi_h_min(a["cpol"], a["cpha"], a["d"]) + a["extra"]
            return ff.spi_fault(*ff.spi_run(a["cpol"], a["cpha"], h, a["d"]))
        if fam == "jtag":
            run = fx.jtag_run_a if a["name"] == "jtag.s" else fx.jtag_run_b
            return run(a["h1"], a["h2"])[0]
        if fam == "can":
            bt = round(F_CLK / a["rate"])
            return fx.can_case(bt, round(a["frac"] * bt), delay=10, node_ppm=a["ppm"], parts=(a["part"],))
        return "no model check for this family (run on the RTL)"
    except AssertionError as e:
        return "assertion: %s" % str(e)[:200]


# ----- the RTL testcases -----


def _define_tests():
    import cocotb
    import test_rtl_fw as rf

    async def uart_tx(dut, name, rate, fmt):
        prog = ff.assemble("uart_tx_full.s")
        k, c, b = ff.uart_cfg(F_CLK, rate)
        data = [_rng(name).randrange(256) for _ in range(_uart_n(fmt, b))]
        label = f"rtl uart_tx_full 60 MHz {rate} bit/s {fmt} (K {k}, C {c})"
        bench = rf.RtlBench(dut, prog.words, owner=ff.owners({ff.P_TX: 0}), label=label)
        bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
        frames = [ff.frame_bits(x, fmt) for x in data]
        bench.send(0, c, k, *[ff.frame_word(f) for f in frames])
        await bench.run(rf.HOST_START + (len(frames) * len(frames[0]) + 2) * b)
        ff.check_tx(dut._log, bench.changes("tx"), b, F_CLK / rate, frames, label)
        await bench.finish(dut._log)

    async def uart_rx(dut, name, rate, fmt, err):
        k, c, b = ff.uart_cfg(F_CLK, rate)
        assert k == 1, "the sweep uses uart_rx_full.s (bit time up to 32,767 cycles)"
        data = [_rng(name).randrange(256) for _ in range(_uart_n(fmt, b))]
        label = f"rtl uart_rx_full 60 MHz {rate} bit/s {fmt} (B {b}, H {ff.rx_h(b)}), sender {err * 100:+.1f} %"
        await rf.rtl_rx(dut, b, ff.rx_h(b), fmt, (F_CLK / rate) * (1 + err), data, label=label)
        dut._log.info(f"{label}: {len(data)} frames correct, glitch ignored")

    async def spi_ctl(dut, name, cpol, cpha, d, extra):
        h = ff.spi_h_min(cpol, cpha, d) + extra
        cl = cpol if cpha == 0 else 1 - cpol
        prog = ff.assemble("spi_ctl.s", CPOL=cpol, CL=cl, D=d)
        dev = ff.SpiDevice(cpol, cpha, ff.SPI_DEV)
        label = f"rtl spi_ctl CPOL {cpol} CPHA {cpha} D {d} H {h}"
        bench = rf.RtlBench(dut, prog.words, owner=ff.owners({ff.P_CS: 0, ff.P_MOSI: 0, ff.P_SCK: 0}), inputs=dev,
                            label=label)
        bench.watch("sck", lambda uo, uio, oe: ff.level(uio, oe, ff.P_SCK))
        bench.watch("cs", lambda uo, uio, oe: ff.level(uio, oe, ff.P_CS))
        bench.send(0, h, *ff.SPI_WORDS)
        await bench.run(rf.HOST_START + len(ff.SPI_WORDS) * (20 * h + 120) + 2000)
        await bench.collect(0, len(ff.SPI_WORDS))
        fault = ff.spi_fault(bench, bench.m, dev)
        assert fault is None, f"{label}: {fault}; {bench.diag()}"
        periods, n = ff.spi_half_periods(bench)
        assert periods == {h} and n == 16 * len(ff.SPI_WORDS), (label, periods, n)
        await bench.finish(dut._log)

    async def spi_tgt(dut, name, mode, half):
        import pe_asm
        prog = pe_asm.assemble(ff.spi_tgt_text(mode), "spi_tgt.s", 0)
        ctl = ff.SpiController(mode, half, [mosi for mosi, _ in ff.SPI_TGT_XFERS], lead=30, gap=3000,
                               t0=rf.HOST_START + 1000)
        label = f"rtl spi_tgt mode {mode} half period {half}"
        bench = rf.RtlBench(dut, prog.words, owner=ff.owners({ff.P_MISO: 0}), inputs=ctl, label=label)
        for _, miso in ff.SPI_TGT_XFERS:
            bench.send(0, *[byte << 2 for byte in miso], 0)
        await bench.run(ctl.end + 1000)
        await bench.collect(0, sum(len(mosi) for mosi, _ in ff.SPI_TGT_XFERS))
        fault = ff.spi_tgt_fault(bench, bench.m, ctl)
        assert fault is None, f"{label}: {fault}"
        await bench.finish(dut._log)

    async def i2c(dut, name, mode, stretch):
        words, replies, events = ff.i2c_script()
        label = f"rtl i2c_ctl {mode} 60 MHz stretch {stretch}"
        bench, bus = rf.i2c_bench(dut, mode, stretch, label)
        rf.send_pairs(bench, words)
        await bench.run(rf.HOST_START + (36_000 if mode == "Fm" else 100_000) + 8 * stretch)
        await bench.collect(0, len(replies))
        assert bench.words(0) == replies, f"{label}: replies {[hex(w) for w in bench.words(0)]}"
        assert ff.i2c_events(bus) == events, f"{label}: bus events {ff.i2c_events(bus)}"
        ff.check_i2c_times(dut._log, bus.changes, mode, rf.F_MHZ, label)
        assert max(ff.i2c_times(bus.changes)["low"]) >= stretch, f"{label}: no stretched low period"
        await bench.finish(dut._log)

    async def i2c_arb(dut, name, bit):
        label = f"rtl i2c_ctl Sm arbitration lost in address bit {bit}"
        bench, bus = rf.i2c_bench(dut, "Sm", 0, label)
        bus.lose_at = (0, bit)
        rf.send_pairs(bench, ff.i2c_write(ff.I2C_ADDR << 1, start=True))
        await bench.run(rf.HOST_START + 26_000)
        got = bench.words(0)
        assert len(got) == 1 and got[0] & 0x8000, f"{label}: replies {[hex(w) for w in got]}"
        assert bench.changes("own")[-1][1] == (None, None), f"{label}: pins at the end {bench.changes('own')[-1]}"
        await bench.finish(dut._log)

    async def jtag(dut, name, h1, h2, **kw):
        await rf.rtl_jtag(dut, kw["fw"], h1, h2)

    async def can(dut, name, rate, frac, part, ppm):
        bt = round(F_CLK / rate)
        sp = round(frac * bt)
        label = f"rtl can {rate // 1000} kbit/s BT {bt} SP {sp}, part {part}, node {ppm / 1e4:+.1f} %, delay 10"
        if part == "tx":
            bench, node = rf.can_bench(dut, bt, sp, node_ppm=ppm, delay=10, label=label)
            await bench.run(rf.HOST_START)
            sent = await rf.can_send_all(bench, fx.CAN_FRAMES, bt)
            await bench.run(int(20 * bt))
            fault = fx.can_fault(bench, bench.m, node, sent, fx.CAN_FRAMES)
        elif part == "rx":
            bench, node = rf.can_bench(dut, bt, sp, node_frames=fx.NODE_FRAMES, node_ppm=ppm, node_start=rf.HOST_START,
                                       delay=10, label=label)
            await bench.run(rf.HOST_START + int(300 * bt))
            fault = fx.can_fault(bench, bench.m, node, node_frames=fx.NODE_FRAMES)
        else:
            node_f = [dict(ident=0x700, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
            chip_f = [dict(ident=0x3C2, data=[0x22, 0x33])]
            bench, node = rf.can_bench(dut, bt, sp, node_frames=node_f, node_ppm=ppm, node_start=rf.HOST_START,
                                       delay=10, label=label)
            await bench.run(rf.HOST_START + int(30 * bt))
            sent = await rf.can_send_all(bench, chip_f, bt)
            await bench.run(int(20 * bt))
            fault = fx.can_fault(bench, bench.m, node, sent, chip_f, node_f, expect_lost=1)
        assert fault is None, f"{label}: {fault}"
        await bench.finish(dut._log)

    run = dict(uart_tx=uart_tx, uart_rx=uart_rx, spi_ctl=spi_ctl, spi_tgt=spi_tgt, i2c=i2c, i2c_arb=i2c_arb, jtag=jtag,
               can=can)
    shards = int(os.environ.get("SWEEP_SHARDS", "0") or 0)
    shard = int(os.environ.get("SWEEP_SHARD", "0") or 0)
    named = bool(os.environ.get("COCOTB_TEST_FILTER") or os.environ.get("COCOTB_TESTCASE"))
    defined = {}
    for i, case in enumerate(CASES):
        if not ((shards and i % shards == shard) or (not shards and named)):
            continue
        fn, args = run[case["family"]], dict(case["args"])
        if case["family"] == "jtag":
            args = dict(h1=args["h1"], h2=args["h2"], fw=args["name"])

        def make(fn=fn, args=args, name=case["name"]):
            async def test(dut):
                rf.start_clock(dut)
                await fn(dut, name, **args)
            return test
        t = make()
        t.__name__ = t.__qualname__ = case["name"]
        t.__doc__ = f"{case['what']}. Rule: {case['rule']}."
        defined[case["name"]] = cocotb.test(t)
    return defined


if __name__ == "__main__":
    if "--model" in sys.argv:
        bad = 0
        for case in CASES:
            f = model_fault(case)
            bad += f is not None and not f.startswith("no model check")
            print("%-46s %s" % (case["name"], "model ok" if f is None else f))
        print("cases %d, model faults %d" % (len(CASES), bad))
        sys.exit(1 if bad else 0)
    print("| Case | Rule | Parameters |")
    print("|---|---|---|")
    for case in CASES:
        print("| %s | %s | %s |" % (case["name"], case["rule"], ", ".join("%s %s" % kv for kv in case["args"].items())))
    print("\n%d cases" % len(CASES))
elif os.environ.get("GATES") != "yes":
    globals().update(_define_tests())

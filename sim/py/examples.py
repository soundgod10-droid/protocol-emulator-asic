# SPDX-License-Identifier: Apache-2.0
"""Example setups of the browser simulator: each one is a procedure of the model tests.

Each example calls a test helper or a test function of test/test_*.py with stated parameters, or
(where the test builds its run inside a loop over many cases) repeats the lines of one case of that
test with the test's own helpers and device models. `s.arm(name)` marks the bench that the page
shows: the next bench that is made in the function `name`. The procedure returns (passed, text, log
lines); the checks are the test's own check functions.

Pins (docs/isa.md section 5.1): output pins 0 to 7 are uio0 to uio7, 8 to 14 are uo0 to uo6; input
pins 0 to 7 are uio0 to uio7, 8 to 12 are ui0 to ui4.
"""

from v1_runner import Log, run_test_function  # first: it puts the chip folders on sys.path

import pe_model  # noqa: E402
import test_flex as fx  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_holdout as ho  # noqa: E402
import test_isa as isa  # noqa: E402


def _uart_tx(f_clk, rate, text):
    def proc(s):
        s.arm("tx_run")
        data = list(text.encode())
        bench, m, frames, b = ff.tx_run(f_clk, rate, "8N1", data)
        log = Log()
        k, c, _ = ff.uart_cfg(f_clk, rate)
        ff.check_tx(log, bench.changes("tx"), b, f_clk / rate, frames,
                    f"uart_tx_full {f_clk / 1e6:.0f} MHz {rate} bit/s 8N1 (K {k}, C {c})")
        ff.assert_status_clean(m, [0])
        return True, f"test_fw_full.check_tx: {len(frames)} frames correct, bit time {b} cycles", log.lines
    return proc


def e2a(s):
    s.arm("test_model_lead_and_late")
    lines = run_test_function(isa.test_model_lead_and_late)
    return True, "test_isa.test_model_lead_and_late passed (host words [0, 1, 0, 1], pin changes at 9, 25, 57, 172)", lines


def e2b(s):
    s.arm("test_model_owner_and_slot")
    lines = run_test_function(isa.test_model_owner_and_slot)
    return True, "test_isa.test_model_owner_and_slot passed (threads 1 and 2 read STATUS = 2, ERR)", lines


def e3(s):
    s.arm("all_four")
    r = ff.all_four(60e6, ff.TOL)
    log = Log()
    ff.check_all_four(log, r, "all four, 60 MHz, UART sender +3.3 %")
    return True, (f"test_fw_full.check_all_four: {r['words']} words in one memory, UART TX, UART RX, SPI and I2C "
                  f"correct at the same time, no LATE, no ERR"), log.lines


def e4(s):
    f_clk, rate = 60e6, 115_200
    k, c, b = ff.uart_cfg(f_clk, rate)
    h = ff.rx_h(b)
    data = list(b"Hi 60MHz")
    s.arm("rx_run")
    ok, words, m, frames, nd1 = ff.rx_run(b, h, "8N1", (f_clk / rate) * (1 + ff.TOL), data, glitch=True)
    got = bytes(sum(bit << i for i, bit in enumerate(ff.rx_bits(w, nd1)[1:9])) for w in words)
    return ok, (f"test_fw_full.rx_run: {len(words)} words, data {got!r}, sender +3.3 %, 3-cycle glitch ignored "
                f"(B {b}, H {h})"), []


def e5(s):
    h = ff.spi_h_min(0, 0, 0)
    s.arm("spi_run")
    bench, m, dev = ff.spi_run(0, 0, h, 0)
    fault = ff.spi_fault(bench, m, dev)
    return fault is None, (fault or f"test_fw_full.spi_fault: none; mode 0, smallest half period H {h} cycles "
                                    f"(test_fw_full.spi_h_min); device received {[hex(x) for x in dev.rx]}"), []


def e6(s):
    s.arm("spi_tgt_run")
    bench, m, ctl = ff.spi_tgt_run(0, 30, lead=30, gap=3000)
    fault = ff.spi_tgt_fault(bench, m, ctl)
    got, _ = ctl.received()
    return fault is None, (fault or f"test_fw_full.spi_tgt_fault: none; mode 0, half period 30 cycles (1 MHz at 60 MHz); "
                                    f"controller received {[[hex(x) for x in row] for row in got]}"), []


def e7(s):
    words, replies, events = ff.i2c_script()
    s.arm("i2c_run")
    bench, m, bus = ff.i2c_run("Fm", 60, 360, words)
    bench.run(36_000)
    log = Log()
    assert bench.words(0) == replies, f"replies {[hex(w) for w in bench.words(0)]}"
    assert ff.i2c_events(bus) == events, f"bus events {ff.i2c_events(bus)}"
    ff.assert_status_clean(m, [0])
    ff.check_i2c_times(log, bus.changes, "Fm", 60, "i2c_ctl Fm 60 MHz stretch 360")
    return True, "6 replies and 11 bus events as test_fw_full.i2c_script expects; UM10204 Fast-mode times met", log.lines


def e7b(s):
    s.arm("i2c_run")
    ok = ff.i2c_spike_run(60, "SCL", 148, 3)
    return ok, ("test_fw_full.i2c_spike_run: transfer correct with a 3-sample spike on SCL inside the clock stretch "
                "(the spike filter of uio6 needs 4 samples)" if ok else "transfer broken by the spike"), []


def e8(s):
    tsu_min = 20
    lo, hi = 1, ff.I2C_EQU[("Fm", 50)]["TLOW"]
    tries = 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        tries += 1
        lo, hi = (lo, mid) if fx.i2c_char_run(mid, tsu_min)[0] == "ok" else (mid, hi)
    s.arm("i2c_char_run")
    res, su = fx.i2c_char_run(hi, tsu_min)
    below = fx.i2c_char_run(hi - 1, tsu_min)
    return res == "ok", (f"binary search of test_flex.test_flex_i2c_char: target set-up limit {tsu_min} cycles; "
                         f"smallest TLOW that works {hi} ({tries} tries), bus SDA set-up {su} cycles; "
                         f"TLOW {hi - 1}: {below[0]}. Shown: the run at TLOW {hi}"), []


CAN_BT = round(60e6 / 250_000)  # 240 cycles: 250 kbit/s at 60 MHz (test_rtl_fw.test_rtl_can)
CAN_SP = round(0.55 * CAN_BT)


def e9(s):
    frames = fx.CAN_FRAMES[:3]
    s.arm("can_run")
    bench, m, node = fx.can_run(CAN_BT, CAN_SP, node_bt=CAN_BT * 1.005, delay=10)
    bench.run(100)
    sent = fx.can_send_all(bench, frames, CAN_BT)
    bench.run(int(20 * CAN_BT))
    fault = fx.can_fault(bench, m, node, sent, frames)
    return fault is None, (fault or f"test_flex.can_fault: none; {len(frames)} frames sent and acknowledged by the node "
                                    f"(node clock +0.5 %, loop delay 10 cycles)"), []


def e9b(s):
    node_f = [dict(ident=0x700, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
    chip_f = [dict(ident=0x3C2, data=[0x22, 0x33])]
    s.arm("can_run")
    bench, m, node = fx.can_run(CAN_BT, CAN_SP, node_frames=node_f, node_start=100, node_bt=CAN_BT * 1.005, delay=10)
    bench.run(100 + int(30 * CAN_BT))
    sent = fx.can_send_all(bench, chip_f, CAN_BT)
    bench.run(int(20 * CAN_BT))
    fault = fx.can_fault(bench, m, node, sent, chip_f, node_f, expect_lost=1)
    return fault is None, (fault or "test_flex.can_fault: none; the chip lost arbitration once (ID 0x3C2 against 0x3C1), "
                                    "received the winner's frame, then sent its frame again"), []


def e10(s):
    s.arm("jtag_run_a")
    fault, span, n = fx.jtag_run_a(12, 28)
    return fault is None, (fault or f"test_flex.jtag_run_a: correct; two TAPs, both IDCODEs, IR scan, DR scan through "
                                    f"Pause-DR, read-back; 64-bit IDCODE scan {n} TCK cycles in {span} clock cycles"), []


def e10b(s):
    s.arm("jtag_run_b")
    fault, span, n = fx.jtag_run_b(12, 28)
    return fault is None, (fault or "test_flex.jtag_run_b: correct (fw/jtag_tap.s, TAP state handled in the firmware)"), []


def e11(s):
    s.arm("swd_run")
    fault, span = fx.swd_run(12, 20)
    return fault is None, (fault or f"test_flex.swd_run: correct; connect, DPIDR read ({span} clock cycles), WAIT and "
                                    f"retry, posted AP read, parity error, line reset"), []


def e12(s):
    s.arm("ps2_host_run")
    fault, got = fx.ps2_host_run(5000, frames=fx.PS2_BYTES[:3])
    return fault is None, (fault or f"test_flex.ps2_host_run: correct; device clock 10 kHz, frames "
                                    f"{[hex(b) for b, _ in got]}, then command 0xED with request to send and "
                                    f"answer 0xFA"), []


def e13(s):
    s.arm("ps2_dev_run")
    fault, periods = fx.ps2_dev_run(None, (1, 5), frames=(0xAA, 0xFA, 0x12))
    return fault is None, (fault or f"test_flex.ps2_dev_run: correct; frame 1 inhibited by the host at falling edge 5 "
                                    f"and sent again; command 0xF4 acknowledged; clock period {min(periods)} to "
                                    f"{max(periods)} cycles"), []


def e14(s):
    s.arm("trigger_case")
    fault, lat = fx.trigger_case("fire_t", 200, offsets=range(1))
    return fault is None, (fault or f"test_flex.trigger_case: two pulses, delay from the start edge of each matching "
                                    f"byte {lat} cycles (B 434, D 200)"), []


def _bench_run(s, m, dev, words, cycles, done=None, part=6000):
    """The loop of the hold-out tests (inputs, host push, step, host pull) is the loop of Bench.run, so the
    run is made with the test bench; with `done`, it stops at the first `part` boundary after done() is true,
    as the test loop stops when its condition is true."""
    s.arm("_bench_run")
    bench = ff.Bench(m, inputs=dev)
    bench.send(0, *words)
    if done is None:
        bench.run(cycles)
    else:
        while m.cycle < cycles and not done(bench):
            bench.run(min(part, cycles - m.cycle))
    return bench


def e15(s):
    f_mhz = 60
    prog = ho.assemble("onewire.s", **ho.OW_EQU[f_mhz])
    owner = [None] * pe_model.N_OUT
    owner[ho.P_OW] = 0
    od = [p == ho.P_OW for p in range(pe_model.N_OUT)]
    m = pe_model.PeModel(prog.words, owner=owner, open_drain=od)
    dev = ho.OneWireDevice(f_mhz, 30, 30, 120, 30)
    words = [ho.OW_RESET, 0x33] + [0xFF] * 8  # Read ROM: the first part of ho.OW_SCRIPT
    resets = 1
    cycles = 2000 + resets * 1000 * f_mhz + (len(words) - resets) * 8 * 72 * f_mhz  # the formula of ho.ow_run
    bench = _bench_run(s, m, dev, words, cycles)
    got = bench.words(0)
    want = ho.ow_expected()[:10]
    ok = got == want and m.threads[0].status == 0
    return ok, (f"replies {[hex(w) for w in got]}: presence 0, Read ROM 0x33, ROM {[hex(b) for b in ho.ROM]} "
                f"as test_holdout.ow_expected" if ok else f"replies {[hex(w) for w in got]}, expected {want}"), []


def e16(s):
    f_mhz = 60
    prog = ho.assemble("am2302.s", **ho.AM_EQU[f_mhz])
    owner = [None] * pe_model.N_OUT
    owner[ho.P_AM] = 0
    od = [p == ho.P_AM for p in range(pe_model.N_OUT)]
    m = pe_model.PeModel(prog.words, owner=owner, open_drain=od)
    hum, tmp = ho.AM_DATA[0]
    dev = ho.Am2302Device(f_mhz, hum, tmp)

    def done(bench):
        got = bench.words(0)
        return bool(got) and (got[0] != 0 or len(got) == 4)
    bench = _bench_run(s, m, dev, [1], 12000 * f_mhz, done)  # ho.am_run pushes one word, runs at most 12 ms
    got = bench.words(0)
    want = [0, hum, tmp, ho.am_sum(hum, tmp)]
    ok = got == want and dev.starts == 1 and dev.conflicts == 0
    return ok, (f"replies {[hex(w) for w in got]}: humidity 0x{hum:04X} ({hum / 10:.1f} %), temperature "
                f"0x{tmp:04X} ({tmp / 10:.1f} C), check sum, as test_holdout.test_holdout_am2302 expects"
                if ok else f"replies {[hex(w) for w in got]}, expected {[hex(w) for w in want]}"), []


def e17(s):
    f_mhz = 60
    equ = dict(ho.PDI_EQU[f_mhz])
    prog = ho.assemble("pdi.s", **equ)
    owner = [None] * pe_model.N_OUT
    owner[ho.P_CLK] = owner[ho.P_DAT] = 0
    m = pe_model.PeModel(prog.words, owner=owner, open_drain=[False] * pe_model.N_OUT)
    dev = ho.PdiDevice(f_mhz, t_rst=1.0, d_out=1, undriven=0, mem=ho.PDI_MEM)
    words = ho.PDI_SCRIPT
    want = sum(w & 0xFF for w in words if w & 0x8000)
    bits = 100 + sum(280 * (w & 0xFF) if w & 0x8000 else 14 for w in words)
    cycles = equ["TEN"] + bits * 2 * equ["HALF"]  # the limit of ho.pdi_run

    def done(bench):
        return len(bench.words(0)) >= want and not bench.to_thread[0]
    bench = _bench_run(s, m, dev, words, cycles, done)
    got = bench.words(0)
    ok = ho.pdi_result(got) == ho.PDI_REPLY and not dev.faults and m.threads[0].status == 0
    return ok, ("test_holdout.pdi_result equals PDI_REPLY: enable, KEY, LDCS, STCS, STS, LDS, REPEAT with "
                "LD *(ptr++), parity error and BREAK recovery" if ok else
                f"replies {[hex(w) for w in got]}, faults {dev.faults[:2]}"), []


UART_TX_PINS = [["uo4", "TX"]]
SPI_PINS = [["uio0", "CS"], ["uio3", "SCK"], ["uio1", "MOSI"], ["uio2", "MISO"]]
I2C_PINS = [["uio6", "SCL"], ["uio7", "SDA"]]
JTAG_PINS = [["uo0", "TCK"], ["uo1", "TMS"], ["uo2", "TDI"], ["ui0", "TDO"]]


def _uart_dec(pin, bit_cycles):
    return {"name": "UART", "protocol": "uart", "pins": {"rx": pin}, "options": {"bit_cycles": bit_cycles, "format": "8N1"}}


SPI_DEC = {"name": "SPI", "protocol": "spi", "pins": {"cs": "uio0 CS", "clk": "uio3 SCK", "mosi": "uio1 MOSI", "miso": "uio2 MISO"},
           "options": {"cpol": 0, "cpha": 0}}
I2C_DEC = {"name": "I2C", "protocol": "i2c", "pins": {"scl": "uio6 SCL", "sda": "uio7 SDA"}, "options": {}}
JTAG_DEC = {"name": "JTAG", "protocol": "jtag", "pins": {"tck": "uo0 TCK", "tms": "uo1 TMS", "tdi": "uo2 TDI", "tdo": "ui0 TDO"},
            "options": {}}

EXAMPLES = [
    dict(id="E1", title="UART transmitter, 115,200 bit/s", clock_hz=60e6, proc=_uart_tx(60e6, 115_200, "Hello, world"),
         source="test_fw_full.tx_run, test_fw_full.check_tx",
         pins=UART_TX_PINS, decoders=[_uart_dec("uo4 TX", 521)],
         text="Thread 0 runs fw/uart_tx_full.s. The host gives the bit time (K chunks of C cycles) and one word per "
              "frame. Each bit is an OUT at time T, then TADD adds the bit time to T: the event slot of uo4 holds "
              "the next edge before its cycle comes, so every edge is exactly on a bit boundary."),
    dict(id="E2a", title="Late event sets LATE", clock_hz=60e6, proc=e2a,
         source="test_isa.test_model_lead_and_late",
         pins=[["uo0", ""], ["uo1", ""], ["uo2", ""], ["uo3", ""]], decoders=[],
         text="An OUT needs its event time at least 4 cycles ahead (isa 4.2). The OUT to uo1 has a lead of 3: the "
              "pin changes 4 cycles after the OUT and the thread's LATE bit is set; the program reads STATUS, "
              "pushes it to the host and clears it. Watch the LATE flag of thread 0 at cycle 24."),
    dict(id="E2b", title="Pin owner and full event slot", clock_hz=60e6, proc=e2b,
         source="test_isa.test_model_owner_and_slot",
         pins=[["uo0", ""]], decoders=[],
         text="Only the owner thread of a pin can change it (isa 5.2). Thread 1 writes uo0, which thread 0 owns: "
              "the pin does not change and thread 1 gets ERR. Thread 0's second OUT waits until the slot of uo0 "
              "is free (cycles 12 to 40)."),
    dict(id="E3", title="Four protocols at once: UART TX, UART RX, SPI, I2C", clock_hz=60e6, proc=e3,
         source="test_fw_full.all_four, test_fw_full.check_all_four",
         pins=[["uo4", "TX"], ["ui3", "RX"]] + SPI_PINS + I2C_PINS,
         decoders=[_uart_dec("uo4 TX", 60), dict(_uart_dec("ui3 RX", 60), name="UART RX"), SPI_DEC, I2C_DEC],
         text="One memory image (103 words of the 512-word program memory) holds four programs, one per thread: UART TX and UART RX at 1 Mbit/s (sender "
              "+3.3 %), SPI controller at its smallest half period, I2C Fast-mode with 300-cycle clock stretching. "
              "Each thread has a fixed slot every 4th cycle; a waiting thread keeps its slot."),
    dict(id="E4", title="UART receiver, sender +3.3 %", clock_hz=60e6, proc=e4,
         source="test_fw_full.rx_run",
         pins=[["ui3", "RX"]], decoders=[_uart_dec("ui3 RX", 521)],
         text="fw/uart_rx_full.s waits for the start edge with WAIT (the edge cycle comes back in a register), "
              "then samples each bit with SAMPLE at a set cycle. The sender is 3.3 % slow and a 3-cycle glitch "
              "comes before the first frame."),
    dict(id="E5", title="SPI controller, mode 0", clock_hz=60e6, proc=e5,
         source="test_fw_full.spi_run, test_fw_full.spi_fault",
         pins=SPI_PINS, decoders=[SPI_DEC],
         text="fw/spi_ctl.s sends 4 bytes in two transfers to the SPI target model and reads its reply bytes. "
              "The half period is the smallest that test_fw_full.spi_h_min finds; every SCK edge in a byte is "
              "that many cycles apart."),
    dict(id="E6", title="SPI target, 1 MHz", clock_hz=60e6, proc=e6,
         source="test_fw_full.spi_tgt_run, test_fw_full.spi_tgt_fault",
         pins=SPI_PINS, decoders=[SPI_DEC],
         text="fw/spi_tgt.s is the target: it waits for SCK edges from the SPI controller model and drives MISO "
              "from host words; MISO is released while CS is high."),
    dict(id="E7", title="I2C controller, Fast-mode, clock stretching", clock_hz=60e6, proc=e7,
         source="test_fw_full.i2c_run, test_fw_full.check_i2c_times",
         pins=I2C_PINS, decoders=[I2C_DEC],
         text="fw/i2c_ctl.s writes a byte, makes a repeated START, reads two bytes (ACK, then NACK and STOP) and "
              "addresses a missing target. The target model stretches SCL for 360 cycles after each ACK; the "
              "firmware waits for the SCL rise. Both pins are open-drain."),
    dict(id="E7b", title="I2C with a spike on SCL", clock_hz=60e6, proc=e7b,
         source="test_fw_full.i2c_spike_run",
         pins=I2C_PINS, decoders=[I2C_DEC],
         text="A 3-sample high spike on SCL comes during the clock stretch after the data byte. The input filter "
              "of uio6 and uio7 changes only after 4 samples in a row, so the transfer is not disturbed."),
    dict(id="E8", title="I2C target characterization", clock_hz=50e6, proc=e8,
         source="test_flex.i2c_char_run, test_flex.test_flex_i2c_char",
         pins=I2C_PINS, decoders=[I2C_DEC],
         text="The host finds the SDA set-up limit of an I2C target by a binary search on one firmware constant "
              "(TLOW of fw/i2c_ctl.s), with the same firmware each time. The run shown is the one at the limit "
              "found."),
    dict(id="E9", title="CAN node, 250 kbit/s", clock_hz=60e6, proc=e9,
         source="test_flex.can_run, test_flex.can_send_all, test_flex.can_fault",
         pins=[["uio4", "TXA"], ["uio5", "TXB"], ["ui4", "CAN_RX"]],
         decoders=[{"name": "CAN", "protocol": "can", "pins": {"rx": "ui4 CAN_RX"}, "options": {"bit_cycles": CAN_BT}}],
         text="fw/can.s runs on two threads: thread 0 sends, thread 1 receives every frame on the bus with CRC "
              "check and ACK. Three frames go to the CAN node model (its clock 0.5 % fast, 10 cycles of "
              "transceiver delay); the node acknowledges each one."),
    dict(id="E9b", title="CAN arbitration lost and frame sent again", clock_hz=60e6, proc=e9b,
         source="test_flex.can_run, test_flex.can_send_all, test_flex.can_fault",
         pins=[["uio4", "TXA"], ["uio5", "TXB"], ["ui4", "CAN_RX"]],
         decoders=[{"name": "CAN", "protocol": "can", "pins": {"rx": "ui4 CAN_RX"}, "options": {"bit_cycles": CAN_BT}}],
         text="The node and the chip start a frame at the same time. The chip's ID 0x3C2 loses against 0x3C1 at "
              "the last ID bit: the chip stops sending, receives the node's frame, and the host sends the frame "
              "again."),
    dict(id="E10", title="JTAG chain of two TAPs", clock_hz=60e6, proc=e10,
         source="test_flex.jtag_run_a",
         pins=JTAG_PINS, decoders=[JTAG_DEC],
         text="fw/jtag.s shifts TMS and TDI bits from host words and returns TDO bits. The host reads both "
              "IDCODEs after Test-Logic-Reset, scans the instruction registers, writes a data register through "
              "Pause-DR and reads it back. TCK 1.5 MHz at 60 MHz (H1 12, H2 28)."),
    dict(id="E10b", title="JTAG with TAP state in the firmware", clock_hz=60e6, proc=e10b,
         source="test_flex.jtag_run_b",
         pins=JTAG_PINS, decoders=[JTAG_DEC],
         text="fw/jtag_tap.s takes reset and scan commands and makes the TMS sequence itself."),
    dict(id="E11", title="SWD host", clock_hz=60e6, proc=e11,
         source="test_flex.swd_run",
         pins=[["uo3", "SWCLK"], ["uio4", "SWDIO"]],
         decoders=[{"name": "SWD", "protocol": "swd", "pins": {"swclk": "uo3 SWCLK", "swdio": "uio4 SWDIO"}, "options": {}}],
         text="fw/swd.s sends the JTAG-to-SWD sequence, reads DPIDR, writes SELECT, writes an AP register through "
              "two WAIT replies, reads it back (posted read and RDBUFF), and checks the reset-state rule."),
    dict(id="E12", title="PS/2 host, keyboard at 10 kHz", clock_hz=50e6, proc=e12,
         source="test_flex.ps2_host_run",
         pins=[["uio4", "CLK"], ["uio5", "DATA"]],
         decoders=[{"name": "PS/2", "protocol": "ps2", "pins": {"clk": "uio4 CLK", "data": "uio5 DATA"}, "options": {}}],
         text="fw/ps2_host.s receives three frames from the keyboard model (device clock 10 kHz: 66,000 cycles "
              "per frame at 60 MHz, 55,000 at 50 MHz), then sends command 0xED with a request to send; the "
              "keyboard acknowledges and answers 0xFA."),
    dict(id="E13", title="PS/2 device with host inhibit", clock_hz=50e6, proc=e13,
         source="test_flex.ps2_dev_run",
         pins=[["uio4", "CLK"], ["uio5", "DATA"]],
         decoders=[{"name": "PS/2", "protocol": "ps2", "pins": {"clk": "uio4 CLK", "data": "uio5 DATA"}, "options": {}}],
         text="fw/ps2_dev.s is the keyboard. The host model holds CLK low during frame 1 (line contention): the "
              "firmware stops, reports it, and the host program sends the frame again; then the host sends a "
              "command with a request to send."),
    dict(id="E14", title="Timed trigger on a UART byte", clock_hz=50e6, proc=e14,
         source="test_flex.trigger_case",
         pins=[["ui3", "RX"], ["uo5", "TRG"]], decoders=[_uart_dec("ui3 RX", 434)],
         text="fw/trigger.s receives UART bytes and makes a pulse on uo5 a set time D after the start edge of "
              "each byte that matches. The pulse is timed from T, so its delay has no jitter."),
    dict(id="E15", title="Hold-out: 1-Wire Read ROM", clock_hz=60e6, proc=e15,
         source="test_holdout.ow_run (its loop with the test bench), test_holdout.OneWireDevice",
         pins=[["uio4", "DQ"]],
         decoders=[{"name": "1-Wire", "protocol": "onewire", "pins": {"owr": "uio4 DQ"}, "options": {}}],
         text="fw/onewire.s (written for the hold-out trial) makes a reset with presence detect and reads the "
              "64-bit ROM of the DS18B20 model. The line is open-drain with a pull-up."),
    dict(id="E16", title="Hold-out: AM2302 sensor", clock_hz=60e6, proc=e16,
         source="test_holdout.am_run (its loop with the test bench), test_holdout.Am2302Device",
         pins=[["uio5", "SDA"]],
         decoders=[],  # the waveform viewer has no AM2302 decoder
         text="fw/am2302.s sends the start signal and measures the 40 data bits of the sensor model by pulse "
              "length; the host gets humidity, temperature and the check sum."),
    dict(id="E17", title="Hold-out: Atmel PDI", clock_hz=60e6, proc=e17,
         source="test_holdout.pdi_run (its loop with the test bench), test_holdout.PdiDevice",
         pins=[["uio2", "PDI_CLK"], ["uio3", "PDI_DATA"]],
         decoders=[],  # the waveform viewer has no PDI decoder
         text="fw/pdi.s runs the PDI script of the hold-out test against the XMEGA PDI model: enable, KEY, control "
              "and status registers, data space writes and reads, REPEAT, a parity error and the BREAK recovery."),
    dict(id="E18a", title="One file, three clocks: UART TX at 40 MHz", clock_hz=40e6, proc=_uart_tx(40e6, 115_200, "40 MHz"),
         source="test_fw_full.tx_run, test_fw_full.check_tx",
         pins=UART_TX_PINS, decoders=[_uart_dec("uo4 TX", 347)],
         text="The same fw/uart_tx_full.s as E1; only the host words change with the clock (bit time 347 cycles "
              "at 40 MHz). Compare with E1 (60 MHz, 521 cycles) and E18b (50 MHz, 434 cycles)."),
    dict(id="E18b", title="One file, three clocks: UART TX at 50 MHz", clock_hz=50e6, proc=_uart_tx(50e6, 115_200, "50 MHz"),
         source="test_fw_full.tx_run, test_fw_full.check_tx",
         pins=UART_TX_PINS, decoders=[_uart_dec("uo4 TX", 434)],
         text="The same file at 50 MHz: bit time 434 cycles from the host words."),
]

BY_ID = {e["id"]: e for e in EXAMPLES}

# Why the threads that an example does not use stay stopped, and which flags are its expected result. The page puts the
# threads that the host starts (the run's start configuration) in front of this text.
THREADS_WHY = ("The host starts only the threads that this example's protocol needs; the others stay stopped "
               "(RUN = 0), as they would on the chip. That is not a fault: the chip has four threads, so up to four "
               "protocols run at once (E3).")
THREADS = {
    "E2a": ("LATE on thread 0 is the expected result: the program issues two events too late on purpose (lead 3, "
            "then a time in the past), reads STATUS after each one, clears LATE after the first and leaves it set "
            "after the second. " + THREADS_WHY),
    "E2b": ("Three test programs run, one per thread. ERR on threads 1 and 2 is the expected result of the test: "
            "thread 1 writes uo0, which thread 0 owns, and thread 2 writes pin 15, which does not exist; the chip "
            "ignores both OUTs, sets ERR, and each thread reads STATUS = 2 and sends it to the host. Thread 3 is not "
            "needed."),
    "E3": "All four threads run, one protocol each: UART TX, UART RX, SPI controller and I2C controller.",
    "E9": ("fw/can.s runs on two threads: thread 0 sends, thread 1 receives every frame on the bus. Threads 2 and 3 "
           "are not needed for one CAN node."),
    "E9b": ("fw/can.s runs on two threads: thread 0 sends, thread 1 receives every frame on the bus. Threads 2 and 3 "
            "are not needed for one CAN node."),
}


def meta():
    """Example list for the page (without the procedures)."""
    return [{**{k: v for k, v in e.items() if k != "proc"}, "threads": THREADS.get(e["id"], THREADS_WHY)} for e in EXAMPLES]

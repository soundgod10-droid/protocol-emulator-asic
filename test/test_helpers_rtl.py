# SPDX-License-Identifier: Apache-2.0
"""The line-helper model (test/slu_model.py) against the RTL, in every clock cycle.

What it does:
  Each test runs a procedure of test/test_helpers_model.py (fw/usb_dev.s or fw/eth_10bt.s against the per-cycle
  device models), or one of the two USB-helper UART procedures below, on the chip RTL, loaded through the host SPI
  port as a host does (test_rtl_fw.RtlBench), with the reference model plus the helper model (slu_model.HelperChip)
  in lockstep. In each clock cycle: the chip pins must equal the model pins (RtlBench), and every register of the
  helper that the run uses (slu_model.COMPARED, and theta, I, the accepted-edge count, the sync flag and the bit
  strobe of its receive loop) must equal the register of src/pe_slu.v. The words that the host reads must equal the
  model's, and the procedure's own checks (USB 2.0 or 802.3 host model, UART receiver, events) must pass on the
  chip's pins and words.

Why:
  The browser simulator shows USB and 10BASE-T runs of the model; these tests show that those runs are the RTL's
  runs, cycle for cycle, for the same inputs (the claim of the page: the model, compared with the RTL). The UART
  procedures cover the USB helper's NRZ and clocked modes (writable MODE, TXPER and LTC bits of SPEC 1).

Conditions: RTL logic simulation (Icarus Verilog) at 60 MHz; the device models give their inputs per clock cycle at
the falling edge (test_rtl_fw.RtlBench), the convention of slu_model (falling-edge DIN sample = the level of the
cycle before). A run's words in the data memory are read from the model (the core is compared with the model by
test_core.py and by the pins and host words here).
"""

import os
import random
import sys

import cocotb
from cocotb.triggers import FallingEdge

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(ROOT, "model"), os.path.join(ROOT, "asm"), os.path.join(ROOT, "fw"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import host_spi as hs  # noqa: E402
import pe_asm  # noqa: E402
import pe_model  # noqa: E402
import slu_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_helpers_model as thm  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
# The coverage job (Verilator) runs the RTL list too; these tests read helper nets by their names, which this file
# has been checked to find in Icarus Verilog only, so they skip there.
VERILATOR = "verilator" in (getattr(cocotb, "SIM_NAME", None) or "").lower()
RTL_NAME = {"usb": "u_usb", "eth": "u_eth"}


class PrestartWatch:
    """Samples run_rst_n and the cycle counter at each falling clock edge until cycle 0: the counter values of the
    cycles from the release of the processor reset to START, which the helper model runs before cycle 0."""

    def __init__(self, dut):
        self.dut = dut
        self.samples = []
        self.done = False
        cocotb.start_soon(self._watch())

    async def _watch(self):
        top = self.dut.user_project
        fe = FallingEdge(self.dut.clk)
        while not self.done:
            await fe
            self.samples.append((int(top.run_rst_n.value), int(top.cycle_count.value) & 0xFFFF))
            if len(self.samples) > 64:
                del self.samples[0]

    def nows(self):
        s = self.samples
        rise = max(i for i in range(1, len(s)) if s[i][0] == 1 and s[i - 1][0] == 0)
        self.done = True
        return [now for _, now in s[rise:rise + 3]], s[rise + 3:rise + 4]


class LockstepHelperChip(slu_model.HelperChip):
    """HelperChip that compares its helper registers with the RTL's before each cycle."""

    def attach(self, dut, watch, log):
        self.dut, self.watch, self.log = dut, watch, log
        self.rtl = {}
        self.compared = 0
        self.started = False
        if GL:          # the netlist has no named helper registers: pins and host words only (RtlBench)
            return
        proc = dut.user_project.u_proc
        for name in self.helpers:
            hu = getattr(proc, RTL_NAME[name])
            regs = [(n, getattr(hu, n), n) for n in slu_model.COMPARED]
            regs += [(r, getattr(hu.u_loop, r), m) for r, m in slu_model.LOOP_COMPARED]
            self.rtl[name] = [(r, h, m, len(h)) for r, h, m in regs]

    def step(self):
        if not self.started:
            if self.watch is not None:
                nows, after = self.watch.nows()
                self.log.info("helper model: processor reset released %d cycles before cycle 0, counter %s; cycle 0 "
                              "sample %s" % (len(nows), nows, after))
            else:
                # gate level: the counter is not a named net. The prestart cycles see SEL at its reset value (no pin
                # read); their counter values reach only a USB line-condition count while the line reads SE0, which
                # the firmware's SEL write and the J of the idle bus clear before it could count to its threshold.
                nows = (0xFFFD, 0xFFFE, 0xFFFF)
                self.log.info("helper model (gate level): prestart cycles with counter values %s" % (nows,))
            self.prestart(nows)
            self.started = True
        c = self.cycle
        for name, regs in self.rtl.items():
            h = self.helpers[name]
            bad = []
            for r, handle, attr, width in regs:
                v = int(handle.value)
                mv = getattr(h, attr) & ((1 << width) - 1)
                if v != mv:
                    bad.append(f"{r} rtl 0x{v:X} model 0x{mv:X}")
            if bad:
                raise AssertionError(f"helper {name}, model cycle {c}: {'; '.join(bad[:12])}")
            self.compared += len(regs)
        return super().step()


class HelperRtlBench(rf.RtlBench):
    """RtlBench with the helper pins in the host configuration and LockstepHelperChip as the model."""

    def __init__(self, dut, image, pins, helpers, inputs, label, start_pc=(0, 0, 0, 0),
                 run=(True, False, False, False), owner=None):
        super().__init__(dut, image, start_pc=start_pc, run=run, owner=owner, inputs=inputs, label=label)
        self.helper_words = dict(pins)
        cfg = thm.helper_cfg(pins)
        words = self.image[:pe_model.PROG_WORDS + 1024]
        self.m = LockstepHelperChip(words, self.start_pc, self.run_mask, self.owner, self.od, helper_cfg=cfg,
                                    helpers=helpers, prestart_now=None)
        self.m.attach(dut, None if GL else PrestartWatch(dut), dut._log)
        # Pins of the cycles between the processor reset release (3 cycles before cycle 0) and cycle 0: the helper
        # pins drive from the second of them (pe_proc hrun); their values do not depend on the counter.
        scratch = slu_model.HelperChip(words, self.start_pc, self.run_mask, self.owner, self.od, helper_cfg=cfg,
                                       helpers=helpers, prestart_now=None)
        self.pre_pins = [rf.RESET_PINS]
        for _ in range(2):
            scratch.prestart([0])
            self.pre_pins.append(scratch.pins())

    def load_pins(self, t):
        k = t - (self.t0 - 3) if self.t0 is not None else -1
        return self.pre_pins[k] if 0 <= k <= 2 else rf.RESET_PINS

    async def _start(self):
        dut = self.dut
        dut.ena.value = 1
        dut.ui_in.value = 1 << hs.CS_N_BIT
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        await rf.ClockCycles(dut.clk, 5)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1
        await rf.ClockCycles(dut.clk, 4)
        s = self.sched
        s.idle(2)
        head, pins = hs.config_words(self.start_pc, self.run_mask, self.owner, self.od)
        pins = list(pins)
        for p, w in self.helper_words.items():
            pins[p] = w
        for out in (hs.t_write_mem(0, self.image[:self.used]), hs.t_write_cfg(0, head), hs.t_write_cfg(16, pins)):
            s.add(out, "load")
        k = s.add([hs.START], "START")
        self.t0 = s.act_cycle(k, 0) + 4
        self.started = True


async def drive_rtl(dut, proc_fn, *args, clock=True, **kw):
    """Runs a procedure of test_helpers_model on the RTL with the model in lockstep; returns (result, bench).
    clock=False: the clock runs already (several procedures one after the other; each starts with a chip reset)."""
    if clock:
        rf.start_clock(dut)
    made = []

    def make_bench(image, pins, helpers, inputs, label):
        made.append(HelperRtlBench(dut, image, pins, helpers, inputs, label))
        return made[-1]
    proc = proc_fn(make_bench, *args, **kw)
    try:
        req = next(proc)
        while True:
            bench, done, limit, part = req
            while bench.m.cycle < limit and not done():
                await bench.run(min(part, limit - bench.m.cycle))
            req = proc.send(None)
    except StopIteration as stop:
        result = stop.value
    bench = made[0]
    await bench.finish(dut._log)
    return result, bench


# ----- the USB helper as a UART (NRZ transmit, clocked edge-started receive) -----

def uart_bits(data):
    """8N1 line bits of the bytes, start bit first."""
    out = []
    for d in data:
        out += [0] + [(d >> i) & 1 for i in range(8)] + [1]
    return out


def bits_words(bits):
    """Words of a bit stream, first bit in bit 0 of the first word (test_slu_clk.words_of)."""
    return [sum(b << i for i, b in enumerate(bits[k:k + 16])) for k in range(0, len(bits), 16)]


def uart_htx_proc(make_bench, nbytes=64, seed=1119):
    """test_slu.test_slu_chip_uart_tx: thread 0 configures the USB helper (NRZ code, TXPER 4 = 15 Mbaud at 60 MHz) and
    feeds back-to-back 8N1 bytes from the data memory; TXA on uio0. Check: an independent receiver at each bit middle
    of the 4-cycle grid, every edge on the grid, TXDONE without TXUNDER."""
    import test_slu
    rng = random.Random(seed)
    data = [0x55, 0x00, 0xFF, 0x0F] + [rng.randrange(256) for _ in range(nbytes - 4)]
    bits = uart_bits(data)
    prog = pe_asm.assemble(test_slu.UART_HTX_FW, "uart_htx")
    image = thm.image_of(prog, [(512, [len(bits)] + bits_words(bits))])
    bench = make_bench(image, {0: thm.helper_pin(0)}, ("usb",), None, "USB helper UART TX")
    bench.watch("tx", lambda uo, uio, oe: uio & 1)
    yield bench, (lambda: bench.words(0)), 4 * len(bits) + 4000, 1000
    ch = bench.changes("tx")
    t_mode = next(c for c, v in ch if v == 1)               # the helper shows the USB idle (TXA 0) until MODE
    t0 = next(c for c, v in ch if v == 0 and c > t_mode)    # first start bit
    got = []
    for k in range(len(data)):
        b0 = t0 + 40 * k
        assert ff.value_at(ch, b0 + 2) == 0, "byte %d: no start bit at cycle %d" % (k, b0)
        got.append(sum(ff.value_at(ch, b0 + 4 * (i + 1) + 2) << i for i in range(8)))
    off = [c for c, _ in ch if c >= t0 and (c - t0) % 4]
    status = bench.words(0)[0]
    assert got == data, "decoded %s" % ["%02X" % b for b in got[:8]]
    assert not off, "edges off the bit grid at cycles %s" % off[:8]
    assert status & 1 and not status & 4, "status %04x: TXDONE without TXUNDER expected" % status
    return "%d bytes back to back at 4 cycles a bit, decoded equal, 0 edges off the grid; status %04x" % (
        len(data), status)


def uart_hrx_proc(make_bench, nbytes=32, seed=1120):
    """test_slu.test_slu_chip_uart_rx with a per-cycle sender: thread 0 configures the USB helper (clocked code, RX on,
    TXPER 2 = 4 cycles a bit, DIN = ui_in[0], LTC[10] edge-started frames of 10 samples) and stores every received
    word from data address 768. The sender sends back-to-back 8N1 bytes at -2.5 % and then at +2.5 % of the bit
    time. Check: one word a byte, data in bits 7 to 14, start bit 6 = 0, stop bit 15 = 1."""
    import test_slu
    rng = random.Random(seed)
    data = [rng.randrange(256) for _ in range(2 * nbytes)]
    frames, t = [], 300
    for k, d in enumerate(data):
        bt = 4 * (0.975 if k < nbytes else 1.025)
        b = [0] + [(d >> i) & 1 for i in range(8)] + [1]
        frames.append((round(t), b, bt))
        t += 10 * bt
    line = [(0, 1)]
    for t0, b, bt in frames:
        for i, bit in enumerate(b):
            c = t0 + round(i * bt)
            if bit != line[-1][1]:
                line.append((c, bit))
    prog = pe_asm.assemble(test_slu.UART_HRX_FW, "uart_hrx")
    image = thm.image_of(prog, [(768, [0xFFFF] * (len(data) + 8))])
    dev = lambda c, uo, uio, oe: (ff.value_at(line, c), 0)  # noqa: E731 - ui_in[0] = the UART line
    bench = make_bench(image, {}, ("usb",), dev, "USB helper UART RX")
    end = round(t) + 400
    yield bench, (lambda: False), end, 1000
    got = [bench.m.mem[768 + k] for k in range(len(data))]
    bad = [k for k, (w, d) in enumerate(zip(got, data)) if (w >> 7) & 0xFF != d or (w >> 6) & 1 or not (w >> 15) & 1]
    assert not bad, "words %s for bytes %s" % ([hex(got[k]) for k in bad[:4]], [hex(data[k]) for k in bad[:4]])
    assert bench.m.mem[768 + len(data)] == 0xFFFF, "a word after the last byte"
    return "%d bytes received (sender -2.5 %% then +2.5 %%), %d words equal (start 0, stop 1)" % (len(data), len(got))


# ----- tests -----

async def _lockstep(dut, proc_fn):
    result, bench = await drive_rtl(dut, proc_fn)
    if GL:
        dut._log.info("gate level: %s; %d model cycles, pins and host words in lockstep with the model" % (
            result, bench.m.cycle))
    else:
        dut._log.info("RTL: %s; %d model cycles in lockstep, %d helper register comparisons, all equal" % (
            result, bench.m.cycle, bench.m.compared))


@cocotb.test(skip=VERILATOR)
async def test_helpers_rtl_usb_enum(dut):
    """USB enumeration (test_helpers_model.usb_enum_proc) on the RTL; pins and USB helper registers equal to the
    model in every cycle; the checks of test_usb_dev.enumerate_once pass on the chip. At gate level (the waveform
    samples of the simulator): the pins and host words equal to the model's."""
    await _lockstep(dut, thm.usb_enum_proc)


@cocotb.test(skip=GL or VERILATOR)
async def test_helpers_rtl_usb_errors(dut):
    """Damaged USB packets (test_helpers_model.usb_errors_proc) on the RTL, model in lockstep."""
    await _lockstep(dut, thm.usb_errors_proc)


@cocotb.test(skip=VERILATOR)
async def test_helpers_rtl_eth(dut):
    """10BASE-T receive and send (test_helpers_model.eth_rxtx_proc) on the RTL, model in lockstep (at gate level:
    pins and host words)."""
    await _lockstep(dut, thm.eth_rxtx_proc)


@cocotb.test(skip=GL or VERILATOR)
async def test_helpers_rtl_eth_collision(dut):
    """10BASE-T link pulses and collision (test_helpers_model.eth_collision_proc) on the RTL, model in lockstep."""
    await _lockstep(dut, thm.eth_collision_proc)


@cocotb.test(skip=GL or VERILATOR)
async def test_helpers_rtl_uart_tx(dut):
    """USB helper as a UART transmitter (NRZ, 15 Mbaud) on the RTL, model in lockstep."""
    await _lockstep(dut, uart_htx_proc)


@cocotb.test(skip=GL or VERILATOR)
async def test_helpers_rtl_uart_rx(dut):
    """USB helper as a UART receiver (clocked mode, edge-started frames) on the RTL, model in lockstep."""
    await _lockstep(dut, uart_hrx_proc)

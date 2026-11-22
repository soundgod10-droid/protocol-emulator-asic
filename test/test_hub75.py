# SPDX-License-Identifier: Apache-2.0
"""HUB75 LED panel driver firmware (fw/hub75.s) against an independent panel model, in the reference model and on the
RTL at 60 MHz.

What it does:
  HUB75 has no official document (Adafruit's RGB LED Matrix Basics guide, \"How the Matrix Works\": \"There's no
  'official' documentation out there on how these matrices work\"). The panel model (Hub75Panel) is written from that
  guide (a 32 x 16 panel: 8 interleaved row pairs, row r and row r + 8 driven together, selected by the A, B, C address
  pins; one chain of 16-output constant-current drivers per colour line that works like 74HC595s; the controller clocks
  in 192 bits and latches them) and from the TI SN74HC595 data sheet SCLS041J (6.6 timing requirements at VCC = 4.5 V,
  -40 to 85 C: clock at most 25 MHz, SRCLK and RCLK high or low at least 20 ns, SER set-up before SRCLK rise at least
  25 ns, hold 0 ns, SRCLK rise before RCLK rise at least 19 ns; 8.4: OE high = outputs off) and the SN74HC138 decoder
  function table (row = A + 2B + 4C). It does not use the firmware, the assembler or the processor model. Each colour
  line feeds a chain of COLS stages on the CLK rise (stage k = column k after COLS clocks), LAT's rise copies the
  chains into the output latch, and while OE is low row pair A + 2B + 4C shows the latch. It checks those timing
  limits, that the controller never latches or changes the address while the panel is lit (a ghost row), that every lit
  interval shows exactly the frame-buffer row pair it addresses, and that all row pairs are lit for the same time.
  The model test loads a frame, checks two full refreshes, loads a second frame and checks again; negative controls
  shorten the data set-up and give the firmware a column count that does not match the panel.
  The RTL test drives an 8-column panel in lockstep with the reference model; the same test runs on the gate-level
  netlist (gl_fw workflow).

Why:
  Nineteenth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). The libsigrokdecode
  commit pinned by the project has no HUB75 decoder, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the panel's driver propagation delays, the LED currents and
the board pull-downs on CLK, LAT, A, B, C are not simulated.
"""

import os
import sys

import cocotb
from cocotb.utils import get_sim_time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
PINS = {8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 13: 0, 14: 0, 0: 0, 1: 0, 2: 0, 3: 0, 4: 0}  # UO0..UO6, UIO0..UIO4
ROWS = 8
# SN74HC595 SCLS041J 6.6, VCC = 4.5 V, -40 to 85 C (ns)
T_W, T_SU, T_SR_RC, F_MAX_MHZ = 20.0, 25.0, 19.0, 25.0


class Hub75Panel:
    """HUB75 panel (see the module text) with cols columns per row."""

    def __init__(self, cols=32, us=F_MHZ):
        self.cols, self.us = cols, us
        self.ns = lambda cyc: cyc * 1000.0 / us
        self.chains = [[0] * cols for _ in range(6)]
        self.latch = [[0] * cols for _ in range(6)]
        self.clk = self.lat = 0
        self.oe = 1
        self.addr = 0
        self.data = None
        self.t_data = self.t_rise = self.t_fall = self.t_lat = self.t_lit = None
        self.lit, self.clocks, self.latches, self.errors = [], 0, 0, []
        self.edges = []  # (cycle, name, level) for the waveform review

    def _err(self, c, text):
        if len(self.errors) < 20:
            self.errors.append(f"cycle {c}: {text}")

    def __call__(self, c, uo, uio, oe):
        def drv(bit):
            return (uio >> bit) & 1 if (oe >> bit) & 1 else 0  # released: board pull-down
        data = uo & 0x3F
        o = (uo >> 6) & 1
        clk, lat = drv(0), drv(1)
        addr = drv(2) | drv(3) << 1 | drv(4) << 2
        if data != self.data:
            self.data, self.t_data = data, c
        if addr != self.addr:
            if not self.oe:
                self._err(c, f"address changed to {addr} while the panel is lit (ghost row)")
            self.addr = addr
        if clk != self.clk:
            self.edges.append((c, "clk", clk))
            if clk:
                if self.t_data is not None and self.clocks and self.ns(c - self.t_data) < T_SU:
                    self._err(c, f"data set-up {self.ns(c - self.t_data):.1f} ns before the CLK rise (25 ns)")
                if self.t_fall is not None and self.ns(c - self.t_fall) < T_W:
                    self._err(c, f"CLK low {self.ns(c - self.t_fall):.1f} ns (20 ns)")
                if self.t_rise is not None and 1000.0 / self.ns(c - self.t_rise) > F_MAX_MHZ:
                    self._err(c, f"clock {1000.0 / self.ns(c - self.t_rise):.1f} MHz (25 MHz)")
                for k in range(6):
                    self.chains[k] = [(data >> k) & 1] + self.chains[k][:-1]
                self.t_rise = c
                self.clocks += 1
            else:
                if self.t_rise is not None and self.ns(c - self.t_rise) < T_W:
                    self._err(c, f"CLK high {self.ns(c - self.t_rise):.1f} ns (20 ns)")
                self.t_fall = c
            self.clk = clk
        if lat != self.lat:
            self.edges.append((c, "lat", lat))
            if lat:
                if not self.oe:
                    self._err(c, "latch while the panel is lit (ghost row)")
                if self.t_rise is not None and self.ns(c - self.t_rise) < T_SR_RC:
                    self._err(c, f"LAT rise {self.ns(c - self.t_rise):.1f} ns after the CLK rise (19 ns)")
                self.latch = [list(ch) for ch in self.chains]
                self.latches += 1
                self.t_lat = c
            elif self.t_lat is not None and self.ns(c - self.t_lat) < T_W:
                self._err(c, f"LAT high {self.ns(c - self.t_lat):.1f} ns (20 ns)")
            self.lat = lat
        if o != self.oe:
            self.edges.append((c, "oe", o))
            if not o:
                self.t_lit = c
            elif self.t_lit is not None:
                self.lit.append(dict(t=self.t_lit, dur=c - self.t_lit, row=self.addr,
                                     pix=[sum(self.latch[k][col] << k for k in range(6)) for col in range(self.cols)]))
            self.oe = o
        return 0, 0


def frame_words(frame):
    """Host words of a frame: rows x cols pixels (6 bits each), two columns a word, bit 15 on the first."""
    out = []
    for r, row in enumerate(frame):
        for c in range(0, len(row), 2):
            out.append(row[c] | row[c + 1] << 8)
    out[0] |= 0x8000
    return out


def make_frame(cols, f):
    return [[f(r, c) & 63 for c in range(cols)] for r in range(ROWS)]


FRAME_A = lambda cols: make_frame(cols, lambda r, c: r * 7 + c * 5 + 1)
FRAME_B = lambda cols: make_frame(cols, lambda r, c: 63 if (r + c) % 2 else 0)


def check_frame(panel, frame, since, label):
    shown = [x for x in panel.lit if x["t"] >= since]
    assert len(shown) >= 2 * ROWS, f"{label}: {len(shown)} lit intervals"
    bad = [(x["t"], x["row"]) for x in shown if x["pix"] != frame[x["row"]]]
    assert not bad, f"{label}: lit intervals with other data than the frame row: {bad[:4]}"
    rows = sorted({x["row"] for x in shown})
    assert rows == list(range(ROWS)), f"{label}: rows lit {rows}"
    durs = [x["dur"] for x in shown]
    assert max(durs) - min(durs) <= 1, f"{label}: lit times {min(durs)} to {max(durs)} cycles"
    return len(shown), min(durs)


def assemble(**equ):
    return ff.assemble("hub75.s", **equ)


def row_period(cols, colt=64, hostt=400):
    return hostt + cols * colt + 20


@cocotb.test(skip=GL)
async def test_hub75_words(dut):
    """Memory words of fw/hub75.s."""
    size = assemble().size
    dut._log.info(f"fw/hub75.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_hub75_model(dut):
    """fw/hub75.s in the reference model against the panel model: two frames, each checked over two full refreshes;
    negative controls for the CLK high time, the data set-up and a column count that does not match the panel."""
    log = dut._log
    cols = 32
    frame_t = ROWS * row_period(cols)
    panel = Hub75Panel(cols)
    m = pe_model.PeModel(assemble().words, owner=ff.owners(PINS))
    bench = ff.Bench(m, inputs=panel)
    wa = frame_words(FRAME_A(cols))
    bench.send(0, *wa)
    bench.run(6 * frame_t)  # 128 words at 4 a row pair: 4 refreshes to load
    since = m.cycle
    bench.run(2 * frame_t + row_period(cols))  # a lit interval counts when it ends: one row period of slack
    assert not panel.errors, f"frame A: {panel.errors[:3]}"
    n, dur = check_frame(panel, FRAME_A(cols), since, "frame A")
    ff.assert_status_clean(m, [0])
    log.info(f"frame A ({len(wa)} host words): {n} lit intervals all equal to the frame rows, each lit {dur} cycles "
             f"({dur / F_MHZ:.2f} us), refresh {frame_t / F_MHZ:.1f} us ({F_MHZ * 1e6 / frame_t:.0f} Hz), "
             f"{panel.clocks} clocks, {panel.latches} latches")
    bench.send(0, *frame_words(FRAME_B(cols)))
    bench.run(6 * frame_t)
    since = m.cycle
    bench.run(2 * frame_t + row_period(cols))
    assert not panel.errors, f"frame B: {panel.errors[:3]}"
    n, dur = check_frame(panel, FRAME_B(cols), since, "frame B")
    ff.assert_status_clean(m, [0])
    log.info(f"frame B: {n} lit intervals all equal to the frame rows")
    for equ, want in ((dict(CHI=2, SU=1), "data set-up"), (dict(COLS=16, CSH=4), "other data")):
        panel = Hub75Panel(cols)
        m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners(PINS))
        bench = ff.Bench(m, inputs=panel)
        bench.send(0, *wa)
        bench.run(6 * frame_t)
        since = m.cycle
        bench.run(2 * frame_t)
        found = panel.errors[:1]
        if not found:
            try:
                check_frame(panel, FRAME_A(cols), since, "control")
            except AssertionError as e:
                found = [str(e)]
        found = [f for f in found if want in f]
        log.info(f"control {equ}: {found[0][:160] if found else 'no fault found'}")
        assert found, f"control {equ}: expected a fault with '{want}', got {panel.errors[:2]}"


@cocotb.test()
async def test_hub75_rtl(dut):
    """fw/hub75.s (COLS 8) on the chip at 60 MHz against an 8-column panel model: one frame checked over two
    refreshes, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl hub75"
    cols = 8
    frame_t = ROWS * row_period(cols)
    panel = Hub75Panel(cols)
    bench = rf.RtlBench(dut, assemble(COLS=8, CSH=3).words, owner=ff.owners(PINS), inputs=panel, label=label)
    bench.send(0, *frame_words(FRAME_A(cols)))
    if hasattr(dut, "dump_off"):  # waveform of the first row pair shift, latch and light (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 2 * row_period(cols))
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; first edges (ps, pin, level) "
                 f"{[(round(now - (bench.m.cycle - c) * rf.CLK_PS), n, v) for c, n, v in panel.edges[:22]]}")
    await bench.run(rf.HOST_START + 3 * frame_t - bench.m.cycle)
    since = bench.m.cycle
    await bench.run(2 * frame_t + 500)
    await bench.finish(log)
    assert not panel.errors, f"{label}: {panel.errors[:3]}"
    n, dur = check_frame(panel, FRAME_A(cols), since, label)
    log.info(f"{label}: {n} lit intervals equal to the frame rows, each lit {dur} cycles; {panel.clocks} clocks, "
             f"{panel.latches} latches")

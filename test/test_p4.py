# SPDX-License-Identifier: Apache-2.0
"""Four rows of list P at once: one firmware on each of the four threads, each against its own independent model, in
the reference model and on the RTL at 60 MHz.

What it does:
  Loads fw/hx711.s (thread 0), fw/asi.s (thread 1), fw/sega6.s (thread 2) and fw/dmx512.s (thread 3) into one memory
  image, each at its own start address, and runs the four threads together. The pins are moved with the firmware's
  .equ names so that no two programs share a pin: HX711 PD_SCK = UO4 and DOUT = UI3 (fixed in fw/hx711.s, which reads
  DOUT through IOR PINS), AS-i IRD = UO1 and LED1 = UI0, Sega SELECT = UO0 with the pad on uio[0..5], DMX_TX = UO2.
  Each device model is the model of that row's own test (test_hx711.Hx711, test_asi.AsiLine, test_sega6.Sega6Pad,
  test_dmx512.dmx_receive), unchanged; a small adapter moves its pin bits to the chip pins used here. Each row must
  give the same results as in its own test: the HX711 reads, the AS-i responses and the slaves' outputs, the pad's
  buttons, the DMX512 packets, with no model fault and no LATE or ERR on any thread. The RTL test runs in lockstep with
  the reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  C1 WP10 (functions at once with new firmware): the threads share one memory and one barrel, so four protocols that
  each pass alone must also pass together, with one image of 4 programs.

Conditions: logic simulation at 60 MHz with ideal pins; the HX711 conversion period is shortened to 0.2 ms as in
test_hx711_rtl.
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
import test_asi as asi  # noqa: E402
import test_dmx512 as dmx  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_hx711 as hx  # noqa: E402
import test_rtl_fw as rf  # noqa: E402
import test_sega6 as sg  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
P_SCK, P_IRD, P_SEL, P_DMX = 12, 9, 8, 10  # UO4, UO1, UO0, UO2
OWNER = {P_SCK: 0, P_IRD: 1, P_SEL: 2, P_DMX: 3}
PERIOD = 12_000  # HX711 conversion period (0.2 ms, as test_hx711_rtl)
HX_CODES = [1, 2]
HX_SEL = ["A128", "A128"]
ASI_REQS = [asi.REQS[1], asi.REQS[4], asi.REQS[2]]
SEGA_SETS = [["X", "Start"], ["up", "A", "Z"]]
DMX_PACKETS = dmx.PACKETS[1:3]


class Remap:
    """Runs a device model written for UO4 / UI3 on other chip pins: uo_map {model uo bit: chip uo bit}, ui_map
    {model ui bit: chip ui bit}, uio_mask: the uio bits the model drives (it sees the chip's uio unchanged)."""

    def __init__(self, model, uo_map, ui_map=None, uio_mask=0):
        self.model, self.uo_map, self.ui_map, self.uio_mask = model, uo_map, ui_map or {}, uio_mask

    def __call__(self, c, uo, uio, oe):
        muo = 0
        for mb, cb in self.uo_map.items():
            muo |= ((uo >> cb) & 1) << mb
        ui, uin = self.model(c, muo, uio, oe)
        cui = 0
        for mb, cb in self.ui_map.items():
            cui |= ((ui >> mb) & 1) << cb
        return cui, uin & self.uio_mask


def image():
    """One memory image of the four programs, each at its own start address; returns (words, starts, sizes)."""
    progs, starts, base = [], [], 0
    for name, equ in (("hx711.s", {}), ("asi.s", dict(IRD="UO1", LED1="UI0")), ("sega6.s", dict(SEL="UO0")),
                      ("dmx512.s", dict(TX="UO2"))):
        p = ff.assemble(name, base, **equ)
        progs.append((name, p))
        starts.append(base)
        base += p.size
    words = {}
    for _, p in progs:
        words.update(p.words)
    return words, tuple(starts), [(n, p.size) for n, p in progs]


def devices():
    dev = dict(hx=hx.Hx711(PERIOD), asi=asi.AsiLine(asi.SLAVES), pad=sg.Sega6Pad(SEGA_SETS))
    maps = [Remap(dev["hx"], {4: 4}, {3: 3}), Remap(dev["asi"], {4: 1}, {3: 0}), Remap(dev["pad"], {4: 0}, {},
                                                                                        uio_mask=0x3F)]

    def inputs(c, uo, uio, oe):
        ui = uin = 0
        for f in maps:
            a, b = f(c, uo, uio, oe)
            ui, uin = ui | a, uin | b
        return ui, uin
    return dev, inputs


def send_all(bench):
    bench.send(0, *HX_CODES)
    bench.send(1, *ASI_REQS)
    bench.send(2, *range(len(SEGA_SETS)))
    bench.send(3, *dmx.host_words(DMX_PACKETS))


def done(bench):
    return (len(bench.words(0)) >= 2 * len(HX_CODES) and len(bench.words(1)) >= len(ASI_REQS)
            and len(bench.words(2)) >= len(SEGA_SETS))


def check(bench, dev, end, label):
    """Every row's own checks; returns the summary lines."""
    hx.check(dev["hx"], bench.words(0), f"{label} hx711", want=[w for s in HX_SEL for w in hx.words_of(s)],
             want_sel=HX_SEL)
    want_asi = asi.expect(asi.SLAVES, ASI_REQS)
    assert not dev["asi"].errors and bench.words(1) == want_asi, \
        f"{label} asi: {dev['asi'].errors[:3]} {[hex(w) for w in bench.words(1)]}"
    want_pad = [sg.expect(s) for s in SEGA_SETS]
    assert bench.words(2) == want_pad, f"{label} sega6: {[hex(w) for w in bench.words(2)]}"
    got, stats = dmx.check(bench.changes("dmx"), end, DMX_PACKETS, f"{label} dmx512")
    return [f"{label}: thread 0 hx711 reads {[(r[1], hex(r[2]), r[3]) for r in dev['hx'].reads]}, host words "
            f"{[hex(w) for w in bench.words(0)]}",
            f"{label}: thread 1 asi host words {[hex(w) for w in bench.words(1)]}, slaves kept {dev['asi'].outputs}",
            f"{label}: thread 2 sega6 host words {[hex(w) for w in bench.words(2)]}",
            f"{label}: thread 3 dmx512 {dmx.summary(got, stats)}"]


@cocotb.test(skip=GL)
async def test_p4_model(dut):
    """The four programs from one image on the four threads of the reference model, each against its own model."""
    log = dut._log
    words, starts, sizes = image()
    log.info(f"image: {sizes}, start addresses {starts}, {sum(s for _, s in sizes)} of {pe_model.PROG_WORDS} words")
    assert sum(s for _, s in sizes) <= pe_model.PROG_WORDS
    dev, inputs = devices()
    m = pe_model.PeModel(words, starts, (True,) * 4, ff.owners(OWNER), [False] * pe_model.N_OUT)
    bench = ff.Bench(m, inputs=inputs)
    bench.watch("dmx", lambda uo, uio, oe: (uo >> 2) & 1)
    send_all(bench)
    total = dmx.run_cycles(DMX_PACKETS)
    while m.cycle < 400_000 and (not done(bench) or m.cycle < total):
        bench.run(10_000)
    bench.run(2_000)
    ff.assert_status_clean(m, [0, 1, 2, 3])
    for line in check(bench, dev, m.cycle, "model"):
        log.info(line)
    log.info(f"model: {m.cycle} cycles, all four threads RUN with no LATE or ERR")


@cocotb.test()
async def test_p4_rtl(dut):
    """The four programs from one image on the four threads of the chip at 60 MHz, each against its own model, in
    lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl p4"
    words, starts, sizes = image()
    dev, inputs = devices()
    bench = rf.RtlBench(dut, words, start_pc=starts, run=(True,) * 4, owner=ff.owners(OWNER), inputs=inputs,
                        label=label)
    bench.watch("dmx", lambda uo, uio, oe: (uo >> 2) & 1)
    send_all(bench)
    total = rf.HOST_START + dmx.run_cycles(DMX_PACKETS)
    if hasattr(dut, "dump_off"):  # waveform of the start, with three threads active at once (tb.fst), for review
        bench.watch("sel", lambda uo, uio, oe: uo & 1)
        bench.watch("ird", lambda uo, uio, oe: (uo >> 1) & 1)
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 12_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        ps = lambda c: round(now - (bench.m.cycle - c) * rf.CLK_PS)  # noqa: E731
        firsts = {n: [(ps(c), v) for c, v in bench.changes(n)[1:4]] for n in ("sel", "ird", "dmx")}
        log.info(f"{label}: waveform from the start; first changes (ps, level) {firsts}")
    for _ in range(60):
        if done(bench) and bench.m.cycle >= total:
            break
        await bench.run(10_000)
    await bench.run(2_000)
    await bench.finish(log)
    lines = check(bench, dev, bench.m.cycle, label)
    log.info(f"{label}: image {sizes}, start addresses {starts}")
    for line in lines:
        log.info(line)

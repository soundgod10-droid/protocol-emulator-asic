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


P_ROWS = ["dmx512.s", "dcc.s", "sbus_tx.s", "sbus_rx.s", "lin_cmd.s", "lin_resp.s", "hd44780.s", "hx711.s", "irda_tx.s",
          "irda_rx.s", "oregon.s", "ssi.s", "tm1637.s", "mdb.s", "tlc5940.s", "unio.s", "sent.s", "adb.s", "hub75.s",
          "joybus.s", "iolink.s", "mil1553_bc.s", "mil1553_rt.s", "sdi12.s", "centronics.s", "sega6.s", "asi.s"]
IOR_UIO = {"sega6.s": set(range(6))}  # uio inputs read with IOR PINS, which the words do not name


def pin_use(words):
    """Pins named by the program words (isa 11.2): OUT pins (0-7 uio, 8-14 uo_out) and SAMPLE / WAIT pins on uio
    (0-7); ui_in pins are inputs that programs can share. Also whether the program uses data memory (LDX / STX)."""
    pins, dmem = set(), False
    for w in words.values():
        op = w >> 12
        if op == 0xC:
            p, kind = (w >> 8) & 15, (w >> 3) & 7
            if kind <= 5 or p < 8:
                pins.add(p)
        elif op == 0xD and ((w >> 8) & 15) < 8:
            pins.add((w >> 8) & 15)
        elif op == 0 and (w >> 8) in (3, 4):
            dmem = True
    return pins, dmem


@cocotb.test(skip=GL)
async def test_p4_fit(dut):
    """Fit matrix of the list P programs (C1 WP10): word count of each, and the sets of 2 to 4 that fit in the program
    memory and the 4 threads; for each set, whether two programs use one uio or uo_out pin at their default .equ pins,
    and whether more than one uses data memory (fixed buffers at the default .equ). The clash-free sets of 4 with the
    most words are assembled at consecutive base addresses as a check of the sums."""
    import itertools
    log = dut._log
    info = {}
    for n in P_ROWS:
        p = ff.assemble(n)
        pins, dmem = pin_use(p.words)
        info[n] = (p.size, pins | IOR_UIO.get(n, set()), dmem)
    name = lambda p: f"uio{p}" if p < 8 else f"uo{p - 8}"  # noqa: E731
    for n in P_ROWS:
        size, pins, dmem = info[n]
        log.info(f"P program {n}: {size} words; pins {', '.join(name(p) for p in sorted(pins)) or 'none (ui only)'}"
                 f"{'; data memory' if dmem else ''}")
    fits, clean = [], []
    for k in range(2, pe_model.N_THREADS + 1):
        for combo in itertools.combinations(P_ROWS, k):
            total = sum(info[n][0] for n in combo)
            if total > pe_model.PROG_WORDS:
                continue
            pins = [p for n in combo for p in info[n][1]]
            shared = sorted({p for p in pins if pins.count(p) > 1})
            dm = sum(info[n][2] for n in combo)
            fits.append((combo, total, shared, dm))
            if not shared and dm <= 1:
                clean.append((combo, total))
    four = [c for c in clean if len(c[0]) == 4]
    for combo, total in sorted(four, key=lambda c: -c[1])[:5]:
        base = 0
        for n in combo:
            base += ff.assemble(n, base).size
        assert base == total, (combo, base, total)
    by_k = {k: (sum(1 for f in fits if len(f[0]) == k), sum(1 for c in clean if len(c[0]) == k)) for k in (2, 3, 4)}
    biggest = max(fits, key=lambda f: f[1])
    log.info(f"P programs: {len(P_ROWS)} ({sum(info[n][0] for n in P_ROWS)} words in all, largest "
             f"{max(info[n][0] for n in P_ROWS)}); sets that fit in {pe_model.PROG_WORDS} words and 4 threads: "
             + "; ".join(f"{k} programs {a} (no shared pin or data memory at the default .equ: {b})"
                         for k, (a, b) in by_k.items())
             + f"; the largest set {' + '.join(biggest[0])} = {biggest[1]} words; the 5 clean sets of 4 with the most "
             f"words assemble at consecutive bases to the same sums")
    assert all(f[1] <= pe_model.PROG_WORDS for f in fits) and by_k[4][1] > 0


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

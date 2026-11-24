# SPDX-License-Identifier: Apache-2.0
"""SPI target at 2 MHz: fw/spi_tgt2.s (two threads) against an independent SPI controller model, in the reference
model and on the RTL at 60 MHz, and the single-thread loop structures it was compared with.

What it does:
  The controller model is test_fw_full.SpiController (written from CPOL and CPHA, not from the firmware): CS, SCK and
  MOSI of each mode with a half period of h cycles, MOSI changed at the shift edges, MISO read in the cycle of each
  sample edge. For each of four loop structures (A: fw/spi_tgt.s as it is; B: one thread, SAMPLE of MOSI before the
  OUT of MISO; C: one thread, MOSI read with IOR PINS in the slot after the WAIT; D: fw/spi_tgt2.s, two threads) and
  each of the four modes, the sweep lowers the half period from 25 cycles until a case fails at one of 4 start
  phases (transfers of 5 and 2 bytes, CS lead 30 cycles, CS high 600 cycles between transfers), and records the
  smallest passing half period, the fault at the first failure, and at the smallest passing value: the smallest
  MOSI hold after the sample edge, the smallest CS lead, the MISO set-up and hold at the sample edges and the
  release time after CS. The negative control is the half period one below the smallest passing value.
  The RTL test runs fw/spi_tgt2.s on the chip at the rated half period (the smallest passing value plus 2 cycles
  of margin) in all four modes at 4 start phases, in lockstep with the reference model; the same test runs on the
  gate-level netlist (gl_fw workflow).

Why:
  fw/spi_tgt.s is correct up to a half period of 25 cycles (1.2 MHz at 60 MHz), set by the instruction rate of one
  thread (docs/host_port.md section 13). This file measures what a different loop structure or a second thread gives
  with the same chip, and keeps the alternatives that fall short with their numbers.

Conditions: logic simulation at 60 MHz with ideal pins; the pad delays (docs/verification.md section 9) and the board
multiplexer are not in it. The chip samples an input at the rising clock edge through a 2-stage synchronizer, so a
pin change at any time inside a clock cycle gives the same sample: the 4 start phases cover the phase of the SCK edges
against the thread's slot.
"""

import os
import sys

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
H_RATED = 17  # rated half period of fw/spi_tgt2.s: the smallest passing value of the sweep (15) + 2 cycles of margin
XFERS = ff.SPI_TGT_XFERS

HEAD = """
        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    SE, 1
        .equ    GAP, 200
        TNOW    8
        OUT     MISO, Z
        PULL    r0
        JMP     got
idle:   LDI     r0, 0x3FC
        PULL.NB r0
got:    SHL     r0, 6
        LDI     r2, 7
        WAIT    CS, FALL, r3, CT
        TADD    12
        OUT     MISO, r0<<, 4
        TADD    GAP
"""
TAIL = """
done:   TNOW    8
        OUT     MISO, Z
        JMP     idle
"""
# B: one thread, the loop of fw/spi_tgt.s reordered: SAMPLE of MOSI first (edge + 16, the earliest on-time sample
# after a WAIT: TADD 12 and offset 4 give a lead of 4 to 7), then OUT of MISO at edge + 32 (TADD 16, offset 4:
# the earliest on-time event after the SAMPLE completes at edge + 17..20).
TEXT_B = HEAD + """
first:  WAIT    SCK, SE, r3, TO, CT
        BEC     done
        TADD    12
        SAMPLE  MOSI, r1<<, 4
        TADD    16
        OUT     MISO, r0<<, 4
        LDI     r3, 0x3FC
        PULL.NB r3
        OR      r0, r3
bit:    WAIT    SCK, SE, r3, CT
        TADD    12
        SAMPLE  MOSI, r1<<, 4
        TADD    16
        OUT     MISO, r0<<, 4
        DJNZ    r2, bit
        PUSH    r1
        LDI     r2, 7
        TADD    GAP
        JMP     first
""" + TAIL
# C: one thread, MOSI read with IOR PINS in the slot after the WAIT (edge + 5..8, no lead), MISO at edge + 16
# (edge + 20 at the first bit of a byte, after the timeout check); the bit is masked and shifted in (3 slots).
TEXT_C = HEAD + """
first:  WAIT    SCK, SE, r3, TO, CT
        BEC     done
        IOR     r1, PINS
        TADD    16
        OUT     MISO, r0<<, 4
        ANDI    r1, 2
        LDI     r3, 0x3FC
        PULL.NB r3
        OR      r0, r3
bit:    WAIT    SCK, SE, r3, CT
        IOR     r3, PINS
        TADD    12
        OUT     MISO, r0<<, 4
        ANDI    r3, 2
        SHL     r1, 1
        OR      r1, r3
        DJNZ    r2, bit
        SHR     r1, 1
        PUSH.NB r1
        TADD    GAP
        JMP     first
""" + TAIL

STRUCTURES = {
    "A": "fw/spi_tgt.s, one thread (SAMPLE after the OUT, as it is)",
    "B": "one thread, reordered: SAMPLE of MOSI first, then OUT of MISO",
    "C": "one thread, MOSI by IOR PINS in the slot after the WAIT",
    "D": "fw/spi_tgt2.s, two threads: thread 0 MISO, thread 1 MOSI by IOR PINS",
}


def program(name, mode):
    """(assembled program, start_pc, run, thread that gives the MOSI words) of a structure for a mode."""
    se = ff.SPI_TGT_SE[mode]
    if name == "A":
        prog = pe_asm.assemble(ff.spi_tgt_text(mode), "spi_tgt.s", 0)
    elif name == "D":
        prog = pe_asm.assemble(ff.source("spi_tgt2.s", SE=se), "spi_tgt2.s", 0)
        return prog, (prog.symbols["tx"][0], prog.symbols["rx"][0], 0, 0), (True, True, False, False), 1
    else:
        text = (TEXT_B if name == "B" else TEXT_C).replace(".equ    SE, 1", f".equ    SE, {se}")
        prog = pe_asm.assemble(text, f"spi_tgt_{name}.s", 0)
    return prog, (0, 0, 0, 0), (True, False, False, False), 0


def run(name, mode, h, t0=300, transfers=XFERS, **kw):
    prog, start_pc, go, _ = program(name, mode)
    m = pe_model.PeModel(prog.words, start_pc=start_pc, run=go, owner=ff.owners({ff.P_MISO: 0}))
    ctl = ff.SpiController(mode, h, [mosi for mosi, _ in transfers], t0=t0, **kw)
    bench = ff.Bench(m, inputs=ctl)
    for _, miso in transfers:
        bench.send(0, *[byte << 2 for byte in miso], 0)  # N + 1 words for N bytes; the last one is not sent
    bench.run(ctl.end + 1000)
    return bench, m, ctl


def fault(name, bench, m, ctl, transfers=XFERS):
    """Text of the first fault of a run, or None if the run is correct (both directions, status, MISO release)."""
    _, _, go, rx_thread = program(name, 0)
    for n, running in enumerate(go):
        th = m.threads[n]
        if running and (th.status or not th.run):
            return f"thread {n} status {th.status}, run {th.run}, {th.stop_reason}"
    got, f = ctl.received()
    if f:
        return f
    if got != [list(miso) for _, miso in transfers]:
        return f"controller received {[[hex(x) for x in row] for row in got]}"
    words = bench.words(rx_thread)
    if [w & 0xFF for w in words] != [b for mosi, _ in transfers for b in mosi]:
        return f"target words {[hex(w) for w in words]}"
    faults = ctl.miso_timing()[3]
    return faults[0] if faults else None


def all_phases(name, mode, h, offsets=range(4), **kw):
    """None if the run is correct at each start phase (start cycle 300 + offset), else the first fault."""
    for off in offsets:
        f = fault(name, *run(name, mode, h, t0=300 + off, **kw))
        if f:
            return f"start phase {off}: {f}"
    return None


def last_ok(fn, values):
    """Last value of `values` (in their order) before the first one with a fault, and that fault."""
    last = None
    for v in values:
        f = fn(v)
        if f:
            return last, f"{v}: {f}"
        last = v
    return last, None


def timing(name, mode, h, **kw):
    """Smallest MISO set-up and hold at the sample edges and the largest release time over the 4 start phases."""
    rows = [run(name, mode, h, t0=300 + off, **kw)[2].miso_timing() for off in range(4)]
    return min(r[0] for r in rows), min(r[1] for r in rows), max(r[2] for r in rows)


@cocotb.test(skip=GL)
async def test_spi_tgt2_words(dut):
    """Memory words of fw/spi_tgt2.s and of the alternatives."""
    for name in STRUCTURES:
        prog = program(name, 0)[0]
        dut._log.info(f"{name}: {STRUCTURES[name]}: {prog.size} words")
    assert program("D", 0)[0].size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_spi_tgt2_sweep(dut):
    """Four loop structures in the reference model against the SPI controller model: smallest half period in each
    mode, the fault at the first failure, MOSI hold, CS lead, MISO set-up and hold at that half period; the control
    one below must fail. fw/spi_tgt2.s must pass at 15 cycles (2 MHz) in every mode, and at every half period from
    15 to 40."""
    log = dut._log
    lines = []
    smallest = {}
    for name in STRUCTURES:
        for mode in range(4):
            h, hf = last_ok(lambda v: all_phases(name, mode, v), range(25, 7, -1))
            smallest[(name, mode)] = h
            if h is None:
                lines.append(f"{name} mode {mode}: half period 25 fails: {hf}")
                continue
            hold, hof = last_ok(lambda v: all_phases(name, mode, h, hold=v), range(h, 0, -1))
            lead, lf = last_ok(lambda v: all_phases(name, mode, h, lead=v), range(60, 0, -1))
            gap, gf = last_ok(lambda v: all_phases(name, mode, h, gap=v), range(600, 0, -20))
            su, ho, rel = timing(name, mode, h)
            lines.append(f"{name} mode {mode} (CPOL {mode >> 1}, CPHA {mode & 1}): smallest half period {h} cycles = "
                         f"{F_MHZ * 1e3 / (2 * h):.0f} kHz; control {hf}; MOSI hold after the sample edge >= {hold} "
                         f"(hold {hof}); CS low to the first edge >= {lead} (lead {lf}); CS high between transfers "
                         f">= {gap} (gap {gf}); MISO set-up >= {su}, hold >= {ho} cycles at the sample edge, "
                         f"released <= {rel} cycles after the CS rise")
    for name in STRUCTURES:
        log.info(f"{name}: {STRUCTURES[name]}")
    for line in lines:
        log.info(line)
    os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
    with open(os.path.join(ROOT, "build", "spi_tgt2_sweep.txt"), "w", encoding="utf-8") as f:  # the full result table
        f.write("\n".join([f"{k}: {v}" for k, v in STRUCTURES.items()] + lines) + "\n")
    # fw/spi_tgt2.s: every half period from 15 to 40 passes in every mode (no hole above the limit)
    holes = [(mode, v) for mode in range(4) for v in range(15, 41) if all_phases("D", mode, v)]
    log.info(f"D: half periods 15 to 40, all modes, 4 phases: {'no hole' if not holes else holes}")
    assert not holes, f"fw/spi_tgt2.s fails at {holes}"
    assert all(smallest[("D", mode)] is not None and smallest[("D", mode)] <= 15 for mode in range(4)), smallest
    assert H_RATED >= max(smallest[("D", mode)] for mode in range(4)) + 2, (H_RATED, smallest)


@cocotb.test()
async def test_spi_tgt2_rtl(dut):
    """fw/spi_tgt2.s on the chip at 60 MHz in all four modes at the rated half period (H_RATED cycles) and 4 start
    phases, against the controller model, in lockstep with the reference model: MISO bytes at the sample edges, the
    MOSI bytes in the host words of thread 1, MISO released while CS is high."""
    rf.start_clock(dut)
    log = dut._log
    for mode in range(4):
        prog, start_pc, go, _ = program("D", mode)
        for off in range(4):
            ctl = ff.SpiController(mode, H_RATED, [mosi for mosi, _ in XFERS], lead=30, gap=600,
                                   t0=rf.HOST_START + 1000 + off)
            label = f"rtl spi_tgt2 mode {mode} half period {H_RATED} ({F_MHZ * 1e3 / (2 * H_RATED):.0f} kHz), phase {off}"
            bench = rf.RtlBench(dut, prog.words, start_pc=start_pc, run=go, owner=ff.owners({ff.P_MISO: 0}),
                                inputs=ctl, label=label)
            for _, miso in XFERS:
                bench.send(0, *[byte << 2 for byte in miso], 0)
            if mode == 0 and off == 0 and hasattr(dut, "dump_off"):  # waveform of the first transfer, for review
                await bench.run(rf.HOST_START + 900)
                dut.dump_off.value = 0
                await bench.run(2400)
                dut.dump_off.value = 1
            await bench.run(ctl.end + 1000 - bench.m.cycle)
            await bench.collect(1, sum(len(mosi) for mosi, _ in XFERS))
            f = fault("D", bench, bench.m, ctl)
            assert f is None, f"{label}: {f}; {bench.diag()}"
            su, hold, rel, _ = ctl.miso_timing()
            log.info(f"{label}: correct; MISO set-up >= {su}, hold >= {hold} cycles at the sample edge, released <= "
                     f"{rel} cycles after the CS rise")
            await bench.finish(log)

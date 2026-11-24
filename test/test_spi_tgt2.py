# SPDX-License-Identifier: Apache-2.0
"""SPI target at 2 MHz: fw/spi_tgt2.s (two threads) and fw/spi_tgt3.s (three threads) against an independent SPI
controller model, in the reference model and on the RTL at 60 MHz, and the loop structures they were compared with.

What it does:
  The controller model is test_fw_full.SpiController (written from CPOL and CPHA, not from the firmware): CS, SCK and
  MOSI of each mode with a half period of h cycles, MOSI changed at the shift edges, MISO read in the cycle of each
  sample edge. Six loop structures: A fw/spi_tgt.s as it is; B one thread, SAMPLE of MOSI before the OUT of MISO;
  C one thread, MOSI read with IOR PINS in the slot after the WAIT; D0 the first two-thread version (6 instructions
  after the WAIT at the byte boundary of both threads); D fw/spi_tgt2.s (two threads, at most 5 after a WAIT);
  E fw/spi_tgt3.s (three threads, at most 4). For each structure and each of the four modes, the sweep lowers the
  half period from 25 cycles until a case fails at one of 4 start phases (transfers of 5 and 2 bytes, CS lead 30
  cycles, CS high 600 cycles between transfers), and records the smallest passing half period, the fault at the
  first failure (the negative control), and at the smallest passing value: the smallest MOSI hold after the sample
  edge, the smallest CS lead, the smallest CS high time, the MISO set-up and hold at the sample edges and the
  release time after CS. The table is written to build/spi_tgt2_sweep.txt as well as to the log.
  The RTL tests run fw/spi_tgt2.s and fw/spi_tgt3.s on the chip at their rated half periods (the smallest passing
  value plus 2 cycles of margin) in all four modes at 4 start phases, in lockstep with the reference model; the
  same tests run on the gate-level netlist (gl_fw workflow).

Why:
  fw/spi_tgt.s is correct up to a half period of 25 cycles (1.2 MHz at 60 MHz), set by the instruction rate of one
  thread (docs/host_port.md section 13). The next WAIT must be issued before the next sample edge (docs/isa.md 6.3)
  and a thread has one slot every 4 cycles, so with n instructions between two WAITs the half period H must be
  2 H >= 4 n + 9. This file measures what each loop structure gives with the same chip, and keeps the alternatives
  that fall short with their numbers.

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
H_RATED_D = 17  # rated half period of fw/spi_tgt2.s: the smallest passing value of the sweep (15) + 2 cycles of margin
H_RATED_E = 15  # rated half period of fw/spi_tgt3.s: the smallest passing value of the sweep (13) + 2 cycles of margin
XFERS = ff.SPI_TGT_XFERS

STRUCTURES = {
    "A": "fw/spi_tgt.s, one thread (SAMPLE after the OUT, as it is)",
    "B": "one thread, reordered: SAMPLE of MOSI first, then OUT of MISO",
    "C": "one thread, MOSI by IOR PINS in the slot after the WAIT",
    "D0": "two threads, first version: thread 0 MISO, thread 1 MOSI by IOR PINS, 6 instructions at the byte boundary",
    "D": "fw/spi_tgt2.s, two threads: thread 0 MISO (byte unrolled), thread 1 MOSI (sliding word), at most 5",
    "E": "fw/spi_tgt3.s, three threads: thread 0 MISO, threads 1 and 2 MOSI bytes in turn, at most 4",
}
RX_THREADS = {"A": (0,), "B": (0,), "C": (0,), "D0": (1,), "D": (1,), "E": (1, 2)}  # threads that give the MOSI words
MISO_SHIFT = {"A": 2, "B": 2, "C": 2, "D0": 2, "D": 4, "E": 4}  # host word for a MISO byte: byte << shift

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
# B: one thread, the loop of fw/spi_tgt.s reordered: SAMPLE of MOSI first, then OUT of MISO. Bit loop: sample at
# edge + 16 (TADD 12, offset 4: lead 4 to 7 after the WAIT), MISO at edge + 32 (the earliest on-time event after the
# SAMPLE completes at edge + 17..20). First bit of a byte (BEC before the TADD): sample at edge + 20, MISO at edge + 36.
# The first version had TADD 12 at the first bit: the SAMPLE executed at edge + 13..16 for edge + 16, a lead of 0 to 3,
# and the thread set LATE at every half period (recorded in the stage note).
TEXT_B = HEAD + """
first:  WAIT    SCK, SE, r3, TO, CT
        BEC     done
        TADD    16
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
# C: one thread, MOSI read with IOR PINS in the slot after the WAIT (edge + 5..8, no lead), MISO at edge + 20 in the
# bit loop (TADD 16: the OUT executes at edge + 13..16) and at edge + 24 at the first bit of a byte (TADD 20 after the
# BEC and the IOR); the bit is masked and shifted in (3 slots). The first version had TADD 12 and 16: a lead of 0 to 3.
TEXT_C = HEAD + """
first:  WAIT    SCK, SE, r3, TO, CT
        BEC     done
        IOR     r1, PINS
        TADD    20
        OUT     MISO, r0<<, 4
        ANDI    r1, 2
        LDI     r3, 0x3FC
        PULL.NB r3
        OR      r0, r3
bit:    WAIT    SCK, SE, r3, CT
        IOR     r3, PINS
        TADD    16
        OUT     MISO, r0<<, 4
        ANDI    r3, 2
        SHL     r1, 1
        OR      r1, r3
        DJNZ    r2, bit
        SHR     r1, 1
        PUSH.NB r1
        LDI     r2, 7
        TADD    GAP
        JMP     first
""" + TAIL
# D0: the first two-thread version of fw/spi_tgt2.s (chip commits 1af4d890 to dbcc14bf): thread 0 MISO with a DJNZ
# bit loop and the next host word fetched at the first bit (BEC, TADD, OUT, LDI, PULL.NB, OR: 6 after the WAIT),
# thread 1 MOSI by IOR PINS with the word given at the first bit of the next byte (IOR, BEC, ANDI, SHR, PUSH.NB, MOV:
# 6 after the WAIT). Measured: smallest half period 17 in every mode. Kept here as the comparison.
TEXT_D0 = """
        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    SE, 1
        .equ    GAP, 200
tx:     TNOW    8
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
first:  WAIT    SCK, SE, r3, TO, CT
        BEC     done
        TADD    16
        OUT     MISO, r0<<, 4
        LDI     r3, 0x3FC
        PULL.NB r3
        OR      r0, r3
bit:    WAIT    SCK, SE, r3, CT
        TADD    12
        OUT     MISO, r0<<, 4
        DJNZ    r2, bit
        LDI     r2, 7
        TADD    GAP
        JMP     first
done:   TNOW    8
        OUT     MISO, Z
        JMP     idle
rx:     WAIT    CS, FALL, r2
        WAIT    SCK, SE, r2, CT
        IOR     r1, PINS
        ANDI    r1, 2
        JMP     b6
first1: WAIT    SCK, SE, r2, TO, CT
        IOR     r2, PINS
        BEC     flush
        ANDI    r2, 2
        SHR     r1, 1
        PUSH.NB r1
        MOV     r1, r2
b6:     WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        TADD    GAP
        JMP     first1
flush:  SHR     r1, 1
        PUSH.NB r1
        WAIT    CS, FALL, r2
        WAIT    SCK, SE, r2, CT
        IOR     r1, PINS
        ANDI    r1, 2
        JMP     b6
"""


def program(name, mode):
    """(assembled program, start_pc, run) of a structure for a mode."""
    se = ff.SPI_TGT_SE[mode]
    if name == "A":
        return pe_asm.assemble(ff.spi_tgt_text(mode), "spi_tgt.s", 0), (0, 0, 0, 0), (True, False, False, False)
    if name in ("B", "C"):
        text = (TEXT_B if name == "B" else TEXT_C).replace(".equ    SE, 1", f".equ    SE, {se}")
        return pe_asm.assemble(text, f"spi_tgt_{name}.s", 0), (0, 0, 0, 0), (True, False, False, False)
    if name == "D0":
        prog = pe_asm.assemble(TEXT_D0.replace(".equ    SE, 1", f".equ    SE, {se}"), "spi_tgt_D0.s", 0)
        return prog, (prog.symbols["tx"][0], prog.symbols["rx"][0], 0, 0), (True, True, False, False)
    if name == "D":
        prog = pe_asm.assemble(ff.source("spi_tgt2.s", SE=se), "spi_tgt2.s", 0)
        return prog, (prog.symbols["tx"][0], prog.symbols["rx"][0], 0, 0), (True, True, False, False)
    prog = pe_asm.assemble(ff.source("spi_tgt3.s", SE=se), "spi_tgt3.s", 0)
    return prog, (prog.symbols["tx"][0], prog.symbols["rx0"][0], prog.symbols["rx1"][0], 0), (True, True, True, False)


def miso_words(name, transfers):
    """Host words to thread 0: N + 1 words for N MISO bytes; the last one is not sent."""
    return [[byte << MISO_SHIFT[name] for byte in miso] + [0] for _, miso in transfers]


def expected_words(name, transfers):
    """{thread: words} that the host must read for the MOSI bytes of `transfers`; for A, B, C and D0 the words are
    compared on bits 7 to 0 only (bits 15 to 8 hold the byte before or pin levels)."""
    if name in ("A", "B", "C", "D0"):
        return {RX_THREADS[name][0]: [b for mosi, _ in transfers for b in mosi]}
    want = {n: [] for n in RX_THREADS[name]}
    for mosi, _ in transfers:
        n = len(mosi)
        if name == "D":
            want[1] += [(mosi[j - 1] << 3) | ((mosi[j] >> 6) << 1) for j in range(1, n)] + [(mosi[n - 1] << 3) | 1]
        else:
            for j, b in enumerate(mosi):
                want[1 + (j & 1)].append((b << 3) | (1 if j == n - 1 else 0))
    return want


def words_fault(name, bench, transfers=XFERS):
    want = expected_words(name, transfers)
    mask = 0xFF if name in ("A", "B", "C", "D0") else 0x7FF if name == "D" else 0xFFFF  # D: bits 15 to 11 are older bits
    got = {n: [w & mask for w in bench.words(n)] for n in want}
    if got != want:
        return "target words " + "; ".join(f"thread {n}: {[hex(w) for w in got[n]]}" for n in got)
    return None


def run(name, mode, h, t0=300, transfers=XFERS, lead=30, **kw):
    prog, start_pc, go = program(name, mode)
    m = pe_model.PeModel(prog.words, start_pc=start_pc, run=go, owner=ff.owners({ff.P_MISO: 0}))
    ctl = ff.SpiController(mode, h, [mosi for mosi, _ in transfers], t0=t0, lead=lead, **kw)
    bench = ff.Bench(m, inputs=ctl)
    for words in miso_words(name, transfers):
        bench.send(0, *words)
    bench.run(ctl.end + 1000)
    return bench, m, ctl


def fault(name, bench, m, ctl, transfers=XFERS):
    """Text of the first fault of a run, or None if the run is correct (both directions, status, MISO release)."""
    _, _, go = program(name, 0)
    for n, running in enumerate(go):
        th = m.threads[n]
        if running and (th.status or not th.run):
            return f"thread {n} status {th.status}, run {th.run}, {th.stop_reason}"
    got, f = ctl.received()
    if f:
        return f
    if got != [list(miso) for _, miso in transfers]:
        return f"controller received {[[hex(x) for x in row] for row in got]}"
    f = words_fault(name, bench, transfers)
    if f:
        return f
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
    """Memory words of fw/spi_tgt2.s, fw/spi_tgt3.s and of the alternatives."""
    for name in STRUCTURES:
        prog = program(name, 0)[0]
        dut._log.info(f"{name}: {STRUCTURES[name]}: {prog.size} words")
    assert program("D", 0)[0].size <= pe_model.PROG_WORDS
    assert program("E", 0)[0].size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_spi_tgt2_sweep(dut):
    """Six loop structures in the reference model against the SPI controller model: smallest half period in each
    mode, the fault at the first failure (the control one below), MOSI hold, CS lead, CS high time, MISO set-up and
    hold at that half period. fw/spi_tgt2.s must pass at 15 cycles (2 MHz) and fw/spi_tgt3.s at 13 in every mode,
    and at every half period from there to 40 (no hole)."""
    log = dut._log
    lines = []
    smallest = {}
    for name in STRUCTURES:
        for mode in range(4):
            h, hf = last_ok(lambda v: all_phases(name, mode, v), range(60, 7, -1))
            smallest[(name, mode)] = h
            if h is None:
                lines.append(f"{name} mode {mode}: half period 60 fails: {hf}")
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
    holes = {name: [(mode, v) for mode in range(4) for v in range(lo, 41) if all_phases(name, mode, v)]
             for name, lo in (("D", 15), ("E", 13))}
    for name in STRUCTURES:
        log.info(f"{name}: {STRUCTURES[name]}")
    for line in lines:
        log.info(line)
    for name, lo in (("D", 15), ("E", 13)):
        lines.append(f"{name}: half periods {lo} to 40, all modes, 4 phases: {'no hole' if not holes[name] else holes[name]}")
        log.info(lines[-1])
    os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
    with open(os.path.join(ROOT, "build", "spi_tgt2_sweep.txt"), "w", encoding="utf-8") as f:  # the full result table
        f.write("\n".join([f"{k}: {v}" for k, v in STRUCTURES.items()] + lines) + "\n")
    assert not holes["D"] and not holes["E"], holes
    for name, h_max, rated in (("D", 15, H_RATED_D), ("E", 13, H_RATED_E)):
        worst = max(smallest[(name, mode)] if smallest[(name, mode)] is not None else 99 for mode in range(4))
        assert worst <= h_max, (name, smallest)
        assert rated >= worst + 2, (name, rated, smallest)


RTL_GAP = 3000  # CS high between the transfers of the RTL tests (50 us): the real-time host at SCK 10 MHz refills the
# 4-word FIFO of thread 0 between transfers (9 MISO words for 7 bytes); with 600 cycles the three-thread program's
# host, which also serves two receiving threads, was late with the second word of the second transfer (MISO 0xFF)


async def rtl_run(dut, name, h_rated, window, modes=range(4), offsets=range(4), gap=RTL_GAP):
    """A program on the chip at 60 MHz in all four modes at its rated half period and 4 start phases, against the
    controller model, in lockstep with the reference model: MISO bytes at the sample edges, the MOSI bytes in the host
    words of the receiving threads, MISO released while CS is high. `window`: the waveform of the first transfer of
    mode 0, phase 0 is dumped for review (the Icarus bench only). Returns the first fault text or None (the asserting
    tests stop at it)."""
    rf.start_clock(dut)
    log = dut._log
    for mode in modes:
        prog, start_pc, go = program(name, mode)
        for off in offsets:
            ctl = ff.SpiController(mode, h_rated, [mosi for mosi, _ in XFERS], lead=30, gap=gap,
                                   t0=rf.HOST_START + 1000 + off)
            label = f"rtl {name} mode {mode} half period {h_rated} ({F_MHZ * 1e3 / (2 * h_rated):.0f} kHz), phase {off}"
            bench = rf.RtlBench(dut, prog.words, start_pc=start_pc, run=go, owner=ff.owners({ff.P_MISO: 0}),
                                inputs=ctl, label=label)
            for words in miso_words(name, XFERS):
                bench.send(0, *words)
            if window and mode == 0 and off == 0 and hasattr(dut, "dump_off"):
                await bench.run(rf.HOST_START + 900)
                dut.dump_off.value = 0
                t_on = bench.t
                await bench.run(2400)
                dut.dump_off.value = 1
                t_off = bench.t
            await bench.run(ctl.end + 1000 - bench.m.cycle)
            if window and mode == 0 and off == 0 and hasattr(dut, "dump_off"):
                # Times of the dumped window for a waveform review (ps of the test bench): the sample edges of the
                # controller (pin time) and the MISO changes seen at the pin, model cycle -> (t0 + cycle) * CLK_PS.
                ps = lambda c: (bench.t0 + c) * rf.CLK_PS  # noqa: E731
                edges = sorted(ctl.sample_at)
                miso = [c for c, _ in ctl.miso_seen[1:]]
                os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
                with open(os.path.join(ROOT, "build", f"spi_tgt_{name}_window.txt"), "w", encoding="utf-8") as f:
                    f.write(f"{label}\ndump on {t_on * rf.CLK_PS} ps, off {t_off * rf.CLK_PS} ps (test-bench cycles {t_on}, "
                            f"{t_off}); model cycle 0 at test-bench cycle {bench.t0}\n")
                    f.write("sample edges (pin), ps: " + " ".join(str(ps(c)) for c in edges[:16]) + "\n")
                    f.write("MISO changes at the pin, ps: " + " ".join(str(ps(c)) for c in miso[:16]) + "\n")
                    f.write("CS fall, ps: " + " ".join(str(ps(c)) for c, v in ctl.cs if v == 0) + "\n")
            for n, words in expected_words(name, XFERS).items():
                await bench.collect(n, len(words))
            f = fault(name, bench, bench.m, ctl)
            assert f is None, f"{label}: {f}; {bench.diag()}"
            su, hold, rel, _ = ctl.miso_timing()
            log.info(f"{label}: correct; MISO set-up >= {su}, hold >= {hold} cycles at the sample edge, released <= "
                     f"{rel} cycles after the CS rise")
            await bench.finish(log)


@cocotb.test()
async def test_spi_tgt2_rtl(dut):
    """fw/spi_tgt2.s (two threads) on the chip at the rated half period H_RATED_D, all modes, 4 phases, lockstep."""
    await rtl_run(dut, "D", H_RATED_D, True)


@cocotb.test()
async def test_spi_tgt3_rtl(dut):
    """fw/spi_tgt3.s (three threads) on the chip at the rated half period H_RATED_E, all modes, 4 phases, lockstep."""
    await rtl_run(dut, "E", H_RATED_E, True)

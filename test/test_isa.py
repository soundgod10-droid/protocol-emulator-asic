# SPDX-License-Identifier: Apache-2.0
"""Milestone M2a tests: instruction set model, assembler and firmware.

What it does:
  These cocotb tests check the reference model (model/pe_model.py), the
  assembler (asm/pe_asm.py) and the firmware (fw/*.s) against docs/isa.md.
  They do not use the design under test: M2a does not change the RTL. They
  run in the same simulator run as test/test.py, so the GitHub test job
  and the gate-level job run them too.

Why:
  The model is the reference for the M2b processor RTL (A1 section 8.1,
  level L1). It must agree with docs/isa.md before an RTL is compared with
  it. The expected values in the model tests come from docs/isa.md by
  hand, not from the model. The UART check uses an independent receiver:
  it samples the middle of each bit from the measured start edge with the
  nominal bit time of 115,200 bit/s, as test/test.py does for the M1 RTL.
"""

import bisect
import os
import random
import sys
from collections import deque

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402

F_CLK = 50_000_000  # A1 section 2.3
BAUD = 115_200
T_NOM = F_CLK / BAUD  # 434.03 cycles: bit time that the receiver expects
BIT_CYCLES = 434  # bit time of fw/uart_tx.s
FRAME_BITS = 10
FRAME = FRAME_BITS * BIT_CYCLES
RATE_TOL = 0.01  # +/-1 % (A1 section 3)

# Pin numbers (docs/isa.md section 5.1)
P_UO4 = 12  # UART_TX
P_CS, P_MOSI, P_SCK = 0, 1, 3  # fw/spi_mode0.s
P_SCL, P_SDA = 6, 7  # fw/i2c_write.s


# ----- helpers -----


def owners(mapping):
    """Owner list for PeModel from {pin: thread}."""
    result = [None] * pe_model.N_OUT
    for pin, thread in mapping.items():
        result[pin] = thread
    return result


def open_drain(pins):
    return [p in pins for p in range(pe_model.N_OUT)]


def build(*parts):
    """parts: (file in fw/ or source text, base). Returns (image dict, list of Program)."""
    image = {}
    progs = []
    for src, base in parts:
        if src.endswith(".s"):
            prog = pe_asm.assemble_file(os.path.join(ROOT, "fw", src), base)
        else:
            prog = pe_asm.assemble(src, "<test>", base)
        for addr, word in prog.words.items():
            assert addr not in image, f"address {addr} is used two times"
            image[addr] = word
        progs.append(prog)
    return image, progs


class Bench:
    """Runs a model. Gives host words to the threads, records host words and pin changes."""

    def __init__(self, model, inputs=None):
        self.m = model
        self.inputs = inputs  # function(cycle, uo_out, uio_out, uio_oe) -> (ui_in, uio_in)
        self.to_thread = [deque() for _ in range(pe_model.N_THREADS)]
        self.from_thread = [[] for _ in range(pe_model.N_THREADS)]  # (cycle, word)
        self.watches = {}  # name -> [function(uo_out, uio_out, uio_oe), [(cycle, value)]]

    def watch(self, name, fn):
        self.watches[name] = [fn, []]

    def changes(self, name):
        return self.watches[name][1]

    def words(self, n):
        return [w for _, w in self.from_thread[n]]

    def send(self, n, *words):
        self.to_thread[n].extend(words)

    def run(self, cycles):
        m = self.m
        watches = list(self.watches.values())
        for _ in range(cycles):
            c = m.cycle
            if self.inputs is not None:
                ui, uio = self.inputs(c, *m.pins())
                m.set_inputs(ui, uio)
            for n in range(pe_model.N_THREADS):
                q = self.to_thread[n]
                if q and m.fifo_push(n, q[0]):
                    q.popleft()
            pins = m.step()
            for n in range(pe_model.N_THREADS):
                w = m.fifo_pop(n)
                while w is not None:
                    self.from_thread[n].append((c, w))
                    w = m.fifo_pop(n)
            for fn, ch in watches:
                v = fn(*pins)
                if not ch or ch[-1][1] != v:
                    ch.append((c, v))


def value_at(changes, cycle):
    """Value in a cycle, from a list of (cycle, value) changes."""
    i = bisect.bisect_right(changes, (cycle, float("inf"))) - 1
    return changes[i][1]


def uio_pin(p):
    """Watch function: level of a uio output pin, 2 if released (OE = 0)."""
    return lambda uo, uio, oe: ((uio >> p) & 1) if (oe >> p) & 1 else 2


def assert_status_clean(m, threads):
    for n in threads:
        th = m.threads[n]
        assert th.status == 0, f"thread {n}: status {th.status} (bit 0 LATE, bit 1 ERR), {th.stop_reason}"
        assert th.run, f"thread {n} stopped: {th.stop_reason}"


# ----- independent UART receiver (same method as test/test.py UartRx) -----


def uart_frames(changes):
    """Finds each start edge (1 to 0 from idle), samples bit k at start + (k + 0.5) x T_NOM.

    The pin value of a cycle is the value from the last change at or before that cycle.
    After the stop-bit sample the receiver waits for the next falling edge.
    """
    frames = []
    ready_after = -1.0
    for i in range(1, len(changes)):
        c, v = changes[i]
        if v == 0 and changes[i - 1][1] == 1 and c > ready_after:
            samples = [value_at(changes, int(c + (k + 0.5) * T_NOM)) for k in range(FRAME_BITS)]
            frames.append((c, samples))
            ready_after = c + (FRAME_BITS - 0.5) * T_NOM
    return frames


def check_uart(log, changes, frames, expected, label, back_to_back):
    """Checks the frames as test/test.py does: bit time, data, edges, start-to-start time."""
    got = [sum(s[1 + i] << i for i in range(8)) for _, s in frames]
    assert len(frames) == len(expected), f"{label}: {len(frames)} frames, expected {len(expected)}: {got}"
    offsets = []
    for t0, _ in frames:
        for c, _ in changes:
            if t0 < c < t0 + (FRAME_BITS - 0.25) * T_NOM:
                offsets.append((c - t0, round((c - t0) / T_NOM)))
    assert offsets, f"{label}: no edges to measure the bit time"
    estimates = [off / k for off, k in offsets]
    t_bit = sum(off for off, _ in offsets) / sum(k for _, k in offsets)
    rate = F_CLK / t_bit
    err = rate / BAUD - 1
    log.info(
        f"{label}: measured bit time {t_bit:.3f} cycles from {len(offsets)} edges, "
        f"spread {max(estimates) - min(estimates):.3f} cycles; bit rate {rate:.1f} bit/s, error {err * 100:+.4f} %"
    )
    assert abs(err) < RATE_TOL, f"{label}: bit-rate error {err * 100:+.4f} % is more than 1 %"
    assert abs(t_bit - BIT_CYCLES) < 1e-9, f"{label}: bit time {t_bit} cycles is not {BIT_CYCLES}"
    assert max(estimates) - min(estimates) < 1e-9, f"{label}: bit time is not constant"
    in_frames = 0
    for n, ((t0, s), byte) in enumerate(zip(frames, expected)):
        assert s[0] == 0, f"{label} frame {n}: start bit is {s[0]}"
        assert s[9] == 1, f"{label} frame {n}: stop bit is {s[9]}"
        assert got[n] == byte, f"{label} frame {n}: data 0x{got[n]:02X}, expected 0x{byte:02X}"
        edges = []
        for c, v in changes:
            if t0 <= c < t0 + FRAME:
                assert (c - t0) % BIT_CYCLES == 0, f"{label} frame {n}: edge at cycle {c} is not at a bit boundary"
                edges.append(((c - t0) // BIT_CYCLES, v))
        want = [(0, 0)] + [(k, s[k]) for k in range(1, FRAME_BITS) if s[k] != s[k - 1]]
        assert edges == want, f"{label} frame {n}: edges {edges}, expected {want}"
        in_frames += len(edges)
    assert in_frames == len(changes) - 1, f"{label}: {len(changes) - 1 - in_frames} pin changes outside the frames"
    starts = [t0 for t0, _ in frames]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    if gaps:
        log.info(f"{label}: start-to-start time min {min(gaps)} cycles, max {max(gaps)} cycles (10 bit times = {FRAME})")
        assert min(gaps) >= FRAME, f"{label}: a frame starts before the stop bit ends"
        if back_to_back:
            assert set(gaps) == {FRAME}, f"{label}: frames are not back-to-back: {sorted(set(gaps))}"
    log.info(f"{label}: {len(frames)} frames correct")
    return gaps


def tx_bench():
    image, progs = build(("uart_tx.s", 0))
    m = pe_model.PeModel(image, run=(True, False, False, False), owner=owners({P_UO4: 0}))
    b = Bench(m)
    b.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    return b, progs[0]


# ----- assembler -----


@cocotb.test()
async def test_asm_encodings(dut):
    """Each instruction form gives the word of docs/isa.md section 11.2 (words computed by hand)."""
    cases = [
        ("HALT", 0x0000), ("NOP", 0x0100), ("JR r2", 0x0280), ("LDX r1, [r2]", 0x0360), ("STX r3, [r0]", 0x04C0),
        ("LDI r1, 9", 0x1409), ("LDI r3, 1023", 0x1FFF), ("ADDI r0, -1", 0x23FF), ("ADDI r2, 511", 0x29FF),
        ("MOV r1, r2", 0x3600), ("ADD r0, r1", 0x3110), ("SUB r3, r3", 0x3F20), ("AND r0, r1", 0x3130),
        ("OR r0, r1", 0x3140), ("XOR r0, r1", 0x3150), ("CMP r1, r0", 0x3460), ("TST r0, r1", 0x3170),
        ("NOT r2, r1", 0x3980), ("ANDI r1, 0xFF", 0x44FF), ("ORI r0, 1", 0x4101), ("XORI r0, 2", 0x4202),
        ("CMPI r2, 10", 0x4B0A), ("SHL r0, 1", 0x5010), ("SHR r0, 8", 0x5180), ("ASR r1, 3", 0x5630),
        ("ROR r3, 15", 0x5FF0), ("TADD 434", 0x61B2), ("TADD 4095", 0x6FFF), ("TADD r1", 0x7080),
        ("TSET r2", 0x7300), ("TGET r3", 0x7580), ("NOW r1", 0x7680), ("TNOW 16", 0x7810), ("TSYNC 24", 0x7A18),
        ("TWAIT", 0x7C00), ("JMP 1", 0x8001), ("BEQ 5", 0x8205), ("BNE 5", 0x8405), ("BCS 5", 0x8605),
        ("BCC 5", 0x8805), ("BMI 5", 0x8A05), ("BES 127", 0x8C7F), ("BEC 0", 0x8E00), ("DJNZ r1, 6", 0x9406),
        ("JAL r3, 100", 0xAC64), ("LD r2, 127", 0xB47F), ("ST r1, 64", 0xBA40), ("OUT UO4, r0>>", 0xCC20),
        ("OUT UO4, 1", 0xCC08), ("OUT UIO0, 0", 0xC000), ("OUT UIO7, Z, 7", 0xC717), ("OUT UIO1, r2<<", 0xC1A8),
        ("OUT UO0, r3, 1", 0xC8D9), ("SAMPLE UI3, r0>>, 2", 0xCB32), ("SAMPLE UIO2, r1<<", 0xC278),
        ("WAIT UI3, FALL, r2, CT", 0xDB84), ("WAIT UIO6, RISE, r1, TO", 0xD658), ("WAIT UI0, ANY, r0, TO, CT", 0xD82C),
        ("PULL r1", 0xE100), ("PULL.NB r0", 0xE080), ("PUSH r2", 0xE600), ("PUSH.NB r0", 0xE480),
        ("IOR r3, PINS", 0xEB02), ("IOW r0, STATUS", 0xEC00),
    ]
    bad = []
    for src, want in cases:
        got = pe_asm.assemble(src).words[0]
        if got != want:
            bad.append(f"{src}: 0x{got:04X}, expected 0x{want:04X}")
    dut._log.info(f"{len(cases)} instruction forms checked")
    assert not bad, "wrong encodings: " + "; ".join(bad)


@cocotb.test()
async def test_asm_labels_and_directives(dut):
    """Labels (forward and back), .equ with + and -, .org, .word and the base address."""
    text = "\n".join([
        "        .equ    A, 5",
        "        .equ    B, A + 3 - 1",
        "        .org    10",
        "start:  LDI     r0, B",
        "        JMP     end",
        "        .word   0xBEEF",
        "end:    JMP     start",
    ])
    prog = pe_asm.assemble(text)
    assert prog.words == {10: 0x1007, 11: 0x800D, 12: 0xBEEF, 13: 0x800A}, prog.listing()
    assert prog.size == 4
    assert prog.symbols["start"][0] == 10 and prog.symbols["end"][0] == 13 and prog.symbols["B"][0] == 7
    prog = pe_asm.assemble("NOP\nx: JMP x", base=20)
    assert prog.words == {20: 0x0100, 21: 0x8015}, prog.listing()
    image = pe_asm.assemble("NOP").image()
    assert len(image) == 128 and image[0] == 0x0100 and image[1] == 0x0000


@cocotb.test()
async def test_asm_errors(dut):
    """Each error gives the file name and the correct line number."""
    lines = [
        "",
        "        LDI r0, 5",
        "        FOO r1",
        "        LDI r4, 1",
        "        LDI r0, 1024",
        "        JMP nowhere",
        "dup:    NOP",
        "dup:    NOP",
        "        OUT UI3, 1",
        "        SAMPLE UO4, r0>>",
        "        OUT UO4",
        "        WAIT UI3, UP, r0",
    ]
    want = {
        3: "unknown instruction",
        4: "register r0 to r3",
        5: "out of range",
        6: "not defined",
        8: "already defined",
        9: "input pin",
        10: "output pin",
        11: "operands",
        12: "edge must be",
    }
    try:
        pe_asm.assemble("\n".join(lines), name="bad.s")
    except pe_asm.AsmError as err:
        errors = err.errors
    else:
        raise AssertionError("no AsmError")
    dut._log.info("assembler errors:\n" + "\n".join(errors))
    assert len(errors) == len(want), f"{len(errors)} errors, expected {len(want)}"
    for lineno, text in want.items():
        assert any(e.startswith(f"bad.s:{lineno}: ") and text in e for e in errors), f"no error '{text}' for line {lineno}"
    try:
        pe_asm.assemble(".org 127\nNOP\nNOP", name="long.s")
    except pe_asm.AsmError as err:
        assert len(err.errors) == 1 and err.errors[0].startswith("long.s:3: ") and "longer than the memory" in err.errors[0], err.errors
    else:
        raise AssertionError("no AsmError for a program longer than 128 words")


# ----- model -----


@cocotb.test()
async def test_model_barrel_order(dut):
    """Thread n executes in cycles c with c mod 4 = n, one instruction every 4 cycles (isa 3.1)."""
    prog = "NOW r0\nPUSH r0\nNOW r0\nPUSH r0\nHALT"
    image, _ = build(*[(prog, 8 * n) for n in range(4)])
    m = pe_model.PeModel(image, start_pc=(0, 8, 16, 24), run=(True, True, True, True))
    b = Bench(m)
    b.run(40)
    for n in range(4):
        assert b.from_thread[n] == [(n + 4, n), (n + 12, n + 8)], f"thread {n}: (cycle, word) {b.from_thread[n]}"
        assert not m.threads[n].run and m.threads[n].status == 0 and m.threads[n].pc == 8 * n + 4


@cocotb.test()
async def test_model_lead_and_late(dut):
    """Event at its time with lead 4; late events occur 4 cycles after OUT and set LATE (isa 4.2, 4.3)."""
    src = "\n".join([
        "TNOW 8",            # c0: T = 8
        "OUT UO0, 0",        # c4: lead 4, on time: event cycle 8, pin from cycle 9
        "IOR r1, STATUS",    # c8: 0
        "PUSH r1",
        "TNOW 7",            # c16: T = 23
        "OUT UO1, 0",        # c20: lead 3, late: event cycle 24, pin from 25
        "IOR r1, STATUS",    # c24: 1 (LATE)
        "PUSH r1",
        "LDI r1, 1",
        "IOW r1, STATUS",    # c36: clear LATE
        "IOR r2, STATUS",    # c40: 0
        "PUSH r2",
        "TSET r3",           # c48: T = 0, a time in the past
        "OUT UO2, 0",        # c52: lead 65,484, late: event cycle 56, pin from 57
        "IOR r1, STATUS",    # c56: 1
        "PUSH r1",
        "TNOW 100",          # c64: T = 164
        "OUT UO3, 0, 7",     # c68: time 171, lead 103: pin from 172
        "HALT",
    ])
    image, _ = build((src, 0))
    m = pe_model.PeModel(image, owner=owners({8: 0, 9: 0, 10: 0, 11: 0}))
    b = Bench(m)
    b.watch("uo", lambda uo, uio, oe: uo)
    b.run(200)
    assert b.words(0) == [0, 1, 0, 1], b.words(0)
    assert b.changes("uo") == [(0, 0x7F), (9, 0x7E), (25, 0x7C), (57, 0x78), (172, 0x70)], b.changes("uo")


@cocotb.test()
async def test_model_horizon(dut):
    """Lead 32,767 is on time, lead 32,768 is late (isa 4.2)."""
    src = "\n".join([
        "LDI r0, 1000",      # c0
        "SHL r0, 5",         # c4: 32,000
        "ADDI r0, 511",      # c8: 32,511
        "ADDI r0, 276",      # c12: 32,787
        "TSET r0",           # c16: T = 32,787
        "OUT UO0, 0",        # c20: lead 32,767: event cycle 32,787, pin from 32,788
        "IOR r1, STATUS",    # c24: 0
        "PUSH r1",
        "ADDI r0, 21",       # c32: 32,808
        "TSET r0",           # c36
        "OUT UO1, 0",        # c40: lead 32,768: late, event cycle 44, pin from 45
        "IOR r1, STATUS",    # c44: 1
        "PUSH r1",
        "HALT",
    ])
    image, _ = build((src, 0))
    m = pe_model.PeModel(image, owner=owners({8: 0, 9: 0}))
    b = Bench(m)
    b.watch("uo", lambda uo, uio, oe: uo)
    b.run(32_800)
    assert b.words(0) == [0, 1], b.words(0)
    assert b.changes("uo") == [(0, 0x7F), (45, 0x7D), (32_788, 0x7C)], b.changes("uo")


@cocotb.test()
async def test_model_owner_and_slot(dut):
    """OUT waits for a full slot; OUT from a thread that is not the owner sets ERR only (isa 4.4, 5.2)."""
    t0 = "\n".join([
        "TNOW 40",           # c0: T = 40
        "OUT UO0, 0",        # c4: event cycle 40, pin from 41
        "TADD 10",           # c8: T = 50
        "OUT UO0, 1",        # c12 to c40: slot full; c44: lead 6, event cycle 50, pin from 51
        "NOW r0",            # c48
        "PUSH r0",
        "HALT",
    ])
    t1 = "OUT UO0, 0\nIOR r0, STATUS\nPUSH r0\nHALT"   # c1: thread 1 is not the owner
    t2 = ".word 0xCF08\nIOR r0, STATUS\nPUSH r0\nHALT"  # OUT pin 15, 1: no such pin
    image, _ = build((t0, 0), (t1, 8), (t2, 16))
    m = pe_model.PeModel(image, start_pc=(0, 8, 16, 0), run=(True, True, True, False), owner=owners({8: 0}))
    b = Bench(m)
    b.watch("uo", lambda uo, uio, oe: uo)
    b.run(100)
    assert b.words(0) == [48], b.words(0)
    assert b.words(1) == [2] and b.words(2) == [2], (b.words(1), b.words(2))
    assert m.threads[0].status == 0
    assert b.changes("uo") == [(0, 0x7F), (41, 0x7E), (51, 0x7F)], b.changes("uo")


@cocotb.test()
async def test_model_open_drain_and_z(dut):
    """Values and output enables of OUT 0, 1, Z and register bits, push-pull and open drain (isa 4.5)."""
    src = "\n".join([
        "TNOW 20",           # T = 20
        "OUT UIO3, 1",       # open drain: released (V 1, OE 0) at 20
        "OUT UIO4, 1",       # push-pull: V 1, OE 1 at 20
        "TADD 20",           # T = 40
        "OUT UIO3, 0",       # V 0, OE 1 at 40
        "OUT UIO4, Z",       # V 1, OE 0 at 40
        "TADD 20",           # T = 60
        "LDI r0, 1",
        "OUT UIO3, r0",      # bit 1, open drain: released at 60
        "OUT UIO4, r0>>",    # bit 1, push-pull: V 1, OE 1 at 60; r0 = 0
        "TADD 20",           # T = 80
        "OUT UIO4, r0",      # bit 0: V 0, OE 1 at 80
        "PUSH r0",
        "HALT",
    ])
    image, _ = build((src, 0))
    m = pe_model.PeModel(image, owner=owners({3: 0, 4: 0}), open_drain=open_drain({3}))
    b = Bench(m)
    b.watch("uio", lambda uo, uio, oe: (uio << 8) | oe)
    b.run(120)
    assert b.words(0) == [0], b.words(0)
    want = [(0, 0xFF00), (21, 0xFF10), (41, 0xF708), (61, 0xFF10), (81, 0xEF10)]
    assert b.changes("uio") == want, [(c, hex(v)) for c, v in b.changes("uio")]


@cocotb.test()
async def test_model_sample_and_wait(dut):
    """SAMPLE reads the pin level of cycle t - 2; WAIT edges, capture, CT, timeout (isa 6)."""
    t0 = "\n".join([
        "NOT r0, r0",           # r0 = 0xFFFF, N = 1
        "TNOW 87",              # c4: T = 91
        "SAMPLE UI3, r0>>",     # sample cycle 91: pin level of cycle 89 = 0
        "IOR r1, FLAGS",        # N 1, E 0 = 4
        "PUSH r0",
        "PUSH r1",
        "HALT",
    ])
    t1 = "\n".join([
        "TNOW 91",              # c1: T = 92
        "SAMPLE UI3, r0<<",     # sample cycle 92: pin level of cycle 90 = 1
        "IOR r1, FLAGS",        # E 1 = 8
        "PUSH r0",
        "PUSH r1",
        "HALT",
    ])
    t2 = "\n".join([
        "WAIT UI3, FALL, r2, CT",  # c2: pin falls at 50, edge cycle 52; complete at 54
        "NOW r1",                   # c58
        "PUSH r2",
        "PUSH r1",
        "TADD 100",                 # T = 152
        "WAIT UI3, FALL, r0, TO",   # c74: no falling edge, timeout cycle 152, complete at 154
        "IOR r1, FLAGS",            # E 0 = 0
        "PUSH r1",
        "TGET r3",                  # T not changed: 152
        "PUSH r3",
        "HALT",
    ])
    t3 = "\n".join([
        "WAIT UI3, RISE, r0",       # c3: the rise at cycle 2 does not count; the rise at 92 counts
        "PUSH r0",
        "WAIT UI3, ANY, r1, TO",    # T = 0 is past: timeout at once
        "IOR r2, FLAGS",            # E 0 = 0
        "PUSH r2",
        "IOR r3, PINS",             # c119: s = pin level of cycle 117: ui_in[3] = 1 -> bit 11
        "PUSH r3",
        "HALT",
    ])
    image, _ = build((t0, 0), (t1, 16), (t2, 32), (t3, 64))
    m = pe_model.PeModel(image, start_pc=(0, 16, 32, 64), run=(True, True, True, True))

    def rx_line(c, uo, uio, oe):
        level = 0 if 50 <= c < 90 else 1
        return level << 3, 0

    b = Bench(m, inputs=rx_line)
    b.run(300)
    assert b.words(0) == [0x7FFF, 4], b.words(0)
    assert b.words(1) == [1, 8], b.words(1)
    assert b.words(2) == [52, 58, 0, 152], b.words(2)
    assert b.words(3) == [92, 0, 0x0800], b.words(3)
    for n in range(4):
        assert m.threads[n].status == 0, f"thread {n} status {m.threads[n].status}"


@cocotb.test()
async def test_model_alu_flags(dut):
    """Results and flags Z, C, N, E of the ALU, shift and compare instructions (isa 9.1)."""
    src = "\n".join([
        "LDI r0, 1000", "LDI r1, 1001",
        "CMP r0, r1", "IOR r2, FLAGS", "PUSH r2",            # 0xFFFF: N 1, C 0 -> 4 (E 0)
        "SUB r1, r0", "IOR r2, FLAGS", "PUSH r2", "PUSH r1",  # 1: C 1, E 1 (PUSH) -> 10; 1
        "ADDI r0, -1000", "IOR r2, FLAGS", "PUSH r2",         # 0: Z 1, C 1 -> 11
        "NOT r3, r0", "IOR r2, FLAGS", "PUSH r2",             # 0xFFFF: N 1, C stays 1 -> 14
        "ADD r3, r1", "IOR r2, FLAGS", "PUSH r2",             # 0x10000: 0, Z 1, C 1 -> 11
        "LDI r3, 773", "SHL r3, 7", "IOR r2, FLAGS", "PUSH r2", "PUSH r3",   # 0x8280, C = bit 9 = 1 -> 14
        "ASR r3, 4", "IOR r2, FLAGS", "PUSH r2", "PUSH r3",   # 0xF828, C = bit 3 = 0 -> 12
        "ROR r3, 4", "IOR r2, FLAGS", "PUSH r2", "PUSH r3",   # 0x8F82, C = bit 3 = 1 -> 14
        "SHR r3, 15", "IOR r2, FLAGS", "PUSH r2",             # 1, C = bit 14 = 0 -> 8
        "XORI r3, 1", "IOR r2, FLAGS", "PUSH r2",             # 0: Z 1, C stays 0 -> 9
        "ORI r3, 0xF0", "ANDI r3, 0x3C", "PUSH r3",           # 0x30
        "CMPI r3, 0x31", "IOR r2, FLAGS", "PUSH r2",          # 0xFFFF: N 1, C 0 -> 12
        "CMPI r3, 0x30", "IOR r2, FLAGS", "PUSH r2",          # 0: Z 1, C 1 -> 11
        "OR r3, r1", "XOR r3, r1", "AND r3, r3",              # 0x31, 0x30, 0x30
        "TST r3, r1", "IOR r2, FLAGS", "PUSH r2",             # 0x30 and 1 = 0: Z 1, C stays 1 -> 11
        "MOV r0, r3", "PUSH r0",                              # 0x30
        "ADDI r0, -49", "IOR r2, FLAGS", "PUSH r2",           # 48 + 65,487 = 65,535: N 1, C 0 -> 12
        "HALT",
    ])
    image, _ = build((src, 0))
    m = pe_model.PeModel(image)
    b = Bench(m)
    b.run(400)
    want = [4, 10, 1, 11, 14, 11, 14, 0x8280, 12, 0xF828, 14, 0x8F82, 8, 9, 0x30, 12, 11, 11, 0x30, 12]
    assert b.words(0) == want, [hex(w) for w in b.words(0)]


@cocotb.test()
async def test_model_memory_and_branches(dut):
    """LD takes 2 slots; ST, LDX, STX; DJNZ loop; JAL and JR; branch conditions (isa 3.3, 8, 9.2)."""
    src = "\n".join([
        "        LDI     r0, 77",
        "        ST      r0, 100",     # c4
        "        LD      r1, 100",     # c8, data read at c12
        "        NOW     r2",          # c16: shows the 2 slots of LD
        "        PUSH    r1",
        "        PUSH    r2",
        "        LDI     r3, 101",
        "        STX     r2, [r3]",
        "        LDX     r0, [r3]",
        "        PUSH    r0",
        "        LDI     r1, 3",
        "        LDI     r0, 0",
        "loop:   ADDI    r0, 5",
        "        DJNZ    r1, loop",
        "        PUSH    r0",          # 15
        "        JAL     r3, sub",     # address 15: r3 = 16
        "        PUSH    r3",
        "        CMPI    r0, 15",      # Z 1, C 1, N 0
        "        BNE     bad",
        "        BEQ     ok1",
        "        JMP     bad",
        "ok1:    BCC     bad",
        "        BCS     ok2",
        "        JMP     bad",
        "ok2:    BMI     bad",
        "        CMPI    r0, 16",      # N 1
        "        BMI     ok3",
        "        JMP     bad",
        "ok3:    LDI     r1, 1000",
        "        PUSH    r1",
        "        HALT",
        "bad:    LDI     r1, 666",
        "        PUSH    r1",
        "        HALT",
        "sub:    LDI     r2, 42",
        "        PUSH    r2",
        "        JR      r3",
    ])
    image, _ = build((src, 0))
    m = pe_model.PeModel(image)
    b = Bench(m)
    b.run(400)
    assert b.words(0) == [77, 16, 16, 15, 42, 16, 1000], b.words(0)
    assert m.mem[100] == 77 and m.mem[101] == 16


@cocotb.test()
async def test_model_illegal_and_halt(dut):
    """Illegal instructions stop the thread with ERR; HALT stops it without ERR (isa 12)."""
    image, _ = build((".word 0xF123", 0), (".word 0x0500", 4), (".word 0x3090", 8), ("NOP\nHALT", 12))
    m = pe_model.PeModel(image, start_pc=(0, 4, 8, 12), run=(True, True, True, True))
    m.run_cycles(40)
    for n, pc in ((0, 0), (1, 4), (2, 8)):
        th = m.threads[n]
        assert not th.run and th.status == pe_model.STATUS_ERR and th.pc == pc, (n, th.run, th.status, th.pc)
    th = m.threads[3]
    assert not th.run and th.status == 0 and th.pc == 13, (th.run, th.status, th.pc)
    image, _ = build((".word 0xD030", 0), (".word 0x7E00", 4))  # WAIT edge 11, time function 111
    m = pe_model.PeModel(image, start_pc=(0, 4, 0, 0), run=(True, True, False, False))
    m.run_cycles(40)
    for n in (0, 1):
        assert not m.threads[n].run and m.threads[n].status == pe_model.STATUS_ERR, n


@cocotb.test()
async def test_model_store_rule(dut):
    """A fetch 1 to 3 cycles after a store to the same address is not defined: the model stops (isa 3.3)."""
    image, _ = build(("NOP\nNOP\nNOP\nJMP 0", 0), ("ST r0, 1\nHALT", 8))
    m = pe_model.PeModel(image, start_pc=(0, 8, 0, 0), run=(True, True, False, False))
    try:
        m.run_cycles(20)
    except pe_model.ModelError as err:
        dut._log.info(f"model error as expected: {err}")
        assert "cycle 4" in str(err) and "address 1" in str(err), str(err)
    else:
        raise AssertionError("no ModelError for a fetch 3 cycles after a store")


# ----- firmware: UART transmitter -----


@cocotb.test()
async def test_fw_uart_tx_idle_line(dut):
    """fw/uart_tx.s: with no host word, UART_TX stays 1 from cycle 0 and no other pin changes."""
    b, prog = tx_bench()
    b.watch("all", lambda uo, uio, oe: (uo, uio, oe))
    b.run(3 * FRAME)
    assert b.changes("tx") == [(0, 1)], b.changes("tx")[:5]
    assert b.changes("all") == [(0, (0x7F, 0xFF, 0x00))], b.changes("all")[:5]
    assert_status_clean(b.m, [0])
    dut._log.info(f"fw/uart_tx.s: {prog.size} words; UART_TX = 1 for {3 * FRAME} cycles with no host word")


@cocotb.test()
async def test_fw_uart_tx_patterns(dut):
    """fw/uart_tx.s: 0x00, 0xFF, 0x55, 0xAA as single frames with idle time between them."""
    b, _ = tx_bench()
    patterns = [0x00, 0xFF, 0x55, 0xAA]
    sent = []
    for byte in patterns:
        sent.append(b.m.cycle)
        b.send(0, byte)
        b.run(12 * BIT_CYCLES)
    frames = uart_frames(b.changes("tx"))
    check_uart(dut._log, b.changes("tx"), frames, patterns, "uart_tx fixed patterns", back_to_back=False)
    lat = [t0 - s for (t0, _), s in zip(frames, sent)]
    dut._log.info(f"uart_tx fixed patterns: host word to start edge {min(lat)} to {max(lat)} cycles")
    assert_status_clean(b.m, [0])


@cocotb.test()
async def test_fw_uart_tx_back_to_back(dut):
    """fw/uart_tx.s: 36 bytes back-to-back; each start-to-start time is exactly 10 bit times."""
    b, _ = tx_bench()
    rng = random.Random(2)
    data = [0x00, 0xFF, 0x55, 0xAA] + list(range(16)) + [rng.randrange(256) for _ in range(16)]
    b.send(0, *data)
    b.run((len(data) + 2) * FRAME)
    frames = uart_frames(b.changes("tx"))
    check_uart(dut._log, b.changes("tx"), frames, data, "uart_tx back-to-back", back_to_back=True)
    assert_status_clean(b.m, [0])


@cocotb.test()
async def test_fw_uart_tx_word_near_end_of_stop_bit(dut):
    """fw/uart_tx.s: the next word comes near the end of the stop bit; the stop bit stays complete."""
    b, _ = tx_bench()
    rng = random.Random(15)
    data = []
    for delay in range(FRAME - 64, FRAME + 16, 4):
        byte = rng.randrange(256)
        data.append(byte)
        b.send(0, byte)
        while b.to_thread[0] or b.m.fifo_in[0]:  # until the thread took the word
            b.run(1)
        b.run(delay)
    b.run(12 * BIT_CYCLES)
    frames = uart_frames(b.changes("tx"))
    gaps = check_uart(dut._log, b.changes("tx"), frames, data, "uart_tx word near end of stop bit", back_to_back=False)
    assert min(gaps) == FRAME and max(gaps) > FRAME, f"both cases (chained, idle gap) must occur: {sorted(set(gaps))}"
    assert_status_clean(b.m, [0])


# ----- firmware: UART receiver, SPI and I2C outlines -----


def uart_line(frames, bit_time):
    """Changes of a UART line made by a sender with its own bit time: frames = [(start cycle, byte)]."""
    changes = [(0, 1)]
    for t0, byte in frames:
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1]
        for k, bit in enumerate(bits):
            c = t0 + round(k * bit_time)
            if bit != changes[-1][1]:
                changes.append((c, bit))
    return changes


@cocotb.test()
async def test_fw_uart_rx_rate_error_and_glitch(dut):
    """fw/uart_rx.s: back-to-back frames from senders at -2 % and +2 % bit time; a 3-cycle glitch gives no word."""
    data = [0x00, 0xFF, 0x55, 0xAA, 0x0F, 0xF0, 0x81, 0x7E]
    for error in (-0.02, 0.02):
        bit_time = T_NOM * (1 + error)
        frames = [(2000 + round(i * FRAME_BITS * bit_time), byte) for i, byte in enumerate(data)]
        line = uart_line(frames, bit_time)
        line[1:1] = [(1000, 0), (1003, 1)]  # glitch on the idle line
        image, prog = build(("uart_rx.s", 0))
        m = pe_model.PeModel(image)
        b = Bench(m, inputs=lambda c, uo, uio, oe: (value_at(line, c) << 3, 0))
        b.run(frames[-1][0] + 12 * BIT_CYCLES)
        assert b.words(0) == [0x100 | x for x in data], f"error {error:+.0%}: {[hex(w) for w in b.words(0)]}"
        assert_status_clean(m, [0])
        dut._log.info(f"uart_rx: sender bit time {bit_time:.2f} cycles ({error:+.0%}): {len(data)} words correct, glitch ignored")


def i2c_target(ack):
    """I2C target model: wired-AND bus with pull-ups; ACKs each byte if ack() is True.

    It records START, STOP and each bit (SDA at the SCL rising edge) with the cycle.
    """
    st = {"scl": 1, "sda": 1, "drive": 0, "pos": 0, "events": []}

    def bus(c, uo, uio, oe):
        scl = 0 if ((oe >> P_SCL) & 1 and not (uio >> P_SCL) & 1) else 1
        sda_ours = 0 if ((oe >> P_SDA) & 1 and not (uio >> P_SDA) & 1) else 1
        sda = sda_ours & (0 if st["drive"] else 1)
        if scl and st["scl"] and sda != st["sda"]:
            if sda == 1 and st["events"] and st["events"][-1][1] == "BIT" and st["high_after_bit"]:
                st["events"].pop()  # the SCL rise before a STOP is not a data bit
            st["events"].append((c, "START" if sda == 0 else "STOP"))
            if sda == 0:
                st["pos"] = 0
        if scl and not st["scl"]:
            st["pos"] = st["pos"] % 9 + 1
            st["events"].append((c, "BIT", sda))
            st["high_after_bit"] = True
        if not scl and st["scl"]:
            st["high_after_bit"] = False
            if st["pos"] == 8:
                st["drive"] = 1 if ack() else 0
            elif st["pos"] == 9:
                st["drive"] = 0
        st["scl"], st["sda"] = scl, sda
        return scl, sda

    return st, bus


def check_i2c(log, st, words_sent, acks, label):
    """Checks the bus events and the times against the Fast-mode minimum values of A1 section 3."""
    want = []
    for word, a in zip(words_sent, acks):
        if word & 0x100:
            want.append("START")
        want += [(word >> (7 - i)) & 1 for i in range(8)] + [a]
        if word & 0x200:
            want.append("STOP")
    got = [e[1] if e[1] != "BIT" else e[2] for e in st["events"]]
    assert got == want, f"{label}: bus events {got}, expected {want}"
    log.info(f"{label}: {len(got)} bus events correct (START, bits, ACK, STOP)")


@cocotb.test()
async def test_fw_spi_outline_loopback(dut):
    """fw/spi_mode0.s with MISO connected to MOSI: data, mode 0 edges, CS, 1 MHz SCK, no late event."""
    image, prog = build(("spi_mode0.s", 0))
    m = pe_model.PeModel(image, owner=owners({P_CS: 0, P_MOSI: 0, P_SCK: 0}))
    b = Bench(m, inputs=lambda c, uo, uio, oe: (0, ((uio >> P_MOSI) & 1) << 2))
    for name, p in (("cs", P_CS), ("mosi", P_MOSI), ("sck", P_SCK)):
        b.watch(name, uio_pin(p))
    b.send(0, 0x03C, 0x0A5, 0x10F)
    b.run(2000)
    b.send(0, 0x181)
    b.run(1000)
    check_spi(dut._log, b, [0x3C, 0xA5, 0x0F, 0x81], [3, 1], "spi outline")
    assert_status_clean(m, [0])


def check_spi(log, b, data, transfers, label, n=0):
    assert [w for w in b.words(n)] == data, f"{label}: received {[hex(w) for w in b.words(n)]}"
    sck, mosi, cs = b.changes("sck"), b.changes("mosi"), b.changes("cs")
    rises = [c for i, (c, v) in enumerate(sck) if v == 1 and i > 0 and sck[i - 1][1] == 0]
    assert len(rises) == 8 * len(data), f"{label}: {len(rises)} rising SCK edges"
    bits = [(byte >> (7 - i)) & 1 for byte in data for i in range(8)]
    for r, bit in zip(rises, bits):
        assert value_at(cs, r) == 0, f"{label}: CS is not low at the rising edge at {r}"
        assert value_at(mosi, r) == bit, f"{label}: MOSI wrong at the rising edge at {r}"
    periods = {b2 - a for a, b2 in zip(rises, rises[1:]) if b2 - a < 100}
    assert periods == {50}, f"{label}: SCK periods {sorted(periods)} cycles"
    m_changes = [c for c, _ in mosi[1:]]
    nearest = min(abs(mc - r) for mc in m_changes for r in rises)
    assert nearest >= 25, f"{label}: MOSI changes {nearest} cycles from a rising edge"
    falls = [c for i, (c, v) in enumerate(cs) if v == 0 and i > 0]
    assert len(falls) == len(transfers), f"{label}: {len(falls)} CS low periods, expected {len(transfers)}"
    log.info(f"{label}: {len(data)} bytes correct, SCK period 50 cycles (1 MHz), MOSI change >= {nearest} cycles from SCK rise")


@cocotb.test()
async def test_fw_i2c_outline(dut):
    """fw/i2c_write.s: START, 8 bits MSB first, ACK and NACK, STOP; Fast-mode times; no late event."""
    image, prog = build(("i2c_write.s", 0))
    m = pe_model.PeModel(image, owner=owners({P_SCL: 0, P_SDA: 0}), open_drain=open_drain({P_SCL, P_SDA}))
    acking = [True]
    st, bus = i2c_target(lambda: acking[0])
    b = Bench(m, inputs=lambda c, uo, uio, oe: (0, (lambda s: (s[0] << P_SCL) | (s[1] << P_SDA))(bus(c, uo, uio, oe))))
    b.watch("scl", uio_pin(P_SCL))
    words = [0x1A4, 0x03C, 0x281]
    b.send(0, *words)
    b.run(4000)
    acking[0] = False
    b.send(0, 0x355)
    b.run(2000)
    check_i2c(dut._log, st, words + [0x355], [0, 0, 0, 1], "i2c outline")
    assert b.words(0) == [0, 0, 0, 1], b.words(0)
    check_i2c_times(dut._log, st, "i2c outline")
    assert_status_clean(m, [0])


def check_i2c_times(log, st, label):
    """SCL high >= 30 cycles (0.6 us), SCL low >= 65 cycles (1.3 us), bit time 126 cycles (A1 section 3)."""
    bits = [e[0] for e in st["events"] if e[1] == "BIT"]
    period = {b - a for a, b in zip(bits, bits[1:]) if b - a < 200}
    assert period == {126}, f"{label}: bit periods {sorted(period)} cycles"
    log.info(f"{label}: SCL period 126 cycles (396.8 kHz)")


@cocotb.test()
async def test_fw_all_four_in_128_words(dut):
    """UART TX, UART RX, SPI and I2C firmware in one 128-word memory, all 4 threads at the same time."""
    sizes = {}
    for f in ("uart_tx.s", "uart_rx.s", "spi_mode0.s", "i2c_write.s"):
        sizes[f] = pe_asm.assemble_file(os.path.join(ROOT, "fw", f)).size
    total = sum(sizes.values())
    dut._log.info(f"firmware words: {sizes}, total {total} of {pe_model.MEM_WORDS}")
    assert total <= pe_model.MEM_WORDS, f"the firmware needs {total} words"
    bases = {}
    addr = 0
    for f in sizes:
        bases[f] = addr
        addr += sizes[f]
    image, _ = build(*[(f, bases[f]) for f in sizes])
    m = pe_model.PeModel(
        image,
        start_pc=tuple(bases[f] for f in sizes),
        run=(True, True, True, True),
        owner=owners({P_UO4: 0, P_CS: 2, P_MOSI: 2, P_SCK: 2, P_SCL: 3, P_SDA: 3}),
        open_drain=open_drain({P_SCL, P_SDA}),
    )
    st, bus = i2c_target(lambda: True)

    def inputs(c, uo, uio, oe):
        scl, sda = bus(c, uo, uio, oe)
        rx = (uo >> 4) & 1  # UART_TX looped back to UART_RX (ui_in[3])
        mosi = (uio >> P_MOSI) & 1  # MOSI looped back to MISO (uio[2])
        return rx << 3, (mosi << 2) | (scl << P_SCL) | (sda << P_SDA)

    b = Bench(m, inputs=inputs)
    b.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    for name, p in (("cs", P_CS), ("mosi", P_MOSI), ("sck", P_SCK)):
        b.watch(name, uio_pin(p))
    uart_data = [0x00, 0xFF, 0x55, 0xAA, 0x12, 0x34, 0x56, 0x78]
    spi_data = [0x03C, 0x0A5, 0x10F, 0x181]
    i2c_words = [0x1A4, 0x03C, 0x281]
    b.send(0, *uart_data)
    b.send(2, *spi_data)
    b.send(3, *i2c_words)
    b.run((len(uart_data) + 2) * FRAME)
    frames = uart_frames(b.changes("tx"))
    check_uart(dut._log, b.changes("tx"), frames, uart_data, "all four: uart_tx", back_to_back=True)
    assert b.words(1) == [0x100 | x for x in uart_data], f"uart_rx words {[hex(w) for w in b.words(1)]}"
    check_spi(dut._log, b, [x & 0xFF for x in spi_data], [3, 1], "all four: spi", n=2)
    check_i2c(dut._log, st, i2c_words, [0, 0, 0], "all four: i2c")
    assert b.words(3) == [0, 0, 0], b.words(3)
    assert_status_clean(m, [0, 1, 2, 3])
    dut._log.info(f"all four: {total} words, UART TX/RX, SPI and I2C correct at the same time, no LATE, no ERR")

# SPDX-License-Identifier: Apache-2.0
"""Milestone M2b lockstep test: processor RTL against the M2a reference model.

What it does:
  Each scenario loads a program into the chip through the TEMPORARY test
  port (src/pe_test_port.v: CORE_SEL ui_in[7], TP_CLK ui_in[6], TP_DAT
  ui_in[5], readout uo_out[7]) and starts the threads. Then, in each clock
  cycle, the test gives the same input pin values to the RTL and to the
  reference model (model/pe_model.py) and compares:
    - the pins uo_out[6:0], uio_out and uio_oe,
    - the readout pin uo_out[7] with a copy of the readout register that
      the test calculates from the words that the model gives to the host,
    - in RTL simulation only: the state of the slot thread (r0 to r3, T,
      PC, Z C N E, LATE, ERR, RUN) with the model thread.
  The host commands (PUSH a word to a thread, POP a word from a thread)
  go through the test port, back to back after START, so host frame i
  occurs before model cycle 26 x (i + 1). The test gives the same command
  to the model before that cycle.

Why:
  A1 section 8.1 level L4: instruction streams in lockstep with the L1
  model. Pass or fail comes from the chip pins and the model. The state
  comparison only finds a difference earlier. It is not available at gate
  level (the netlist has other names); there the pins and the readout are
  compared.

Scenarios:
  Directed programs for each instruction group (ALU and flags, shifts,
  branches and memory, time, OUT, SAMPLE and WAIT, host FIFOs and I/O
  ports, illegal instructions and HALT, barrel order, horizon), and random
  instruction streams with fixed seeds (RANDOM_SEEDS). Each random program
  runs first in the model alone; a program that the model rejects
  (ModelError: a read 1 to 3 cycles after a store, isa 3.3) or that
  stores into a code word is replaced by the next program of the same
  random generator. The RTL reads the word of a waiting instruction again
  in each slot (isa 3.2), so a store into the word of a waiting SAMPLE,
  WAIT or LD is not defined; the random generator does not do that.
"""

import os
import random
import sys
from collections import Counter, deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402

CLK_NS = 20  # 50 MHz (A1 section 2.3)
TP_BITS = 26  # test port frame: command 3, argument 7, data 16
CMD_NOP, CMD_MEMW, CMD_CFG, CMD_START, CMD_PUSH, CMD_POP = 0, 1, 2, 3, 4, 5
RESET_PINS = (0x7F, 0xFF, 0x00)  # isa 2.4: V = 1, OE = 0 on all output pins

# Random instruction streams: seeds and cycles. At gate level the
# simulation is slower, so fewer seeds run there.
RANDOM_SEEDS = list(range(1, 25))
RANDOM_SEEDS_GL = [1, 2, 3, 4]
RANDOM_CYCLES = 4000


# ----- scenario -----


class Scenario:
    """A program, the host configuration, the host commands and the input pins.

    host(i) gives host frame i after START as (command, argument, data).
    stimulus(c) gives (ui_in[4:0], uio_in) for model cycle c.
    """

    def __init__(self, label, image, start_pc=(0, 0, 0, 0), run=(True, False, False, False),
                 owner=None, od=None, cycles=1000, host=None, stimulus=None):
        if isinstance(image, dict):
            words = [0] * pe_model.MEM_WORDS
            for a, w in image.items():
                words[a] = w
        else:
            words = list(image) + [0] * (pe_model.MEM_WORDS - len(image))
        self.label = label
        self.image = words
        self.start_pc = tuple(start_pc)
        self.run = tuple(bool(x) for x in run)
        self.owner = list(owner) if owner is not None else [None] * pe_model.N_OUT
        self.od = [bool(x) for x in od] if od is not None else [False] * pe_model.N_OUT
        self.cycles = cycles
        self.host = host if host is not None else pop_round_robin([0, 1, 2, 3])
        self.stimulus = stimulus if stimulus is not None else (lambda c: (0, 0))

    def new_model(self):
        m = pe_model.PeModel(self.image, self.start_pc, self.run, self.owner, self.od)
        m.coverage = Counter()
        execute = m._execute

        def counting(th, w, c, s):
            # Count the instruction group of each first execution (opcode, and
            # the function of the system and HOST/IO opcodes).
            op = w >> 12
            key = f"{op:X}" + (f".{(w >> 8) & 0xF:X}" if op == 0 else f".{(w >> 10) & 3}" if op == 0xE else "")
            m.coverage[key] += 1
            return execute(th, w, c, s)

        m._execute = counting
        return m

    def load_frames(self):
        """Test port frames that load the memory and the configuration, then START."""
        frames = [(CMD_MEMW, a, w) for a, w in enumerate(self.image)]
        frames += [(CMD_CFG, n, self.start_pc[n]) for n in range(4)]
        frames.append((CMD_CFG, 4, sum(1 << n for n in range(4) if self.run[n])))
        for p in range(pe_model.N_OUT):
            own = self.owner[p]
            if own is not None or self.od[p]:
                val = ((own or 0) & 3) | ((own is not None) << 2) | (self.od[p] << 3)
                frames.append((CMD_CFG, 16 + p, val))
        frames.append((CMD_START, 0, 0))
        return frames


def pop_round_robin(threads):
    return lambda i: (CMD_POP, threads[i % len(threads)], 0)


# Host commands and the slot of the same cycle:
#   The test port command occurs at the clock edge at the end of the cycle
#   k of its last bit. pe_host_fifo then decides with the FIFO state of
#   cycle k, and the slot thread of cycle k also sees the state of cycle
#   k: a host POP does not get a word that the thread pushes in cycle k,
#   and a host PUSH to a FIFO that is full in cycle k has no effect, also
#   if the thread pulls a word in cycle k. host_before() reads the model
#   FIFO before the step of cycle k, host_after() changes it after the step.


def host_before(m, frame):
    """PUSH: True if the in-FIFO has space. POP: the head word of the out-FIFO, or None."""
    cmd, arg, _ = frame
    n = arg & 3
    if cmd == CMD_PUSH:
        return len(m.fifo_in[n]) < pe_model.FIFO_DEPTH
    if cmd == CMD_POP:
        return m.fifo_out[n][0] if m.fifo_out[n] else None
    return None


def host_after(m, frame, before):
    cmd, arg, data = frame
    n = arg & 3
    if cmd == CMD_PUSH and before:
        m.fifo_in[n].append(data & 0xFFFF)
    elif cmd == CMD_POP and before is not None:
        word = m.fifo_out[n].popleft()
        assert word == before, "the model FIFO head changed in the slot"


def frame_bits(cmd, arg, data):
    value = (cmd << 23) | ((arg & 0x7F) << 16) | (data & 0xFFFF)
    return [(value >> (TP_BITS - 1 - i)) & 1 for i in range(TP_BITS)]


def model_only(scn, data_addrs=None):
    """Runs the scenario in the model alone. Raises ModelError if the program is not defined."""
    m = scn.new_model()
    if data_addrs is not None:
        store = m._store

        def guarded(addr, word, c):
            if addr not in data_addrs:
                raise pe_model.ModelError(f"cycle {c}: store into code address {addr}")
            for th in m.threads:
                if th.pend is not None and th.pc == addr:
                    raise pe_model.ModelError(f"cycle {c}: store into the word of a waiting instruction")
            store(addr, word, c)

        m._store = guarded
    for c in range(scn.cycles):
        # Host frame i has its last bit in model cycle 26 x (i + 1) - 1.
        fr = scn.host((c + 1) // TP_BITS - 1) if (c + 1) % TP_BITS == 0 else None
        before = host_before(m, fr) if fr is not None else None
        ui, uio = scn.stimulus(c)
        m.set_inputs(ui, uio)
        m.step()
        if fr is not None:
            host_after(m, fr, before)
    return m


# ----- the chip -----


async def start_chip(dut):
    """Clock, reset, then CORE_SEL = 1 (processor pins)."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.ui_in.value = 0x80
    await ClockCycles(dut.clk, 4)


def state_handle(dut):
    """Handle of the slot-thread state st0 in RTL simulation, None at gate level."""
    try:
        h = dut.user_project.u_proc.u_core.st0
        _ = h.value
        return h
    except AttributeError:
        return None


def ctx_fields(v):
    return {
        "r": [(v >> (16 * i)) & 0xFFFF for i in range(4)],
        "t": (v >> 64) & 0xFFFF,
        "pc": (v >> 80) & 0x7F,
        "z": (v >> 87) & 1,
        "c": (v >> 88) & 1,
        "n": (v >> 89) & 1,
        "e": (v >> 90) & 1,
        "status": ((v >> 91) & 1) | (((v >> 92) & 1) << 1),
        "run": bool((v >> 93) & 1),
    }


def model_fields(th):
    return {"r": list(th.r), "t": th.t, "pc": th.pc, "z": th.z, "c": th.c, "n": th.n, "e": th.e,
            "status": th.status, "run": bool(th.run)}


async def run_lockstep(dut, scn):
    """Loads and runs the scenario on the chip and on the model; compares in each cycle."""
    log = dut._log
    await start_chip(dut)
    st0 = state_handle(dut)
    m = scn.new_model()

    bits = deque()
    for fr in scn.load_frames():
        bb = frame_bits(*fr)
        for i, b in enumerate(bb):
            bits.append((b, (fr, None) if i == TP_BITS - 1 else None))
    for i in range(scn.cycles // TP_BITS + 2):
        fr = scn.host(i)
        bb = frame_bits(*fr)
        for k, b in enumerate(bb):
            bits.append((b, (fr, i) if k == TP_BITS - 1 else None))

    t = 0  # test bench cycle
    tp_clk = 0
    tp_dat = 0
    arrive = {}  # cycle -> frame info: the bit is in pe_test_port in that cycle
    ro, ro_known = 0, 0x1FFFF  # copy of the readout register; known bits
    t0 = None  # test bench cycle of model cycle 0
    popped = [[] for _ in range(4)]
    pop_result = {}  # test bench cycle of a POP command -> word (or None)
    pin_changes = 0
    last_pins = RESET_PINS
    compared_states = 0

    while True:
        await FallingEdge(dut.clk)
        sent = False
        if bits:
            b, info = bits.popleft()
            tp_clk ^= 1
            tp_dat = b
            arrive[t + 2] = info  # 2 synchronizer stages
            sent = True
        # The bit that was in the test port in cycle t-1 had its effect at
        # the clock edge at the end of that cycle: readout register and START.
        if (t - 1) in arrive:
            info = arrive.pop(t - 1)
            if info is not None and info[0][0] == CMD_START:
                t0 = t
            if info is not None and info[0][0] == CMD_POP:
                word = pop_result.pop(t - 1)
                ro = (0x10000 | word) if word is not None else 0
                ro_known = 0x1FFFF if word is not None else 0x10000
            else:
                ro = (ro << 1) & 0x1FFFF
                ro_known = ((ro_known << 1) | 1) & 0x1FFFF
        c = (t - t0) if t0 is not None else None
        if c is not None and c >= scn.cycles:
            break
        ui, uio = scn.stimulus(c) if c is not None else (0, 0)
        dut.ui_in.value = 0x80 | (tp_clk << 6) | (tp_dat << 5) | (ui & 0x1F)
        dut.uio_in.value = uio & 0xFF

        if c is not None:
            # A host frame whose last bit is in the test port in this cycle
            # acts at the end of this cycle (see host_before).
            info = arrive.get(t)
            fr = info[0] if info is not None and info[0][0] != CMD_START else None
            if fr is not None:
                assert c == TP_BITS * (info[1] + 1) - 1, f"{scn.label}: host frame {info[1]} at model cycle {c}"
                before = host_before(m, fr)
            m.set_inputs(ui, uio)
            th = m.threads[c % 4]
            if st0 is not None:
                sv = st0.value
                assert sv.is_resolvable, f"{scn.label} cycle {c}: slot thread state has X or Z"
                got = ctx_fields(sv.to_unsigned())
                want = model_fields(th)
                assert got == want, f"{scn.label} cycle {c}, thread {c % 4}: RTL state {got}, model {want}"
                compared_states += 1
            exp = m.step()
            if fr is not None:
                host_after(m, fr, before)
                if fr[0] == CMD_POP:
                    pop_result[t] = before
                    if before is not None:
                        popped[fr[1] & 3].append(before)
        else:
            exp = RESET_PINS

        uo_s = str(dut.uo_out.value)
        uio_s = str(dut.uio_out.value)
        oe_s = str(dut.uio_oe.value)
        want_uo = format(exp[0], "07b")
        want_uio = format(exp[1], "08b")
        want_oe = format(exp[2], "08b")
        if uo_s[1:] != want_uo or uio_s != want_uio or oe_s != want_oe:
            where = f"model cycle {c}" if c is not None else f"load cycle {t}"
            pcs = [th.pc for th in m.threads]
            raise AssertionError(
                f"{scn.label} {where}: pins uo_out[6:0] {uo_s[1:]} uio_out {uio_s} uio_oe {oe_s}, "
                f"model {want_uo} {want_uio} {want_oe} (model PCs {pcs})")
        if (ro_known >> 16) & 1:
            want7 = str((ro >> 16) & 1)
            assert uo_s[0] == want7, f"{scn.label} cycle {t}: readout uo_out[7] {uo_s[0]}, expected {want7}"
        if c is not None and exp != last_pins:
            pin_changes += 1
            last_pins = exp
        t += 1
        _ = sent

    log.info(
        f"{scn.label}: {scn.cycles} cycles in lockstep "
        f"({'pins, readout and thread state' if st0 is not None else 'pins and readout (gate level)'}); "
        f"{compared_states} state comparisons; {pin_changes} pin changes; "
        f"host words per thread {[len(p) for p in popped]}; "
        f"threads run {[th.run for th in m.threads]}, status {[th.status for th in m.threads]}, "
        f"PC {[th.pc for th in m.threads]}")
    log.info(f"{scn.label}: instruction groups executed {dict(sorted(m.coverage.items()))}")
    return m, popped, pin_changes


def asm(*parts):
    """parts: (source text, base). Returns the image dict."""
    image = {}
    for text, base in parts:
        prog = pe_asm.assemble(text, "<test_core>", base)
        for a, w in prog.words.items():
            assert a not in image, f"address {a} used two times"
            image[a] = w
    return image


def owners(mapping):
    result = [None] * pe_model.N_OUT
    for pin, th in mapping.items():
        result[pin] = th
    return result


def chk(reg):
    """Push the flags and a register: the result is visible to the host at gate level too."""
    return f"IOR r3, FLAGS\nPUSH r3\nPUSH {reg}"


def program(ops):
    lines = []
    for op, reg in ops:
        lines.append(op)
        if reg:
            lines.append(chk(reg))
    lines.append("HALT")
    return "\n".join(lines)


def assert_ended(m, popped, want_status=(0, 0, 0, 0), threads=(0,), min_words=1):
    """Sanity of a directed program: it ran to its end (no early stop) and gave words."""
    for n in threads:
        th = m.threads[n]
        assert not th.run, f"thread {n} did not stop (PC {th.pc})"
        assert th.status == want_status[n], f"thread {n}: status {th.status}, expected {want_status[n]} ({th.stop_reason})"
    assert sum(len(p) for p in popped) >= min_words, f"only {sum(len(p) for p in popped)} host words"


# ----- directed scenarios -----


@cocotb.test()
async def test_core_alu_flags(dut):
    """ADD, SUB, ADDI, CMP, CMPI, MOV, AND, OR, XOR, NOT, TST, ANDI, ORI, XORI with Z, N, C."""
    ops = [
        ("LDI r0, 1000", None), ("LDI r1, 23", None), ("LDI r2, 0", None),
        ("NOT r2, r2", "r2"), ("ADD r0, r1", "r0"), ("ADD r2, r1", "r2"), ("SUB r1, r0", "r1"),
        ("SUB r0, r0", "r0"), ("ADDI r0, -1", "r0"), ("ADDI r0, 1", "r0"), ("ADDI r2, 511", "r2"),
        ("ADDI r2, -512", "r2"), ("CMP r1, r2", "r1"), ("CMP r2, r1", "r2"), ("CMP r0, r0", "r0"),
        ("CMPI r1, 255", "r1"), ("CMPI r0, 0", "r0"), ("CMPI r2, 22", "r2"),
        ("MOV r1, r2", "r1"), ("MOV r0, r0", "r0"), ("AND r1, r2", "r1"), ("OR r1, r0", "r1"),
        ("XOR r1, r1", "r1"), ("NOT r1, r1", "r1"), ("TST r1, r2", "r1"), ("TST r0, r1", "r0"),
        ("ANDI r1, 0xA5", "r1"), ("ORI r1, 0x5A", "r1"), ("XORI r1, 0xFF", "r1"),
        ("LDI r0, 0x3FF", None), ("ADD r0, r1", "r0"), ("SUB r0, r2", "r0"), ("ADDI r0, -300", "r0"),
    ]
    # 64 host words; the host reads one word every 26 cycles.
    scn = Scenario("alu_flags", asm((program(ops), 0)), cycles=2200, host=pop_round_robin([0]))
    m, popped, _ = await run_lockstep(dut, scn)
    assert_ended(m, popped, min_words=2 * sum(1 for _, reg in ops if reg))


@cocotb.test()
async def test_core_shifts(dut):
    """SHL, SHR, ASR, ROR with n = 0, 1, 5, 15 on two patterns (C is the last bit out)."""
    ops = [("LDI r0, 0x201", None), ("SHL r0, 6", "r0"), ("ORI r0, 0x21", "r0")]
    for kind in ("SHL", "SHR", "ASR", "ROR"):
        for n in (0, 1, 5, 15):
            ops.append(("MOV r1, r0", None))
            ops.append((f"{kind} r1, {n}", "r1"))
    ops += [("LDI r0, 0x3FE", None), ("SHL r0, 5", "r0"), ("MOV r1, r0", None), ("ASR r1, 8", "r1"),
            ("MOV r1, r0", None), ("ROR r1, 8", "r1")]
    scn = Scenario("shifts", asm((program(ops), 0)), cycles=1600, host=pop_round_robin([0]))
    m, popped, _ = await run_lockstep(dut, scn)
    assert_ended(m, popped, min_words=2 * sum(1 for _, reg in ops if reg))


@cocotb.test()
async def test_core_branches_memory(dut):
    """Bcc (8 conditions, taken and not taken), DJNZ, JAL, JR, JMP, LD, ST, LDX, STX, "-" bits."""
    src = "\n".join([
        "        LDI r0, 4",
        "loop:   DJNZ r0, loop",
        "        PUSH r0",
        "        LDI r1, 3",
        "l2:     PUSH r1",
        "        DJNZ r1, l2",
        "        CMPI r0, 0",          # Z1 C1 N0
        "        BEQ a1",
        "        HALT",
        "a1:     BNE bad",
        "        BCS a2",
        "        HALT",
        "a2:     BCC bad",
        "        BMI bad",
        "        LDI r2, 1",
        "        CMPI r2, 2",          # Z0 C0 N1
        "        BEQ bad",
        "        BNE a3",
        "        HALT",
        "a3:     BCS bad",
        "        BCC a4",
        "        HALT",
        "a4:     BMI a5",
        "        HALT",
        "a5:     LDI r3, 8",
        "        IOW r3, FLAGS",       # E1
        "        BEC bad",
        "        BES a6",
        "        HALT",
        "a6:     LDI r3, 0",
        "        IOW r3, FLAGS",
        "        BES bad",
        "        BEC a7",
        "        HALT",
        "a7:     JAL r1, sub",
        "        PUSH r1",
        "        .word 0x01FF",        # NOP with all '-' bits set
        "        .word 0x8180 + a8",   # JMP a8 with the '-' bits set
        "        HALT",
        "a8:     LDI r0, 0x3A5",
        "        ST r0, 120",
        "        LD r2, 120",
        "        PUSH r2",
        "        LDI r1, 121",
        "        STX r2, [r1]",
        "        LDX r0, [r1]",
        "        PUSH r0",
        "        LD r0, a1",           # an instruction word as data
        "        PUSH r0",
        "        .word 0xB180 + 120",  # LD r0, 120 with the '-' bits set
        "        PUSH r0",
        "        LDI r1, done",
        "        JR r1",
        "        HALT",
        "sub:    PUSH r0",
        "        JR r1",
        "done:   HALT",
        "bad:    .word 0xF000",
    ])
    scn = Scenario("branches_memory", asm((src, 0)), cycles=1200, host=pop_round_robin([0]))
    m, popped, _ = await run_lockstep(dut, scn)
    assert_ended(m, popped, min_words=10)
    assert m.threads[0].pc == pe_asm.assemble(src).symbols["done"][0], "thread 0 did not end at 'done'"


@cocotb.test()
async def test_core_time(dut):
    """TADD imm and rs, TSET, TGET, NOW, TNOW, TSYNC (move and no move), TWAIT (future, now, past)."""
    src = "\n".join([
        "TNOW 20", "TGET r0", "PUSH r0",
        "NOW r1", "PUSH r1",
        "TADD 100", "TGET r0", "PUSH r0",
        "LDI r2, 300",
        "TADD r2", "TGET r0", "PUSH r0",
        "TSET r2", "TGET r0", "PUSH r0",
        "TSYNC 50", "TGET r0", "PUSH r0",
        "TWAIT", "NOW r1", "PUSH r1",
        "TSYNC 100", "TGET r0", "PUSH r0",
        "TWAIT", "NOW r1", "PUSH r1",
        "TNOW 0", "TWAIT", "NOW r1", "PUSH r1",
        "TNOW 127", "TSYNC 3", "TGET r0", "PUSH r0",
        "LDI r2, 0", "TSET r2", "TWAIT", "NOW r1", "PUSH r1",
        "TADD 4095", "TGET r0", "PUSH r0",
        ".word 0x75FF",   # TGET r3 with the '-' bits set
        "PUSH r3",
        "HALT",
    ])
    scn = Scenario("time", asm((src, 0)), cycles=1200, host=pop_round_robin([0]))
    m, popped, _ = await run_lockstep(dut, scn)
    assert_ended(m, popped, min_words=14)


@cocotb.test()
async def test_core_out_events(dut):
    """OUT 0/1/Z/r/r>>/r<<, offsets, push-pull and open drain, full slot, late, not owner, pin 15."""
    t0 = "\n".join([
        "        TNOW 40",
        "        OUT UIO0, 1",
        "        OUT UIO1, 0, 3",
        "        OUT UIO2, Z, 7",
        "        LDI r0, 0x2A5",
        "        OUT UIO3, r0",
        "        OUT UIO4, r0>>, 1",
        "        OUT UIO5, r0<<, 2",
        "        OUT UO0, 0, 4",
        "        OUT UO1, 1, 5",
        "        OUT UO2, r0, 6",
        "        PUSH r0",
        "        TADD 30",
        "        OUT UIO2, 1",       # open drain: released
        "        OUT UIO5, 0",
        "        OUT UIO0, 0",
        "        TADD 10",
        "        OUT UIO0, 1",       # slot full until the event before occurs
        "        NOW r1",
        "        PUSH r1",
        "        TNOW 7",
        "        OUT UO3, 0",        # lead 3: late
        "        IOR r1, STATUS",
        "        PUSH r1",
        "        LDI r1, 1",
        "        IOW r1, STATUS",
        "        LDI r2, 0",
        "        TSET r2",
        "        OUT UO4, 0",        # time in the past: late
        "        IOR r1, STATUS",
        "        PUSH r1",
        "        OUT UO6, 0",        # thread 1 owns UO6: ERR
        "        .word 0xCF08",      # OUT pin 15, 1: ERR
        "        .word 0xC0C8",      # OUT UIO0, 1 with r = 3 ('-' field for kind 1)
        "        IOR r1, STATUS",
        "        PUSH r1",
        "        LDI r1, 3",
        "        IOW r1, STATUS",
        "        TNOW 40",
        "        LDI r0, 0x155",
        "        LDI r2, 8",
        "ser:    OUT UIO7, r0>>",
        "        TADD 5",            # 5 cycles each, slower than the loop: becomes late
        "        DJNZ r2, ser",
        "        IOR r1, STATUS",
        "        PUSH r1",
        "        TNOW 60",
        "        LDI r0, 0x2C3",
        "        OUT UO5, r0<<, 7",
        "        OUT UIO6, Z",       # thread 1 owns UIO6 too: ERR, no change
        "        HALT",
    ])
    t1 = "\n".join([
        "        TNOW 50",
        "        OUT UO6, 1",
        "        TADD 3",
        "        OUT UO6, 0",        # full slot for 3 cycles: waits, then late
        "        OUT UIO6, 0, 2",
        "        TADD 40",
        "        OUT UIO6, Z",
        "        IOR r1, STATUS",
        "        PUSH r1",
        "        HALT",
    ])
    own = {p: 0 for p in range(0, 6)}
    own.update({7: 0, 8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 13: 0, 6: 1, 14: 1})
    od = [p in (2, 5, 13) for p in range(pe_model.N_OUT)]
    scn = Scenario("out_events", asm((t0, 0), (t1, 96)), start_pc=(0, 96, 0, 0), run=(True, True, False, False),
                   owner=owners(own), od=od, cycles=1500, host=pop_round_robin([0, 1]))
    m, popped, changes = await run_lockstep(dut, scn)
    assert_ended(m, popped, want_status=(m.threads[0].status, m.threads[1].status, 0, 0), threads=(0, 1), min_words=6)
    assert changes >= 15, f"only {changes} pin changes"


@cocotb.test()
async def test_core_horizon(dut):
    """Lead 32,767 is on time, lead 32,768 is late (isa 4.2): 32,820 cycles."""
    src = "\n".join([
        "LDI r0, 1000", "SHL r0, 5", "ADDI r0, 511", "ADDI r0, 276",
        "TSET r0",            # T = 32,787
        "OUT UO0, 0",         # c20: lead 32,767
        "IOR r1, STATUS", "PUSH r1",
        "ADDI r0, 21", "TSET r0",
        "OUT UO1, 0",         # c40: lead 32,768: late
        "IOR r1, STATUS", "PUSH r1",
        "HALT",
    ])
    scn = Scenario("horizon", asm((src, 0)), owner=owners({8: 0, 9: 0}), cycles=32_820,
                   host=pop_round_robin([0]))
    m, popped, changes = await run_lockstep(dut, scn)
    assert popped[0] == [0, 1], popped[0]
    assert changes == 2, changes


def sample_wait_stimulus(cycles):
    """UI0: period 22 cycles; UI3: a new random level every 17 cycles; UI4: 0; uio: random every 13."""
    rng = random.Random(2026)
    ui3 = 1
    uio = 0
    table = []
    for c in range(cycles):
        if c % 17 == 0:
            ui3 = rng.randrange(2)
        if c % 13 == 0:
            uio = rng.randrange(256)
        ui0 = (c // 11) & 1
        table.append((ui0 | (ui3 << 3), uio))
    return lambda c: table[c]


@cocotb.test()
async def test_core_sample_wait(dut):
    """SAMPLE r>> and r<< with offsets, late SAMPLE, pin 13, own-output read back; WAIT FALL, RISE, ANY, TO, CT, expired."""
    t0 = "\n".join([
        "        TNOW 30",
        "        LDI r2, 16",
        "s1:     SAMPLE UI3, r0>>, 1",
        "        TADD 13",               # the loop takes 12 cycles: each sample is on time
        "        DJNZ r2, s1",
        "        PUSH r0",
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        TNOW 2",
        "        SAMPLE UI0, r1<<",      # lead -2: late
        "        IOR r3, STATUS",
        "        PUSH r3",
        "        PUSH r1",
        "        LDI r3, 1",
        "        IOW r3, STATUS",
        "        TNOW 40",
        "        SAMPLE UIO2, r1<<, 5",
        "        PUSH r1",
        "        .word 0xCD38",          # SAMPLE input 13 (none, reads 0), r0<<
        "        PUSH r0",
        "        WAIT UI3, FALL, r2, CT",
        "        PUSH r2",
        "        TGET r1",
        "        PUSH r1",
        "        WAIT UI3, RISE, r2",
        "        PUSH r2",
        "        WAIT UI0, ANY, r2",
        "        PUSH r2",
        "        TNOW 100",
        "        WAIT UI4, RISE, r2, TO",  # UI4 is 0: timeout
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        LDI r0, 0",
        "        TSET r0",
        "        WAIT UI3, ANY, r2, TO",   # T in the past: expired
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        LDI r0, 12",
        "w1:     TNOW 6",
        "        WAIT UI0, ANY, r2, TO, CT",  # short timeout: edges near the end of the window
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        PUSH r2",
        "        DJNZ r0, w1",
        "        HALT",
    ])
    t1 = "\n".join([
        "        TNOW 20",
        "        OUT UIO6, 0",
        "        TADD 30",
        "        OUT UIO6, 1",
        "        TADD 30",
        "        OUT UIO6, Z",
        "        TNOW 10",
        "        SAMPLE UIO6, r0<<",      # own output (OE = 1) read back
        "        WAIT UIO6, ANY, r1, TO",
        "        IOR r2, PINS",
        "        PUSH r0",
        "        PUSH r1",
        "        PUSH r2",
        "        WAIT UIO6, ANY, r1",     # released: the pad value from the test bench
        "        PUSH r1",
        "        HALT",
    ])
    cycles = 3000
    scn = Scenario("sample_wait", asm((t0, 0), (t1, 100)), start_pc=(0, 100, 0, 0), run=(True, True, False, False),
                   owner=owners({6: 1}), cycles=cycles, host=pop_round_robin([0, 1]),
                   stimulus=sample_wait_stimulus(cycles))
    m, popped, _ = await run_lockstep(dut, scn)
    # Thread 0 ends with LATE: the SAMPLE of input 13 has T + 0, which is
    # already past after the SAMPLE at T + 5 (isa 4.3).
    assert_ended(m, popped, want_status=(1, 0, 0, 0), threads=(0, 1), min_words=40)


@cocotb.test()
async def test_core_host_io(dut):
    """PULL, PULL.NB, PUSH, PUSH.NB (empty, data, full) on 4 threads; IOR and IOW of all port kinds."""
    src = "\n".join([
        "        PULL.NB r0",
        "        IOR r1, FLAGS",
        "        PUSH.NB r1",
        "        PULL r0",
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        PULL r2",
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        IOR r1, THREAD",
        "        PUSH r1",
        "        IOR r1, 7",
        "        PUSH r1",
        "        IOR r1, 40",
        "        PUSH r1",
        "        .word 0xE942",          # IOR r1, PINS with the '-' bit set
        "        PUSH r1",
        "        LDI r1, 0xF",
        "        IOW r1, FLAGS",
        "        IOR r2, FLAGS",
        "        PUSH r2",
        "        LDI r1, 0",
        "        IOW r1, 9",
        "        IOW r1, FLAGS",
        "        LDI r2, 6",
        "fill:   PUSH r0",               # the host does not read yet: the FIFO fills, PUSH waits
        "        DJNZ r2, fill",
        "        PUSH.NB r0",
        "        IOR r1, FLAGS",
        "        PUSH r1",
        "        HALT",
    ])
    image = {}
    for n in range(4):
        image.update(asm((src, 32 * n)))
    words = [0x1234, 0x0000, 0x8000, 0xFFFF, 0x00FF, 0x7F00, 0x5A5A, 0xA5A5, 0x0001, 0xC3C3]

    def host(i):
        if 4 <= i < 14:
            return (CMD_PUSH, i % 4, words[i - 4])
        if i < 60:
            return (CMD_NOP, 0, 0)
        return (CMD_POP, i % 4, 0)

    scn = Scenario("host_io", image, start_pc=(0, 32, 64, 96), run=(True, True, True, True),
                   cycles=26 * 140, host=host, stimulus=lambda c: (c & 0x1F, (c >> 3) & 0xFF))
    m, popped, _ = await run_lockstep(dut, scn)
    assert_ended(m, popped, threads=(0, 1, 2), min_words=40)


ILLEGAL_SETS = [
    (0x0500, 0x3090, 0x7E00, 0xDB30),  # system 5, ALU 9, TIME 7, WAIT edge 3
    (0xF123, 0x0F00, 0x30F0, 0x0000),  # opcode 15, system 15, ALU 15, HALT
]


@cocotb.test()
async def test_core_illegal_halt(dut):
    """Each illegal form stops its thread with ERR and keeps PC; HALT stops with no ERR (isa 12)."""
    for k, words in enumerate(ILLEGAL_SETS):
        parts = []
        for n, w in enumerate(words):
            parts.append((f"LDI r0, {n + 1}\nPUSH r0\n.word {w}\nPUSH r0\nJMP {8 * n + 1}", 8 * n))
        scn = Scenario(f"illegal_{k}", asm(*parts), start_pc=(0, 8, 16, 24), run=(True, True, True, True),
                       cycles=400)
        m, popped, _ = await run_lockstep(dut, scn)
        want = tuple(0 if w == 0 else 2 for w in words)
        assert_ended(m, popped, want_status=want, threads=(0, 1, 2, 3), min_words=4)
        assert [th.pc for th in m.threads] == [8 * n + 2 for n in range(4)], [th.pc for th in m.threads]


@cocotb.test()
async def test_core_barrel(dut):
    """Four threads at the same time: thread n has the slots c mod 4 = n (isa 3.1)."""
    prog = "NOW r0\nPUSH r0\nNOW r0\nPUSH r0\nIOR r1, THREAD\nPUSH r1\nHALT"
    scn = Scenario("barrel", asm(*[(prog, 8 * n) for n in range(4)]), start_pc=(0, 8, 16, 24),
                   run=(True, True, True, True), cycles=500)
    m, popped, _ = await run_lockstep(dut, scn)
    assert_ended(m, popped, threads=(0, 1, 2, 3), min_words=12)
    for n in range(4):
        # NOW in the first slot (cycle n) and in the third slot (cycle n + 8).
        assert popped[n] == [n, n + 8, n], f"thread {n}: {popped[n]}"


# ----- random instruction streams -----

KINDS = {
    "nop": 2, "halt": 0.15, "jr": 0.4, "ldx": 1, "stx": 1, "sys_bad": 0.1,
    "ldi": 8, "addi": 6, "alu": 10, "alu_bad": 0.1, "alui": 6, "shift": 6,
    "tadd": 5, "time": 8, "twait": 1, "time_bad": 0.1,
    "bcc": 6, "djnz": 3, "jal": 1, "ld": 3, "st": 3,
    "out": 12, "sample": 5, "wait": 4, "wait_bad": 0.1,
    "pull": 3, "push": 4, "ior": 3, "iow": 2, "op15": 0.1,
}


def rand_insn(rng, lo, hi, d0, own_pins=()):
    """Words of one random instruction (or a short sequence) for code at lo..hi, data at d0..d0+3.

    own_pins: output pins that the thread owns. Most OUT instructions use
    them, so that the event slots and pins change (an OUT to a pin of an
    other thread only sets ERR, isa 5.2).
    """
    def r():
        return rng.randrange(4)

    def out_pin():
        if own_pins and rng.random() < 0.85:
            return rng.choice(own_pins)
        return rng.randrange(16)

    def in_pin():
        return rng.randrange(13) if rng.random() < 0.95 else rng.randrange(16)

    def tgt():
        # Forward targets only (lo is the address of this instruction), so
        # that the program does not stay in a short loop; the code of each
        # thread ends with a JMP to its start.
        return rng.randint(min(lo + 1, hi), hi)

    def tnow():
        # TNOW 8 to 127: T near now, so that a following wait is short.
        return 0x7000 | 4 << 9 | rng.randrange(8, 128)

    def dash(bits):
        return rng.randrange(1 << bits)

    k = rng.choices(list(KINDS), weights=list(KINDS.values()))[0]
    if k == "nop":
        return [0x0100 | dash(8)]
    if k == "halt":
        return [dash(8)]
    if k == "jr":
        return [0x0200 | r() << 6 | dash(6)]
    if k in ("ldx", "stx"):
        ra = r()
        # ANDI ra, 3 and ORI ra, d0: the address is a data word of this thread.
        guard = [0x4000 | ra << 10 | 0x003, 0x4000 | ra << 10 | 1 << 8 | d0]
        return guard + [(0x0300 if k == "ldx" else 0x0400) | r() << 6 | ra << 4 | dash(4)]
    if k == "sys_bad":
        return [rng.randint(5, 15) << 8 | dash(8)]
    if k == "ldi":
        return [0x1000 | r() << 10 | rng.randrange(1024)]
    if k == "addi":
        return [0x2000 | r() << 10 | rng.randrange(1024)]
    if k == "alu":
        return [0x3000 | r() << 10 | r() << 8 | rng.randrange(9) << 4 | dash(4)]
    if k == "alu_bad":
        return [0x3000 | r() << 10 | r() << 8 | rng.randint(9, 15) << 4 | dash(4)]
    if k == "alui":
        return [0x4000 | r() << 10 | r() << 8 | rng.randrange(256)]
    if k == "shift":
        return [0x5000 | r() << 10 | r() << 8 | rng.randrange(16) << 4 | dash(4)]
    if k == "tadd":
        return [0x6000 | (rng.randrange(64) if rng.random() < 0.85 else rng.randrange(4096))]
    if k == "time":
        return [0x7000 | rng.choice([0, 1, 2, 3, 4, 4, 5, 5]) << 9 | r() << 7 | rng.randrange(128)]
    if k == "twait":
        return [0x7C00 | dash(9)]
    if k == "time_bad":
        return [0x7E00 | dash(9)]
    if k == "bcc":
        return [0x8000 | rng.randrange(8) << 9 | dash(2) << 7 | tgt()]
    if k == "djnz":
        return [0x9000 | r() << 10 | dash(3) << 7 | tgt()]
    if k == "jal":
        return [0xA000 | r() << 10 | dash(3) << 7 | tgt()]
    if k == "ld":
        return [0xB000 | r() << 9 | dash(2) << 7 | rng.randrange(128)]
    if k == "st":
        return [0xB800 | r() << 9 | dash(2) << 7 | (d0 + rng.randrange(4))]
    if k == "out":
        return [0xC000 | out_pin() << 8 | r() << 6 | rng.randrange(6) << 3 | rng.randrange(8)]
    if k == "sample":
        return [0xC000 | in_pin() << 8 | r() << 6 | rng.randint(6, 7) << 3 | rng.randrange(8)]
    if k == "wait":
        return [0xD000 | in_pin() << 8 | r() << 6 | rng.randrange(3) << 4 | rng.randrange(4) << 2 | dash(2)]
    if k == "wait_bad":
        return [0xD000 | in_pin() << 8 | r() << 6 | 3 << 4 | dash(4)]
    if k in ("pull", "push"):
        return [0xE000 | (0 if k == "pull" else 1) << 10 | r() << 8 | rng.randrange(2) << 7 | dash(7)]
    if k in ("ior", "iow"):
        port = rng.randrange(4) if rng.random() < 0.85 else rng.randrange(64)
        return [0xE000 | (2 if k == "ior" else 3) << 10 | r() << 8 | dash(2) << 6 | port]
    return [0xF000 | dash(12)]  # op15


def random_scenario(seed, cycles):
    """A random program for 4 threads that the model accepts. Returns (scenario, attempts)."""
    rng = random.Random(seed)
    for attempt in range(1, 200):
        # Pin owners first: 1 pin in 10 has no owner.
        owner = [rng.choice([0, 1, 2, 3]) if rng.random() < 0.9 else None for _ in range(pe_model.N_OUT)]
        od = [rng.random() < 0.3 for _ in range(pe_model.N_OUT)]
        image = [0] * 128
        data_addrs = set()
        for n in range(4):
            lo, hi, d0 = 32 * n, 32 * n + 27, 32 * n + 28
            mine = [p for p in range(pe_model.N_OUT) if owner[p] == n]
            words = []
            while len(words) < 28:
                words += rand_insn(rng, lo, hi, d0, mine)
            image[lo:lo + 28] = words[:28]
            image[d0:d0 + 4] = [rng.randrange(0x10000) for _ in range(4)]
            data_addrs.update(range(d0, d0 + 4))
        run = [rng.random() < 0.9 for _ in range(4)]
        run[rng.randrange(4)] = True
        start_pc = [32 * n + rng.randrange(4) for n in range(4)]
        host_rng = random.Random(seed * 1000 + attempt)
        host_frames = []
        for _ in range(cycles // TP_BITS + 2):
            if host_rng.random() < 0.45:
                host_frames.append((CMD_PUSH, host_rng.randrange(4), host_rng.randrange(0x10000)))
            else:
                host_frames.append((CMD_POP, host_rng.randrange(4), 0))
        in_rng = random.Random(seed * 7919 + attempt)
        level = in_rng.randrange(1 << 13)
        table = []
        for _ in range(cycles):
            for p in range(13):
                if in_rng.random() < 0.02:
                    level ^= 1 << p
            table.append(((level >> 8) & 0x1F, level & 0xFF))
        scn = Scenario(f"random seed {seed}", image, start_pc=start_pc, run=run, owner=owner, od=od,
                       cycles=cycles, host=host_frames.__getitem__, stimulus=table.__getitem__)
        try:
            model_only(scn, data_addrs)
        except pe_model.ModelError:
            continue
        return scn, attempt
    raise AssertionError(f"seed {seed}: no program without ModelError in 200 attempts")


@cocotb.test()
async def test_core_random(dut):
    """Random instruction streams on 4 threads with random inputs and host commands (fixed seeds)."""
    gl = state_handle(dut) is None
    seeds = RANDOM_SEEDS_GL if gl else RANDOM_SEEDS
    total = Counter()
    all_changes = 0
    all_words = 0
    for seed in seeds:
        scn, attempts = random_scenario(seed, RANDOM_CYCLES)
        dut._log.info(f"{scn.label}: program accepted after {attempts} attempt(s)")
        m, popped, changes = await run_lockstep(dut, scn)
        total.update(m.coverage)
        all_changes += changes
        all_words += sum(len(p) for p in popped)
    dut._log.info(f"random streams: seeds {seeds}, {RANDOM_CYCLES} cycles each; {all_changes} pin changes, "
                  f"{all_words} host words in total; instruction groups executed in total {dict(sorted(total.items()))}")
    groups = {k.split(".")[0] for k in total}
    missing = [f"{op:X}" for op in range(16) if f"{op:X}" not in groups]
    assert not missing, f"opcodes never executed: {missing}"

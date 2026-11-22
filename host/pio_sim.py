# SPDX-License-Identifier: Apache-2.0
"""CPython stand-in for MicroPython's rp2 module: an asm_pio assembler and a PIO state-machine interpreter
for the instructions that host/pe_host/pio_spi.py uses, so that the same program text runs in the test bench.

Written from the RP2350 data sheet, PIO chapter (instruction encoding table; side-set and delay field;
autopull and autopush; wrap; JMP X-- tests X before the decrement). Only these instructions are decoded:
JMP (all conditions except PIN), IN, OUT (pins, x, y, null), PUSH, PULL, MOV (no operations), SET. Any
other word raises an error, so an unsupported instruction cannot pass silently.

Not modelled: the input synchronizer (the test bench adds its 2-cycle delay on the IN pin), IRQ, WAIT,
EXEC, FIFO join, status selection, side-set enable bit (SIDE_EN), PINCTRL pin wrapping.
"""

import types


class PIO:
    IN_LOW, IN_HIGH, OUT_LOW, OUT_HIGH = 0, 1, 2, 3          # MicroPython rp2.PIO constants
    SHIFT_LEFT, SHIFT_RIGHT = 0, 1

    def __init__(self, n):
        self.n = n
        self.base = 0

    def gpio_base(self, base=None):
        if base is not None:
            self.base = int(base)
        return self.base


JMP_COND = {None: 0, "not_x": 1, "x_dec": 2, "not_y": 3, "y_dec": 4, "x_not_y": 5, "pin": 6, "not_osre": 7}
IN_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 6, "osr": 7}
OUT_DST = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6, "exec": 7}
MOV_DST = {"pins": 0, "x": 1, "y": 2, "exec": 4, "pc": 5, "isr": 6, "osr": 7}
MOV_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "status": 5, "isr": 6, "osr": 7}
SET_DST = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}


class Program:
    def __init__(self, words, side_count, wrap_bottom, wrap_top, kw):
        self.words = words
        self.side_count = side_count
        self.wrap_bottom, self.wrap_top = wrap_bottom, wrap_top
        self.kw = kw


class _Ins:
    def __init__(self, em, word):
        self.em, self.word = em, word
        self.idx = em.emit(self)
        self.side_v = None
        self.delay_v = 0

    def side(self, v):
        self.side_v = v
        return self

    def __getitem__(self, d):
        self.delay_v = d
        return self

    def encode(self):
        s = self.em.side_count
        if self.side_v is None and s:
            raise ValueError("instruction %d has no side-set value (side-set count %d, no SIDE_EN)" % (self.idx, s))
        if not 0 <= self.delay_v < (1 << (5 - s)):
            raise ValueError("delay %d does not fit next to %d side-set bits" % (self.delay_v, s))
        field = ((self.side_v or 0) << (5 - s)) | self.delay_v
        return self.word(self.em) | (field << 8)


class _Emitter:
    def __init__(self, kw):
        self.kw = kw
        si = kw.get("sideset_init")
        self.side_count = len(si) if isinstance(si, tuple) else (1 if si is not None else 0)
        self.labels = {}
        self.ins = []
        self.wrap_bottom = 0
        self.wrap_top = None

    def emit(self, ins):
        self.ins.append(ins)
        return len(self.ins) - 1

    def names(self):
        em = self

        def label(n):
            em.labels[n] = len(em.ins)

        def wrap_target():
            em.wrap_bottom = len(em.ins)

        def wrap():
            em.wrap_top = len(em.ins) - 1

        def nop():
            return _Ins(em, lambda e: 0xA042)          # MOV Y, Y

        def jmp(a, b=None):
            cond, lab = (None, a) if b is None else (a, b)
            return _Ins(em, lambda e: (JMP_COND[cond] << 5) | e.labels[lab])

        def in_(src, n):
            return _Ins(em, lambda e: 0x4000 | (IN_SRC[src] << 5) | (n & 31))

        def out(dst, n):
            return _Ins(em, lambda e: 0x6000 | (OUT_DST[dst] << 5) | (n & 31))

        def pull(block="block", ifempty=False):
            return _Ins(em, lambda e: 0x8080 | (bool(ifempty) << 6) | ((block == "block") << 5))

        def push(block="block", iffull=False):
            return _Ins(em, lambda e: 0x8000 | (bool(iffull) << 6) | ((block == "block") << 5))

        def mov(dst, src):
            return _Ins(em, lambda e: 0xA000 | (MOV_DST[dst] << 5) | MOV_SRC[src])

        def set_(dst, v):
            return _Ins(em, lambda e: 0xE000 | (SET_DST[dst] << 5) | (v & 31))

        g = {"label": label, "wrap_target": wrap_target, "wrap": wrap, "nop": nop, "jmp": jmp, "in_": in_,
             "out": out, "pull": pull, "push": push, "mov": mov, "set": set_}
        for n in ("pins", "x", "y", "null", "isr", "osr", "pindirs", "pc", "exec", "status", "block", "noblock",
                  "x_dec", "y_dec", "not_x", "not_y", "x_not_y", "not_osre"):
            g[n] = n
        return g


def asm_pio(**kw):
    """Decorator as rp2.asm_pio: runs the body with the instruction names as its globals and returns a
    Program. Words are encoded after the body has run, so a JMP to a later label resolves in one pass."""
    def dec(f):
        em = _Emitter(kw)
        fn = types.FunctionType(f.__code__, em.names(), f.__name__, f.__defaults__, f.__closure__)
        fn()
        words = [i.encode() for i in em.ins]
        top = em.wrap_top if em.wrap_top is not None else len(words) - 1
        return Program(words, em.side_count, em.wrap_bottom, top, kw)
    return dec


class StateMachine:
    """One state machine. The test bench calls step() once per state-machine clock cycle; drive(pin, v) and
    read(pin) connect it to the pins (read returns the synchronized level)."""

    def __init__(self, sm_id, prog, freq=None, sideset_base=None, out_base=None, in_base=None,
                 drive=None, read=None):
        self.prog, self.freq = prog, freq
        kw = prog.kw
        self.side_base, self.out_base, self.in_base = sideset_base, out_base, in_base
        self.out_left = kw.get("out_shiftdir", PIO.SHIFT_RIGHT) == PIO.SHIFT_LEFT
        self.in_left = kw.get("in_shiftdir", PIO.SHIFT_RIGHT) == PIO.SHIFT_LEFT
        self.autopull, self.autopush = kw.get("autopull", False), kw.get("autopush", False)
        self.pull_thresh = kw.get("pull_thresh", 32) or 32
        self.push_thresh = kw.get("push_thresh", 32) or 32
        self.drive, self.read = drive, read
        self.pc = 0
        self.x = self.y = self.osr = self.isr = 0
        self.osr_cnt, self.isr_cnt = 32, 0
        self.delay = 0
        self.txf, self.rxf = [], []
        self.running = False
        self.cycles = 0
        self.stalled = False
        si = kw.get("sideset_init")
        for k, v in enumerate(si if isinstance(si, tuple) else ((si,) if si is not None else ())):
            self._drive(self.side_base + k, 1 if v == PIO.OUT_HIGH else 0)
        oi = kw.get("out_init")
        if oi is not None:
            self._drive(self.out_base, 1 if oi == PIO.OUT_HIGH else 0)

    def _drive(self, pin, v):
        if self.drive is not None:
            self.drive(pin, v)

    def active(self, on=None):
        if on is not None:
            self.running = bool(on)
        return self.running

    def put(self, w):
        if len(self.txf) >= 4:
            raise RuntimeError("TX FIFO full (the bench must check tx_fifo first)")
        self.txf.append(w & 0xFFFFFFFF)

    def get(self):
        return self.rxf.pop(0)

    def tx_fifo(self):
        return len(self.txf)

    def rx_fifo(self):
        return len(self.rxf)

    def step(self):
        """One clock cycle. Returns the executed word or None (delay cycle)."""
        self.cycles += 1
        if not self.running:
            return None
        if self.delay:
            self.delay -= 1
            return None
        w = self.prog.words[self.pc]
        s = self.prog.side_count
        field = (w >> 8) & 0x1F
        if s:
            side = field >> (5 - s)
            for k in range(s):
                self._drive(self.side_base + k, (side >> k) & 1)
        dly = field & ((1 << (5 - s)) - 1)
        op = w >> 13
        jump = None
        stall = False
        if op == 0:                                     # JMP
            cond, addr = (w >> 5) & 7, w & 31
            if cond == 0:
                take = True
            elif cond == 1:
                take = self.x == 0
            elif cond == 2:
                take = self.x != 0
                self.x = (self.x - 1) & 0xFFFFFFFF
            elif cond == 3:
                take = self.y == 0
            elif cond == 4:
                take = self.y != 0
                self.y = (self.y - 1) & 0xFFFFFFFF
            elif cond == 5:
                take = self.x != self.y
            elif cond == 7:
                take = self.osr_cnt < self.pull_thresh
            else:
                raise NotImplementedError("JMP PIN")
            if take:
                jump = addr
        elif op == 2:                                   # IN
            src, n = (w >> 5) & 7, (w & 31) or 32
            if self.autopush and self.isr_cnt >= self.push_thresh and len(self.rxf) >= 4:
                stall = True
            else:
                if src == 0:
                    v = 0
                    for k in range(n):
                        v |= (self.read(self.in_base + k) & 1) << k
                elif src in (1, 2, 3, 6, 7):
                    v = {1: self.x, 2: self.y, 3: 0, 6: self.isr, 7: self.osr}[src]
                else:
                    raise NotImplementedError("IN source %d" % src)
                v &= (1 << n) - 1 if n < 32 else 0xFFFFFFFF
                if self.in_left:
                    self.isr = ((self.isr << n) | v) & 0xFFFFFFFF
                else:
                    self.isr = ((self.isr >> n) | (v << (32 - n))) & 0xFFFFFFFF
                self.isr_cnt = min(32, self.isr_cnt + n)
                if self.autopush and self.isr_cnt >= self.push_thresh:
                    self.rxf.append(self.isr)
                    self.isr, self.isr_cnt = 0, 0
        elif op == 3:                                   # OUT
            dst, n = (w >> 5) & 7, (w & 31) or 32
            if self.autopull and self.osr_cnt >= self.pull_thresh:
                if not self.txf:
                    stall = True
                else:
                    self.osr, self.osr_cnt = self.txf.pop(0), 0
            if not stall:
                if self.out_left:
                    v = self.osr >> (32 - n)
                    self.osr = (self.osr << n) & 0xFFFFFFFF if n < 32 else 0
                else:
                    v = self.osr & ((1 << n) - 1 if n < 32 else 0xFFFFFFFF)
                    self.osr = self.osr >> n if n < 32 else 0
                self.osr_cnt = min(32, self.osr_cnt + n)
                if dst == 0:
                    for k in range(n):
                        self._drive(self.out_base + k, (v >> k) & 1)
                elif dst == 1:
                    self.x = v
                elif dst == 2:
                    self.y = v
                elif dst == 3:
                    pass
                else:
                    raise NotImplementedError("OUT destination %d" % dst)
        elif op == 4:                                   # PUSH / PULL
            is_pull, if_c, block = (w >> 7) & 1, (w >> 6) & 1, (w >> 5) & 1
            if is_pull:
                if if_c and self.osr_cnt < self.pull_thresh:
                    pass
                elif self.txf:
                    self.osr, self.osr_cnt = self.txf.pop(0), 0
                elif block:
                    stall = True
                else:
                    self.osr, self.osr_cnt = self.x, 0
            else:
                if if_c and self.isr_cnt < self.push_thresh:
                    pass
                elif len(self.rxf) < 4:
                    self.rxf.append(self.isr)
                    self.isr, self.isr_cnt = 0, 0
                elif block:
                    stall = True
        elif op == 5:                                   # MOV
            dst, mop, src = (w >> 5) & 7, (w >> 3) & 3, w & 7
            if mop:
                raise NotImplementedError("MOV operation %d" % mop)
            v = {1: self.x, 2: self.y, 3: 0, 6: self.isr, 7: self.osr}.get(src)
            if v is None:
                raise NotImplementedError("MOV source %d" % src)
            if dst == 1:
                self.x = v
            elif dst == 2:
                self.y = v
            elif dst == 6:
                self.isr, self.isr_cnt = v, 0
            elif dst == 7:
                self.osr, self.osr_cnt = v, 0
            else:
                raise NotImplementedError("MOV destination %d" % dst)
        elif op == 7:                                   # SET
            dst, v = (w >> 5) & 7, w & 31
            if dst == 1:
                self.x = v
            elif dst == 2:
                self.y = v
            else:
                raise NotImplementedError("SET destination %d" % dst)
        else:
            raise NotImplementedError("instruction %04x" % w)
        self.stalled = stall
        if stall:
            return w
        if jump is not None:
            self.pc = jump
        elif self.pc == self.prog.wrap_top:
            self.pc = self.prog.wrap_bottom
        else:
            self.pc += 1
        self.delay = dly
        return w


class Pin:
    """Pin numbers only (the bench maps them to chip signals)."""

    def __init__(self, n, *a, **k):
        self.n = n

    def __int__(self):
        return self.n

    def __index__(self):
        return self.n

    def __add__(self, k):
        return int(self) + k


class Rp2Module:
    """What PioSpi needs from rp2: PIO, asm_pio and a StateMachine bound to the bench's pin callbacks."""

    PIO = PIO
    asm_pio = staticmethod(asm_pio)

    def __init__(self, drive, read):
        self._drive, self._read = drive, read
        self.machines = []

    def StateMachine(self, sm_id, prog, freq=None, sideset_base=None, out_base=None, in_base=None):  # noqa: N802
        sm = StateMachine(sm_id, prog, freq, int(sideset_base), int(out_base), int(in_base),
                          drive=self._drive, read=self._read)
        self.machines.append(sm)
        return sm

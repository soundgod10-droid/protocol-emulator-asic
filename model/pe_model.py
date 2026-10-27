# SPDX-License-Identifier: Apache-2.0
"""Cycle reference model of the protocol emulator processor (docs/isa.md).

What it does:
  The model executes the instruction set of docs/isa.md. It has 4 threads
  in barrel order: thread n executes one instruction in each clock cycle c
  with c mod 4 = n. It also has the timed output unit (one event slot for
  each output pin), the timed input unit (2-stage synchronizer, SAMPLE and
  WAIT) and the host FIFOs. step() executes one clock cycle and gives the
  pin values of that cycle.

Why:
  The model is the reference for the processor RTL of milestone M2b
  (A1_architecture.md section 8.1, level L1). A test gives the same inputs
  to the RTL and to the model and compares the pins in each clock cycle.
  The model comes from docs/isa.md only. It does not use the assembler,
  so that a fault in one of them does not hide the same fault in the other.
  The comments give the section of docs/isa.md ("isa 4.2") for each rule.
"""

from collections import deque

N_THREADS = 4
MEM_WORDS = 128
N_OUT = 15  # output pins 0 to 14 (isa 5.1)
LEAD = 4  # minimum lead of a pin event in cycles (isa 4.2)
HORIZON = 0x8000  # a lead of 32,768 or more is a time in the past (isa 4.2)
FIFO_DEPTH = 4  # words in each host FIFO (isa 2.2)

STATUS_LATE = 0x1  # port STATUS bit 0 (isa 10)
STATUS_ERR = 0x2  # port STATUS bit 1 (isa 10)

PORT_STATUS = 0
PORT_FLAGS = 1
PORT_PINS = 2
PORT_THREAD = 3


class ModelError(Exception):
    """The firmware did something that docs/isa.md does not define."""


def _signed16(value):
    """The 16-bit value as a signed number (-32,768 to 32,767)."""
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


class Thread:
    """State of one thread (isa 2.1)."""

    def __init__(self, number):
        self.number = number
        self.pc = 0
        self.r = [0, 0, 0, 0]
        self.t = 0
        self.z = 0
        self.c = 0
        self.n = 0
        self.e = 0
        self.status = 0  # LATE and ERR (sticky)
        self.run = False
        self.pend = None  # blocking operation that started and is not complete
        self.stop_reason = None  # text for test messages only

    def flags(self):
        """Flags as port FLAGS shows them (isa 10)."""
        return self.z | (self.c << 1) | (self.n << 2) | (self.e << 3)


class PeModel:
    """Processor, timed I/O unit and host FIFOs.

    image: list of up to 128 words, or a dict {address: word}.
    start_pc, run: start address and RUN bit of each thread (host configuration).
    owner: for each output pin 0 to 14, the owner thread number or None (isa 5.2).
    open_drain: for each output pin, True for open-drain mode (isa 5.3).
    """

    def __init__(self, image, start_pc=(0, 0, 0, 0), run=(True, False, False, False), owner=None, open_drain=None):
        if isinstance(image, dict):
            words = [0] * MEM_WORDS
            for addr, word in image.items():
                words[addr] = word
        else:
            words = list(image)
            if len(words) > MEM_WORDS:
                raise ValueError(f"image has {len(words)} words, the memory has {MEM_WORDS}")
            words += [0] * (MEM_WORDS - len(words))
        self.mem = [w & 0xFFFF for w in words]
        self.store_cycle = {}  # address -> cycle of the last store (isa 3.3)

        self.cycle = 0
        self.threads = [Thread(n) for n in range(N_THREADS)]
        for th, pc, go in zip(self.threads, start_pc, run):
            th.pc = pc & 0x7F
            th.run = bool(go)

        self.owner = list(owner) if owner is not None else [None] * N_OUT
        self.od = [bool(x) for x in open_drain] if open_drain is not None else [False] * N_OUT
        if len(self.owner) != N_OUT or len(self.od) != N_OUT:
            raise ValueError(f"owner and open_drain must have {N_OUT} entries")

        # Output pins: bit p of pin_v is V of pin p, bit p of pin_oe is OE (isa 2.4).
        self.pin_v = (1 << N_OUT) - 1
        self.pin_oe = 0
        self.slot = [None] * N_OUT  # event slot of each pin: (event cycle, V, OE) or None
        self.fires = {}  # event cycle -> list of pins

        # Inputs: chip pin values for this cycle, set by the test with set_inputs().
        self.ui_in = 0
        self.uio_in = 0
        self.sync1 = 0  # synchronizer stage 1: pin level of the last cycle
        self.sync2 = 0  # stage 2: s(c), pin level two cycles before (isa 6.1)
        self.s_prev = 0  # s(c - 1), for edge detection (isa 6.3)
        self.samples = {}  # sample cycle -> list of thread numbers

        self.fifo_in = [deque() for _ in range(N_THREADS)]  # host to thread
        self.fifo_out = [deque() for _ in range(N_THREADS)]  # thread to host

    # ----- interface for the test (host and chip pins) -----

    def set_inputs(self, ui_in=None, uio_in=None):
        """Sets the chip input pins for the next step()."""
        if ui_in is not None:
            self.ui_in = ui_in & 0xFF
        if uio_in is not None:
            self.uio_in = uio_in & 0xFF

    def pins(self):
        """(uo_out, uio_out, uio_oe) in this cycle. uo_out[7] is the host port: 0 here."""
        return (self.pin_v >> 8) & 0x7F, self.pin_v & 0xFF, self.pin_oe & 0xFF

    def fifo_push(self, n, word):
        """The host writes one word to thread n. Returns False if the FIFO is full."""
        if len(self.fifo_in[n]) >= FIFO_DEPTH:
            return False
        self.fifo_in[n].append(word & 0xFFFF)
        return True

    def fifo_pop(self, n):
        """The host reads one word from thread n. Returns None if the FIFO is empty."""
        return self.fifo_out[n].popleft() if self.fifo_out[n] else None

    # ----- one clock cycle -----

    def step(self):
        """Executes cycle self.cycle. Returns the pins of that cycle (see pins())."""
        c = self.cycle
        s = self.sync2
        level = self._input_level()
        pins = self.pins()

        th = self.threads[c % N_THREADS]
        if th.run:
            self._slot(th, c, s)

        # Clock edge at the end of cycle c: events with event cycle c occur (isa 4.4).
        for p in self.fires.pop(c, ()):
            _, v, oe = self.slot[p]
            bit = 1 << p
            self.pin_v = (self.pin_v | bit) if v else (self.pin_v & ~bit)
            self.pin_oe = (self.pin_oe | bit) if oe else (self.pin_oe & ~bit)
            self.slot[p] = None
        # Samples with sample cycle c keep s(c) (isa 6.2).
        for n in self.samples.pop(c, ()):
            pend = self.threads[n].pend
            pend["bit"] = (s >> pend["pin"]) & 1
        # Edges at cycle c for the threads in WAIT (isa 6.3).
        edges = s ^ self.s_prev
        if edges:
            for t in self.threads:
                pend = t.pend
                if pend is None or pend["op"] != "wait" or pend["edge"] is not None:
                    continue
                if c <= pend["armed"] or (pend["until"] is not None and c > pend["until"]):
                    continue
                if not (edges >> pend["pin"]) & 1:
                    continue
                new = (s >> pend["pin"]) & 1
                if pend["kind"] == 2 or new == pend["kind"]:  # 0 FALL: new 0, 1 RISE: new 1
                    pend["edge"] = c

        self.s_prev = s
        self.sync2 = self.sync1
        self.sync1 = level
        self.cycle = c + 1
        return pins

    def run_cycles(self, cycles):
        """Executes some cycles with the inputs that do not change."""
        for _ in range(cycles):
            self.step()

    # ----- internal -----

    def _input_level(self):
        """Pin level of the input pins in this cycle (isa 5.1): uio pins with OE = 1 read V."""
        oe = self.pin_oe & 0xFF
        uio = ((self.uio_in & ~oe) | (self.pin_v & oe)) & 0xFF
        return uio | ((self.ui_in & 0x1F) << 8)

    def _read(self, addr, c, what):
        """Memory read at cycle c, with the store rule of isa 3.3."""
        cs = self.store_cycle.get(addr)
        if cs is not None and cs < c < cs + 4:
            raise ModelError(f"cycle {c}: {what} of address {addr} only {c - cs} cycles after a store (isa 3.3)")
        return self.mem[addr]

    def _store(self, addr, word, c):
        self.mem[addr] = word & 0xFFFF
        self.store_cycle[addr] = c

    def _event_cycle(self, th, c, t):
        """Cycle of a pin event with time t that executes at cycle c (isa 4.2, 4.3)."""
        d = (t - c) & 0xFFFF
        if LEAD <= d < HORIZON:
            return c + d
        th.status |= STATUS_LATE
        return c + LEAD

    def _illegal(self, th, word):
        th.run = False
        th.status |= STATUS_ERR
        th.stop_reason = f"illegal instruction 0x{word:04X} at address {th.pc}"

    def _set_zn(self, th, value):
        th.z = int(value == 0)
        th.n = (value >> 15) & 1

    def _slot(self, th, c, s):
        """The slot of thread th at cycle c (isa 3.1, 3.2)."""
        if th.pend is not None:
            if self._finish(th, c):
                th.pend = None
                th.pc = (th.pc + 1) & 0x7F
            return
        word = self._read(th.pc, c, "instruction fetch")
        nxt = self._execute(th, word, c, s)
        if nxt is not None:
            th.pc = nxt & 0x7F

    def _finish(self, th, c):
        """Completes a blocking operation if its time is past. Returns True when complete."""
        pend = th.pend
        op = pend["op"]
        if op == "sample":
            if c <= pend["at"]:
                return False
            bit = pend["bit"]
            r = th.r[pend["reg"]]
            if pend["left"]:
                r = ((r << 1) | bit) & 0xFFFF
            else:
                r = (r >> 1) | (bit << 15)
            th.r[pend["reg"]] = r
            th.e = bit
            return True
        if op == "wait":
            if pend["edge"] is not None and c > pend["edge"]:
                th.r[pend["reg"]] = pend["edge"] & 0xFFFF
                if pend["ct"]:
                    th.t = pend["edge"] & 0xFFFF
                th.e = 1
                return True
            if pend["until"] is not None and c > pend["until"]:
                th.e = 0
                return True
            return False
        if op == "twait":
            return c > pend["at"]
        if op == "ld":
            th.r[pend["reg"]] = self._read(pend["addr"], c, "LD")
            return True
        raise AssertionError(op)

    def _execute(self, th, w, c, s):
        """Executes word w. Returns the next PC, or None if the instruction is not complete."""
        op = w >> 12
        nxt = th.pc + 1
        rd = (w >> 10) & 3

        if op == 0x0:  # system (isa 11.2)
            sub = (w >> 8) & 0xF
            ra = (w >> 6) & 3
            rb = (w >> 4) & 3
            if sub == 0:  # HALT
                th.run = False
                th.stop_reason = f"HALT at address {th.pc}"
                return th.pc
            if sub == 1:  # NOP
                return nxt
            if sub == 2:  # JR
                return th.r[ra]
            if sub == 3:  # LDX rd, [ra]: 2 slots (isa 3.3)
                th.pend = {"op": "ld", "reg": ra, "addr": th.r[rb] & 0x7F}
                return None
            if sub == 4:  # STX rs, [ra]
                self._store(th.r[rb] & 0x7F, th.r[ra], c)
                return nxt
            self._illegal(th, w)
            return None

        if op == 0x1:  # LDI
            th.r[rd] = w & 0x3FF
            return nxt

        if op == 0x2:  # ADDI, immediate sign-extended to 16 bits
            imm = w & 0x3FF
            if imm & 0x200:
                imm -= 0x400
            total = th.r[rd] + (imm & 0xFFFF)
            th.c = total >> 16
            th.r[rd] = total & 0xFFFF
            self._set_zn(th, th.r[rd])
            return nxt

        if op == 0x3:  # ALU register
            rs = (w >> 8) & 3
            fn = (w >> 4) & 0xF
            a = th.r[rd]
            b = th.r[rs]
            if fn == 0:
                res = b
            elif fn == 1:
                total = a + b
                th.c = total >> 16
                res = total & 0xFFFF
            elif fn in (2, 6):
                res = (a - b) & 0xFFFF
                th.c = int(a >= b)
            elif fn in (3, 7):
                res = a & b
            elif fn == 4:
                res = a | b
            elif fn == 5:
                res = a ^ b
            elif fn == 8:
                res = ~b & 0xFFFF
            else:
                self._illegal(th, w)
                return None
            self._set_zn(th, res)
            if fn not in (6, 7):
                th.r[rd] = res
            return nxt

        if op == 0x4:  # ALU immediate
            fn = (w >> 8) & 3
            imm = w & 0xFF
            a = th.r[rd]
            if fn == 0:
                res = a & imm
            elif fn == 1:
                res = a | imm
            elif fn == 2:
                res = a ^ imm
            else:
                res = (a - imm) & 0xFFFF
                th.c = int(a >= imm)
            self._set_zn(th, res)
            if fn != 3:
                th.r[rd] = res
            return nxt

        if op == 0x5:  # shift
            fn = (w >> 8) & 3
            n = (w >> 4) & 0xF
            a = th.r[rd]
            if n == 0:
                res = a
            elif fn == 0:
                res = (a << n) & 0xFFFF
                th.c = (a >> (16 - n)) & 1
            elif fn == 1:
                res = a >> n
                th.c = (a >> (n - 1)) & 1
            elif fn == 2:
                res = (_signed16(a) >> n) & 0xFFFF
                th.c = (a >> (n - 1)) & 1
            else:
                res = ((a >> n) | (a << (16 - n))) & 0xFFFF
                th.c = (a >> (n - 1)) & 1
            th.r[rd] = res
            self._set_zn(th, res)
            return nxt

        if op == 0x6:  # TADD imm12
            th.t = (th.t + (w & 0xFFF)) & 0xFFFF
            return nxt

        if op == 0x7:  # time (isa 7)
            fn = (w >> 9) & 7
            r = (w >> 7) & 3
            imm = w & 0x7F
            now = c & 0xFFFF
            if fn == 0:
                th.t = (th.t + th.r[r]) & 0xFFFF
            elif fn == 1:
                th.t = th.r[r]
            elif fn == 2:
                th.r[r] = th.t
            elif fn == 3:
                th.r[r] = now
            elif fn == 4:
                th.t = (now + imm) & 0xFFFF
            elif fn == 5:
                if _signed16(th.t - now) < imm:
                    th.t = (now + imm) & 0xFFFF
            elif fn == 6:
                d = (th.t - c) & 0xFFFF
                if d < HORIZON:
                    th.pend = {"op": "twait", "at": c + d}
                    return None
            else:
                self._illegal(th, w)
                return None
            return nxt

        if op == 0x8:  # Bcc (isa 9.2)
            cc = (w >> 9) & 7
            taken = (
                cc == 0
                or (cc == 1 and th.z)
                or (cc == 2 and not th.z)
                or (cc == 3 and th.c)
                or (cc == 4 and not th.c)
                or (cc == 5 and th.n)
                or (cc == 6 and th.e)
                or (cc == 7 and not th.e)
            )
            return (w & 0x7F) if taken else nxt

        if op == 0x9:  # DJNZ
            th.r[rd] = (th.r[rd] - 1) & 0xFFFF
            return (w & 0x7F) if th.r[rd] else nxt

        if op == 0xA:  # JAL
            th.r[rd] = nxt & 0x7F
            return w & 0x7F

        if op == 0xB:  # LD, ST
            r = (w >> 9) & 3
            addr = w & 0x7F
            if (w >> 11) & 1:
                self._store(addr, th.r[r], c)
                return nxt
            th.pend = {"op": "ld", "reg": r, "addr": addr}
            return None

        if op == 0xC:  # OUT, SAMPLE (isa 4, 6.2)
            return nxt if self._pin_event(th, w, c) else None

        if op == 0xD:  # WAIT (isa 6.3)
            kind = (w >> 4) & 3
            if kind == 3:
                self._illegal(th, w)
                return None
            until = None
            if (w >> 3) & 1:
                d = (th.t - c) & 0xFFFF
                until = c + d if 1 <= d < HORIZON else c
            th.pend = {
                "op": "wait",
                "pin": (w >> 8) & 0xF,
                "reg": (w >> 6) & 3,
                "kind": kind,
                "until": until,
                "ct": (w >> 2) & 1,
                "armed": c,
                "edge": None,
            }
            return None

        if op == 0xE:  # host FIFOs and I/O ports (isa 8.5, 10)
            fn = (w >> 10) & 3
            r = (w >> 8) & 3
            nb = (w >> 7) & 1
            port = w & 0x3F
            n = th.number
            if fn == 0:  # PULL
                if not self.fifo_in[n]:
                    if nb:
                        th.e = 0
                        return nxt
                    return None
                th.r[r] = self.fifo_in[n].popleft()
                self._set_zn(th, th.r[r])
                th.e = 1
                return nxt
            if fn == 1:  # PUSH
                if len(self.fifo_out[n]) >= FIFO_DEPTH:
                    if nb:
                        th.e = 0
                        return nxt
                    return None
                self.fifo_out[n].append(th.r[r])
                th.e = 1
                return nxt
            if fn == 2:  # IOR
                if port == PORT_STATUS:
                    th.r[r] = th.status
                elif port == PORT_FLAGS:
                    th.r[r] = th.flags()
                elif port == PORT_PINS:
                    th.r[r] = s
                elif port == PORT_THREAD:
                    th.r[r] = n
                else:
                    th.r[r] = 0
                return nxt
            value = th.r[r]  # IOW
            if port == PORT_STATUS:
                th.status &= ~value
            elif port == PORT_FLAGS:
                th.z = value & 1
                th.c = (value >> 1) & 1
                th.n = (value >> 2) & 1
                th.e = (value >> 3) & 1
            return nxt

        self._illegal(th, w)  # opcode 15 (isa 11.2)
        return None

    def _pin_event(self, th, w, c):
        """OUT and SAMPLE. Returns True if the instruction is complete."""
        pin = (w >> 8) & 0xF
        reg = (w >> 6) & 3
        kind = (w >> 3) & 7
        t = (th.t + (w & 7)) & 0xFFFF

        if kind >= 6:  # SAMPLE: arm the sample unit, complete later (isa 6.2)
            at = self._event_cycle(th, c, t)
            th.pend = {"op": "sample", "at": at, "pin": pin, "reg": reg, "left": kind == 7, "bit": 0}
            self.samples.setdefault(at, []).append(th.number)
            return False

        owned = pin < N_OUT and self.owner[pin] == th.number
        if owned and self.slot[pin] is not None:
            return False  # slot full: wait (isa 4.4)

        r = th.r[reg]
        if kind in (0, 1):
            bit = kind
        elif kind == 2:
            bit = None  # Z
        elif kind == 3:
            bit = r & 1
        elif kind == 4:
            bit = r & 1
            th.r[reg] = r >> 1
        else:
            bit = r >> 15
            th.r[reg] = (r << 1) & 0xFFFF

        if not owned:  # isa 5.2: no pin change, ERR, no wait
            th.status |= STATUS_ERR
            return True

        if bit is None or (bit == 1 and self.od[pin]):
            v, oe = 1, 0
        else:
            v, oe = bit, 1
        at = self._event_cycle(th, c, t)
        self.slot[pin] = (at, v, oe)
        self.fires.setdefault(at, []).append(pin)
        return True

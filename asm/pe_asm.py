# SPDX-License-Identifier: Apache-2.0
"""Assembler for the protocol emulator instruction set (docs/isa.md).

What it does:
  The assembler reads firmware text (fw/*.s) and makes the 16-bit words
  for the 128-word shared memory. It accepts labels, the directives .equ,
  .org and .word, and the instructions of docs/isa.md section 8. For each
  error it gives the file name and the line number. Use it as
  "python asm/pe_asm.py file.s [base address]" to get a listing.

Why:
  The firmware must be text that a reviewer can read and check. The
  encoding comes from docs/isa.md section 11. This file does not import the
  reference model (model/pe_model.py), so that an encoding fault in one of
  them does not hide the same fault in the other.

Syntax:
  label:  MNEMONIC operand, operand   ; comment
  .equ NAME, expression       a constant (pin names keep their kind)
  .org expression             the next word goes to this address
  .word expression            one data word
  An expression is numbers (123, 0x7B, 0b1111011) and names with + and -.
"""

import re
import sys

MEM_WORDS = 128

# Predefined names (isa 5.1, 10). Kind "io": OUT and SAMPLE/WAIT,
# "out": OUT only, "in": SAMPLE/WAIT only, "port": IOR/IOW.
PREDEFINED = {}
for _i in range(8):
    PREDEFINED[f"UIO{_i}"] = (_i, "io")
for _i in range(7):
    PREDEFINED[f"UO{_i}"] = (8 + _i, "out")
for _i in range(5):
    PREDEFINED[f"UI{_i}"] = (8 + _i, "in")
for _name, _port in (("STATUS", 0), ("FLAGS", 1), ("PINS", 2), ("THREAD", 3)):
    PREDEFINED[_name] = (_port, "port")

BRANCHES = {"JMP": 0, "BEQ": 1, "BNE": 2, "BCS": 3, "BCC": 4, "BMI": 5, "BES": 6, "BEC": 7}
ALU_R = {"MOV": 0, "ADD": 1, "SUB": 2, "AND": 3, "OR": 4, "XOR": 5, "CMP": 6, "TST": 7, "NOT": 8}
ALU_I = {"ANDI": 0, "ORI": 1, "XORI": 2, "CMPI": 3}
SHIFTS = {"SHL": 0, "SHR": 1, "ASR": 2, "ROR": 3}
EDGES = {"FALL": 0, "RISE": 1, "ANY": 2}
TIME_REG = {"TSET": 1, "TGET": 2, "NOW": 3}
TIME_IMM = {"TNOW": 4, "TSYNC": 5}

_TOKEN = re.compile(r"0[xX][0-9A-Fa-f]+|0[bB][01]+|\d+|[A-Za-z_]\w*|\S")
_REG = re.compile(r"[rR]([0-3])")
_REG_SHIFT = re.compile(r"[rR]([0-3])\s*(>>|<<)?")
_LABEL = re.compile(r"\s*([A-Za-z_]\w*)\s*:")


class AsmError(Exception):
    """One or more errors. Each text has the form "file:line: message"."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


class _LineError(Exception):
    """An error on one line. The assembler adds the file name and the line number."""


class Program:
    """Result: words[address] = 16-bit word, source[address] = (line number, text)."""

    def __init__(self, name):
        self.name = name
        self.words = {}
        self.source = {}
        self.symbols = {}

    @property
    def size(self):
        """Number of memory words that the program uses."""
        return len(self.words)

    def image(self):
        """All 128 memory words. Words that the program does not use are 0 (HALT)."""
        return [self.words.get(a, 0) for a in range(MEM_WORDS)]

    def listing(self):
        lines = []
        for addr in sorted(self.words):
            lineno, text = self.source[addr]
            lines.append(f"{addr:3d}  {self.words[addr]:04X}  {lineno:4d}  {text.rstrip()}")
        return "\n".join(lines)


class _Assembler:
    def __init__(self, name):
        self.name = name
        self.symbols = {}  # name -> (value, kind)
        self.errors = []

    # ----- values -----

    def lookup(self, name):
        if name in self.symbols:
            return self.symbols[name]
        if name.upper() in PREDEFINED:
            return PREDEFINED[name.upper()]
        raise _LineError(f"name '{name}' is not defined")

    def value(self, text):
        """Value of an expression, and the kind if it is one name only."""
        tokens = _TOKEN.findall(text)
        if not tokens:
            raise _LineError("value missing")
        if _REG.fullmatch(text.strip()):
            raise _LineError(f"register '{text.strip()}' is not permitted here, a value is necessary")
        total = 0
        sign = 1
        expect_term = True
        terms = 0
        kind = None
        for tok in tokens:
            if expect_term:
                if tok in "+-" and len(tok) == 1:
                    sign = -sign if tok == "-" else sign
                    continue
                if tok[0].isdigit():
                    try:
                        val = int(tok, 0)
                    except ValueError:
                        raise _LineError(f"'{tok}' is not a number") from None
                    kind = None
                elif tok[0].isalpha() or tok[0] == "_":
                    val, kind = self.lookup(tok)
                else:
                    raise _LineError(f"'{tok}' is not permitted in a value")
                if sign < 0:
                    kind = None
                total += sign * val
                sign = 1
                terms += 1
                expect_term = False
            else:
                if tok not in ("+", "-"):
                    raise _LineError(f"'{tok}' is not permitted in a value (use + or -)")
                sign = -1 if tok == "-" else 1
                expect_term = True
        if expect_term:
            raise _LineError(f"value '{text.strip()}' is not complete")
        return total, (kind if terms == 1 else None)

    def number(self, text, lo, hi, what):
        val, _ = self.value(text)
        if not lo <= val <= hi:
            raise _LineError(f"{what} {val} is out of range {lo} to {hi}")
        return val

    def reg(self, text):
        m = _REG.fullmatch(text.strip())
        if not m:
            raise _LineError(f"register r0 to r3 is necessary, not '{text.strip()}'")
        return int(m.group(1))

    def reg_shift(self, text, need_shift):
        m = _REG_SHIFT.fullmatch(text.strip())
        if not m or (need_shift and not m.group(2)):
            raise _LineError(f"'{text.strip()}' is not r0>> to r3>> or r0<< to r3<<" if need_shift
                             else f"'{text.strip()}' is not a register source")
        return int(m.group(1)), m.group(2)

    def pin(self, text, direction):
        val, kind = self.value(text)
        if direction == "out":
            if kind == "in":
                raise _LineError(f"'{text.strip()}' is an input pin; OUT needs an output pin (UIO0 to UIO7, UO0 to UO6)")
            if not 0 <= val <= 14:
                raise _LineError(f"output pin {val} is out of range 0 to 14")
        else:
            if kind == "out":
                raise _LineError(f"'{text.strip()}' is an output pin; SAMPLE and WAIT need an input pin (UIO0 to UIO7, UI0 to UI4)")
            if not 0 <= val <= 12:
                raise _LineError(f"input pin {val} is out of range 0 to 12")
        return val

    def addr(self, text):
        return self.number(text, 0, MEM_WORDS - 1, "address")

    def port(self, text):
        return self.number(text, 0, 63, "port")

    # ----- instructions (isa 11.2) -----

    def encode(self, mnem, ops):
        def need(count):
            if len(ops) != count:
                raise _LineError(f"{mnem} needs {count} operand(s), not {len(ops)}")

        if mnem in ("HALT", "NOP", "TWAIT"):
            need(0)
            return {"HALT": 0x0000, "NOP": 0x0100, "TWAIT": 0x7C00}[mnem]
        if mnem == "JR":
            need(1)
            return 0x0200 | self.reg(ops[0]) << 6
        if mnem in ("LDX", "STX"):
            need(2)
            m = re.fullmatch(r"\[\s*([rR][0-3])\s*\]", ops[1])
            if not m:
                raise _LineError(f"{mnem} needs [r0] to [r3] as the second operand, not '{ops[1]}'")
            base = 0x0300 if mnem == "LDX" else 0x0400
            return base | self.reg(ops[0]) << 6 | self.reg(m.group(1)) << 4
        if mnem == "LDI":
            need(2)
            return 0x1000 | self.reg(ops[0]) << 10 | self.number(ops[1], 0, 1023, "LDI value")
        if mnem == "ADDI":
            need(2)
            return 0x2000 | self.reg(ops[0]) << 10 | (self.number(ops[1], -512, 511, "ADDI value") & 0x3FF)
        if mnem in ALU_R:
            need(2)
            return 0x3000 | self.reg(ops[0]) << 10 | self.reg(ops[1]) << 8 | ALU_R[mnem] << 4
        if mnem in ALU_I:
            need(2)
            return 0x4000 | self.reg(ops[0]) << 10 | ALU_I[mnem] << 8 | self.number(ops[1], 0, 255, f"{mnem} value")
        if mnem in SHIFTS:
            need(2)
            return 0x5000 | self.reg(ops[0]) << 10 | SHIFTS[mnem] << 8 | self.number(ops[1], 0, 15, "shift") << 4
        if mnem == "TADD":
            need(1)
            if _REG.fullmatch(ops[0].strip()):
                return 0x7000 | self.reg(ops[0]) << 7
            return 0x6000 | self.number(ops[0], 0, 4095, "TADD value")
        if mnem in TIME_REG:
            need(1)
            return 0x7000 | TIME_REG[mnem] << 9 | self.reg(ops[0]) << 7
        if mnem in TIME_IMM:
            need(1)
            return 0x7000 | TIME_IMM[mnem] << 9 | self.number(ops[0], 0, 127, f"{mnem} value")
        if mnem in BRANCHES:
            need(1)
            return 0x8000 | BRANCHES[mnem] << 9 | self.addr(ops[0])
        if mnem in ("DJNZ", "JAL"):
            need(2)
            base = 0x9000 if mnem == "DJNZ" else 0xA000
            return base | self.reg(ops[0]) << 10 | self.addr(ops[1])
        if mnem in ("LD", "ST"):
            need(2)
            base = 0xB000 if mnem == "LD" else 0xB800
            return base | self.reg(ops[0]) << 9 | self.addr(ops[1])
        if mnem == "OUT":
            if len(ops) not in (2, 3):
                raise _LineError(f"OUT needs 2 or 3 operands, not {len(ops)}")
            pin = self.pin(ops[0], "out")
            src = ops[1].strip()
            reg = 0
            if src.upper() == "Z":
                kind = 2
            elif _REG_SHIFT.fullmatch(src):
                reg, sh = self.reg_shift(src, need_shift=False)
                kind = {None: 3, ">>": 4, "<<": 5}[sh]
            else:
                kind = self.number(src, 0, 1, "OUT value")
            off = self.number(ops[2], 0, 7, "offset") if len(ops) == 3 else 0
            return 0xC000 | pin << 8 | reg << 6 | kind << 3 | off
        if mnem == "SAMPLE":
            if len(ops) not in (2, 3):
                raise _LineError(f"SAMPLE needs 2 or 3 operands, not {len(ops)}")
            pin = self.pin(ops[0], "in")
            reg, sh = self.reg_shift(ops[1], need_shift=True)
            kind = 6 if sh == ">>" else 7
            off = self.number(ops[2], 0, 7, "offset") if len(ops) == 3 else 0
            return 0xC000 | pin << 8 | reg << 6 | kind << 3 | off
        if mnem == "WAIT":
            if not 3 <= len(ops) <= 5:
                raise _LineError(f"WAIT needs 3 to 5 operands, not {len(ops)}")
            pin = self.pin(ops[0], "in")
            edge = ops[1].strip().upper()
            if edge not in EDGES:
                raise _LineError(f"edge must be FALL, RISE or ANY, not '{ops[1].strip()}'")
            reg = self.reg(ops[2])
            opts = [o.strip().upper() for o in ops[3:]]
            for o in opts:
                if o not in ("TO", "CT"):
                    raise _LineError(f"WAIT option must be TO or CT, not '{o}'")
            if len(set(opts)) != len(opts):
                raise _LineError("WAIT option is given two times")
            return 0xD000 | pin << 8 | reg << 6 | EDGES[edge] << 4 | ("TO" in opts) << 3 | ("CT" in opts) << 2
        if mnem in ("PULL", "PULL.NB", "PUSH", "PUSH.NB"):
            need(1)
            fn = 0 if mnem.startswith("PULL") else 1
            return 0xE000 | fn << 10 | self.reg(ops[0]) << 8 | mnem.endswith(".NB") << 7
        if mnem in ("IOR", "IOW"):
            need(2)
            fn = 2 if mnem == "IOR" else 3
            return 0xE000 | fn << 10 | self.reg(ops[0]) << 8 | self.port(ops[1])
        raise _LineError(f"unknown instruction '{mnem}'")

    # ----- two passes -----

    def parse(self, text):
        """Returns a list of (line number, labels, mnemonic, operands, text)."""
        items = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            code = line.split(";", 1)[0]
            labels = []
            while True:
                m = _LABEL.match(code)
                if not m:
                    break
                labels.append(m.group(1))
                code = code[m.end():]
            code = code.strip()
            if not code and not labels:
                continue
            mnem, ops = None, []
            if code:
                parts = code.split(None, 1)
                mnem = parts[0].upper()
                if len(parts) > 1:
                    ops = [o.strip() for o in parts[1].split(",")]
                    if any(o == "" for o in ops):
                        self.errors.append(f"{self.name}:{lineno}: operand missing")
                        continue
            items.append((lineno, labels, mnem, ops, line))
        return items

    def define(self, name, value, kind, lineno):
        if name in self.symbols or name.upper() in PREDEFINED:
            raise _LineError(f"name '{name}' is already defined")
        if _REG.fullmatch(name):
            raise _LineError(f"'{name}' is a register name")
        self.symbols[name] = (value, kind)

    def run(self, text, base):
        prog = Program(self.name)
        items = self.parse(text)
        # Pass 1: addresses of the labels, values of .equ.
        addr = base
        placed = []  # (address, item)
        for item in items:
            lineno, labels, mnem, ops, line = item
            try:
                for label in labels:
                    self.define(label, addr, "label", lineno)
                if mnem is None:
                    continue
                if mnem == ".EQU":
                    if len(ops) != 2:
                        raise _LineError(".equ needs a name and a value")
                    if not re.fullmatch(r"[A-Za-z_]\w*", ops[0]):
                        raise _LineError(f"'{ops[0]}' is not a name")
                    val, kind = self.value(ops[1])
                    self.define(ops[0], val, kind, lineno)
                elif mnem == ".ORG":
                    if len(ops) != 1:
                        raise _LineError(".org needs one value")
                    addr = self.number(ops[0], 0, MEM_WORDS - 1, ".org address")
                else:
                    if addr >= MEM_WORDS:
                        raise _LineError(f"the program is longer than the memory ({MEM_WORDS} words)")
                    placed.append((addr, item))
                    addr += 1
            except _LineError as err:
                self.errors.append(f"{self.name}:{lineno}: {err}")
        # Pass 2: words.
        for addr, (lineno, _, mnem, ops, line) in placed:
            try:
                if addr in prog.words:
                    raise _LineError(f"address {addr} is already used by line {prog.source[addr][0]}")
                if mnem == ".WORD":
                    if len(ops) != 1:
                        raise _LineError(".word needs one value")
                    word = self.number(ops[0], -0x8000, 0xFFFF, ".word value") & 0xFFFF
                else:
                    word = self.encode(mnem, ops)
                prog.words[addr] = word
                prog.source[addr] = (lineno, line)
            except _LineError as err:
                self.errors.append(f"{self.name}:{lineno}: {err}")
        if self.errors:
            raise AsmError(self.errors)
        prog.symbols = dict(self.symbols)
        return prog


def assemble(text, name="<text>", base=0):
    """Assembles text. The first word goes to address base. Raises AsmError."""
    return _Assembler(name).run(text, base)


def assemble_file(path, base=0):
    with open(path, encoding="utf-8") as f:
        return assemble(f.read(), name=path, base=base)


def main(argv):
    if len(argv) not in (2, 3):
        print("use: python pe_asm.py file.s [base address]")
        return 2
    try:
        prog = assemble_file(argv[1], int(argv[2], 0) if len(argv) == 3 else 0)
    except AsmError as err:
        print(err)
        return 1
    print(prog.listing())
    print(f"{prog.size} words")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# SPDX-License-Identifier: Apache-2.0
"""Firmware timing certificates: a static bound on the lead of every pin event of a program (docs/isa.md 3, 4, 6, 7).

What it does:
  For each program (fw/*.s, assembled with its default .equ values or the ones given) and each thread entry, an
  abstract interpretation over the control-flow graph bounds, at every OUT and SAMPLE, the lead d = (T + offset) - c
  of the event (c: the cycle in which the instruction completes, isa 4.2). A program is certified when every such
  lead is within 4 to 32,767 cycles on every path, for every input and every host timing: then the thread never sets
  LATE (isa 4.3). The result gives the smallest and largest lead of each event and the verdict of the program.

The abstract state at an instruction (its first slot at cycle c):
  L        interval of T - c;
  D[pin]   interval of t - T for the event that may still be in the event slot of an output pin (t: its event time);
           a slot holds the event while t >= c (isa 4.4), so it may be full when D + L >= 0;
  r0..r3   ('v', interval) a value, ('n', interval) a time as value - c, ('a', set) code addresses (JAL), or None.
Rules (each instruction of a thread takes one slot every 4 cycles, isa 3.1):
  an instruction that completes in its slot: the next one starts 4 cycles later; LD, LDX, ST, STX: 8 (isa 3.3);
  OUT: if its slot is free, the lead is off + L; if the slot holds an event (t >= c), OUT waits and completes in the
    first slot after t, so the lead is off - D - (1 to 4) (isa 3.2, 4.4); then D[pin] = off;
  SAMPLE: the lead is off + L; it completes in the first slot after its time: T - c becomes -off - (1 to 4);
  WAIT: with TO the timeout is T (when 1 <= T - c <= 32,767) or at once; it completes in the first slot after the edge
    or the timeout; with CT, T becomes the edge time, so T - c is -4 to -1 at completion; without TO the wait has no
    bound (T - c has no lower bound unless CT gives T); TWAIT: completes in the first slot after T, or at once if T is
    past (isa 6.3, 7);
  PULL, PUSH (blocking forms): no bound on the wait (isa 3.2); the .NB forms complete in their slot;
  TADD, TSET, TGET, NOW, TNOW, TSYNC: isa 7 (TSET with a register that holds a time; TADD with a register value);
  branches: both ways (flags are not tracked); JAL/JR through the code addresses of the link register.
At a join the intervals are joined; after 20 visits of an address an interval that still grows becomes unbounded
(widening), so the analysis ends. Host input words have no bound unless an assumption of the program bounds them
(ASSUME: an interval for the register that a PULL at a label loads, from the firmware header: a host rule).

What it does not cover: the pin terms (clock-to-pin and pad delay) are added by cert/pinwin.py; the flags are not
tracked, so a path that cannot happen is still analysed (the result is sound but may reject a correct program).
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402

INF = 10 ** 9
LEAD_MIN, LEAD_MAX = 4, 32767
MASK = 0x1FF


class Iv(tuple):
    """Closed interval [lo, hi] of integers; INF stands for no bound."""

    def __new__(cls, lo, hi=None):
        hi = lo if hi is None else hi
        return tuple.__new__(cls, (max(-INF, min(INF, lo)), max(-INF, min(INF, hi))))

    lo = property(lambda s: s[0])
    hi = property(lambda s: s[1])

    def __add__(self, o):
        o = o if isinstance(o, Iv) else Iv(o)
        return Iv(self.lo + o.lo if -INF < self.lo and -INF < o.lo else -INF,
                  self.hi + o.hi if self.hi < INF and o.hi < INF else INF)

    def __neg__(self):
        return Iv(-self.hi, -self.lo)

    def __sub__(self, o):
        return self + (-(o if isinstance(o, Iv) else Iv(o)))

    def join(self, o):
        return Iv(min(self.lo, o.lo), max(self.hi, o.hi))

    def widen(self, new):
        return Iv(self.lo if new.lo >= self.lo else -INF, self.hi if new.hi <= self.hi else INF)

    def clip(self, lo, hi):
        """The part within [lo, hi], or None."""
        a, b = max(self.lo, lo), min(self.hi, hi)
        return Iv(a, b) if a <= b else None


TOP = Iv(-INF, INF)


class State:
    __slots__ = ("L", "D", "r")

    def __init__(self, L, D=None, r=None):
        self.L, self.D, self.r = L, dict(D or {}), list(r) if r is not None else [("v", Iv(0))] * 4

    def copy(self):
        return State(self.L, self.D, self.r)

    def key(self):
        return (self.L, tuple(sorted(self.D.items())), tuple(self.r))

    def advance(self, x):
        """The next instruction starts x cycles later (x an interval of cycles >= 0)."""
        x = x if isinstance(x, Iv) else Iv(x)
        self.L = self.L - x
        self.r = [("n", v[1] - x) if v and v[0] == "n" else v for v in self.r]
        for p in list(self.D):
            if (self.D[p] + self.L).hi < 0:  # the event time is before c: it occurred, the slot is free
                del self.D[p]

    def move_t(self, new_L):
        """T changes; T - c becomes new_L (same c); every D follows (t - T_new = D + L_old - L_new)."""
        old = self.L
        self.D = {p: d + old - new_L for p, d in self.D.items()}
        self.L = new_L


def join_reg(a, b):
    if a == b:
        return a
    if a and b and a[0] == b[0]:
        return (a[0], a[1] | b[1]) if a[0] == "a" else (a[0], a[1].join(b[1]))
    return None


def join_state(a, b, widen=False):
    L = a.L.widen(a.L.join(b.L)) if widen else a.L.join(b.L)
    D = dict(a.D)
    for p, d in b.D.items():
        j = D[p].join(d) if p in D else d
        D[p] = D[p].widen(j) if (widen and p in D) else j
    r = []
    for x, y in zip(a.r, b.r):
        j = join_reg(x, y)
        if widen and j and x and j[0] in ("v", "n") and x[0] == j[0]:
            j = (j[0], x[1].widen(j[1]))
        r.append(j)
    return State(L, D, r)


def decode(w):
    """(form, fields) of an instruction word (docs/isa.md 11.2)."""
    op = w >> 12
    if op == 0x0:
        sub = (w >> 8) & 0xF
        return ({0: "HALT", 1: "NOP", 2: "JR", 3: "LDX", 4: "STX"}.get(sub, "ILLEGAL"), {"ra": (w >> 6) & 3})
    if op == 0x1:
        return "LDI", {"rd": (w >> 10) & 3, "imm": w & 0x3FF}
    if op == 0x2:
        imm = w & 0x3FF
        return "ADDI", {"rd": (w >> 10) & 3, "imm": imm - 0x400 if imm & 0x200 else imm}
    if op == 0x3:
        fn = (w >> 4) & 0xF
        name = ("MOV", "ADD", "SUB", "AND", "OR", "XOR", "CMP", "TST", "NOT")[fn] if fn <= 8 else "ILLEGAL"
        return name, {"rd": (w >> 10) & 3, "rs": (w >> 8) & 3}
    if op == 0x4:
        return ("ANDI", "ORI", "XORI", "CMPI")[(w >> 8) & 3], {"rd": (w >> 10) & 3}
    if op == 0x5:
        return "SHIFT", {"rd": (w >> 10) & 3}
    if op == 0x6:
        return "TADDI", {"imm": w & 0xFFF}
    if op == 0x7:
        fn = (w >> 9) & 7
        name = ("TADDR", "TSET", "TGET", "NOW", "TNOW", "TSYNC", "TWAIT", "ILLEGAL")[fn]
        return name, {"r": (w >> 7) & 3, "imm": w & 0x7F}
    if op == 0x8:
        return ("JMP" if (w >> 9) & 7 == 0 else "BCC"), {"target": w & MASK}
    if op == 0x9:
        return "DJNZ", {"rd": (w >> 10) & 3, "target": w & MASK}
    if op == 0xA:
        return "JAL", {"rd": (w >> 10) & 3, "target": w & MASK}
    if op == 0xB:
        return ("ST" if (w >> 11) & 1 else "LD"), {"r": (w >> 9) & 3}
    if op == 0xC:
        kind = (w >> 3) & 7
        return ("SAMPLE" if kind >= 6 else "OUT"), {"pin": (w >> 8) & 0xF, "reg": (w >> 6) & 3, "kind": kind,
                                                    "off": w & 7}
    if op == 0xD:
        kind = (w >> 4) & 3
        return ("WAIT" if kind < 3 else "ILLEGAL"), {"rd": (w >> 6) & 3, "to": (w >> 3) & 1, "ct": (w >> 2) & 1}
    if op == 0xE:
        fn, nb = (w >> 10) & 3, (w >> 7) & 1
        return ("PULL", "PUSH", "IOR", "IOW")[fn] + (".NB" if nb and fn < 2 else ""), {"r": (w >> 8) & 3}
    return "ILLEGAL", {}


def reg_add(a, b, sign=1):
    """Register arithmetic on the abstract values (a + sign * b)."""
    if a is None or b is None:
        return None
    if a[0] == "v" and b[0] == "v":
        return ("v", a[1] + b[1] if sign > 0 else a[1] - b[1])
    if a[0] == "n" and b[0] == "v":
        return ("n", a[1] + b[1] if sign > 0 else a[1] - b[1])
    if sign < 0 and a[0] == "n" and b[0] == "n":
        return ("v", a[1] - b[1])
    return None


class Result:
    def __init__(self):
        self.events = {}     # address -> lead interval (joined over all visits)
        self.problems = []   # text


def analyse(prog, entries, assume=None, bugs=(), max_steps=200000):
    """Abstract interpretation of prog from each entry (address, thread). Returns Result."""
    assume = assume or {}
    res = Result()
    for entry, thread in entries:
        start = State(Iv(-thread))  # T = 0 at model cycle 0; thread n has its first slot at cycle n (isa 3.1)
        states, visits, work = {entry: start}, {}, [entry]
        steps = 0
        while work:
            steps += 1
            if steps > max_steps:
                res.problems.append("entry %d: no fixpoint after %d steps" % (entry, max_steps))
                break
            pc = work.pop()
            st = states[pc]
            if pc not in prog.words:
                res.problems.append("entry %d: control reaches address %d, which holds no word" % (entry, pc))
                continue
            for nxt, ns in step(prog, pc, st, res, assume, bugs):
                nxt &= MASK
                visits[nxt] = visits.get(nxt, 0) + 1
                if nxt in states:
                    j = join_state(states[nxt], ns, widen=visits[nxt] > 20)
                    if j.key() == states[nxt].key():
                        continue
                    states[nxt] = j
                else:
                    states[nxt] = ns
                if nxt not in work:
                    work.append(nxt)
    return res


def step(prog, pc, st, res, assume, bugs):
    """Successors (address, state at its first slot) of the instruction at pc with state st."""
    form, f = decode(prog.words[pc])
    s = st.copy()
    nxt = pc + 1
    slot = 4

    def cont(state, x=slot, to=None):
        state.advance(x)
        return [((nxt if to is None else to), state)]

    if form in ("HALT", "ILLEGAL"):
        if form == "ILLEGAL":
            res.problems.append("address %d: illegal instruction" % pc)
        return []
    if form in ("LDX", "STX", "LD", "ST"):
        if form in ("LDX", "LD"):
            s.r[f.get("ra", f.get("r", 0))] = None
        return cont(s, 4 if "two_slot_ld" in bugs else 8)
    if form == "JR":
        v = s.r[f["ra"]]
        if not v or v[0] != "a":
            res.problems.append("address %d: JR through a register with no known code address" % pc)
            return []
        out = []
        for a in sorted(v[1]):
            s2 = s.copy()
            out += cont(s2, to=a)
        return out
    if form == "JAL":
        s.r[f["rd"]] = ("a", frozenset([nxt & MASK]))
        return cont(s, to=f["target"])
    if form == "JMP":
        return cont(s, to=f["target"])
    if form == "BCC":
        return cont(s.copy()) + cont(s, to=f["target"])
    if form == "DJNZ":
        rd = f["rd"]
        s.r[rd] = reg_add(s.r[rd], ("v", Iv(1)), -1)
        return cont(s.copy()) + cont(s, to=f["target"])
    if form == "LDI":
        s.r[f["rd"]] = ("v", Iv(f["imm"]))
        return cont(s)
    if form == "ADDI":
        s.r[f["rd"]] = reg_add(s.r[f["rd"]], ("v", Iv(f["imm"])))
        return cont(s)
    if form in ("MOV", "ADD", "SUB"):
        rd, rs = f["rd"], f["rs"]
        s.r[rd] = s.r[rs] if form == "MOV" else reg_add(s.r[rd], s.r[rs], 1 if form == "ADD" else -1)
        return cont(s)
    if form in ("CMP", "TST", "CMPI"):
        return cont(s)
    if form in ("AND", "OR", "XOR", "NOT", "ANDI", "ORI", "XORI", "SHIFT"):
        s.r[f["rd"]] = None
        return cont(s)
    if form == "NOP":
        return cont(s)
    if form == "TADDI":
        s.L = s.L + (0 if "tadd_ignored" in bugs else f["imm"])
        s.D = {p: d - f["imm"] for p, d in s.D.items()}
        return cont(s)
    if form == "TADDR":
        v = s.r[f["r"]]
        k = v[1] if v and v[0] == "v" else TOP
        s.L = s.L + k
        s.D = {p: d - k for p, d in s.D.items()}
        return cont(s)
    if form == "TSET":
        v = s.r[f["r"]]
        s.move_t(v[1] if v and v[0] == "n" else TOP)
        return cont(s)
    if form == "TGET":
        s.r[f["r"]] = ("n", s.L)
        return cont(s)
    if form == "NOW":
        s.r[f["r"]] = ("n", Iv(0))
        return cont(s)
    if form == "TNOW":
        s.move_t(Iv(f["imm"]))
        return cont(s)
    if form == "TSYNC":
        imm = f["imm"]
        if "tsync_as_tnow" in bugs:
            s.move_t(Iv(imm))
            return cont(s)
        keep, low = s.L.clip(imm, INF), s.L.clip(-INF, imm - 1)
        parts = []
        if keep:
            a = s.copy()
            a.L = keep
            parts.append(a)
        if low:
            b = s.copy()
            b.L = low
            b.move_t(Iv(imm))
            # D followed T for each L of the low part: t - T_new = D + L - imm (L within the low part)
            b.D = {p: d + low - imm for p, d in s.D.items()}
            parts.append(b)
        out = parts[0]
        for p in parts[1:]:
            out = join_state(out, p)
        return cont(out)
    if form == "TWAIT":
        parts = []
        fut, past = s.L.clip(0, INF), s.L.clip(-INF, -1)
        if fut:
            a = s.copy()
            a.L = fut
            a.advance(Iv(fut.lo + 1, fut.hi + 4))   # completes in the first slot after T
            a.L = Iv(-4, -1)
            parts.append(a)
        if past:
            b = s.copy()
            b.L = past
            parts.append(b)
        out = parts[0]
        for p in parts[1:]:
            out = join_state(out, p)
        return cont(out)
    if form == "OUT":
        pin, off = f["pin"], f["off"]
        if f["kind"] in (4, 5):
            s.r[f["reg"]] = None
        d = s.D.get(pin)
        full = d is not None and (d + s.L).hi >= 0 and "slot_wait_ignored" not in bugs
        empty = d is None or (d + s.L).lo < 0 or "slot_wait_ignored" in bugs
        leads, done = [], []
        if empty:
            le = s.L if d is None or "slot_wait_ignored" in bugs else (s.L.clip(-INF, -d.lo - 1) or s.L)
            leads.append(le + off)
            done.append(le)
        if full:
            df = d.clip(-s.L.hi, INF) or d   # a full slot holds an event with t >= c, so D >= -L
            leads.append(Iv(off - df.hi - 4, off - df.lo - 1))
            done.append(Iv(-df.hi - 4, -df.lo - 1))
        lead = leads[0] if len(leads) == 1 else leads[0].join(leads[1])
        L_done = done[0] if len(done) == 1 else done[0].join(done[1])
        record(res, pc, lead, bugs)
        s.advance(s.L - L_done)          # the cycles OUT waited (0 if the slot was free)
        s.L = L_done
        s.D[pin] = Iv(off)
        return cont(s)
    if form == "SAMPLE":
        off = f["off"]
        lead = s.L + off
        record(res, pc, lead, bugs)
        s.advance(Iv(max(0, lead.lo + 1), lead.hi + 4) if lead.lo > -INF else Iv(0, INF))
        if "sample_no_wait" in bugs:
            s.L = s.L
        s.r[f["reg"]] = None
        return cont(s)
    if form == "WAIT":
        rd = f["rd"]
        L0 = s.L
        if f["to"]:
            in_win = L0.clip(1, LEAD_MAX)
            outs = []
            if in_win:          # timeout at T: completes in (c0, T + 4]
                a = s.copy()
                a.L = in_win
                a.advance(Iv(1, in_win.hi + 4))
                if f["ct"]:
                    a.move_t(Iv(-4, -1))
                a.r[rd] = join_reg(a.r[rd], ("n", Iv(-4, -1)))
                outs.append(a)
            rest = [x for x in (L0.clip(-INF, 0), L0.clip(LEAD_MAX + 1, INF)) if x]
            for part in rest:  # timeout at once: completes in the next slot
                b = s.copy()
                b.L = part
                b.advance(Iv(1, 4))
                outs.append(b)
            out = outs[0]
            for o in outs[1:]:
                out = join_state(out, o)
            return cont(out)
        s.advance(Iv(1, INF))   # no timeout: no bound on the wait
        if f["ct"]:
            s.move_t(Iv(-4, -1))
        s.r[rd] = ("n", Iv(-4, -1))
        return cont(s)
    if form in ("PULL", "PUSH"):
        if form == "PULL":
            s.r[f["r"]] = assume.get(pc)
        s.advance(Iv(0, INF))
        return cont(s)
    if form in ("PULL.NB", "PUSH.NB", "IOR", "IOW"):
        if form in ("PULL.NB", "IOR"):
            s.r[f["r"]] = assume.get(pc)
        return cont(s)
    res.problems.append("address %d: form %s not handled" % (pc, form))
    return []


def record(res, pc, lead, bugs):
    res.events[pc] = res.events[pc].join(lead) if pc in res.events else lead


def lead_min(bugs):
    return 3 if "lead_min_3" in bugs else LEAD_MIN


# ----- programs -----

# Thread entries (address label or 0) and host assumptions for each program. An assumption bounds the register that
# a PULL (or PULL.NB) at a label loads: a host rule stated in the firmware header. Programs not listed: entry 0, thread
# 0, no assumption.
PROGRAMS = {
    "can.s": {"entries": [(0, 0), ("rx", 1)]},
}


def load(name, equ=None):
    with open(os.path.join(ROOT, "fw", name), encoding="utf-8") as fh:
        text = fh.read()
    for key, val in (equ or {}).items():
        text, n = re.subn(r"^(\s*\.equ\s+" + key + r"\s*,)[^;\n]*", lambda m: "%s %s " % (m.group(1), val), text,
                          flags=re.M)
        assert n == 1, "%s: .equ %s found %d times" % (name, key, n)
    return pe_asm.assemble(text, name)


def certify(name, cfg=None, bugs=()):
    cfg = cfg if cfg is not None else PROGRAMS.get(name, {})
    prog = load(name, cfg.get("equ"))
    sym = {k: v[0] for k, v in prog.symbols.items()}
    entries = [((sym[e] if isinstance(e, str) else e), t) for e, t in cfg.get("entries", [(0, 0)])]
    assume = {}
    for label, (lo, hi) in cfg.get("assume", {}).items():
        assume[sym[label] if isinstance(label, str) else label] = ("v", Iv(lo, hi))
    res = analyse(prog, entries, assume, bugs)
    lo_ok = lead_min(bugs)
    bad = {pc: lead for pc, lead in res.events.items() if lead.lo < lo_ok or lead.hi > LEAD_MAX}
    certified = not bad and not res.problems and bool(res.events)
    return {
        "program": name, "words": prog.size, "events": len(res.events), "certified": certified,
        "min_lead": min((l.lo for l in res.events.values()), default=None),
        "max_lead": max((l.hi for l in res.events.values()), default=None),
        "not_certified": {("%d: %s" % (pc, prog.source[pc][1].strip())): [lead.lo, lead.hi]
                          for pc, lead in sorted(bad.items())},
        "problems": res.problems,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("programs", nargs="*")
    ap.add_argument("--json")
    args = ap.parse_args()
    names = args.programs or sorted(f for f in os.listdir(os.path.join(ROOT, "fw")) if f.endswith(".s"))
    rows = [certify(n) for n in names]
    print("| Program | Words | Events | Certified (never LATE) | Smallest lead | Largest lead | Not certified |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        why = "; ".join(["%s lead %s..%s" % (k, *v) for k, v in list(r["not_certified"].items())[:3]] +
                        r["problems"][:2])
        print("| %s | %d | %d | %s | %s | %s | %s |" % (r["program"], r["words"], r["events"],
                                                       "yes" if r["certified"] else "no", r["min_lead"], r["max_lead"],
                                                       why.replace("|", "/")[:300]))
    n = sum(r["certified"] for r in rows)
    print("\n%d of %d programs certified" % (n, len(rows)))
    if args.json:
        json.dump(rows, open(args.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

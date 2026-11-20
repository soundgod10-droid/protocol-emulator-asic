# SPDX-License-Identifier: Apache-2.0
"""Functional coverage of the RTL tests (cocotb-coverage 2.0), sampled from the reference model in lockstep.

What it does:
  attach(model, label) wraps one PeModel instance (model/pe_model.py) that runs in lockstep with the chip: the RtlBench
  of test_rtl_fw.py (protocol firmware, test_rtl_sweep.py) and run_lockstep of test_core.py. In those benches the chip
  pins equal the model pins in every cycle (and in test_core also the slot-thread state), so a bin that the model hits
  there is a behaviour that the RTL showed in the same cycle. Model-only tests do not attach. Each group of bins below
  names the clause of docs/isa.md that it checks (column clause; "isa 4.2" = section 4.2). attach_jtag() adds the TAP
  states and transitions of the JTAG device models (IEEE 1149.1, TI SSYA002C Figure 3-3).

  A bin is sampled at its first hit only (the rest is a set lookup), so the cost per simulated cycle stays small; the
  report counts a bin as hit or not, and records the bench label of the first hit (fcov_first.json) for the trace table.
  export() writes fcov.yml (cocotb-coverage YAML export: bins and hits) and fcov_first.json next to this file.

Enabled with FCOV=1 and cocotb-coverage installed (.github/workflows/sweep.yaml); otherwise attach() does nothing.
test/fcov_report.py merges the files of all jobs, applies the zero-bin rule and writes the report and the trace table.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

FORMS = [
    ("HALT", "isa 11.2"), ("NOP", "isa 11.2"), ("JR", "isa 9"), ("LDX", "isa 3.3"), ("STX", "isa 3.3"),
    ("LDI", "isa 8"), ("ADDI", "isa 8, 9"), ("MOV", "isa 8, 9"), ("ADD", "isa 8, 9"), ("SUB", "isa 8, 9"),
    ("AND", "isa 8, 9"), ("OR", "isa 8, 9"), ("XOR", "isa 8, 9"), ("CMP", "isa 8, 9"), ("TST", "isa 8, 9"),
    ("NOT", "isa 8, 9"), ("ANDI", "isa 8, 9"), ("ORI", "isa 8, 9"), ("XORI", "isa 8, 9"), ("CMPI", "isa 8, 9"),
    ("SHL", "isa 8, 9"), ("SHR", "isa 8, 9"), ("ASR", "isa 8, 9"), ("ROR", "isa 8, 9"),
    ("TADD imm", "isa 7"), ("TADD r", "isa 7"), ("TSET", "isa 7"), ("TGET", "isa 7"), ("NOW", "isa 7"),
    ("TNOW", "isa 7"), ("TSYNC", "isa 7"), ("TWAIT", "isa 7, 3.2"),
    ("B", "isa 9.2"), ("BEQ", "isa 9.2"), ("BNE", "isa 9.2"), ("BCS", "isa 9.2"), ("BCC", "isa 9.2"),
    ("BMI", "isa 9.2"), ("BES", "isa 9.2"), ("BEC", "isa 9.2"), ("DJNZ", "isa 9"), ("JAL", "isa 9"),
    ("LD", "isa 3.3"), ("ST", "isa 3.3"),
    ("OUT 0", "isa 4, 4.5"), ("OUT 1", "isa 4, 4.5"), ("OUT Z", "isa 4, 4.5"), ("OUT r[0]", "isa 4, 4.5"),
    ("OUT r>>", "isa 4, 4.5"), ("OUT r<<", "isa 4, 4.5"), ("SAMPLE r>>", "isa 6.2"), ("SAMPLE r<<", "isa 6.2"),
    ("WAIT FALL", "isa 6.3"), ("WAIT RISE", "isa 6.3"), ("WAIT ANY", "isa 6.3"),
    ("PULL", "isa 3.2, 8.5"), ("PULL.NB", "isa 8.5"), ("PUSH", "isa 3.2, 8.5"), ("PUSH.NB", "isa 8.5"),
    ("IOR", "isa 10"), ("IOW", "isa 10"), ("ILLEGAL", "isa 12"),
]
FORM_NAMES = [f for f, _ in FORMS]
KINDS = ["OUT 0", "OUT 1", "OUT Z", "OUT r[0]", "OUT r>>", "OUT r<<", "SAMPLE r>>", "SAMPLE r<<"]
LEADS = ["late<4", "4", "5-4095", "4096-32767", "past>=32768"]
ALU = ["MOV", "ADD", "SUB", "AND", "OR", "XOR", "CMP", "TST", "NOT", "ADDI", "ANDI", "ORI", "XORI", "CMPI",
       "SHL", "SHR", "ASR", "ROR"]
CARRY = ["ADD", "SUB", "CMP", "ADDI", "CMPI", "SHL", "SHR", "ASR", "ROR"]
BRANCH = ["B", "BEQ", "BNE", "BCS", "BCC", "BMI", "BES", "BEC"]
FIFO = ["PULL", "PULL.NB", "PUSH", "PUSH.NB"]
PORTS = [0, 1, 2, 3] + list(range(16, 48))
ILLEGAL = ["op 0 sub > 4", "op 3 fn > 8", "op 7 fn 7", "op D kind 3", "op F"]
TAP = ["TLR", "RTI", "SELDR", "CAPDR", "SHDR", "EX1DR", "PADR", "EX2DR", "UPDR",
       "SELIR", "CAPIR", "SHIR", "EX1IR", "PAIR", "EX2IR", "UPIR"]

# group -> (bins, clause)
GROUPS = {
    "isa.form": (FORM_NAMES, "isa 11.2 (instruction encodings; per form: FORMS)"),
    "isa.form_thread": ([(f, n) for f in FORM_NAMES for n in range(4)], "isa 3.1 (each thread executes each form)"),
    "alu.result": ([(f, r) for f in ALU for r in ("zero", "negative", "positive")], "isa 9 (Z and N flags)"),
    "alu.carry": ([(f, c) for f in CARRY for c in (0, 1)], "isa 9 (C flag)"),
    "event.kind_lead": ([(k, d) for k in KINDS for d in LEADS], "isa 4.2, 4.3 (lead classes and late events)"),
    "event.kind_thread": ([(k, n) for k in KINDS for n in range(4)], "isa 3.1, 4.1 (each thread issues each event kind)"),
    "out.pin_kind": ([(p, k) for p in range(15) for k in KINDS[:6]], "isa 4.5, 5.1 (each output pin, each source)"),
    "wait.pin_edge": ([(p, e) for p in range(16) for e in ("FALL", "RISE", "ANY")], "isa 5.1, 6.3 (WAIT on each input)"),
    "sample.pin_dir": ([(p, d) for p in range(16) for d in (">>", "<<")], "isa 5.1, 6.2 (each input pin)"),
    "out.slot_full": (list(range(15)), "isa 4.4 (OUT waits for the event slot of its pin)"),
    "out.not_owner": (list(range(4)), "isa 5.2 (OUT to a pin the thread does not own: ERR)"),
    "late.thread": (list(range(4)), "isa 4.3 (LATE of each thread)"),
    "late.kind": (KINDS, "isa 4.3, 6.2 (late OUT and late SAMPLE)"),
    "wait.outcome": ([(k, "no TO", "edge") for k in ("FALL", "RISE", "ANY")] +
                     [(k, "TO", o) for k in ("FALL", "RISE", "ANY") for o in ("edge", "timeout")],
                     "isa 6.3 (edges and timeout)"),
    "branch.taken": ([("B", True)] + [(b, t) for b in BRANCH[1:] for t in (True, False)], "isa 9.2 (conditions)"),
    "loop.djnz": ([True, False], "isa 9 (DJNZ taken and not taken)"),
    "time.twait": (["waits", "at once"], "isa 7 (TWAIT before and after T)"),
    "time.tsync": (["moved", "kept"], "isa 7 (TSYNC moves T forward only)"),
    "fifo.op": ([(f, o, n) for f in FIFO for o in (("done", "blocked") if "NB" not in f else ("done", "E=0"))
                 for n in range(4)], "isa 3.2, 8.5 (blocking and non-blocking FIFO forms)"),
    "fifo.level": ([("in after PULL", k, n) for k in range(4) for n in range(4)] +
                   [("out after PUSH", k, n) for k in range(1, 5) for n in range(4)], "isa 2.2 (4-word FIFOs)"),
    "port.access": ([(f, p) for f in ("IOR", "IOW") for p in PORTS], "isa 10 (ports 0-3, helper ports 16-47)"),
    "illegal.class": (ILLEGAL, "isa 12 (illegal instructions: RUN 0, ERR)"),
    "pin.change": ([(p, e) for p in range(15) for e in ("rise", "fall", "oe on", "oe off")],
                   "isa 4.4, 4.5 (pin registers V and OE)"),
    "owner.pin_thread": ([(p, n) for p in range(15) for n in range(4)], "isa 5.2 (owner thread of each pin)"),
    "owner.open_drain": (list(range(15)), "isa 5.3 (open-drain mode of each pin)"),
    "jtag.state": (TAP, "IEEE 1149.1 TAP states (TI SSYA002C Figure 3-3)"),
    "jtag.transition": ([(s, t) for s in TAP for t in (0, 1)], "IEEE 1149.1 TAP transitions (Figure 3-3)"),
}
FORM_CLAUSE = dict(FORMS)

ENABLED = False
_seen, _first, _samplers, _label = set(), {}, {}, [""]
if os.environ.get("FCOV") == "1":
    try:
        from cocotb_coverage.coverage import CoverPoint, coverage_db
        for _g, (_bins, _clause) in GROUPS.items():
            _samplers[_g] = CoverPoint("top." + _g, xf=lambda v: v, bins=list(_bins))(lambda v: None)
        ENABLED = True
    except ImportError:
        pass


def hit(group, value):
    key = (group, value)
    if key in _seen:
        return
    _seen.add(key)
    _first[repr(key)] = _label[0]
    _samplers[group](value)


def form_of(w):
    op = w >> 12
    if op == 0x0:
        return {0: "HALT", 1: "NOP", 2: "JR", 3: "LDX", 4: "STX"}.get((w >> 8) & 0xF, "ILLEGAL")
    if op == 0x1:
        return "LDI"
    if op == 0x2:
        return "ADDI"
    if op == 0x3:
        fn = (w >> 4) & 0xF
        return ALU[fn] if fn <= 8 else "ILLEGAL"
    if op == 0x4:
        return ("ANDI", "ORI", "XORI", "CMPI")[(w >> 8) & 3]
    if op == 0x5:
        return ("SHL", "SHR", "ASR", "ROR")[(w >> 8) & 3]
    if op == 0x6:
        return "TADD imm"
    if op == 0x7:
        return ("TADD r", "TSET", "TGET", "NOW", "TNOW", "TSYNC", "TWAIT", "ILLEGAL")[(w >> 9) & 7]
    if op == 0x8:
        return BRANCH[(w >> 9) & 7]
    if op == 0x9:
        return "DJNZ"
    if op == 0xA:
        return "JAL"
    if op == 0xB:
        return "ST" if (w >> 11) & 1 else "LD"
    if op == 0xC:
        return KINDS[(w >> 3) & 7]
    if op == 0xD:
        k = (w >> 4) & 3
        return ("WAIT FALL", "WAIT RISE", "WAIT ANY")[k] if k < 3 else "ILLEGAL"
    if op == 0xE:
        fn, nb = (w >> 10) & 3, (w >> 7) & 1
        return ("PULL", "PUSH", "IOR", "IOW")[fn] + (".NB" if nb and fn < 2 else "")
    return "ILLEGAL"


def illegal_class(w):
    op = w >> 12
    return {0x0: "op 0 sub > 4", 0x3: "op 3 fn > 8", 0x7: "op 7 fn 7", 0xD: "op D kind 3"}.get(op, "op F")


def lead_class(d):
    if d < 4:
        return "late<4"
    if d == 4:
        return "4"
    if d <= 4095:
        return "5-4095"
    return "4096-32767" if d <= 32767 else "past>=32768"


def attach(model, label=""):
    """Wraps model (a PeModel in lockstep with the chip) so that its execution samples the bins."""
    if not ENABLED:
        return
    _label[0] = label
    for p in range(15):
        if model.owner[p] is not None:
            hit("owner.pin_thread", (p, model.owner[p]))
        if model.od[p]:
            hit("owner.open_drain", p)
    execute, finish, step = model._execute, model._finish, model.step

    def _execute(th, w, c, s):
        n, st0, t0, pc0 = th.number, th.status, th.t, th.pc
        r = execute(th, w, c, s)
        form = form_of(w)
        if r is None and th.pend is None and form != "ILLEGAL":  # blocked: the same instruction again next slot
            if w >> 12 == 0xC:
                hit("out.slot_full", (w >> 8) & 0xF)
            elif form in ("PULL", "PUSH"):
                hit("fifo.op", (form, "blocked", n))
            return r
        hit("isa.form", form)
        hit("isa.form_thread", (form, n))
        op = w >> 12
        if form in ALU:
            hit("alu.result", (form, "zero" if th.z else "negative" if th.n else "positive"))
            if form in CARRY:
                hit("alu.carry", (form, th.c))
        elif op == 0xC:
            pin, kind = (w >> 8) & 0xF, (w >> 3) & 7
            hit("event.kind_lead", (KINDS[kind], lead_class(((t0 + (w & 7)) - c) & 0xFFFF)))
            hit("event.kind_thread", (KINDS[kind], n))
            if kind >= 6:
                hit("sample.pin_dir", (pin, ">>" if kind == 6 else "<<"))
            elif pin < 15 and model.owner[pin] == n and pin not in model.helper_pins:
                hit("out.pin_kind", (pin, KINDS[kind]))
            else:
                hit("out.not_owner", n)
            if th.status & 1 and not st0 & 1:
                hit("late.kind", KINDS[kind])
        elif form in BRANCH and (w & 0x1FF) != ((pc0 + 1) & 0x1FF):
            hit("branch.taken", (form, (r & 0x1FF) != ((pc0 + 1) & 0x1FF)))
        elif form.startswith("WAIT"):
            hit("wait.pin_edge", ((w >> 8) & 0xF, form[5:]))
        elif form == "DJNZ":
            hit("loop.djnz", th.r[(w >> 10) & 3] != 0)
        elif form == "TWAIT":
            hit("time.twait", "waits" if th.pend is not None else "at once")
        elif form == "TSYNC":
            hit("time.tsync", "moved" if th.t != t0 else "kept")
        elif form in FIFO:
            hit("fifo.op", (form, "done" if th.e else "E=0", n))
            if th.e and form.startswith("PULL"):
                hit("fifo.level", ("in after PULL", len(model.fifo_in[n]), n))
            elif th.e:
                hit("fifo.level", ("out after PUSH", len(model.fifo_out[n]), n))
        elif form in ("IOR", "IOW") and (w & 0x3F) in PORTS:
            hit("port.access", (form, w & 0x3F))
        elif form == "ILLEGAL":
            hit("illegal.class", illegal_class(w))
        if th.status & 1 and not st0 & 1:
            hit("late.thread", n)
        return r

    def _finish(th, c):
        pend = th.pend
        done = finish(th, c)
        if done and pend["op"] == "wait":
            kind = ("FALL", "RISE", "ANY")[pend["kind"]]
            hit("wait.outcome", (kind, "TO" if pend["until"] is not None else "no TO", "edge" if th.e else "timeout"))
        return done

    def _step():
        v0, o0 = model.pin_v, model.pin_oe
        pins = step()
        dv, do = v0 ^ model.pin_v, o0 ^ model.pin_oe
        if dv or do:
            for p in range(15):
                if (dv >> p) & 1:
                    hit("pin.change", (p, "rise" if (model.pin_v >> p) & 1 else "fall"))
                if (do >> p) & 1:
                    hit("pin.change", (p, "oe on" if (model.pin_oe >> p) & 1 else "oe off"))
        return pins

    model._execute, model._finish, model.step = _execute, _finish, _step


def attach_jtag(device_class):
    """Samples the TAP state and each transition of a JTAG device model class (test_flex.JtagDevice)."""
    if not ENABLED or getattr(device_class, "_fcov", False):
        return
    step = device_class.step

    def _step(self, c, tck, tms, tdi):
        before, rising = self.state, tck == 1 and self.tck == 0
        step(self, c, tck, tms, tdi)
        if rising:
            hit("jtag.transition", (before, tms))
            hit("jtag.state", self.state)

    device_class.step, device_class._fcov = _step, True


def export():
    """Writes fcov.yml (bins and hits) and fcov_first.json (bench label of the first hit of each bin)."""
    if not ENABLED:
        return
    coverage_db.export_to_yaml(filename=os.path.join(HERE, "fcov.yml"))
    with open(os.path.join(HERE, "fcov_first.json"), "w", encoding="utf-8") as f:
        json.dump({"job": os.environ.get("FCOV_JOB", ""), "first": _first}, f, indent=0)

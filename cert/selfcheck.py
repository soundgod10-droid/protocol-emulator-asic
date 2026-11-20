# SPDX-License-Identifier: Apache-2.0
"""Checks of the lead analysis (cert/leadcert.py) against the reference model (model/pe_model.py).

1. Soundness on random programs: N random programs (OUT, SAMPLE, TADD, TNOW, TSYNC, TWAIT, WAIT with timeout, PULL.NB,
   LD, NOP, loops) run in the model with random input pins and random host words; for every program that the analysis
   certifies, the model must never set LATE. A certified program with LATE in the model is a fault of the analysis.
2. Planted analyser bugs: each bug of BUGS changes one rule of the analysis; a directed program that the model shows
   to be late must then be certified by the changed analysis (so the check above would catch the bug), and must not
   be certified by the correct one.
3. Measured leads: the firmware runs in the scenarios of its model test (test_fw_full.py, test_flex.py) with the lead
   of every event recorded; every measured lead must be within the interval that the analysis gives for that event.

Usage: python cert/selfcheck.py [--programs N] [--seed S]. Returns 1 on any fault.
"""

import argparse
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in ("model", "asm", "test", "cert"):
    sys.path.insert(0, os.path.join(ROOT, p))
import leadcert as lc  # noqa: E402
import pe_asm  # noqa: E402
import pe_model  # noqa: E402

BUGS = ("lead_min_3", "tadd_ignored", "slot_wait_ignored", "two_slot_ld", "tsync_as_tnow", "sample_no_wait")
DIRECTED = {  # bug -> program text (thread 0 owns UIO0 to UIO3); each is late in the model
    "lead_min_3": "TNOW 7\nOUT UIO0, 1\nHALT",                                        # lead 3 at c = 4
    "tadd_ignored": "TNOW 100\n" + "TADD 4095\n" * 8 + "OUT UIO0, 1\nHALT",            # lead beyond 32,767
    "slot_wait_ignored": "TNOW 40\nOUT UIO0, 1\nOUT UIO0, 0\nHALT",                    # second OUT waits past T
    "two_slot_ld": "TNOW 13\nLD r1, 10\nOUT UIO0, 1\nHALT\n.org 10\n.word 0",         # LD takes 8 cycles: lead 1
    "tsync_as_tnow": "TNOW 100\n" + "TADD 4095\n" * 8 + "TSYNC 10\nOUT UIO0, 1\nHALT",  # TSYNC keeps T: lead too big
    "sample_no_wait": "TNOW 20\nSAMPLE UIO0, r0>>\nOUT UIO1, 1\nHALT",                 # SAMPLE waits to T + 1..4
}
OWNER = [0, 0, 0, 0] + [None] * 11


def model_late(words, cycles, rng, host=True):
    """Runs words on thread 0 in the model with random inputs and host words; True if LATE was set."""
    m = pe_model.PeModel(words, owner=OWNER)
    for c in range(cycles):
        if rng.random() < 0.05:
            m.set_inputs(rng.randrange(32), rng.randrange(256))
        if host and rng.random() < 0.02:
            m.fifo_push(0, rng.randrange(65536))
        m.fifo_pop(0)
        try:
            m.step()
        except pe_model.ModelError:
            return None
        if m.threads[0].status & pe_model.STATUS_LATE:
            return True
        if not m.threads[0].run:
            break
    return False


def random_program(rng):
    lines = ["start: TNOW %d" % rng.randrange(4, 60)]
    n = rng.randrange(3, 14)
    for i in range(n):
        k = rng.random()
        pin = rng.randrange(4)
        if k < 0.30:
            lines.append("OUT UIO%d, %d, %d" % (pin, rng.randrange(2), rng.randrange(8)))
        elif k < 0.45:
            lines.append("TADD %d" % rng.choice((rng.randrange(0, 12), rng.randrange(8, 200), rng.randrange(1000, 4096))))
        elif k < 0.52:
            lines.append("SAMPLE UIO%d, r0>>, %d" % (pin, rng.randrange(8)))
        elif k < 0.58:
            lines.append("TSYNC %d" % rng.randrange(0, 40))
        elif k < 0.62:
            lines.append("TNOW %d" % rng.randrange(0, 60))
        elif k < 0.66:
            lines.append("TWAIT")
        elif k < 0.71:
            lines.append("WAIT UIO%d, ANY, r2, TO%s" % (rng.randrange(8), ", CT" if rng.random() < 0.5 else ""))
        elif k < 0.76:
            lines.append("PULL.NB r1")
        elif k < 0.80:
            lines.append("LD r1, 500")
        elif k < 0.86:
            lines.append("L%d: NOP" % i)
            lines.append("LDI r3, %d" % rng.randrange(1, 4))
        else:
            lines.append("NOP")
    tail = rng.random()
    if tail < 0.5:
        lines.append("JMP start")
    elif tail < 0.8:
        lines.append("BEC start")
        lines.append("HALT")
    else:
        lines.append("HALT")
    lines.append(".org 500\n.word 0")
    return "\n".join(lines)


def certified(text, bugs=()):
    prog = pe_asm.assemble(text, "random.s")
    res = lc.analyse(prog, [(0, 0)], {}, bugs)
    lo = lc.lead_min(bugs)
    ok = res.events and not res.problems and all(lc.in_window(v, lo) for v in res.events.values())
    return bool(ok), prog


def measured_leads(name, runner):
    """{pc: [min, max]} of the leads that the model gives while runner() runs (PeModel._event_cycle wrapped)."""
    seen = {}
    orig = pe_model.PeModel._event_cycle

    def wrapped(self, th, c, t):
        d = (t - c) & 0xFFFF
        lo, hi = seen.get(th.pc, (d, d))
        seen[th.pc] = (min(lo, d), max(hi, d))
        return orig(self, th, c, t)
    pe_model.PeModel._event_cycle = wrapped
    try:
        runner()
    finally:
        pe_model.PeModel._event_cycle = orig
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--programs", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=2611)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    faults = []

    # 1. soundness on random programs
    n_cert = n_late = n_both = 0
    progs = []
    for i in range(args.programs):
        text = random_program(rng)
        try:
            ok, prog = certified(text)
        except pe_asm.AsmError:
            continue
        late = model_late(prog.image(), 3000, random.Random(i))
        if late is None:
            continue
        progs.append((text, prog, late))
        n_cert += ok
        n_late += late
        if ok and late:
            n_both += 1
            faults.append("random program %d certified but LATE in the model:\n%s" % (i, text))
    print("random programs: %d run, %d certified, %d LATE in the model, %d certified and LATE (must be 0)"
          % (len(progs), n_cert, n_late, n_both))

    # 2. planted bugs
    for bug in BUGS:
        text = DIRECTED[bug]
        ok_correct, prog = certified(text)
        ok_bug, _ = certified(text, (bug,))
        late = model_late(prog.image(), 2000, random.Random(1), host=False)
        caught_random = sum(1 for t, p, l in progs if l and certified(t, (bug,))[0])
        verdict = "caught" if (late and ok_bug and not ok_correct) else "NOT caught"
        print("planted bug %-18s directed program: model LATE %s, certified with the bug %s, without %s -> %s; "
              "random programs certified with the bug and LATE: %d" % (bug, late, ok_bug, ok_correct, verdict,
                                                                       caught_random))
        if verdict != "caught":
            faults.append("planted bug %s not caught" % bug)

    # 3. measured leads of firmware in its model test scenarios
    import test_fw_full as ff
    import test_flex as fx
    checks = {
        "uart_tx_full.s": lambda: [ff.tx_run(60e6, r, "8N1", [0x55, 0x00, 0xFF]) for r in (1_000_000, 115_200)],
        "spi_ctl.s": lambda: [ff.spi_run(cp, ch, ff.spi_h_min(cp, ch, 0), 0) for cp in (0, 1) for ch in (0, 1)],
        "jtag.s": lambda: fx.jtag_run_a(12, 28),
        "jtag_tap.s": lambda: fx.jtag_run_b(12, 28),
    }
    for name, runner in checks.items():
        r = lc.certify(name)
        prog = lc.load(name)
        res = lc.analyse(prog, [(0, 0)], {}, ())
        seen = measured_leads(name, runner)
        outside = {pc: m for pc, m in seen.items() if pc in res.events and not
                   (res.events[pc].lo <= m[0] and m[1] <= res.events[pc].hi)}
        print("%s: certified %s; %d events measured in the model test, smallest measured lead %s, analysis bound %s; "
              "measured leads outside the analysed interval: %d" % (name, r["certified"], len(seen),
                                                                    min((m[0] for m in seen.values()), default=None),
                                                                    r["min_lead"], len(outside)))
        for pc, m in outside.items():
            faults.append("%s address %d: measured lead %s outside the analysed %s" % (name, pc, m, res.events[pc]))
    for f in faults[:20]:
        print("FAULT: " + f)
    print("self-check: %d fault(s)" % len(faults))
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: Apache-2.0
"""Oregon Scientific version 2.1 sensor transmitter firmware (fw/oregon.s) against an independent receiver model, in
the reference model and on the RTL at 60 MHz.

What it does:
  The receiver model (oregon_receive) is written from "Oregon Scientific RF Protocol Description (Versions 1.0, 2.1,
  3.0) plus (AcuRite 00955 and OS SL-109H)", November 2012, pages 1 to 4 (OOK, Manchester code with the normal
  polarity: a bit is the carrier state before the mid-period transition; bit rate 1024 Hz, within a few tenths of a
  hertz; version 2.1 sends each data bit as the pair (inverted, plain); carrier pulses shortened at their end by about
  93 us; preamble of 16 data bits 1 (32 alternating transmitted bits), sync nibble 0xA, nibbles LSB first, the message
  sent twice), page 6 (protocol summary table: v2.1 pulse length offset -96 us) and page 13 (THGR122NX: the checksum
  byte is the sum of the payload nibbles, low nibble first, with two recorded messages). It does not use the firmware,
  the assembler or the processor model. It recovers the clock from the carrier rises (which are not shortened), checks
  that every rise is on the half-period grid of 1024 Hz within 0.5 Hz and every fall 80 to 110 us before a grid point
  (93 us +/- 15 % plus the -96 us of page 6), reads each half period a quarter period after its start, checks the
  Manchester pairs and the (inverted, plain) bit pairs, the preamble, the sync nibble and the checksum, and returns the
  nibbles of each copy.
  The model test sends the two THGR122NX messages of page 13 at the real bit rate and checks both copies of each;
  negative controls change the firmware's half period (1020 Hz) and remove the pulse shortening. The RTL test runs
  the firmware on the chip in lockstep with the reference model with all times divided by 16 (the receiver model
  scaled the same way), so that the run lasts minutes; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  Eighth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). A slow Manchester stream with
  doubled, inverted bits and shortened pulses whose ends sit off the clock grid.

Conditions: logic simulation at 60 MHz with ideal pins; the 433.92 MHz OOK transmitter, the radio path and a real
receiver's AGC are not simulated: the RF pin level is the carrier state.
"""

import os
import sys

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
P_RF = 4  # RF = uio[4], pull-down on the board

RATE, RATE_TOL = 1024.0, 0.5  # Hz (page 1: within a few tenths of a hertz)
TRUNC_US = (80.0, 110.0)  # carrier falls before the grid point: 93 us (page 3) and -96 us (page 6), +/- 15 %
MESSAGES = ["1D20485C480882835", "1D2016B1091073A14"]  # page 13, THGR122NX, in the order of transmission


def rf_level(uo, uio, oe):
    return (uio >> P_RF) & 1 if (oe >> P_RF) & 1 else 0


def oregon_receive(changes, end, scale=1, us=F_MHZ):
    """Version 2.1 receiver (not from the firmware). scale: all times divided by `scale` (RTL test). Returns (copies,
    errors, stats): copies = [nibble string of each copy with its checksum]."""
    errors, copies = [], []
    h = us * 1e6 / RATE / 2 / scale  # half period in cycles
    stats = dict(rate=[], trunc=[])
    edges = changes[1:]
    i = 0
    while i < len(edges):
        if edges[i][1] != 1:
            i += 1
            continue
        # a copy: edges until a silence of more than 4 half periods
        j = i
        while j + 1 < len(edges) and edges[j + 1][0] - edges[j][0] < 4 * h:
            j += 1
        grp = edges[i:j + 1]
        rises = [c for c, v in grp if v == 1]
        r0 = rises[0]
        # half period from the preamble (data bits 1: a rise every 4 half periods), then over the whole copy
        hh0 = (rises[8] - r0) / 32 if len(rises) > 8 else h
        k_last = round((rises[-1] - r0) / hh0)
        hh = (rises[-1] - r0) / k_last if k_last else hh0
        rate = us * 1e6 / (2 * hh * scale)
        stats["rate"].append(rate)
        if abs(rate - RATE) > RATE_TOL:
            errors.append(f"cycle {r0}: bit rate {rate:.3f} Hz (1024 Hz within {RATE_TOL} Hz)")
        for c, v in grp:
            k = (c - r0) / hh
            if v == 1:
                if abs(k - round(k)) * hh > 0.02 * h:
                    errors.append(f"cycle {c}: carrier rise {abs(k - round(k)) * hh / us:.1f} us off the clock grid")
            else:
                t = (round(k + 0.5) - k) * hh * scale / us  # time before the next grid point, real us
                stats["trunc"].append(t)
                if not TRUNC_US[0] <= t <= TRUNC_US[1]:
                    errors.append(f"cycle {c}: carrier fall {t:.1f} us before the grid point (93 us, page 3)")
        n_half = round((grp[-1][0] - r0) / hh) + 1  # the last edge ends half n_half - 1 (or the one before)

        def level(t):
            lv = 0
            for c, v in grp:
                if c <= t:
                    lv = v
                else:
                    break
            return lv
        halves = [level(r0 + (k - 1) * hh + hh / 4) for k in range(n_half)]  # half 0 starts one half before r0
        if len(halves) % 2:
            halves.append(0)
        tbits = []
        for k in range(0, len(halves), 2):
            if halves[k] == halves[k + 1]:
                errors.append(f"cycle {round(r0 + (k - 1) * hh)}: no Manchester transition in a clock period")
                break
            tbits.append(halves[k])
        dbits = []
        for k in range(0, len(tbits) - 1, 2):
            if tbits[k] == tbits[k + 1]:
                errors.append(f"copy at cycle {r0}: transmitted bit pair {tbits[k]}{tbits[k + 1]} is not 01 or 10")
                break
            dbits.append(tbits[k + 1])
        if dbits[:16] != [1] * 16:
            errors.append(f"copy at cycle {r0}: preamble {dbits[:16]} (16 data bits 1)")
        if dbits[16:20] != [0, 1, 0, 1]:
            errors.append(f"copy at cycle {r0}: sync nibble bits {dbits[16:20]} (0xA, LSB first)")
        body = dbits[20:]
        nibs = [sum(body[k + b] << b for b in range(4)) for k in range(0, len(body) - 3, 4)]
        if len(nibs) >= 3:
            s = sum(nibs[:-2]) & 0xFF
            if nibs[-2] | nibs[-1] << 4 != s:
                errors.append(f"copy at cycle {r0}: checksum {nibs[-1]:X}{nibs[-2]:X}, sum of nibbles {s:02X}")
        copies.append("".join(f"{x:X}" for x in nibs))
        i = j + 1
    return copies, errors, stats


def assemble(**equ):
    return ff.assemble("oregon.s", **equ)


def host_words(msgs):
    out = []
    for m in msgs:
        pay = m[:-2]
        out += [len(pay)] + [int(ch, 16) for ch in pay]
    return out


def copy_cycles(msg, half=29297):
    return (16 + 4 + 4 * len(msg)) * 4 * half


def model_run(msgs, scale=1, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_RF: 0}))
    bench = ff.Bench(m)
    bench.watch("rf", rf_level)
    bench.send(0, *host_words(msgs))
    half = equ.get("HALF", 29297)
    bench.run(sum(2 * (copy_cycles(x, half) + 24 * half) for x in msgs) + 50_000)  # each copy and its gap
    return bench, m


def check(bench, end, msgs, label, scale=1):
    copies, errors, stats = oregon_receive(bench.changes("rf"), end, scale)
    assert not errors, f"{label}: {errors[:3]}"
    want = [x for x in msgs for _ in range(2)]
    assert copies == want, f"{label}: copies {copies}"
    return copies, stats


def summary(copies, stats):
    return (f"{len(copies)} copies {copies}; bit rate {min(stats['rate']):.3f} to {max(stats['rate']):.3f} Hz; carrier "
            f"falls {min(stats['trunc']):.1f} to {max(stats['trunc']):.1f} us before the grid point")


@cocotb.test(skip=GL)
async def test_oregon_words(dut):
    """Memory words of fw/oregon.s."""
    size = assemble().size
    dut._log.info(f"fw/oregon.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_oregon_model(dut):
    """fw/oregon.s in the reference model against the v2.1 receiver model: the two THGR122NX messages of page 13 at
    1024 Hz, each sent twice; negative controls for the bit rate and the pulse shortening."""
    log = dut._log
    bench, m = model_run(MESSAGES)
    copies, stats = check(bench, m.cycle, MESSAGES, "model")
    ff.assert_status_clean(m, [0])
    log.info(f"model: {summary(copies, stats)}; {m.cycle} cycles")
    s = 16  # controls with all times divided by 16
    for equ, want in ((dict(HALF=29400 // s, TRUNC=5580 // s), "bit rate"), (dict(HALF=29297 // s, TRUNC=1), "fall")):  # pulses not shortened
        bench, m = model_run(MESSAGES[:1], **equ)
        _, errors, _ = oregon_receive(bench.changes("rf"), m.cycle, scale=s)
        log.info(f"control {equ} (times / 16): {errors[0] if errors else 'no fault found'}")
        assert errors and want in errors[0], f"control {equ}: expected '{want}', got {errors[:2]}"


@cocotb.test()
async def test_oregon_rtl(dut):
    """fw/oregon.s on the chip at 60 MHz against the v2.1 receiver model, all times divided by 16: a message with the
    sensor ID nibbles of page 13 only, sent twice, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl oregon"
    s = 16
    equ = dict(HALF=29297 // s, TRUNC=5580 // s)
    msgs = ["1D2001"]  # sensor ID 1D20 only, checksum 0x10: a short message keeps the RTL run to minutes
    bench = rf.RtlBench(dut, assemble(**equ).words, owner=ff.owners({P_RF: 0}), label=label)
    bench.watch("rf", rf_level)
    bench.send(0, *host_words(msgs))
    half = equ["HALF"]
    if hasattr(dut, "dump_off"):  # waveform of the preamble and the sync nibble (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 24 * 4 * half)
        dut.dump_off.value = 1
        rises = [c for c, v in bench.changes("rf") if v == 1]
        log.info(f"{label}: waveform from the start; first carrier rises at model cycles {rises[:4]}")
    await bench.run(rf.HOST_START + 2 * copy_cycles(msgs[0], half) + 24 * half + 20_000 - bench.m.cycle)
    await bench.finish(log)
    copies, stats = check(bench, bench.m.cycle, msgs, label, scale=s)
    log.info(f"{label} (times / 16): {summary(copies, stats)}")

# SPDX-License-Identifier: Apache-2.0
"""SENT transmitter firmware (fw/sent.s) against an independent SENT receiver model, in the reference model and on the
RTL at 60 MHz.

What it does:
  The receiver model (sent_receive) is written from the Microchip dsPIC33/PIC24 Family Reference Manual section
  "Single-Edge Nibble Transmission (SENT) Module", DS70005145B (2013-2014), which describes SAE J2716 (the standard
  itself is paid): 3.0 (message format, Figure 3-1) and 4.2 (Example 4-1, the recommended CRC). It does not use the
  firmware, the assembler or the processor model. It reads the line (open drain with a pull-up: 1 when released) and
  takes the time between falling edges: a calibration period of 56 ticks gives the tick of the message (accepted if it
  is within 20 % of the configured tick, 3.0), then the status nibble, the data nibbles and the CRC nibble, each
  12 to 27 ticks (value = ticks - 12), then the pause pulse (12 to 768 ticks). It checks the CRC with the lookup table
  and the seed 0101 of Example 4-1 (the firmware computes it bit by bit), the low time of each period (5 ticks, 3.0:
  the module drives the line low for 5 ticks), and a constant message period when one is given. It also measures how
  far each falling edge lies from the whole-tick grid of its message (a firmware check, not a protocol limit).
  The model test sends four messages (all-zero and all-one nibbles among them), checks that the last values are sent
  again while the host sends nothing, runs a late host, and runs the slowest tick (90 us). Negative controls: a low
  time of 3 ticks, a wrong CRC seed, a message period too short for the 12-tick pause, and a tick 28 % off. The RTL
  test sends two messages and one repeat on the chip in lockstep with the reference model; the same test runs on the
  gate-level netlist (gl_fw workflow).

Why:
  Seventeenth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode
  decoder at the pinned commit decodes SENT, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the pull-up, the line capacitance and the slope-shaping of a
SENT output stage are not simulated (with a pull-up the rising edge is slow, but SENT carries its data in the falling
edges only).
"""

import os
import sys

import cocotb
from cocotb.utils import get_sim_time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import pe_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_rtl_fw as rf  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_MHZ = 60
P_SENT = 0  # uio[0], open drain

# DS70005145B 3.0: sync 56 ticks, nibbles 12 to 27 ticks, pause 12 to 768 ticks, tick variation up to 20 %, the
# transmitter drives the line low for 5 ticks. 4.2 Example 4-1: CRC lookup table and seed 5 (0101), a last step with 0.
SYNC, NIB_MIN, NIB_MAX, PAUSE_MIN, PAUSE_MAX, TICK_VAR, LOW = 56, 12, 27, 12, 768, 0.20, 5
CRC_TABLE = [0, 13, 7, 10, 14, 3, 9, 4, 1, 12, 6, 11, 15, 2, 8, 5]


def crc4(data):
    """CRC nibble of the data nibbles, as Example 4-1 of DS70005145B."""
    c = 5
    for d in data:
        c = d ^ CRC_TABLE[c]
    return 0 ^ CRC_TABLE[c]


def line(uo, uio, oe):
    """Open-drain line with a pull-up: 0 only while the pin drives 0."""
    return 0 if (oe >> P_SENT) & 1 and not (uio >> P_SENT) & 1 else 1


def sent_receive(changes, end, tick_us, nibbles=6, period=None, us=F_MHZ):
    """SENT receiver from DS70005145B (not from the firmware).

    changes: [(cycle, level)] of the line from cycle 0; end: the last cycle; tick_us: the configured tick; period:
    the expected message period in ticks, or None. Returns (messages, errors, grid): messages [{"t": cycle of the
    sync's falling edge, "tick": tick in us from the calibration period, "status", "data", "crc", "pause": pause in
    ticks, "period": message period in us}]; grid: the largest distance (cycles) of a falling edge from the whole-tick
    grid of its message. Messages not complete before end are not counted."""
    errors, msgs = [], []
    grid = 0.0
    if not changes or changes[0][1] != 1:
        return msgs, ["the line does not start high (released)"], grid
    falls = [c for c, v in changes if v == 0]
    rises = [c for c, v in changes if v == 1 and c > 0]
    nom = tick_us * us
    n_int = nibbles + 3  # calibration, status, data, CRC
    i = 0
    while i + n_int + 1 < len(falls):
        t = (falls[i + 1] - falls[i]) / SYNC
        if abs(t / nom - 1) > TICK_VAR:
            errors.append(f"cycle {falls[i]}: calibration period {(falls[i + 1] - falls[i]) / us:.3f} us: tick "
                          f"{t / us:.3f} us is not {tick_us} us +/- 20 % (3.0)")
            break
        vals = []
        for k in range(1, n_int):
            d = falls[i + k + 1] - falls[i + k]
            n = round(d / t)
            grid = max(grid, abs(falls[i + k + 1] - falls[i] - round((falls[i + k + 1] - falls[i]) / t) * t))
            if not NIB_MIN <= n <= NIB_MAX:
                errors.append(f"cycle {falls[i + k]}: nibble period {d / t:.2f} ticks (12 to 27, 3.0)")
            vals.append(n - NIB_MIN)
        for k in range(i, i + n_int + 1):
            r = next((x for x in rises if x > falls[k]), end)
            low = (r - falls[k]) / t
            if abs(low - LOW) > 0.5:
                errors.append(f"cycle {falls[k]}: low time {low:.2f} ticks (the transmitter drives 5 ticks, 3.0)")
        status, data, crc = vals[0], vals[1:-1], vals[-1]
        if crc != crc4(data):
            errors.append(f"cycle {falls[i]}: CRC nibble {crc}, the data {data} give {crc4(data)} (Example 4-1)")
        j = i + n_int
        pause = (falls[j + 1] - falls[j]) / t
        if not PAUSE_MIN <= round(pause) <= PAUSE_MAX:
            errors.append(f"cycle {falls[j]}: pause {pause:.2f} ticks (12 to 768, 3.0)")
        per = falls[j + 1] - falls[i]
        if period is not None and abs(per - period * t) > 1:
            errors.append(f"cycle {falls[i]}: message period {per / t:.2f} ticks, not the constant {period}")
        msgs.append(dict(t=falls[i], tick=t / us, status=status, data=data, crc=crc, pause=pause, period=per / us))
        i = j + 1
    return msgs, errors, grid


# ----- messages and host words -----

MSGS = [(0x0, [0, 0, 0, 0, 0, 0]), (0xF, [15, 15, 15, 15, 15, 15]), (0x5, [10, 11, 12, 1, 2, 3]),
        (0x8, [7, 15, 0, 3, 12, 9])]
PERIOD = 300


def host_words(msgs):
    out = []
    for s, d in msgs:
        out += [s << 12 | d[0] << 8 | d[1] << 4 | d[2], d[3] << 8 | d[4] << 4 | d[5]]
    return out


def assemble(**equ):
    return ff.assemble("sent.s", **equ)


def model_run(words, cycles, late=None, **equ):
    """fw/sent.s in the reference model. late = (wait, words2): words2 follow after wait cycles."""
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_SENT: 0}), open_drain=ff.open_drain({P_SENT}))
    bench = ff.Bench(m)
    bench.watch("line", line)
    bench.send(0, *words)
    if late is not None:
        wait, words2 = late
        bench.run(wait)
        bench.send(0, *words2)
        cycles -= wait
    bench.run(cycles)
    return bench, m


def pairs(msgs):
    return [(m["status"], m["data"]) for m in msgs]


def summary(msgs, grid):
    return (f"{len(msgs)} messages, tick {min(m['tick'] for m in msgs):.3f} to {max(m['tick'] for m in msgs):.3f} us, "
            f"period {min(m['period'] for m in msgs):.3f} to {max(m['period'] for m in msgs):.3f} us, pause "
            f"{min(m['pause'] for m in msgs):.1f} to {max(m['pause'] for m in msgs):.1f} ticks, CRC "
            f"{[m['crc'] for m in msgs]}, largest distance from the tick grid {grid:.1f} cycles")


@cocotb.test(skip=GL)
async def test_sent_words(dut):
    """Memory words of fw/sent.s."""
    size = assemble().size
    dut._log.info(f"fw/sent.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_sent_model(dut):
    """fw/sent.s in the reference model against the SENT receiver model: four messages and repeats, a late host, the
    90 us tick; negative controls for the low time, the CRC seed, the pause and the tick."""
    log = dut._log
    per = PERIOD * 180
    bench, m = model_run(host_words(MSGS), 7 * per)
    got, errors, grid = sent_receive(bench.changes("line"), m.cycle, 3.0, period=PERIOD)
    assert not errors, f"model: {errors[:3]}"
    want = [(s, d) for s, d in MSGS] + [MSGS[-1]] * (len(got) - len(MSGS))
    assert len(got) >= 6 and pairs(got) == want, f"model: {pairs(got)}"
    assert grid <= 1.0, f"model: falling edge {grid} cycles off the tick grid"
    ff.assert_status_clean(m, [0])
    log.info(f"model: {summary(got, grid)}; values as sent, then the last message again "
             f"({len(got) - len(MSGS)} repeats)")
    late = [(0x1, [1, 2, 3, 4, 5, 6])]
    bench, m = model_run(host_words(MSGS[:1]), 6 * per, late=(int(2.5 * per), host_words(late)))
    got, errors, grid = sent_receive(bench.changes("line"), m.cycle, 3.0, period=PERIOD)
    seq = pairs(got)
    k = seq.index(late[0]) if late[0] in seq else -1
    assert not errors and k >= 2 and all(x == MSGS[0] for x in seq[:k]) and all(x == late[0] for x in seq[k:]), (
        f"late host: {errors[:3]} {seq}")
    ff.assert_status_clean(m, [0])
    log.info(f"model late host: the first message {k} times, then the new values {len(seq) - k} times; "
             f"{summary(got, grid)}")
    bench, m = model_run(host_words(MSGS[2:3]), int(2.2 * PERIOD * 5400), TICK=5400)
    got, errors, grid = sent_receive(bench.changes("line"), m.cycle, 90.0, period=PERIOD)
    assert not errors and pairs(got)[:1] == [MSGS[2]] and grid <= 1.0, f"tick 90 us: {errors[:3]} {pairs(got)}"
    ff.assert_status_clean(m, [0])
    log.info(f"model tick 90 us: {summary(got, grid)}")
    allf = [(0xF, [15] * 6)]
    for equ, words, tick, want_text in ((dict(LOWN=3), MSGS, 3.0, "low time"), (dict(SEED=4), MSGS, 3.0, "CRC"),
                                        (dict(PERIOD=278), allf, 3.0, "pause"), (dict(TICK=230), MSGS, 3.0, "tick")):
        bench, m = model_run(host_words(words), 4 * per, **equ)
        _, errors, _ = sent_receive(bench.changes("line"), m.cycle, tick)
        found = [e for e in errors if want_text in e]
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: expected a fault with '{want_text}', got {errors[:2]}"


@cocotb.test()
async def test_sent_rtl(dut):
    """fw/sent.s on the chip at 60 MHz against the SENT receiver model: two messages and a repeat of the second, in
    lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl sent"
    msgs = MSGS[2:4]
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_SENT: 0}), od=ff.open_drain({P_SENT}),
                        label=label)
    bench.watch("line", line)
    bench.send(0, *host_words(msgs))
    if hasattr(dut, "dump_off"):  # waveform of the first calibration period and status nibble (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 20_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        falls = [c for c, v in bench.changes("line") if v == 0][:4]
        log.info(f"{label}: waveform from the start; first falling edges at simulation times (ps) "
                 f"{[round(now - (bench.m.cycle - c) * rf.CLK_PS) for c in falls]}")
    await bench.run(rf.HOST_START + int(3.4 * PERIOD * 180) - bench.m.cycle)
    await bench.finish(log)
    got, errors, grid = sent_receive(bench.changes("line"), bench.m.cycle, 3.0, period=PERIOD)
    want = list(msgs) + [msgs[-1]] * (len(got) - len(msgs))
    assert not errors and len(got) >= 3 and pairs(got) == want and grid <= 1.0, f"{label}: {errors[:3]} {pairs(got)}"
    log.info(f"{label}: {summary(got, grid)}; values {pairs(got)}")

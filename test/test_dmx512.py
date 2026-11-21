# SPDX-License-Identifier: Apache-2.0
"""DMX512-A controller firmware (fw/dmx512.s) against an independent DMX512 receiver model, in the reference model
and on the RTL at 60 MHz.

What it does:
  The receiver model (dmx_receive) reads the chip's DMX_TX pin as the data link of ANSI E1.11-2008 (R2018)
  (Entertainment Technology - USITT DMX512-A, ESTA): clauses 8.1 to 8.10, Table 5 (slot format), Table 6
  (transmitter timing) and Table 7 (receiver timing), PDF pages 28 to 32. It does not use the firmware, the assembler
  or the processor model. It finds each BREAK as a low of at least the receiver minimum (Table 7 #1, 88 us), reads
  each slot at the nominal bit time from its start edge and checks the transmitter's output against Table 6:
    - BREAK at least 92 us (#1); MARK AFTER BREAK 12 us to under 1 s (#2);
    - every edge inside a slot at k bit times from the start edge, with a bit time of 3.92 to 4.08 us;
    - start bit low, both stop bits high (Table 5; clause 9.1: a receiver checks the stop bits);
    - mark between slots and mark before the BREAK under 1 s (#9, #10), and no start bit before the end of the
      previous slot's second stop bit;
    - break-to-break time at least 1,204 us and at most 1 s (#11);
    - at most 513 slots in a packet (clauses 8.1, 8.6).
  The model test sends packets with the NULL START Code and with an alternate START Code, short packets (the
  firmware must add mark for the 1,204 us break-to-break minimum), a full packet of 513 slots, and a host that is late
  with the slot words (mark between slots). Negative controls change the firmware's constants beyond each limit, and
  the receiver model must report each. The RTL test runs the firmware on the chip at 60 MHz in lockstep with the
  reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  First row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). DMX512 frames its slots like a
  UART at 250 kbit/s, but a packet needs a BREAK of at least 23 bit times, a minimum mark after it and a minimum
  packet time, which a UART does not make.

Conditions: logic simulation at 60 MHz with ideal pins; the EIA-485 transceiver (DE high) and the cable are not
simulated.
"""

import bisect
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
P_TX = 12  # DMX_TX = uo_out[4] (docs/isa.md 5.1)

# ANSI E1.11-2008 (R2018), PDF page 32, in us. Table 6 (output of the transmitting UART): bit time 3.92 to 4.08,
# BREAK at least 92 (#1), MARK AFTER BREAK 12 to under 1 s (#2), mark between slots and before the BREAK under 1 s
# (#9, #10), break-to-break 1,204 to 1 s (#11). Table 7 #1: a receiver recognizes a BREAK of 88 us or more.
TX_LIM = dict(bit_min=3.92, bit_max=4.08, brk=92.0, mab=12.0, mark_max=1.0e6, b2b=1204.0, b2b_max=1.0e6)
RX_BRK = 88.0
BIT_NOM = 4.0  # Table 6 typical bit time (250 kbit/s)
MAX_SLOTS = 513  # clause 8.1: slot 0 (START Code) and up to 512 data slots


def dmx_receive(changes, end, us=F_MHZ):
    """DMX512 receiver from E1.11 (not from the firmware).

    changes: [(cycle, level)] of the line from cycle 0 (1 = MARK); end: the last cycle; us: cycles per microsecond.
    Returns (packets, errors, stats). packets: [{"t": cycle of the BREAK's falling edge, "brk": BREAK in us, "mab":
    MARK AFTER BREAK in us, "slots": slot values (START Code first), "gaps": mark between slots in us, "b2b": time to
    the next BREAK in us}]. stats: the shortest and longest bit time (us) seen in the edges inside the slots.
    """
    errors, packets = [], []
    stats = dict(bit_lo=None, bit_hi=None)
    if not changes or changes[0][1] != 1:
        return packets, ["the line does not start at MARK"], stats
    times = [c for c, _ in changes]

    def level(cycle):
        return changes[bisect.bisect_right(times, cycle) - 1][1]

    bit = BIT_NOM * us
    pkt, ready, slot_end = None, 0, None
    i = 1
    while i < len(changes):
        c, v = changes[i]
        if v != 0:
            i += 1
            continue
        rise = changes[i + 1][0] if i + 1 < len(changes) else end
        low = (rise - c) / us
        if low >= RX_BRK:  # a BREAK starts a packet (8.3)
            if low < TX_LIM["brk"]:
                errors.append(f"cycle {c}: BREAK {low:.3f} us (Table 6 #1: at least 92 us)")
            if slot_end is not None and (c - slot_end) / us >= TX_LIM["mark_max"]:
                errors.append(f"cycle {c}: mark before the BREAK of 1 s or more (Table 6 #10)")
            if pkt is not None:
                b2b = (c - pkt["t"]) / us
                pkt["b2b"] = b2b
                if b2b < TX_LIM["b2b"]:
                    errors.append(f"cycle {c}: break-to-break {b2b:.3f} us (Table 6 #11: at least 1204 us)")
                if b2b > TX_LIM["b2b_max"]:
                    errors.append(f"cycle {c}: break-to-break {b2b:.3f} us (Table 6 #11: at most 1 s)")
            pkt = dict(t=c, brk=low, mab=None, slots=[], gaps=[], b2b=None)
            packets.append(pkt)
            if i + 2 < len(changes):
                mab = (changes[i + 2][0] - rise) / us
                pkt["mab"] = mab
                if not TX_LIM["mab"] <= mab < TX_LIM["mark_max"]:
                    errors.append(f"cycle {rise}: MARK AFTER BREAK {mab:.3f} us (Table 6 #2: 12 us to under 1 s)")
            ready, slot_end = rise, None
            i += 2
            continue
        # a start bit (Table 5): sample each bit in its middle at the nominal bit time
        if pkt is None:
            errors.append(f"cycle {c}: a slot before the first BREAK")
        elif c < ready:
            errors.append(f"cycle {c}: start bit {(ready - c) / us:.3f} us before the end of the previous slot "
                          f"(11 bits at 3.92 us)")
        bits = [level(int(c + (k + 0.5) * bit)) for k in range(11)]
        if bits[0] != 0:
            errors.append(f"cycle {c}: start bit not low in its middle (Table 5)")
        for k in (9, 10):
            if bits[k] != 1:
                errors.append(f"cycle {c}: stop bit {k - 8} is low (Table 5, clause 9.1)")
        j = i + 1
        while j < len(changes) and changes[j][0] < c + 10.5 * bit:
            d = (changes[j][0] - c) / us
            k = round(d / BIT_NOM)
            if k == 0 or not k * TX_LIM["bit_min"] <= d <= k * TX_LIM["bit_max"]:
                errors.append(f"cycle {changes[j][0]}: edge {d:.3f} us after the start edge is not at k bit times of "
                              f"3.92 to 4.08 us (Table 6)")
            else:
                b = d / k
                stats["bit_lo"] = b if stats["bit_lo"] is None else min(stats["bit_lo"], b)
                stats["bit_hi"] = b if stats["bit_hi"] is None else max(stats["bit_hi"], b)
            j += 1
        if pkt is not None:
            if slot_end is not None:
                gap = (c - slot_end) / us
                pkt["gaps"].append(gap)
                if gap >= TX_LIM["mark_max"]:
                    errors.append(f"cycle {c}: mark between slots of 1 s or more (Table 6 #9)")
            pkt["slots"].append(sum(bits[1 + k] << k for k in range(8)))
            if len(pkt["slots"]) == MAX_SLOTS + 1:
                errors.append(f"cycle {c}: more than 513 slots in a packet (8.1)")
        ready = c + 11 * TX_LIM["bit_min"] * us
        slot_end = c + 11 * bit
        i = j
    return packets, errors, stats


# ----- packets and host words -----

PACKETS = [
    [0x00, 0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80, 0x7E, 0x81] + [(37 * k + 11) & 0xFF for k in range(16)],  # 25 slots
    [0x00, 0x10, 0x20, 0x30],  # 4 slots: the firmware adds mark for the break-to-break minimum
    [0x17],  # an alternate START Code with no data slot (8.7: no minimum number of data slots)
    [0x00, 0xC3, 0x3C],
]


def host_words(packets):
    """Host words of fw/dmx512.s: the slot count F, then the F slot values, for each packet."""
    out = []
    for p in packets:
        out += [len(p)] + list(p)
    return out


def run_cycles(packets, us=F_MHZ):
    """Cycles for the packets: 176 + 16 us and 44 us for each slot (at least 24 slot times for every packet but the
    last), with 5 % margin for the controls."""
    t = sum((176 + 16 + 44 * (len(p) if k == len(packets) - 1 else max(len(p), 24))) * us
            for k, p in enumerate(packets))
    return int(1.05 * t) + 3000


def assemble(**equ):
    return ff.assemble("dmx512.s", **equ)


def model_run(packets, late=None, **equ):
    """fw/dmx512.s in the reference model. late = (k, wait): the host gives the first k words, then the rest after
    wait cycles."""
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_TX: 0}))
    bench = ff.Bench(m)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    words = host_words(packets)
    if late is not None:
        k, wait = late
        bench.send(0, *words[:k])
        bench.run(wait)
        words = words[k:]
    bench.send(0, *words)
    bench.run(run_cycles(packets))
    return bench, m


def check(changes, end, packets, label):
    got, errors, stats = dmx_receive(changes, end)
    assert not errors, f"{label}: {errors[:3]}"
    assert [p["slots"] for p in got] == [list(p) for p in packets], (
        f"{label}: packets {[p['slots'][:6] for p in got]} ({[len(p['slots']) for p in got]} slots)")
    return got, stats


def summary(got, stats):
    b2b = [p["b2b"] for p in got if p["b2b"] is not None]
    gaps = [g for p in got for g in p["gaps"]]
    return (f"{len(got)} packets ({', '.join(str(len(p['slots'])) for p in got)} slots) correct; BREAK "
            f"{min(p['brk'] for p in got):.3f} to {max(p['brk'] for p in got):.3f} us, MAB "
            f"{min(p['mab'] for p in got):.3f} to {max(p['mab'] for p in got):.3f} us, break-to-break "
            f"{min(b2b):.3f} to {max(b2b):.3f} us, bit time {stats['bit_lo']:.3f} to {stats['bit_hi']:.3f} us, "
            f"mark between slots up to {max(gaps, default=0.0):.3f} us")


@cocotb.test(skip=GL)
async def test_dmx512_words(dut):
    """Memory words of fw/dmx512.s."""
    size = assemble().size
    dut._log.info(f"fw/dmx512.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_dmx512_model(dut):
    """fw/dmx512.s in the reference model against the E1.11 receiver model: four packets (25, 4, 1 and 3 slots), a
    full packet of 513 slots, a late host; negative controls for BREAK, MAB, bit time, break-to-break and stop bits."""
    log = dut._log
    bench, m = model_run(PACKETS)
    got, stats = check(bench.changes("tx"), m.cycle, PACKETS, "model")
    ff.assert_status_clean(m, [0])
    log.info(f"model: {summary(got, stats)}")
    full = [[0x00] + [(7 * k + 3) & 0xFF for k in range(512)], [0x00, 0x01]]
    bench, m = model_run(full)
    got, stats = check(bench.changes("tx"), m.cycle, full, "model 513 slots")
    ff.assert_status_clean(m, [0])
    log.info(f"model 513 slots: {summary(got, stats)}; packet time {got[0]['b2b'] / 1000:.3f} ms (Table 6 typical "
             f"update time for 513 slots 22.7 ms)")
    bench, m = model_run(PACKETS[:2], late=(4, 30_000))
    got, stats = check(bench.changes("tx"), m.cycle, PACKETS[:2], "model late host")
    ff.assert_status_clean(m, [0])
    gaps = [g for p in got for g in p["gaps"]]
    assert max(gaps) > 0, "model late host: no mark between slots"
    log.info(f"model late host (header and 3 slots, the rest 30,000 cycles later): {summary(got, stats)}")
    # Negative controls: the receiver model must catch a transmitter beyond each limit of Table 5 and Table 6.
    for equ, want in ((dict(BRK4=1350), "Table 6 #1"), (dict(MAB=600), "Table 6 #2"),
                      (dict(BIT=246, STOP2=492), "3.92 to 4.08 us"), (dict(NMIN=22), "Table 6 #11"),
                      (dict(STOP2=240), "stop bit 2")):
        bench, m = model_run(PACKETS, **equ)
        _, errors, _ = dmx_receive(bench.changes("tx"), m.cycle)
        log.info(f"control {equ}: {errors[0] if errors else 'no fault found'}")
        assert errors and want in errors[0], f"control {equ}: expected a fault with '{want}', got {errors[:2]}"


@cocotb.test()
async def test_dmx512_rtl(dut):
    """fw/dmx512.s on the chip at 60 MHz against the E1.11 receiver model: four packets (25, 4, 1 and 3 slots, NULL and
    alternate START Code), in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl dmx512"
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_TX: 0}), label=label)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    bench.send(0, *host_words(PACKETS))
    total = rf.HOST_START + run_cycles(PACKETS)
    # waveform of the first BREAK, MARK AFTER BREAK and START Code (tb.fst), for review; the Verilator coverage
    # build has no dump and no dump_off (test/tb.v), so there the test runs without it
    if hasattr(dut, "dump_off"):
        dut.dump_off.value = 0
        await bench.run(18_000)
        dut.dump_off.value = 1
    await bench.run(total - bench.m.cycle)
    await bench.finish(log)
    got, stats = check(bench.changes("tx"), bench.m.cycle, PACKETS, label)
    log.info(f"{label}: {summary(got, stats)}; first BREAK at model cycle {got[0]['t']}")

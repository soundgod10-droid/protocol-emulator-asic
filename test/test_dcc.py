# SPDX-License-Identifier: Apache-2.0
"""DCC command station firmware (fw/dcc.s) against an independent DCC decoder model, in the reference model and on
the RTL at 60 MHz.

What it does:
  The decoder model (dcc_decode) reads the chip's two outputs DCC_A and DCC_B as the inputs of a power station's
  H-bridge, so that the track signal is A - B, and checks the signal against the NMRA standards:
    - S-9.1 Electrical Standards for Digital Command Control (issue of 16 June 2026), section 2 and Table 2.1 (PDF
      pages 2 to 4): a bipolar square wave (A and B never equal while the signal runs); every half bit between two
      transitions is a "1" half bit (t1, 55 to 61 us) or a "0" half bit (t0, 95 to 123 us); the two halves of a bit
      are of the same kind and differ by 3 us at most (t1d, t0d); the signal never stops (S-9.1 2.4);
    - S-9.2 Communications Standards for DCC (July 2004), section A (PDF page 1) and section B (pages 2 to 4): at
      least 14 preamble "1" bits before each packet start bit (counted after the end bit of the packet before), the
      address byte and each data byte MSB first, each after a "0" start bit, the packet end bit "1"; the error
      detection byte is the XOR of the bytes before it (for the extended packets of S-9.2.1 likewise).
  It does not use the firmware, the assembler or the processor model. The model test sends the baseline packet of
  S-9.2 Figure 1 (locomotive 55, forward, speed step 6), the idle, reset and broadcast stop packets, a four-byte
  extended packet, a host that is late between two packets (the preamble grows) and a host that is late inside a
  packet (the decoder must reject the cut packet); negative controls change the firmware's half-bit times and
  preamble beyond the limits, and the decoder model must report each. The RTL test runs the firmware on the chip in
  lockstep with the reference model; the same test runs on the gate-level netlist (gl_fw workflow).

Why:
  Second row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). DCC codes each bit in the
  duration of the half periods of the track power, so every transition must be placed to within microseconds, on
  two pins at once.

Conditions: logic simulation at 60 MHz with ideal pins; the power station (H-bridge), the track and the decoder's
rectifier are not simulated, so the S-9.1 voltage and slope limits (Tables 2.2, 2.3) are outside this test.
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
P_A, P_B = 12, 13  # DCC_A = uo_out[4], DCC_B = uo_out[5] (docs/isa.md 5.1)

# NMRA S-9.1 (16 June 2026) Table 2.1, transmitted values in us: t1 "1" half bit, t0 "0" half bit (not stretched),
# t1d and t0d the largest difference between the two halves of a bit. S-9.2 section A: a command station sends at
# least 14 preamble bits.
T1 = (55.0, 61.0)
T0 = (95.0, 123.0)
T1D = T0D = 3.0
PRE_MIN = 14


def dcc_decode(changes, end, us=F_MHZ):
    """DCC decoder from NMRA S-9.1 and S-9.2 (not from the firmware).

    changes: [(cycle, (A, B))] of the two H-bridge inputs from cycle 0; end: the last cycle; us: cycles per
    microsecond. The signal starts at the first change after which A and B differ. Returns (packets, errors, stats):
    packets = [{"bytes": all bytes with the error byte, "ok": XOR of all bytes is 0, "preamble": "1" bits before the
    start bit}], stats = shortest and longest "1" and "0" half bits and the largest difference inside a bit (us).
    """
    errors = []
    stats = dict(h1=[None, None], h0=[None, None], d=0.0, pre=None)
    first = next((i for i, (_, (a, b)) in enumerate(changes) if a != b), None)
    if first is None:
        return [], ["no DCC signal: A and B never differ"], stats
    trans = []
    for c, (a, b) in changes[first:]:
        if a == b:
            errors.append(f"cycle {c}: A and B both {a} (S-9.1 2: bipolar signal)")
        trans.append(c)
    if (end - trans[-1]) / us > T0[1]:
        errors.append(f"cycle {trans[-1]}: no transition for {(end - trans[-1]) / us:.3f} us at the end (S-9.1 2.4)")
    kinds, halves = [], []
    for c0, c1 in zip(trans, trans[1:]):
        d = (c1 - c0) / us
        if T1[0] <= d <= T1[1]:
            kinds.append(1)
            key = "h1"
        elif T0[0] <= d <= T0[1]:
            kinds.append(0)
            key = "h0"
        else:
            errors.append(f"cycle {c0}: half bit {d:.3f} us is neither t1 (55 to 61 us) nor t0 (95 to 123 us) "
                          f"(S-9.1 Table 2.1)")
            kinds.append(None)
            key = None
        halves.append(d)
        if key:
            lo, hi = stats[key]
            stats[key] = [d if lo is None else min(lo, d), d if hi is None else max(hi, d)]
    bits = []
    for k in range(0, len(halves) - 1, 2):
        a, b = kinds[k], kinds[k + 1]
        if a is None or b is None:
            bits.append(None)
            continue
        if a != b:
            errors.append(f"cycle {trans[k]}: the two halves of a bit are of different kinds ({halves[k]:.3f} and "
                          f"{halves[k + 1]:.3f} us)")
            bits.append(None)
            continue
        diff = abs(halves[k] - halves[k + 1])
        stats["d"] = max(stats["d"], diff)
        if diff > (T1D if a else T0D):
            errors.append(f"cycle {trans[k]}: halves of a \"{a}\" bit differ by {diff:.3f} us (S-9.1 t1d, t0d: 3 us)")
        bits.append(a)
    packets, ones, k = [], 0, 0
    while k < len(bits):
        if bits[k] != 0:
            ones = ones + 1 if bits[k] == 1 else 0
            k += 1
            continue
        if ones < PRE_MIN:
            errors.append(f"bit {k}: packet start bit after {ones} preamble bits (S-9.2 A: at least 14)")
        stats["pre"] = ones if stats["pre"] is None else min(stats["pre"], ones)
        data, k, done = [], k + 1, False
        while k + 9 <= len(bits):
            byte = 0
            for b in bits[k:k + 8]:
                byte = (byte << 1) | (b or 0)
            data.append(byte)
            sep, k = bits[k + 8], k + 9
            if sep == 1:  # packet end bit
                done = True
                break
        if not done:
            break  # the capture ends inside the packet
        x = 0
        for byte in data:
            x ^= byte
        packets.append(dict(bytes=data, ok=x == 0 and len(data) >= 2, preamble=ones))
        ones = 0
    return packets, errors, stats


# ----- packets and host words -----

PACKETS = [
    [0x37, 0x74],  # S-9.2 Figure 1: locomotive 55, forward, speed step 6
    [0xFF, 0x00],  # digital decoder idle packet
    [0x00, 0x00],  # digital decoder reset packet
    [0x00, 0x40],  # broadcast stop packet (reverse, stop)
    [0xC0, 0x12, 0x3F, 0x85],  # extended packet: long address 18, 128 speed steps, forward step 4 (S-9.2.1)
]


def host_words(packets):
    out = []
    for p in packets:
        out += list(p[:-1]) + [p[-1] | 0x100]
    return out


def xor_all(p):
    x = 0
    for b in p:
        x ^= b
    return x


def packet_cycles(p, pre=16, h1=3480, h0=6000):
    """Cycles of one packet: the preamble, the start bit, the bytes and the error byte with their start bits, the end
    bit."""
    bits = [1] * pre + [0]
    for b in list(p) + [xor_all(p)]:
        bits += [(b >> (7 - i)) & 1 for i in range(8)] + [0]
    bits[-1] = 1
    return sum(2 * (h1 if b else h0) for b in bits)


def run_cycles(packets):
    return int(1.1 * sum(packet_cycles(p) for p in packets)) + 20_000


def assemble(**equ):
    return ff.assemble("dcc.s", **equ)


def watch_ab(uo, uio, oe):
    return (uo >> 4) & 1, (uo >> 5) & 1


def model_run(packets, steps=None, **equ):
    """fw/dcc.s in the reference model. steps: [(words, cycles)] to give the host words in parts."""
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_A: 0, P_B: 0}))
    bench = ff.Bench(m)
    bench.watch("ab", watch_ab)
    for words, cycles in steps or [(host_words(packets), run_cycles(packets))]:
        bench.send(0, *words)
        bench.run(cycles)
    return bench, m


def check(changes, end, packets, label):
    got, errors, stats = dcc_decode(changes, end)
    assert not errors, f"{label}: {errors[:3]}"
    want = [list(p) + [xor_all(p)] for p in packets]
    assert [p["bytes"] for p in got] == want and all(p["ok"] for p in got), (
        f"{label}: packets {[(p['bytes'], p['ok']) for p in got]}")
    return got, stats


def summary(got, stats):
    return (f"{len(got)} packets correct ({', '.join(' '.join(f'{b:02X}' for b in p['bytes']) for p in got)}); "
            f"\"1\" half bits {stats['h1'][0]:.3f} to {stats['h1'][1]:.3f} us, \"0\" half bits {stats['h0'][0]:.3f} to "
            f"{stats['h0'][1]:.3f} us, largest difference inside a bit {stats['d']:.3f} us, preamble at least "
            f"{stats['pre']} bits")


@cocotb.test(skip=GL)
async def test_dcc_words(dut):
    """Memory words of fw/dcc.s."""
    size = assemble().size
    dut._log.info(f"fw/dcc.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_dcc_model(dut):
    """fw/dcc.s in the reference model against the S-9.1/S-9.2 decoder model: five packets, a host that is late between
    packets and one that is late inside a packet; negative controls for the half-bit times and the preamble."""
    log = dut._log
    bench, m = model_run(PACKETS)
    got, stats = check(bench.changes("ab"), m.cycle, PACKETS, "model")
    ff.assert_status_clean(m, [0])
    log.info(f"model: {summary(got, stats)}")
    # host late between two packets: the preamble grows, both packets correct
    two = PACKETS[:2]
    bench, m = model_run(two, steps=[(host_words(two[:1]), 1_200_000), (host_words(two[1:]), run_cycles(two))])
    got, stats = check(bench.changes("ab"), m.cycle, two, "model late between packets")
    ff.assert_status_clean(m, [0])
    log.info(f"model late between packets: {summary(got, stats)}; preambles {[p['preamble'] for p in got]}")
    assert max(p["preamble"] for p in got) > 16
    # host late inside a packet: the cut packet must be rejected by the decoder (error byte does not match)
    ext = PACKETS[4]
    words = host_words([ext])
    bench, m = model_run([ext], steps=[(words[:2], 500_000), (words[2:], run_cycles([ext]))])
    got, errors, _ = dcc_decode(bench.changes("ab"), m.cycle)
    ff.assert_status_clean(m, [0])
    log.info(f"model late inside a packet: signal faults {errors[:2]}; packets "
             f"{[(' '.join(f'{b:02X}' for b in p['bytes']), p['ok']) for p in got]}")
    assert not errors and got and got[0]["bytes"] == ext[:2] and not got[0]["ok"], "the cut packet is not rejected"
    # Negative controls: the decoder model must catch a station beyond each limit of S-9.1 Table 2.1 and S-9.2 A.
    for equ, want in ((dict(H1=3720), "neither t1"), (dict(H0=2820), "neither t1"), (dict(PRE=12), "at least 14")):
        bench, m = model_run(PACKETS[:2], **equ)
        _, errors, _ = dcc_decode(bench.changes("ab"), m.cycle)
        log.info(f"control {equ}: {errors[0] if errors else 'no fault found'}")
        assert errors and want in errors[0], f"control {equ}: expected a fault with '{want}', got {errors[:2]}"


@cocotb.test()
async def test_dcc_rtl(dut):
    """fw/dcc.s on the chip at 60 MHz against the S-9.1/S-9.2 decoder model: the S-9.2 Figure 1 packet and the idle
    packet, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl dcc"
    packets = PACKETS[:2]
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_A: 0, P_B: 0}), label=label)
    bench.watch("ab", watch_ab)
    bench.send(0, *host_words(packets))
    total = rf.HOST_START + run_cycles(packets)
    # waveform around the first packet start bit (tb.fst), for review; the Verilator coverage build has no dump and
    # no dump_off (test/tb.v), so there the test runs without it
    if hasattr(dut, "dump_off"):
        await bench.run(100_000)
        dut.dump_off.value = 0
        await bench.run(40_000)
        dut.dump_off.value = 1
        ch = bench.changes("ab")
        log.info(f"{label}: waveform window model cycles 100000 to {bench.m.cycle}, simulation time about "
                 f"{get_sim_time(unit='ps') - 40_000 * rf.CLK_PS} ps at its start; transitions in it at model cycles "
                 f"{[c for c, _ in ch if 100_000 <= c < bench.m.cycle][:6]}")
    await bench.run(total - bench.m.cycle)
    await bench.finish(log)
    got, stats = check(bench.changes("ab"), bench.m.cycle, packets, label)
    log.info(f"{label}: {summary(got, stats)}")

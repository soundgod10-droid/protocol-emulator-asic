# SPDX-License-Identifier: Apache-2.0
"""S.BUS transmitter and receiver firmware (fw/sbus_tx.s, fw/sbus_rx.s) against independent S.BUS line models, in the
reference model and on the RTL at 60 MHz.

What it does:
  The line models are written from the S.BUS description of the RPG Quadrotor Control wiki (page SBUS-Protocol, the
  description that the libsigrokdecode sbus_futaba decoder's documentation names; Futaba publishes no document):
  serial at 100,000 bit/s, 8E2 (start bit, 8 data bits LSB first, even parity bit, 2 stop bits), voltage levels
  inverted (a "0" bit is high, the idle line low); a message of 25 bytes (3 ms): header 0x0F, 16 channels of 11 bits
  in 22 bytes from bit 0 of channel 1 on, the flag byte [0 0 0 0 failsafe frame_lost ch18 ch17], footer 0x00.
  They do not use the firmware, the assembler or the processor model.
    - sbus_receive reads the transmitter's pin: frames are separated by an idle line; each byte is read in the middle
      of its bits at the nominal 10 us from its start edge; the start bit, the even parity and both stop bits are
      checked; every edge inside a byte must lie at k bit times from the start edge within 1 % (the description
      gives the rate, not a tolerance: 1 % is this test's assumption); the bytes of a frame follow back to back, a
      frame is 25 bytes and 3.0 ms long, starts with 0x0F, ends with 0x00 and has flag bits 7..4 zero; frames are
      separated by more than two byte times of idle line, so that a receiver finds their start.
    - sbus_line makes the line of a sender for the receiver firmware, with a chosen bit time and faults (parity bit,
      footer, a short frame).
  The transmitter tests send frames with values over the whole 11-bit range and all flag combinations, also with a host
  that gives the words slowly; negative controls change the firmware's bit time and frame gap, and the receiver model
  must report each. The receiver tests send good frames at the nominal and at +/-2 % bit time, and frames with a parity
  error, a wrong footer and a missing byte, which the firmware must drop. The RTL tests run both programs on the chip in
  lockstep with the reference model; the same tests run on the gate-level netlist (gl_fw workflow).

Why:
  Third row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012), with both roles of the row.

Conditions: logic simulation at 60 MHz with ideal pins; the inverting level shifter of a real S.BUS line is not
simulated (the chip pin carries the inverted logic level directly).
"""

import bisect
import os
import random
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
P_TX = 4  # SBUS_TX = uio[4], with a pull-down on the board (released pin: low, the idle level)
UI_RX = 3  # SBUS_RX = ui_in[3]


def tx_level(uo, uio, oe):
    """Level of the SBUS_TX line: the pin's value when driven, the pull-down's 0 when released."""
    return (uio >> P_TX) & 1 if (oe >> P_TX) & 1 else 0

# RPG Quadrotor Control wiki, SBUS-Protocol: 100,000 bit/s, 8E2, inverted, 25 bytes, header 0x0F, footer 0x00.
BIT_US = 10.0
NBYTES = 25
FRAME_US = 3000.0  # "takes 3ms to be transmitted"
TOL = 0.01  # edge position tolerance inside a byte: this test's assumption (the description gives no tolerance)


def pack(channels, flags):
    """The 25 bytes of a frame: header, 16 channels x 11 bits from bit 0 of channel 1 on, flag byte, footer."""
    stream = 0
    for k, ch in enumerate(channels):
        stream |= (ch & 0x7FF) << (11 * k)
    return [0x0F] + [(stream >> (8 * i)) & 0xFF for i in range(22)] + [flags & 0x0F, 0x00]


def unpack(data22):
    stream = sum(b << (8 * i) for i, b in enumerate(data22))
    return [(stream >> (11 * k)) & 0x7FF for k in range(16)]


def byte_bits(b):
    """Logical bits of one byte: start 0, data LSB first, even parity, two stop bits 1."""
    d = [(b >> i) & 1 for i in range(8)]
    return [0] + d + [sum(d) & 1] + [1, 1]


def sbus_line(frames, bit, t0, gap, parity_flip=()):
    """Line changes [(cycle, level)] of a sender: idle low, inverted bits; frames [[bytes]], each after `gap` cycles of
    idle line (the first at t0); bit = bit time in cycles (float allowed); parity_flip: {(frame, byte)} with a wrong
    parity bit."""
    changes = [(0, 0)]
    t = float(t0)
    for f, data in enumerate(frames):
        for i, b in enumerate(data):
            bits = byte_bits(b)
            if (f, i) in parity_flip:
                bits[9] ^= 1
            for k, lb in enumerate(bits):
                lvl = 1 - lb
                if lvl != changes[-1][1]:
                    changes.append((round(t + k * bit), lvl))
            t += 12 * bit
        t += gap
    return changes, round(t)


def sbus_receive(changes, end, us=F_MHZ):
    """S.BUS receiver from the description (not from the firmware): returns (frames, errors, stats); frames =
    [{"t": start cycle, "bytes": [...], "end": cycle after the last stop bit}]; stats: shortest idle between frames
    and the frame durations in us."""
    errors, frames = [], []
    stats = dict(gap=None, dur=[])
    if not changes or changes[0][1] != 0:
        return frames, ["the line does not start idle (low)"], stats
    times = [c for c, _ in changes]

    def level(cycle):
        return changes[bisect.bisect_right(times, cycle) - 1][1]

    bit = BIT_US * us
    cur, last_end, i = None, None, 1
    while i < len(changes):
        c, v = changes[i]
        if v != 1:  # a start bit is a rising edge (inverted line)
            i += 1
            continue
        if cur is None or c - last_end > bit:
            if cur is not None:
                frames.append(cur)
                gap = (c - last_end) / us
                stats["gap"] = gap if stats["gap"] is None else min(stats["gap"], gap)
                if gap <= 2 * 12 * BIT_US:
                    errors.append(f"cycle {c}: only {gap:.1f} us of idle line between frames (two bytes: 240 us)")
            cur = dict(t=c, bytes=[], end=None)
        elif c < last_end:
            errors.append(f"cycle {c}: start bit inside the byte before")
        bits = [1 - level(int(c + (k + 0.5) * bit)) for k in range(12)]
        if bits[0] != 0:
            errors.append(f"cycle {c}: start bit not high in its middle")
        if bits[10] != 1 or bits[11] != 1:
            errors.append(f"cycle {c}: a stop bit is not low (8E2)")
        if sum(bits[1:10]) % 2:
            errors.append(f"cycle {c}: parity error (even parity)")
        j = i + 1
        while j < len(changes) and changes[j][0] < c + 11.5 * bit:
            d = (changes[j][0] - c) / bit
            k = round(d)
            if k == 0 or abs(d / k - 1) > TOL:
                errors.append(f"cycle {changes[j][0]}: edge {d * BIT_US:.3f} us after the start edge, not at k bit "
                              f"times of 10 us within 1 %")
            j += 1
        cur["bytes"].append(sum(bits[1 + k] << k for k in range(8)))
        last_end = c + 12 * bit
        cur["end"] = last_end
        i = j
    if cur is not None:
        frames.append(cur)
    for f in frames:
        dur = (f["end"] - f["t"]) / us
        stats["dur"].append(dur)
        b = f["bytes"]
        if len(b) != NBYTES:
            errors.append(f"cycle {f['t']}: frame of {len(b)} bytes (25)")
        elif b[0] != 0x0F or b[24] != 0x00 or b[23] & 0xF0:
            errors.append(f"cycle {f['t']}: header 0x{b[0]:02X}, flags 0x{b[23]:02X}, footer 0x{b[24]:02X}")
        elif abs(dur - FRAME_US) > TOL * FRAME_US:
            errors.append(f"cycle {f['t']}: frame of {dur:.1f} us (3 ms)")
    return frames, errors, stats


# ----- frames -----

def frames_list(seed=7):
    rng = random.Random(seed)
    return [
        ([192, 1792, 992, 0, 2047, 1, 1024, 333, 1500, 172, 1811, 700, 1234, 1999, 64, 1000], 0b0001),
        ([rng.randrange(2048) for _ in range(16)], 0b1100),
        ([0] * 16, 0b0000),
        ([2047] * 16, 0b0011),
    ]


FRAMES = frames_list()


def tx_words(frames):
    out = []
    for ch, fl in frames:
        out += list(ch) + [fl]
    return out


def tx_cycles(frames):
    return len(frames) * (3000 + 4000 + 200) * F_MHZ + 20_000


def tx_check(changes, end, frames, label):
    got, errors, stats = sbus_receive(changes, end)
    assert not errors, f"{label}: {errors[:3]}"
    want = [pack(ch, fl) for ch, fl in frames]
    assert [f["bytes"] for f in got] == want, f"{label}: {len(got)} frames, first {got[0]['bytes'][:6] if got else None}"
    return got, stats


def tx_summary(got, stats):
    return (f"{len(got)} frames correct (channels and flags as sent), frame {min(stats['dur']):.3f} to "
            f"{max(stats['dur']):.3f} us, shortest idle between frames {stats['gap']:.1f} us")


def assemble(name, **equ):
    return ff.assemble(name, **equ)


def tx_model(frames, slow=False, **equ):
    m = pe_model.PeModel(assemble("sbus_tx.s", **equ).words, owner=ff.owners({P_TX: 0}))
    bench = ff.Bench(m)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    words = tx_words(frames)
    if slow:  # two words every 1,000 cycles
        for k in range(0, len(words), 2):
            bench.send(0, *words[k:k + 2])
            bench.run(1000)
    else:
        bench.send(0, *words)
    bench.run(tx_cycles(frames))
    return bench, m


# receiver: frames with faults; the good ones give 17 host words each
def rx_case(bit_scale=1.0, gap_us=1000):
    good = [pack(ch, fl) for ch, fl in FRAMES]
    bad_footer = list(good[1])
    bad_footer[24] = 0x01
    frames = [good[0], good[1], good[2][:24], bad_footer, good[3], good[2]]
    flips = {(1, 7)}  # frame 1 has a parity error in byte 7
    # frames 1 (parity error), 2 (24 bytes) and 3 (footer 0x01) are dropped; frames 0, 4 and 5 give their words
    want = tx_words([FRAMES[0]]) + tx_words([FRAMES[3]]) + tx_words([FRAMES[2]])
    return frames, flips, want, BIT_US * F_MHZ * bit_scale, gap_us * F_MHZ


def rx_model(bit_scale=1.0):
    frames, flips, want, bit, gap = rx_case(bit_scale)
    line, end = sbus_line(frames, bit, 30_000, gap, parity_flip=flips)
    m = pe_model.PeModel(assemble("sbus_rx.s").words)
    bench = ff.Bench(m, inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << UI_RX, 0))
    bench.run(end + 30_000)
    return bench, m, want, line, end


@cocotb.test(skip=GL)
async def test_sbus_words(dut):
    """Memory words of fw/sbus_tx.s and fw/sbus_rx.s (both fit together)."""
    tx, rx = assemble("sbus_tx.s").size, assemble("sbus_rx.s").size
    dut._log.info(f"fw/sbus_tx.s: {tx} words, fw/sbus_rx.s: {rx} words; together {tx + rx} of {pe_model.PROG_WORDS}")
    assert tx + rx <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_sbus_model(dut):
    """fw/sbus_tx.s and fw/sbus_rx.s in the reference model against the S.BUS line models."""
    log = dut._log
    # the line models agree with each other: the sender model's line, read by the receiver model
    line, end = sbus_line([pack(ch, fl) for ch, fl in FRAMES], BIT_US * F_MHZ, 1000, 4000 * F_MHZ)
    got, errors, _ = sbus_receive(line, end + 1000)
    assert not errors and [f["bytes"] for f in got] == [pack(ch, fl) for ch, fl in FRAMES], errors[:2]
    assert [unpack(f["bytes"][1:23]) for f in got] == [ch for ch, _ in FRAMES]
    # transmitter
    bench, m = tx_model(FRAMES)
    got, stats = tx_check(bench.changes("tx"), m.cycle, FRAMES, "model tx")
    ff.assert_status_clean(m, [0])
    log.info(f"model tx: {tx_summary(got, stats)}")
    bench, m = tx_model(FRAMES[:2], slow=True)
    got, stats = tx_check(bench.changes("tx"), m.cycle, FRAMES[:2], "model tx slow host")
    ff.assert_status_clean(m, [0])
    log.info(f"model tx, host gives 2 words every 1,000 cycles: {tx_summary(got, stats)}")
    for equ, want in ((dict(BIT=612), "within 1 %"), (dict(GAPN=1), "idle line between frames")):
        bench, m = tx_model(FRAMES[:2], **equ)
        _, errors, _ = sbus_receive(bench.changes("tx"), m.cycle)
        log.info(f"control tx {equ}: {errors[0] if errors else 'no fault found'}")
        assert errors and want in errors[0], f"control {equ}: expected a fault with '{want}', got {errors[:2]}"
    # receiver
    for scale in (1.0, 0.98, 1.02):
        bench, m, want, _, _ = rx_model(scale)
        ff.assert_status_clean(m, [0])
        got = bench.words(0)
        log.info(f"model rx, sender bit time x {scale}: {len(got) // 17} frames of 17 words (6 sent: parity error, "
                 f"short frame and wrong footer dropped)")
        assert got == want, f"model rx x {scale}: words {got[:20]}"


@cocotb.test()
async def test_sbus_tx_rtl(dut):
    """fw/sbus_tx.s on the chip at 60 MHz against the S.BUS receiver model: two frames, in lockstep with the reference
    model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl sbus_tx"
    frames = FRAMES[:2]
    bench = rf.RtlBench(dut, assemble("sbus_tx.s").words, owner=ff.owners({P_TX: 0}), label=label)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    bench.send(0, *tx_words(frames))
    await bench.run(rf.HOST_START + tx_cycles(frames))
    await bench.finish(log)
    got, stats = tx_check(bench.changes("tx"), bench.m.cycle, frames, label)
    log.info(f"{label}: {tx_summary(got, stats)}")


@cocotb.test()
async def test_sbus_rx_rtl(dut):
    """fw/sbus_rx.s on the chip at 60 MHz against the S.BUS sender model: good frames, a parity error, a short frame and a
    wrong footer (dropped), in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl sbus_rx"
    frames, flips, want, bit, gap = rx_case()
    line, end = sbus_line(frames, bit, rf.HOST_START + 30_000, gap, parity_flip=flips)
    bench = rf.RtlBench(dut, assemble("sbus_rx.s").words,
                        inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << UI_RX, 0), label=label)
    # waveform of the start of the first frame (tb.fst), for review; the Verilator coverage build has no dump and
    # no dump_off (test/tb.v), so there the test runs without it
    if hasattr(dut, "dump_off"):
        await bench.run(rf.HOST_START + 29_000)
        dut.dump_off.value = 0
        await bench.run(12_000)
        dut.dump_off.value = 1
    await bench.run(end + 30_000 - bench.m.cycle)
    await bench.collect(0, len(want))
    await bench.finish(log)
    got = bench.words(0)
    assert got == want, f"{label}: words {got[:20]}"
    log.info(f"{label}: {len(got) // 17} frames of 17 words as sent (6 frames on the line: the one with a parity error, "
             f"the short one and the one with a wrong footer dropped); first frame channels {got[:16]}, flags {got[16]}")

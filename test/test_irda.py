# SPDX-License-Identifier: Apache-2.0
"""IrDA SIR transmitter and receiver firmware (fw/irda_tx.s, fw/irda_rx.s) against independent IrDA SIR line models, in
the reference model and on the RTL at 60 MHz.

What it does:
  The line models are written from the Vishay application note "IrDA, Physical Layer" (document 82513, Rev. 1.5,
  12 August 2021), which reproduces the SIR part of the IrDA Serial Infrared Physical Layer Specification IrPHY 1.4:
  a UART frame (start bit, 8 data bits LSB first, stop bit) in which each 0 bit is one light pulse and each 1 bit none
  (RZI, Fig. 2); Table 2: rate tolerance 0.87 %, pulse duration at least 1.41 us, nominal 3/16 of the bit time, at most
  the Table 2 maximum (2.23 us at 115.2 kbit/s, 22.13 us at 9.6 kbit/s, 88.55 us at 2.4 kbit/s); Table 3: edge jitter
  at most 6.5 % of the nominal pulse duration. They do not use the firmware, the assembler or the processor model.
    - irda_receive reads the transmitter's pin (1 = LED on): each pulse must last within the Table 2 limits; a pulse
      when no frame is open is a start bit at s; bit k is 0 if a pulse starts in the window s + (k +/- 0.5) B; every
      pulse start must lie at k nominal bit times from s within the rate tolerance plus the edge jitter; the stop bit
      window must have no pulse.
    - irda_line makes the RXD line of a sender (low pulses) with a chosen rate, pulse width and pulse position in the
      cell (at its start, or centred), with an optional pulse in the stop bit window (framing error).
  The transmitter tests send bytes at 115.2, 9.6 and 2.4 kbit/s; negative controls give a bit time outside the rate
  tolerance and a pulse shorter than 1.41 us. The receiver tests send bytes at the nominal rate and at +/-0.87 %, with
  the shortest and the longest pulse, at the start and in the centre of the cell, and a framing error; a sender 6 %
  fast must give wrong bytes (control). The RTL tests run both programs on the chip in lockstep with the reference
  model; the same tests run on the gate-level netlist (gl_fw workflow).

Why:
  Seventh row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012), with both roles of the row.

Conditions: logic simulation at 60 MHz with ideal pins; the IrDA transceiver (LED driver, photodiode, its rise time
and its receiver latency) is not simulated: the TXD pin level is the LED state and the RXD line is given directly.
The IrPHY 1.4 copy that the candidate list names could not be fetched (its server's certificate chain is incomplete),
so the Vishay note is the source; it states Table 2 as the IrDA standard's values.
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
P_TX = 4  # IR_TXD = uio[4], pull-down on the board (released: LED off)
UI_RX = 3  # IR_RXD = ui_in[3]

# Vishay 82513 Table 2 (IrPHY 1.4): rate -> (nominal pulse us, maximum pulse us); minimum pulse 1.41 us; rate
# tolerance 0.87 %; Table 3 edge jitter 6.5 % of the nominal pulse.
TABLE2 = {2400: (78.13, 88.55), 9600: (19.53, 22.13), 19200: (9.77, 11.07), 38400: (4.88, 5.96),
          57600: (3.26, 4.34), 115200: (1.63, 2.23)}
P_MIN, RATE_TOL, JITTER = 1.41, 0.0087, 0.065


def tx_level(uo, uio, oe):
    """LED state: the pin's value when driven, the pull-down's 0 when released."""
    return (uio >> P_TX) & 1 if (oe >> P_TX) & 1 else 0


def frame_bits(b):
    return [0] + [(b >> i) & 1 for i in range(8)] + [1]


def irda_receive(changes, end, rate, us=F_MHZ):
    """IrDA SIR receiver from Table 2 (not from the firmware): returns (bytes, errors, stats)."""
    errors, out = [], []
    nom, pmax = TABLE2[rate]
    b = us * 1e6 / rate / 1e6 * 1e0  # nominal bit time in cycles
    b = F_MHZ * 1e6 / rate
    stats = dict(pw=[], dev=0.0)
    pulses = []
    for i in range(1, len(changes)):
        c, v = changes[i]
        if v == 1:
            fall = changes[i + 1][0] if i + 1 < len(changes) else end
            w = (fall - c) / us
            pulses.append(c)
            stats["pw"].append(w)
            if not P_MIN <= w <= pmax:
                errors.append(f"cycle {c}: pulse of {w:.3f} us (Table 2: {P_MIN} to {pmax} us)")
    k = 0
    while k < len(pulses):
        s = pulses[k]
        bits, k = [0], k + 1
        for n in range(1, 10):
            lo, hi = s + (n - 0.5) * b, s + (n + 0.5) * b
            j = bisect.bisect_left(pulses, lo)
            inside = [p for p in pulses[j:] if p < hi]
            if inside:
                dev = abs(inside[0] - s - n * b)
                stats["dev"] = max(stats["dev"], dev / us)
                if dev > n * RATE_TOL * b + JITTER * nom * us + 1:
                    errors.append(f"cycle {inside[0]}: pulse {dev / us:.3f} us from k bit times after the start pulse "
                                  f"(rate tolerance 0.87 % plus 6.5 % edge jitter)")
                k = max(k, pulses.index(inside[-1]) + 1)
            bits.append(0 if inside else 1)
        if bits[9] != 1:
            errors.append(f"cycle {s}: pulse in the stop bit window (framing error)")
        out.append(sum(bits[1 + i] << i for i in range(8)))
    return out, errors, stats


def irda_line(data, bit, pulse, t0, offset=0.0, stop_pulse=(), gap=0):
    """RXD line of a sender [(cycle, level)]: idle high, a low pulse of `pulse` cycles for each 0 bit, `offset` (0 to 1
    of the free part of the cell) after the cell start; stop_pulse: frame numbers with a pulse in the stop bit cell."""
    changes = [(0, 1)]
    t = float(t0)
    for f, byte in enumerate(data):
        bits = frame_bits(byte)
        if f in stop_pulse:
            bits[9] = 0
        for k, v in enumerate(bits):
            if v == 0:
                a = round(t + k * bit + offset * (bit - pulse))
                changes += [(a, 0), (a + round(pulse), 1)]
        t += 10 * bit + gap
    return changes, round(t)


DATA = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80, 0xC3, 0x5A]
RATES = {115200: (521, 98), 9600: (6250, 1172), 2400: (25000, 4688)}  # bit time, pulse (3/16) in cycles


def assemble(name, **equ):
    return ff.assemble(name, **equ)


def tx_model(rate, data=DATA, b=None, p=None):
    b0, p0 = RATES[rate]
    m = pe_model.PeModel(assemble("irda_tx.s").words, owner=ff.owners({P_TX: 0}))
    bench = ff.Bench(m)
    bench.watch("tx", tx_level)
    bench.send(0, b or b0, p or p0, *data)
    bench.run((len(data) * 10 + 4) * (b or b0) + 2000)
    return bench, m


def tx_check(bench, m, rate, data, label):
    got, errors, stats = irda_receive(bench.changes("tx"), m.cycle, rate)
    assert not errors, f"{label}: {errors[:3]}"
    assert got == data, f"{label}: bytes {[hex(x) for x in got]}"
    return stats


def rx_case(rate=115200, scale=1.0, pulse_us=None, offset=0.0, t0=2000):
    b = F_MHZ * 1e6 / rate * scale
    pulse = (pulse_us if pulse_us else TABLE2[rate][0]) * F_MHZ
    data = DATA + [0x3C]
    line, end = irda_line(data, b, pulse, t0, offset=offset, stop_pulse={len(data) - 1})
    want = list(DATA) + [0x3C | 0x100]
    return line, end, want


def rx_model(**kw):
    line, end, want = rx_case(**kw)
    m = pe_model.PeModel(assemble("irda_rx.s").words)
    bench = ff.Bench(m, inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << UI_RX, 0))
    bench.send(0, RATES[kw.get("rate", 115200)][0])
    bench.run(end + 2000)
    return bench, m, want


@cocotb.test(skip=GL)
async def test_irda_words(dut):
    """Memory words of fw/irda_tx.s and fw/irda_rx.s (both fit together)."""
    tx, rx = assemble("irda_tx.s").size, assemble("irda_rx.s").size
    dut._log.info(f"fw/irda_tx.s: {tx} words, fw/irda_rx.s: {rx} words; together {tx + rx} of {pe_model.PROG_WORDS}")
    assert tx + rx <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_irda_model(dut):
    """fw/irda_tx.s and fw/irda_rx.s in the reference model against the IrDA SIR line models."""
    log = dut._log
    # the line models agree: the sender model's pulses (inverted to LED levels) read by the receiver model
    line, end = irda_line(DATA, 521, 98, 1000)
    got, errors, _ = irda_receive([(c, 1 - v) for c, v in line], end + 1000, 115200)
    assert not errors and got == DATA, (errors[:2], got)
    # transmitter
    for rate in (115200, 9600, 2400):
        bench, m = tx_model(rate)
        stats = tx_check(bench, m, rate, DATA, f"model tx {rate}")
        ff.assert_status_clean(m, [0])
        b = RATES[rate][0]
        log.info(f"model tx {rate} bit/s: {len(DATA)} bytes correct; bit time {b} cycles = {b / F_MHZ:.3f} us "
                 f"(rate error {(F_MHZ * 1e6 / rate / b - 1) * 100:+.3f} %), pulses {min(stats['pw']):.3f} to "
                 f"{max(stats['pw']):.3f} us, largest pulse offset from k bit times {stats['dev']:.3f} us")
    for kw, want in ((dict(b=530), "rate tolerance"), (dict(p=80), "Table 2")):
        bench, m = tx_model(115200, data=DATA[:3], **kw)
        _, errors, _ = irda_receive(bench.changes("tx"), m.cycle, 115200)
        log.info(f"control tx {kw}: {errors[0] if errors else 'no fault found'}")
        assert errors and want in errors[0], f"control {kw}: expected '{want}', got {errors[:2]}"
    # receiver
    for kw in (dict(), dict(scale=1 - RATE_TOL), dict(scale=1 + RATE_TOL), dict(pulse_us=P_MIN),
               dict(pulse_us=2.23), dict(offset=0.5), dict(scale=1 + RATE_TOL, pulse_us=2.23, offset=1.0),
               dict(rate=9600, offset=0.5), dict(rate=2400, pulse_us=P_MIN)):
        bench, m, want = rx_model(**kw)
        ff.assert_status_clean(m, [0])
        log.info(f"model rx {kw or 'nominal 115.2 kbit/s'}: words {[hex(w) for w in bench.words(0)]}")
        assert bench.words(0) == want, f"model rx {kw}: words {[hex(w) for w in bench.words(0)]}"
    bench, m, want = rx_model(scale=0.94)
    log.info(f"control rx (sender 6 % fast): words {[hex(w) for w in bench.words(0)]}")
    assert bench.words(0) != want, "control rx: a sender 6 % fast was received correctly"


@cocotb.test()
async def test_irda_tx_rtl(dut):
    """fw/irda_tx.s on the chip at 60 MHz against the IrDA SIR receiver model: 115.2 kbit/s, in lockstep with the
    reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl irda_tx"
    b, p = RATES[115200]
    bench = rf.RtlBench(dut, assemble("irda_tx.s").words, owner=ff.owners({P_TX: 0}), label=label)
    bench.watch("tx", tx_level)
    bench.send(0, b, p, *DATA)
    if hasattr(dut, "dump_off"):  # waveform of the first two frames (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 25 * b)
        dut.dump_off.value = 1
        first = next(c for c, v in bench.changes("tx") if v == 1)
        log.info(f"{label}: first pulse at model cycle {first} (the start bit of 0x00, then pulses every {b} cycles)")
    await bench.run(rf.HOST_START + (len(DATA) * 10 + 4) * b - bench.m.cycle)
    await bench.finish(log)
    got, errors, stats = irda_receive(bench.changes("tx"), bench.m.cycle, 115200)
    assert not errors and got == DATA, f"{label}: {errors[:3]} {got}"
    log.info(f"{label}: {len(got)} bytes correct, pulses {min(stats['pw']):.3f} to {max(stats['pw']):.3f} us, largest "
             f"offset from k bit times {stats['dev']:.3f} us")


@cocotb.test()
async def test_irda_rx_rtl(dut):
    """fw/irda_rx.s on the chip at 60 MHz against the IrDA SIR sender model: 115.2 kbit/s, sender 0.87 % slow with the
    shortest pulses in the cell centre, and a framing error, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl irda_rx"
    line, end, want = rx_case(scale=1 + RATE_TOL, pulse_us=P_MIN, offset=0.5, t0=rf.HOST_START + 3000)
    bench = rf.RtlBench(dut, assemble("irda_rx.s").words,
                        inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << UI_RX, 0), label=label)
    bench.send(0, RATES[115200][0])
    await bench.run(end + 2000)
    await bench.collect(0, len(want))
    await bench.finish(log)
    got = bench.words(0)
    assert got == want, f"{label}: words {[hex(w) for w in got]}"
    log.info(f"{label}: {[hex(w) for w in got]} (the last with the framing error flag)")

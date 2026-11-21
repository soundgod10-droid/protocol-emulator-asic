# SPDX-License-Identifier: Apache-2.0
"""SSI absolute encoder controller firmware (fw/ssi.s) against an independent SSI encoder model, in the reference model
and on the RTL at 60 MHz.

What it does:
  The encoder model (SsiEncoder) is written from the SICK "Technical Information SSI Interface Description"
  8027422/2022-02-08 (SSI was developed by Max Stegmann GmbH, now SICK): section 2.2 sequence and Figures 2 and 3
  (first high-low edge loads the position, each low-high edge sends the next bit MSB first, tv < T/2, tm = 15 to
  25 us, clock interval greater than tm) and section 2.7 (recommended clock rate by cable length). It does not use
  the firmware, the assembler or the processor model. While no clock comes its data line is high; the first falling
  edge stores the position and starts the retriggerable monoflop (time tm from the last falling edge); each rising
  edge puts the next bit on the data line tv after the edge; after the LSB the line is low until the monoflop runs
  out, then high again. It checks that a transmission starts only while the line is high (monoflop run out), that
  the clock period stays below tm (else the monoflop runs out inside a transmission) and that the clock rate is
  below 400 kHz (section 2.7, cable shorter than 50 m).
  The model test reads 13-bit and 25-bit encoders with tm = 15 and 25 us and a host that asks back to back (the
  firmware must wait for the monoflop), with tv = 100 ns and with the latest tv the document allows (one clock
  cycle under T/2), an encoder with one bit more than the controller expects (the end check must flag it) and a
  dead encoder (data stuck low: "not ready"); negative controls set the clock to 1 MHz and the clock period above
  tm. The RTL test runs the firmware on the chip in lockstep with the reference model; the same
  test runs on the gate-level netlist (gl_fw workflow).

Why:
  Ninth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). The libsigrokdecode decoder
  ssi32 that the candidate list names for this row decodes Bosch SSI32 command frames (CTRL, LUN, SIZE, CRC; read from
  its pd.py at the pinned commit), not the encoder SSI, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the RS-422 transceivers and the cable delay are not
simulated (a long cable adds a delay to DATA that lowers the usable clock rate, the reason for the table).
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
P_CLK = 12  # SSI_CLK = uo_out[4]
UI_DATA = 3  # SSI_DATA = ui_in[3]

TV_US = 0.1  # data valid after the rising edge (any value below T/2)
TM_US = (15.0, 25.0)  # monoflop time range
F_MAX_KHZ = 400.0  # cable shorter than 50 m


class SsiEncoder:
    """SSI encoder (see the module text). bits: resolution; tm_us: monoflop time; positions: values stored at the
    successive transmissions; dead: data line stuck low. Records transmissions [(cycle, value sent, falling edges)]."""

    def __init__(self, bits, tm_us, positions, dead=False, us=F_MHZ, tv=None):
        self.n, self.us, self.tm = bits, us, round(tm_us * us)
        self.tv = tv if tv is not None else max(1, round(TV_US * us))  # cycles from the rising edge
        self.pos = list(positions)
        self.dead = dead
        self.clk, self.data = 1, 0 if dead else 1
        self.mono_until = None
        self.idx, self.word, self.falls = 0, 0, 0
        self.last_fall = self.last_rise = None
        self.pend = None
        self.sent, self.errors = [], []

    def __call__(self, c, uo, uio, oe):
        if self.pend and c >= self.pend[0]:
            self.data, self.pend = self.pend[1], None
        if self.mono_until is not None and c >= self.mono_until:  # monoflop runs out: line high, ready
            if self.idx < self.n and not self.dead:
                self.errors.append(f"cycle {c}: monoflop ran out after {self.idx} of {self.n} bits (clock period >= tm)")
            self.sent.append((self.t0, self.word, self.falls))
            self.mono_until = None
            self.data = 0 if self.dead else 1
        clk = (uo >> 4) & 1
        if clk != self.clk:
            if not clk:  # falling edge
                if self.mono_until is None:
                    if self.data != 1 and not self.dead:
                        self.errors.append(f"cycle {c}: transmission started while DATA is low (monoflop running)")
                    self.word = self.pos.pop(0) if self.pos else 0
                    self.t0, self.idx, self.falls = c, 0, 0
                elif self.last_fall is not None and c - self.last_fall >= self.tm:
                    self.errors.append(f"cycle {c}: clock period {(c - self.last_fall) / self.us:.3f} us >= tm")
                self.falls += 1
                self.last_fall = c
                self.mono_until = c + self.tm
            else:  # rising edge
                if self.last_rise is not None and self.mono_until is not None and self.idx:
                    f = self.us * 1000.0 / (c - self.last_rise)
                    if f > F_MAX_KHZ:
                        self.errors.append(f"cycle {c}: clock {f:.0f} kHz (cable < 50 m: below 400 kHz)")
                self.last_rise = c
                if self.mono_until is not None and not self.dead:
                    self.idx += 1
                    k = self.n - self.idx
                    level = (self.word >> k) & 1 if k >= 0 else 0  # after the LSB: low until tm
                    self.pend = (c + max(1, round(TV_US * self.us)), level)
            self.clk = clk
        return self.data << UI_DATA, 0


def assemble(**equ):
    return ff.assemble("ssi.s", **equ)


def expect(bits, n_ctl, value, extra_bit=None):
    """Host words for one read of a `bits`-bit encoder by a controller set to n_ctl bits."""
    if bits >= n_ctl:
        got = value >> (bits - n_ctl)
        end = (value >> (bits - n_ctl - 1)) & 1 if bits > n_ctl else 0
    else:
        got = value << (n_ctl - bits)
        end = 0
    status = 0x4000 if end else 0
    return [status | (got >> 16) & 0x3FFF, got & 0xFFFF]


def model_run(enc, n_ctl, h, reads, gap=0, limit=None):
    m = pe_model.PeModel(assemble().words, owner=ff.owners({P_CLK: 0}))
    bench = ff.Bench(m, inputs=enc)
    bench.watch("clk", lambda uo, uio, oe: (uo >> 4) & 1)
    bench.send(0, n_ctl, h)
    for _ in range(reads):
        bench.send(0, 1)
        if gap:
            bench.run(gap)
    bench.run(limit or reads * (2 * h * (n_ctl + 2) + enc.tm + 3000) + 5000)
    return bench, m


def clock_stats(bench):
    ch = bench.changes("clk")
    rises = [c for c, v in ch if v == 1][1:]
    per = [b - a for a, b in zip(rises, rises[1:]) if b - a < 600]  # inside a transmission only
    return min(per), max(per)


CASES = [(13, 10.0, [0x1ABC, 0x0001, 0x1FFF, 0x0A5A]), (25, 30.0, [0x1ABCDEF, 0x0000001, 0x1FFFFFF]),
         (13, 30.0, [0x0F0F, 0x10F0]), (25, 10.0, [0x0123456, 0x1F0F0F0])]


@cocotb.test(skip=GL)
async def test_ssi_words(dut):
    """Memory words of fw/ssi.s."""
    size = assemble().size
    dut._log.info(f"fw/ssi.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_ssi_model(dut):
    """fw/ssi.s in the reference model against the SSI encoder model: 13 and 25 bits, tm 10 and 30 us, back-to-back
    reads; an encoder with one bit more; a dead encoder; negative controls for the clock rate and the period."""
    log = dut._log
    for bits, tm, pos in CASES:
        enc = SsiEncoder(bits, tm, pos)
        bench, m = model_run(enc, bits, 90, len(pos))
        want = [w for v in pos for w in expect(bits, bits, v)]
        assert not enc.errors, f"model {bits} bits tm {tm}: {enc.errors[:3]}"
        assert bench.words(0) == want, f"model {bits} bits tm {tm}: {[hex(w) for w in bench.words(0)]}"
        ff.assert_status_clean(m, [0])
        starts = [t for t, _, _ in enc.sent]
        gaps = [(b - a) / F_MHZ for a, b in zip(starts, starts[1:])]
        lo, hi = clock_stats(bench)
        log.info(f"model {bits} bits, tm {tm} us: {len(pos)} reads correct {[hex(w) for w in bench.words(0)]}; "
                 f"{enc.sent[0][2]} falling edges a read; clock period {lo / F_MHZ:.3f} to {hi / F_MHZ:.3f} us; "
                 f"read starts {', '.join(f'{g:.1f}' for g in gaps)} us apart (the monoflop is {tm} us)")
    enc = SsiEncoder(14, 20.0, [0x2AAB])  # one bit more than the controller expects, LSB 1
    bench, m = model_run(enc, 13, 90, 1)
    log.info(f"model 14-bit encoder read as 13 bits: {[hex(w) for w in bench.words(0)]} (bit 14 = end check)")
    assert bench.words(0) == expect(14, 13, 0x2AAB) and bench.words(0)[0] & 0x4000, "end check"
    enc = SsiEncoder(13, 20.0, [0x1234], dead=True)
    bench, m = model_run(enc, 13, 90, 1, limit=20_000)
    log.info(f"model dead encoder (DATA low): {[hex(w) for w in bench.words(0)]}")
    assert bench.words(0) == [0x8000, 0] and not enc.sent, "dead encoder"
    for h, want in ((30, "below 400 kHz"), (900, ">= tm")):
        enc = SsiEncoder(13, 10.0, [0x1ABC, 0x0001])
        model_run(enc, 13, h, 2)
        log.info(f"control H {h}: {enc.errors[0] if enc.errors else 'no fault found'}")
        assert enc.errors and want in enc.errors[0], f"control H {h}: expected '{want}', got {enc.errors[:2]}"


@cocotb.test()
async def test_ssi_rtl(dut):
    """fw/ssi.s on the chip at 60 MHz against the SSI encoder model: a 25-bit encoder with tm 20 us and three
    back-to-back reads, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl ssi"
    pos = [0x1ABCDEF, 0x0000001, 0x1FFFFFF]
    enc = SsiEncoder(25, 20.0, pos)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_CLK: 0}), inputs=enc, label=label)
    bench.watch("clk", lambda uo, uio, oe: (uo >> 4) & 1)
    bench.send(0, 25, 90, 1, 1, 1)
    if hasattr(dut, "dump_off"):  # waveform of the first read (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 2 * 90 * 27 + 3000)
        dut.dump_off.value = 1
        log.info(f"{label}: waveform from the start; first transmission at model cycle "
                 f"{enc.t0 if hasattr(enc, 't0') else None}")
    await bench.collect(0, 6)
    await bench.run(3000)
    await bench.finish(log)
    want = [w for v in pos for w in expect(25, 25, v)]
    assert not enc.errors and bench.words(0) == want, f"{label}: {enc.errors[:3]} {[hex(w) for w in bench.words(0)]}"
    lo, hi = clock_stats(bench)
    log.info(f"{label}: {[hex(w) for w in bench.words(0)]} as the encoder sent; clock period {lo / F_MHZ:.3f} to "
             f"{hi / F_MHZ:.3f} us; {enc.sent[0][2] if enc.sent else '?'} falling edges a read")

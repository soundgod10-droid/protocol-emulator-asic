# SPDX-License-Identifier: Apache-2.0
"""TM1637 LED driver and key scan controller firmware (fw/tm1637.s) against an independent TM1637 model, in the
reference model and on the RTL at 60 MHz.

What it does:
  The chip model (Tm1637) is written from the Titan Micro TM1637 data sheet V2.4 (English): pin functions and the
  key scan table (page 2), display register addresses and interface interpretation (page 3), transfer figures and
  data command (page 4), address command and display control (page 5), timing (pages 9 and 10). It does not use
  the firmware, the assembler or the processor model. START is DIO falling while CLK is high, STOP is DIO rising
  while CLK is high; the chip takes DIO at each CLK rise, LSB first, pulls DIO low for the ACK from the falling
  edge of the 8th clock to the end of the 9th, and after the read-key command puts the key byte on DIO after each
  CLK fall. The first byte of a transfer is a command (B7 B6 = 01 data command, 11 address command, 10 display
  control); data bytes go to the display registers C0H to C5H with automatic or fixed address; an address of C6H or
  higher makes the chip ignore data. The chip's output delays are the data sheet maxima (tPZL 100 ns, tPLZ 300 ns).
  It checks the controller: CLK high and low at least PWCLK = 400 ns, the ninth (ACK) clock high at least tWAIT =
  1 us (the data sheet gives tWAIT as CLK rise to CLK fall; this reading applies it to the ACK clock), DIO stable
  tSETUP = 100 ns before each CLK rise and tHOLD = 100 ns after it, the clock at most 500 kHz and below 250 kHz
  while the key byte is read, no command bits that the data sheet does not allow, and the controller never pulls
  DIO low while the chip drives a key bit or an ACK.
  The model test writes the six display registers with automatic address, sets the display control, reads two key
  states (K2 with SG5 pressed, then no key), writes one register with fixed address and one to C6H (ignored), and
  runs the same words with no chip on the bus (every byte without ACK, key byte 0xFF). Negative controls: a 500 kHz
  clock (too fast for the key read), a setup time of 5 cycles and a CLK high time of 367 ns. The RTL test runs the
  firmware on the chip in lockstep with the reference model; the same test runs on the gate-level netlist (gl_fw
  workflow).

Why:
  Tenth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode decoder at
  the pinned commit decodes this bus, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins; the 10 kOhm pull-ups and the 100 pF filter capacitors of
the data sheet's drawing are not simulated (their RC time, 1 us, lowers the usable clock rate; the firmware's CLK
low and high times of 2.08 us leave room for it).
"""

import math
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
P_CLK, P_DIO = 0, 1  # uio[0], uio[1]

# Data sheet V2.4, timing (pages 9 and 10)
PW_CLK_US, T_SETUP_US, T_HOLD_US, T_WAIT_US = 0.4, 0.1, 0.1, 1.0
F_MAX_KHZ, F_KEY_KHZ = 500.0, 250.0
T_PZL_US, T_PLZ_US = 0.1, 0.3
# Key scan data (page 2), bits written in the order b0 to b7 (the data sheet: "1110_1111 stands for 0xF7")
KEYS = {1: "1110_1111 0110_1111 1010_1111 0010_1111 1100_1111 0100_1111 1000_1111 0000_1111",
        2: "1111_0111 0111_0111 1011_0111 0011_0111 1101_0111 0101_0111 1001_0111 0001_0111"}
WIDTHS = ["1/16", "2/16", "4/16", "10/16", "11/16", "12/16", "13/16", "14/16"]  # display control B2..B0 (page 5)


def key_code(key):
    """Key byte for key = (k, sg) pressed, or None (no key: 1111_1111)."""
    if key is None:
        return 0xFF
    bits = KEYS[key[0]].split()[key[1] - 1].replace("_", "")
    return int(bits[::-1], 2)


class Tm1637:
    """TM1637 on the bus (see the module text). keys: key states for the successive key reads ((k, sg) or None);
    absent: no chip on the bus (pull-ups only)."""

    def __init__(self, us=F_MHZ, keys=(), absent=False):
        self.cyc = lambda u: int(math.ceil(u * us - 1e-9))
        self.us = us
        self.keys = list(keys)
        self.absent = absent
        self.clk = self.dio = self.ctl_dio = 1
        self.dev = 1  # chip's DIO output (1 = released)
        self.pend = None  # (cycle, level) of the chip's next DIO change
        self.t_dio = self.t_rise = self.t_fall = self.last_rise = None
        self.active = False
        self.k = 0  # rising edges in the current byte (9 = ACK clock)
        self.byte = 0
        self.first = True
        self.reading = False  # the current byte is a key byte (chip drives)
        self.read_next = False
        self.out = 0
        self.auto, self.addr, self.data_ok = True, 0, False
        self.disp = [0] * 6
        self.on, self.width = False, None
        self.sent, self.errors = [], []
        self.rises = []  # cycles of the CLK rises (for the waveform review)

    def _err(self, c, text):
        self.errors.append(f"cycle {c}: {text}")

    def _drive(self, c, level, delay_us):
        if not self.absent:
            self.pend = (c + self.cyc(delay_us), level)

    def _command(self, c, b):
        top = b >> 6
        if top == 1:  # data command
            if b & 0x30:
                self._err(c, f"data command 0x{b:02X}: B5 B4 not zero")
            if b & 0x08:
                self._err(c, f"data command 0x{b:02X}: test mode")
            if b & 3 in (1, 3):
                self._err(c, f"data command 0x{b:02X}: B1 B0 = {b & 3:02b} is not permitted")
            self.auto = not b & 4
            self.read_next = b & 3 == 2
        elif top == 3:  # address command
            if b & 0x30:
                self._err(c, f"address command 0x{b:02X}: B5 B4 not zero")
            self.addr, self.data_ok = b & 0x0F, True
        elif top == 2:  # display control
            if b & 0x30:
                self._err(c, f"display control 0x{b:02X}: B5 B4 not zero")
            self.on, self.width = bool(b & 8), WIDTHS[b & 7]
        else:
            self._err(c, f"0x{b:02X} is not a command (B7 B6 = 00)")

    def _data(self, c, b):
        if not self.data_ok:
            self._err(c, f"data byte 0x{b:02X} without an address command")
            return
        if self.addr < 6:
            self.disp[self.addr] = b
        if self.auto:
            self.addr += 1

    def __call__(self, c, uo, uio, oe):
        if self.pend and c >= self.pend[0]:
            self.dev, self.pend = self.pend[1], None
        clk = 0 if (oe >> P_CLK) & 1 and not (uio >> P_CLK) & 1 else 1
        ctl = 0 if (oe >> P_DIO) & 1 and not (uio >> P_DIO) & 1 else 1
        if ctl != self.ctl_dio:  # the controller changes DIO
            if self.clk and self.t_rise is not None and c - self.t_rise < self.cyc(T_HOLD_US):
                self._err(c, f"DIO changed {c - self.t_rise} cycles after the CLK rise (tHOLD 100 ns)")
            self.ctl_dio, self.t_dio = ctl, c
        dio = ctl & self.dev
        if self.clk and clk and dio != self.dio:  # DIO edge while CLK is high: START or STOP
            if dio == 0:
                self.active, self.first, self.k, self.byte = True, True, 0, 0
                self.reading = self.read_next = False
            else:
                if self.active and 0 < self.k < 9:
                    self._err(c, f"STOP after {self.k} clocks of a byte (the byte is not valid)")
                self.active = False
        self.dio = dio
        if clk != self.clk:
            if clk:  # CLK rise
                if self.active:
                    if self.t_fall is not None and c - self.t_fall < self.cyc(PW_CLK_US):
                        self._err(c, f"CLK low {(c - self.t_fall) / self.us * 1000:.0f} ns (PWCLK 400 ns)")
                    if self.t_dio is not None and c - self.t_dio < self.cyc(T_SETUP_US):
                        self._err(c, f"DIO changed {c - self.t_dio} cycles before the CLK rise (tSETUP 100 ns)")
                    if self.k and self.last_rise is not None:
                        f = self.us * 1000.0 / (c - self.last_rise)
                        if f > F_MAX_KHZ + 1e-9:
                            self._err(c, f"clock {f:.0f} kHz (Fmax 500 kHz)")
                        if self.reading and f >= F_KEY_KHZ:
                            self._err(c, f"key read clock {f:.0f} kHz (below 250 kHz)")
                    self.k += 1
                    if (self.reading or self.k == 9) and self.ctl_dio == 0 and not self.absent:
                        self._err(c, "the controller pulls DIO low while the chip drives it")
                    if self.k <= 8 and not self.reading:
                        self.byte |= dio << (self.k - 1)
                    self.rises.append(c)
                self.t_rise = self.last_rise = c
            else:  # CLK fall
                if self.active:
                    hi = c - self.t_rise
                    if hi < self.cyc(PW_CLK_US):
                        self._err(c, f"CLK high {hi / self.us * 1000:.0f} ns (PWCLK 400 ns)")
                    if self.k == 9 and hi < self.cyc(T_WAIT_US):
                        self._err(c, f"ACK clock high {hi / self.us * 1000:.0f} ns (tWAIT 1 us)")
                    if self.k == 8:
                        if not self.reading:
                            b = self.byte
                            if self.first:
                                self.first = False
                                self._command(c, b)
                            else:
                                self._data(c, b)
                        self._drive(c, 0, T_PZL_US)  # ACK
                    elif self.k == 9:
                        self.k, self.byte = 0, 0
                        if self.read_next:
                            self.read_next, self.reading = False, True
                            self.out = key_code(self.keys.pop(0) if self.keys else None)
                            self.sent.append(self.out)
                            self._drive(c, self.out & 1, T_PLZ_US)
                        else:
                            self.reading = False
                            self._drive(c, 1, T_PLZ_US)
                    elif self.reading:
                        self._drive(c, (self.out >> self.k) & 1, T_PLZ_US)
                self.t_fall = c
            self.clk = clk
        return 0, (clk << P_CLK) | (dio << P_DIO)


STOP, READ = 0x100, 0x200
DIGITS = [0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D]  # 0 to 5 on a seven-segment digit (a = bit 0)
WORDS = ([0x40 | STOP]                                     # data command: write, automatic address
         + [0xC0] + DIGITS[:-1] + [DIGITS[-1] | STOP]      # address C0H, six display bytes
         + [0x8A | STOP]                                   # display on, pulse width 4/16
         + [0x42, READ | STOP]                             # read key
         + [0x44 | STOP] + [0xC2, 0x7F | STOP]             # fixed address: C2H = 0x7F
         + [0xC6, 0x55 | STOP]                             # C6H: ignored
         + [0x40 | STOP] + [0x42, READ | STOP])            # read key again
KEYS_PRESSED = [(2, 5), None]


def expect(words, keys, absent=False):
    """Host words: the key byte of each read, and at each STOP the number of bytes of the transfer without ACK."""
    out, n, keys = [], 0, list(keys)
    for w in words:
        n += 1
        if w & READ:
            out.append(0xFF if absent else key_code(keys.pop(0) if keys else None))
        if w & STOP:
            out.append(n if absent else 0)
            n = 0
    return out


def assemble(**equ):
    return ff.assemble("tm1637.s", **equ)


def model_run(dev, words, cycles=60_000, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners({P_CLK: 0, P_DIO: 0}),
                         open_drain=ff.open_drain({P_CLK, P_DIO}))
    bench = ff.Bench(m, inputs=dev)
    bench.watch("clk", lambda uo, uio, oe: 0 if (oe >> P_CLK) & 1 and not (uio >> P_CLK) & 1 else 1)
    bench.send(0, *words)
    bench.run(cycles)
    return bench, m


def clock_stats(bench):
    ch = bench.changes("clk")
    hi = [b[0] - a[0] for a, b in zip(ch, ch[1:]) if a[1] == 1 and b[1] == 0 and a[0] > 0]
    lo = [b[0] - a[0] for a, b in zip(ch, ch[1:]) if a[1] == 0 and b[1] == 1]
    return min(lo), max(lo), min(hi), max(hi)


@cocotb.test(skip=GL)
async def test_tm1637_words(dut):
    """Memory words of fw/tm1637.s."""
    size = assemble().size
    dut._log.info(f"fw/tm1637.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_tm1637_model(dut):
    """fw/tm1637.s in the reference model against the TM1637 model: display writes with automatic and fixed address,
    display control, two key reads, an address beyond C5H; no chip on the bus; negative controls for the key-read
    clock, the setup time and the CLK high time."""
    log = dut._log
    dev = Tm1637(keys=KEYS_PRESSED)
    bench, m = model_run(dev, WORDS)
    want = expect(WORDS, KEYS_PRESSED)
    assert not dev.errors, f"model: {dev.errors[:3]}"
    assert bench.words(0) == want, f"model: {[hex(w) for w in bench.words(0)]} (want {[hex(w) for w in want]})"
    assert dev.disp == [0x3F, 0x06, 0x7F, 0x4F, 0x66, 0x6D] and dev.on and dev.width == "4/16", (dev.disp, dev.on)
    ff.assert_status_clean(m, [0])
    lo0, lo1, hi0, hi1 = clock_stats(bench)
    log.info(f"model: host words {[hex(w) for w in bench.words(0)]} as expected; key bytes sent "
             f"{[hex(b) for b in dev.sent]} (K2 with SG5: 0xEB, no key: 0xFF); display registers "
             f"{[hex(b) for b in dev.disp]}, display {'on' if dev.on else 'off'}, pulse width {dev.width}; "
             f"CLK low {lo0 / F_MHZ:.3f} to {lo1 / F_MHZ:.3f} us, high {hi0 / F_MHZ:.3f} to {hi1 / F_MHZ:.3f} us; "
             f"{len(dev.rises)} clocks")
    dev = Tm1637(absent=True)
    bench, m = model_run(dev, WORDS)
    want = expect(WORDS, (), absent=True)
    log.info(f"model, no chip on the bus: {[hex(w) for w in bench.words(0)]}")
    assert bench.words(0) == want, f"no chip: {[hex(w) for w in bench.words(0)]} (want {want})"
    for equ, want in ((dict(Q=30, SU=30, HI1=30, HI2=30), "below 250 kHz"), (dict(Q=120, SU=5), "tSETUP"),
                      (dict(HI1=11, HI2=11), "PWCLK")):
        dev = Tm1637(keys=KEYS_PRESSED)
        model_run(dev, WORDS, **equ)
        found = [e for e in dev.errors if want in e]
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: expected a fault with '{want}', got {dev.errors[:2]}"


RTL_WORDS = [0xC0] + DIGITS[:-1] + [DIGITS[-1] | STOP] + [0x8A | STOP] + [0x42, READ | STOP]
RTL_KEYS = [(1, 1)]


@cocotb.test()
async def test_tm1637_rtl(dut):
    """fw/tm1637.s on the chip at 60 MHz against the TM1637 model: six display bytes with automatic address, display
    control and a key read (K1 with SG1: 0xF7), in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl tm1637"
    dev = Tm1637(keys=RTL_KEYS)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_CLK: 0, P_DIO: 0}),
                        od=ff.open_drain({P_CLK, P_DIO}), inputs=dev, label=label)
    bench.watch("clk", lambda uo, uio, oe: 0 if (oe >> P_CLK) & 1 and not (uio >> P_CLK) & 1 else 1)
    bench.send(0, *RTL_WORDS)
    if hasattr(dut, "dump_off"):  # waveform of the first byte (tb.fst), for review
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 3500)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; first byte 0xC0, CLK rises at simulation times (ps) "
                 f"{[round(now - (bench.m.cycle - c) * rf.CLK_PS) for c in dev.rises[:9]]}")
    want = expect(RTL_WORDS, RTL_KEYS)
    await bench.collect(0, len(want))
    await bench.run(1000)
    await bench.finish(log)
    assert not dev.errors and bench.words(0) == want, f"{label}: {dev.errors[:3]} {[hex(w) for w in bench.words(0)]}"
    assert dev.disp == DIGITS and dev.on and dev.width == "4/16", f"{label}: {dev.disp}"
    lo0, lo1, hi0, hi1 = clock_stats(bench)
    log.info(f"{label}: host words {[hex(w) for w in bench.words(0)]}; display registers "
             f"{[hex(b) for b in dev.disp]}; CLK low {lo0 / F_MHZ:.3f} to {lo1 / F_MHZ:.3f} us, high "
             f"{hi0 / F_MHZ:.3f} to {hi1 / F_MHZ:.3f} us; {len(dev.rises)} clocks")

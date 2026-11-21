# SPDX-License-Identifier: Apache-2.0
"""HD44780U 4-bit LCD writer firmware (fw/hd44780.s) against an independent HD44780U controller model, in the
reference model and on the RTL at 60 MHz.

What it does:
  The controller model (Lcd) is written from the Hitachi HD44780U (LCD-II) data sheet, ADE-207-272(Z) '99.9 Rev. 0.0:
  4-bit interface (page 22, Figure 9: high nibble first, two transfers for each byte and for the busy flag read),
  initializing by instruction for the 4-bit interface (Figure 24, page 46), the instructions and execution times of
  Table 6 (pages 24, 25: 37 us, and 1.52 ms for clear display and return home, at fOSC = 270 kHz, scaled by
  270 / fOSC; tADD = 1.5 / fOSC after the busy flag falls before the address counter changes, Figure 10), the
  oscillator range 190 to 350 kHz and the bus timing for VCC = 2.7 to 4.5 V (page 49: tcycE 1,000 ns, PWEH 450 ns,
  tAS 60 ns, tAH 20 ns, tDSW 195 ns, tH 10 ns, tDDR 360 ns, tDHR 5 ns) and the pull-up MOS of DB0 to DB7 (page 48).
  It does not use the firmware, the assembler or the processor model. It checks every E pulse against the bus
  timing, that the chip drives DB4..DB7 at each write and never while the LCD drives them, the waits of the
  initialization (first transfer at least 40 ms after power-on, more than 4.1 ms and more than 100 us after the
  first and second function set), and that no instruction or data comes while the busy flag is 1 (page 24). In a
  read it drives the inverse of the right nibble until tDDR after the E rise (data not yet valid) and the right one
  until tDHR after the E fall. DB0..DB3 are open, so in the 8-bit mode they read 1 (pull-up MOS).
  The model test runs the initialization from the three states a power-up can leave (8-bit mode, 4-bit mode
  waiting for the high nibble, 4-bit mode waiting for the low nibble) at fOSC 190, 270 and 350 kHz, writes two
  lines of text, reads them back and reads the address counter; a stuck busy flag must give the time-out word;
  negative controls change the firmware's address set-up, E pulse width, read time and first initialization wait
  beyond the limits. The RTL test runs the firmware on the chip in lockstep with the reference model; the same test
  runs on the gate-level netlist (gl_fw workflow).

Why:
  Fifth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). The HD44780U bus has no clock:
  each transfer is timed by the E pulse, and the busy flag must be read back through the same four data lines.

Conditions: logic simulation at 60 MHz with ideal pins (16.667 ns resolution of every time); the LCD supply is
2.7 to 4.5 V (3.3 V logic levels), so the bus times of page 49 apply; the LCD's power-on is at the thread start
(cycle 0); the model and RTL runs other than the first model run shorten the firmware's 40 ms power-on wait (PON)
and the model's power-on limit by the same amount, which changes no other time.
"""

import math
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
NS = 1000.0 / F_MHZ  # ns per cycle
PINS = {0: 0, 1: 0, 2: 0, 3: 0, 5: 0, 12: 0, 13: 0}  # DB4..DB7 = uio[0..3], E = uio[5], RS = uo[4], R/W = uo[5]

# HD44780U data sheet page 49 (VCC = 2.7 to 4.5 V), ns
T_CYCE, PW_EH, T_AS, T_AH, T_DSW, T_H, T_DDR, T_DHR = 1000, 450, 60, 20, 195, 10, 360, 5
EXEC_US, LONG_US, F_NOM = 37.0, 1520.0, 270.0  # Table 6 at fOSC = 270 kHz
PON_MS, INIT1_MS, INIT2_US = 40.0, 4.1, 100.0  # Figure 24


class Lcd:
    """HD44780U on the 4-bit bus (see the module text). state: "8bit", "4hi" or "4lo" at power-on; fosc in kHz;
    stuck: the busy flag never falls. Records writes [(cycle, rs, byte)], reads [(cycle, rs, byte)], errors."""

    def __init__(self, fosc=F_NOM, state="8bit", pon_ms=PON_MS, stuck=False):
        self.k = F_NOM / fosc
        self.tadd = math.ceil(1500.0 / fosc * F_MHZ)  # 1.5 / fOSC in cycles
        self.pon = round(pon_ms * 1000 * F_MHZ)
        self.stuck = stuck
        self.dl8 = state == "8bit"
        self.phase = 1 if state == "4lo" else 0
        self.first = (0, 0, 0)  # the high-nibble transfer that a "4lo" power-up waits to complete
        self.ddram, self.cgram = [0x20] * 128, [0] * 64
        self.ac, self.cg, self.inc, self.n, self.f, self.disp = 0, False, 1, 0, 0, 0
        self.dr = None
        self.busy_until = 0
        self.ac_at = None  # (cycle, step): address counter change after the busy flag falls (Figure 10)
        self.e, self.rs, self.rw, self.chip = 0, None, None, None
        self.t_addr = self.t_db = 0
        self.t_rise = self.t_fall = self.last_rise = None
        self.drive = None  # (cycle of valid data, nibble, inverse) while the LCD drives DB4..DB7
        self.drive_end = None
        self.rbyte = 0
        self.wfalls = []
        self.writes, self.reads, self.errors = [], [], []
        self.stats = dict(tas=1e9, pweh=1e9, tcyc=1e9, tdsw=1e9, tah=1e9)
        self.contention = False

    def __call__(self, c, uo, uio, oe):
        if self.ac_at and c >= self.ac_at[0]:
            self._step_ac(self.ac_at[1])
            self.ac_at = None
        rs, rw = (uo >> 4) & 1, (uo >> 5) & 1
        e = (uio >> 5) & 1 if (oe >> 5) & 1 else 0  # pull-down on E
        chip = tuple((uio >> i) & 1 if (oe >> i) & 1 else None for i in range(4))
        if (rs, rw) != (self.rs, self.rw):
            if self.rs is not None:
                if self.e:
                    self.errors.append(f"cycle {c}: RS or R/W changed while E is high (tAS, tAH)")
                elif self.t_fall is not None:
                    tah = (c - self.t_fall) * NS
                    self.stats["tah"] = min(self.stats["tah"], tah)
                    if tah < T_AH:
                        self.errors.append(f"cycle {c}: RS or R/W changed {tah:.1f} ns after the E fall (tAH 20 ns)")
            self.rs, self.rw, self.t_addr = rs, rw, c
        if chip != self.chip:
            if self.t_fall is not None and (c - self.t_fall) * NS < T_H and self.chip is not None:
                self.errors.append(f"cycle {c}: DB changed {(c - self.t_fall) * NS:.1f} ns after the E fall (tH 10 ns)")
            self.chip, self.t_db = chip, c
        if e and not self.e:
            self._rise(c)
        elif self.e and not e:
            self._fall(c)
        self.e = e
        if self.drive_end is not None and c > self.drive_end:
            self.drive = self.drive_end = None
        out = None
        if self.drive:
            start, val, early = self.drive
            out = val if c >= start else early
            if any(b is not None for b in chip) and not self.contention:
                self.contention = True
                self.errors.append(f"cycle {c}: the chip drives DB while the LCD drives it (read transfer)")
        level = 0
        for i in range(4):
            b = chip[i] if chip[i] is not None else ((out >> i) & 1 if out is not None else 1)  # pull-up MOS
            level |= b << i
        return 0, level | e << 5

    # ----- bus -----

    def _rise(self, c):
        if c < self.pon:
            self.errors.append(f"cycle {c}: E pulse {c / F_MHZ / 1000:.3f} ms after power-on (Figure 24: more than "
                               f"{self.pon / F_MHZ / 1000:.1f} ms)")
        if self.last_rise is not None:
            tcyc = (c - self.last_rise) * NS
            self.stats["tcyc"] = min(self.stats["tcyc"], tcyc)
            if tcyc < T_CYCE:
                self.errors.append(f"cycle {c}: E cycle {tcyc:.1f} ns (tcycE 1000 ns)")
        tas = (c - self.t_addr) * NS
        self.stats["tas"] = min(self.stats["tas"], tas)
        if tas < T_AS:
            self.errors.append(f"cycle {c}: RS, R/W set-up {tas:.1f} ns before the E rise (tAS 60 ns)")
        self.last_rise = self.t_rise = c
        self.contention = False
        if self.rw:
            if self.dl8 or self.phase == 0:
                self.rbyte = self._read_value(c)
                nib = self.rbyte >> 4
            else:
                nib = self.rbyte & 15
            self.drive = (c + math.ceil(T_DDR / NS), nib, nib ^ 15)
            self.drive_end = None

    def _fall(self, c):
        pw = (c - self.t_rise) * NS
        self.stats["pweh"] = min(self.stats["pweh"], pw)
        if pw < PW_EH:
            self.errors.append(f"cycle {c}: E high {pw:.1f} ns (PWEH 450 ns)")
        self.t_fall = c
        if self.rw:
            self.drive_end = c + math.ceil(T_DHR / NS)
            self._transfer(c, 1, self.rs, None)
            return
        if any(b is None for b in self.chip):
            self.errors.append(f"cycle {c}: DB4..DB7 not driven by the chip at the E fall of a write")
            return
        tdsw = (c - self.t_db) * NS
        self.stats["tdsw"] = min(self.stats["tdsw"], tdsw)
        if tdsw < T_DSW:
            self.errors.append(f"cycle {c}: data set-up {tdsw:.1f} ns before the E fall (tDSW 195 ns)")
        self._init_waits(c)
        self._transfer(c, 0, self.rs, sum(b << i for i, b in enumerate(self.chip)))

    def _init_waits(self, c):
        self.wfalls.append(c)
        w = self.wfalls
        if len(w) == 2 and (w[1] - w[0]) / F_MHZ < INIT1_MS * 1000:
            self.errors.append(f"cycle {c}: {(w[1] - w[0]) / F_MHZ / 1000:.3f} ms after the first function set "
                               f"(Figure 24: more than 4.1 ms)")
        if len(w) == 3 and (w[2] - w[1]) / F_MHZ < INIT2_US:
            self.errors.append(f"cycle {c}: {(w[2] - w[1]) / F_MHZ:.3f} us after the second function set "
                               f"(Figure 24: more than 100 us)")

    def _transfer(self, c, rw, rs, nib):
        if self.dl8:
            if rw:
                self._read_done(c, rs)
            else:
                self._execute(c, rs, nib << 4 | 0xF)  # DB0..DB3 open: pull-up MOS
            return
        if self.phase == 0:
            self.phase, self.first = 1, (rw, rs, nib)
            return
        self.phase = 0
        rw0, rs0, nib0 = self.first
        if (rw0, rs0) != (rw, rs):
            self.errors.append(f"cycle {c}: the two transfers of a byte have different RS or R/W")
        if rw:
            self._read_done(c, rs)
        else:
            self._execute(c, rs, (nib0 or 0) << 4 | nib)

    # ----- controller -----

    def _busy(self, c):
        return self.stuck or c < self.busy_until

    def _read_value(self, c):
        if self.rs == 0:
            return int(self._busy(c)) << 7 | self.ac
        return self.dr if self.dr is not None else 0

    def _read_done(self, c, rs):
        self.reads.append((c, rs, self.rbyte))
        if rs:
            self._apply_ac()
            self.busy_until = c + round(EXEC_US * self.k * F_MHZ)
            self.ac_at = (self.busy_until + self.tadd, self.inc)

    def _apply_ac(self):
        if self.ac_at:
            self._step_ac(self.ac_at[1])
            self.ac_at = None

    def _step_ac(self, step):
        d = 1 if step else -1
        a = self.ac + d
        if self.cg:
            a &= 0x3F
        elif self.n:
            a = {0x28: 0x40, 0x68: 0x00, 0x3F: 0x27, -1: 0x67}.get(a, a)
        else:
            a %= 80
        self.ac = a
        self.dr = self.cgram[a] if self.cg else self.ddram[a]

    def _execute(self, c, rs, b):
        if self._busy(c):
            self.errors.append(f"cycle {c}: {'data' if rs else 'instruction'} 0x{b:02X} while the busy flag is 1 "
                               f"(page 24)")
        self._apply_ac()
        self.writes.append((c, rs, b))
        t = EXEC_US
        if rs:
            if self.cg:
                self.cgram[self.ac] = b
            else:
                self.ddram[self.ac] = b
            self.dr = None
            self.busy_until = c + round(t * self.k * F_MHZ)
            self.ac_at = (self.busy_until + self.tadd, self.inc)
            return
        if b == 0x01:
            self.ddram = [0x20] * 128
            self.ac, self.cg, self.inc, t = 0, False, 1, LONG_US
        elif b & 0xFE == 0x02:
            self.ac, self.cg, t = 0, False, LONG_US
        elif b & 0xFC == 0x04:
            self.inc = (b >> 1) & 1
        elif b & 0xF8 == 0x08:
            self.disp = (b >> 2) & 1
        elif b & 0xF0 == 0x10:
            if not b & 0x08:
                self._step_ac(1 if b & 0x04 else 0)
        elif b & 0xE0 == 0x20:
            dl = (b >> 4) & 1
            if self.dl8 and not dl:
                self.phase = 0
            self.dl8 = bool(dl)
            self.n, self.f = (b >> 3) & 1, (b >> 2) & 1
        elif b & 0xC0 == 0x40:
            self.ac, self.cg = b & 0x3F, True
            self.dr = self.cgram[self.ac]
        else:
            self.ac, self.cg = b & 0x7F, False
            self.dr = self.ddram[self.ac]
        self.busy_until = c + round(t * self.k * F_MHZ)


# ----- host words -----

LINE1, LINE2 = "HELLO", "PE ASIC"
HOST = ([0x028, 0x008, 0x001, 0x006, 0x00C] + [0x100 | ord(ch) for ch in LINE1] + [0x0C0]
        + [0x100 | ord(ch) for ch in LINE2] + [0x082, 0x300, 0x300, 0x300, 0x200])
WANT = [ord("L"), ord("L"), ord("O"), 0x05]  # data at DDRAM 2, 3, 4, then BF = 0 and the address counter 5
PON_SHORT = 13  # 13 x 4,000 cycles = 0.867 ms


def assemble(**equ):
    return ff.assemble("hd44780.s", **equ)


def model_run(lcd, words=HOST, nwords=len(WANT), limit=6_000_000, **equ):
    m = pe_model.PeModel(assemble(**equ).words, owner=ff.owners(PINS))
    bench = ff.Bench(m, inputs=lcd)
    bench.send(0, *words)
    while m.cycle < limit and (len(bench.words(0)) < nwords or bench.to_thread[0]):
        bench.run(20_000)
    bench.run(2_000)
    return bench, m


def short_lcd(**kw):
    return Lcd(pon_ms=PON_SHORT * 4000 / F_MHZ / 1000 - 0.01, **kw)


def check(lcd, words, label):
    assert not lcd.errors, f"{label}: {lcd.errors[:3]}"
    assert words == WANT, f"{label}: host words {[hex(w) for w in words]}, expected {[hex(w) for w in WANT]}"
    assert not lcd.dl8 and lcd.n == 1 and lcd.f == 0 and lcd.disp == 1 and lcd.inc == 1, (
        f"{label}: mode 8-bit {lcd.dl8}, N {lcd.n}, F {lcd.f}, display {lcd.disp}, increment {lcd.inc}")
    l1 = "".join(chr(b) for b in lcd.ddram[0:len(LINE1)])
    l2 = "".join(chr(b) for b in lcd.ddram[0x40:0x40 + len(LINE2)])
    assert (l1, l2) == (LINE1, LINE2), f"{label}: DDRAM lines {l1!r} {l2!r}"


def summary(lcd):
    s = lcd.stats
    w = lcd.wfalls
    return (f"{len(lcd.writes)} bytes written, {len(lcd.reads)} reads; first transfer {w[0] / F_MHZ / 1000:.3f} ms "
            f"after power-on, init waits {(w[1] - w[0]) / F_MHZ / 1000:.3f} ms and {(w[2] - w[1]) / F_MHZ:.3f} us; "
            f"tAS {s['tas']:.1f} ns, PWEH {s['pweh']:.1f} ns, tcycE {s['tcyc']:.1f} ns, tDSW {s['tdsw']:.1f} ns, "
            f"tAH {s['tah']:.1f} ns (minima)")


@cocotb.test(skip=GL)
async def test_hd44780_words(dut):
    """Memory words of fw/hd44780.s."""
    size = assemble().size
    dut._log.info(f"fw/hd44780.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_hd44780_model(dut):
    """fw/hd44780.s in the reference model against the HD44780U model: initialization from three power-up states at
    three oscillator frequencies, two lines written and read back, the address counter read, a stuck busy flag;
    negative controls for tAS, PWEH, the read time and the first initialization wait."""
    log = dut._log
    lcd = Lcd()
    bench, m = model_run(lcd)
    check(lcd, bench.words(0), "model")
    ff.assert_status_clean(m, [0])
    log.info(f"model (8-bit power-up, fOSC 270 kHz, 40 ms power-on wait): {summary(lcd)}; host words "
             f"{[hex(w) for w in bench.words(0)]}; {m.cycle} cycles")
    for state in ("8bit", "4hi", "4lo"):
        for fosc in (190.0, 350.0):
            lcd = short_lcd(state=state, fosc=fosc)
            bench, m = model_run(lcd, PON=PON_SHORT)
            label = f"model {state} power-up, fOSC {fosc:.0f} kHz"
            check(lcd, bench.words(0), label)
            ff.assert_status_clean(m, [0])
            polls = sum(1 for _, rs, _ in lcd.reads if rs == 0)
            log.info(f"{label}: {summary(lcd)}; {polls} busy flag reads")
    lcd = short_lcd(stuck=True)
    bench, m = model_run(lcd, words=[0x028], nwords=1, PON=PON_SHORT)
    first = next(c for c, rs, _ in lcd.reads if rs == 0)
    t_out = bench.from_thread[0][0][0] if bench.from_thread[0] else None
    log.info(f"model stuck busy flag: host words {[hex(w) for w in bench.words(0)]}, time-out "
             f"{(t_out - first) / F_MHZ / 1000:.3f} ms after the first busy read" if t_out else "no time-out word")
    assert bench.words(0) == [0xFFFF] and not [w for w in lcd.writes if w[0] > first], "stuck busy flag"
    assert (t_out - first) / F_MHZ > 1.1 * LONG_US * F_NOM / 190.0, "time-out shorter than clear display at 190 kHz"
    # Negative controls: the LCD model must catch each bus or initialization fault, or the read-back must fail.
    for equ, want in ((dict(TAS=2), "tAS 60 ns"), (dict(TE=32, TS=30), "PWEH 450 ns"), (dict(W1=60), "more than 4.1 ms")):
        lcd = short_lcd()
        model_run(lcd, words=HOST[:3], nwords=1, limit=600_000, PON=PON_SHORT, **equ)  # no word comes: runs to limit
        log.info(f"control {equ}: {lcd.errors[0] if lcd.errors else 'no fault found'}")
        assert lcd.errors and want in lcd.errors[0], f"control {equ}: expected '{want}', got {lcd.errors[:2]}"
    lcd = short_lcd()
    bench, m = model_run(lcd, limit=1_500_000, PON=PON_SHORT, TS=18)
    log.info(f"control {{'TS': 18}} (DB read 200 ns after the E rise, before tDDR): host words "
             f"{[hex(w) for w in bench.words(0)]}")
    assert bench.words(0) != WANT, "control TS 18: the early read was not noticed"


@cocotb.test()
async def test_hd44780_rtl(dut):
    """fw/hd44780.s on the chip at 60 MHz against the HD44780U model (power-up in 4-bit mode waiting for the low
    nibble, fOSC 270 kHz, power-on wait shortened): initialization, two lines, read-back, in lockstep with the
    reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl hd44780"
    ref = short_lcd(state="4lo")
    model_run(ref, PON=PON_SHORT)
    c0 = ref.writes[4][0]  # the host's function set 0x28 (after the four initialization transfers)
    lcd = short_lcd(state="4lo")
    bench = rf.RtlBench(dut, assemble(PON=PON_SHORT).words, owner=ff.owners(PINS), inputs=lcd, label=label)
    bench.send(0, *HOST)
    if hasattr(dut, "dump_off"):
        await bench.run(c0 - 1_500 - bench.m.cycle)
        dut.dump_off.value = 0
        await bench.run(6_000)
        dut.dump_off.value = 1
        log.info(f"{label}: waveform window model cycles {c0 - 1_500} to {bench.m.cycle}, simulation time about "
                 f"{get_sim_time(unit='ps') - 6_000 * rf.CLK_PS} ps at its start; E rises in it at model cycles "
                 f"{[c for c, _, _ in lcd.reads + lcd.writes if c0 - 1_500 <= c < bench.m.cycle][:8]} (E falls)")
    for _ in range(200):
        if len(bench.words(0)) >= len(WANT):
            break
        await bench.run(20_000)
    await bench.run(2_000)
    await bench.finish(log)
    check(lcd, bench.words(0), label)
    log.info(f"{label}: {summary(lcd)}; host words {[hex(w) for w in bench.words(0)]}")

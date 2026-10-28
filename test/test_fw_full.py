# SPDX-License-Identifier: Apache-2.0
"""G1 tests: full UART, SPI and I2C controller firmware in the model, and the measured word count.

What it does:
  These cocotb tests assemble the full firmware of run G1 step 3a
  (fw/uart_tx_full.s, fw/uart_rx_full.s, fw/uart_rx_slow.s, fw/spi_ctl.s,
  fw/i2c_ctl.s), count the words, and run each protocol in the reference
  model (model/pe_model.py) at the limits of A1 section 3: UART 300 bit/s to
  1 Mbit/s at 50 MHz and 40 MHz with a sender bit-time error of +/-3.3 %,
  SPI in all four modes at the highest rate that the firmware can do, I2C
  Standard-mode and Fast-mode with clock stretching, repeated START, read,
  NACK and arbitration loss. Then the four protocols run at the same time
  from one 128-word memory. The tests do not use the design under test.

Why:
  Decision D2 and D4 item 13 (128 words of memory) had a measured ground
  for 88 words of outline firmware only; the full firmware was an estimate
  of about 119 words (M2a_isa.md section 5). Rule G (DECISIONS.md) needs a
  measurement. The expected values come from A1 section 3, from the I2C
  standard (NXP UM10204, see I2C_LIMITS) and from the firmware headers, not
  from the model. The UART receiver, the UART sender, the SPI device and the
  I2C bus models in this file do not use the firmware or the model.
"""

import bisect
import math
import os
import random
import re
import sys
from collections import deque

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402

GL = os.environ.get("GATES") == "yes"  # the gate-level job also runs this module; it has no DUT use
HORIZON = 32_767  # largest event lead (isa 4.2)
TOL = 0.033  # sender bit-time error that the UART receiver must accept (A1 section 3 and 8.2)
SHARE = 0.01  # our share of the UART clock budget (A1 section 3)

# Pin numbers (docs/isa.md section 5.1)
P_TX = 12  # UART_TX = uo_out[4]
UI_RX = 3  # UART_RX = ui_in[3]
P_CS, P_MOSI, P_MISO, P_SCK = 0, 1, 2, 3
P_SCL, P_SDA = 6, 7

FULL = ("uart_tx_full.s", "uart_rx_full.s", "spi_ctl.s", "i2c_ctl.s")
SLOW = ("uart_tx_full.s", "uart_rx_slow.s", "spi_ctl.s", "i2c_ctl.s")
OUTLINE = ("uart_tx.s", "uart_rx.s", "spi_mode0.s", "i2c_write.s")

# I2C .equ values (cycles) of fw/i2c_ctl.s for each mode and clock. Times after
# a WAIT start 2 cycles after the pin edge, so the bus time is the value + 3.
I2C_EQU = {
    ("Fm", 50): dict(HD=4, TLOW=75, TSMP=16, THIGH2=32, TSUSTA=32, THDSTA=32, TSUSTO=32, TBUF=68),
    ("Sm", 50): dict(HD=4, TLOW=260, TSMP=16, THIGH2=221, TSUSTA=235, THDSTA=205, TSUSTO=200, TBUF=240),
    ("Fm", 40): dict(HD=4, TLOW=60, TSMP=16, THIGH2=24, TSUSTA=24, THDSTA=25, TSUSTO=24, TBUF=53),
}
# Minimum times in us (fSCL: maximum clock in kHz), NXP UM10204 I2C-bus specification,
# table of SDA and SCL bus characteristics (A1 [I2C]; G1 step 3d gives the revision and page).
I2C_LIMITS = {
    "Sm": dict(f_khz=100, hd_sta=4.0, low=4.7, high=4.0, su_sta=4.7, hd_dat=0.0, su_dat=0.25, su_sto=4.0, buf=4.7),
    "Fm": dict(f_khz=400, hd_sta=0.6, low=1.3, high=0.6, su_sta=0.6, hd_dat=0.0, su_dat=0.1, su_sto=0.6, buf=1.3),
}


# ----- helpers -----


def source(name, **equ):
    """Text of fw/name with some .equ values changed (the host sets them when it assembles the file)."""
    with open(os.path.join(ROOT, "fw", name), encoding="utf-8") as f:
        text = f.read()
    for key, val in equ.items():
        pattern = re.compile(r"^(\s*\.equ\s+" + key + r"\s*,)[^;\n]*", re.M)
        text, n = pattern.subn(lambda m: f"{m.group(1)} {val} ", text)
        assert n == 1, f"{name}: .equ {key} found {n} times"
    return text


def assemble(name, base=0, **equ):
    return pe_asm.assemble(source(name, **equ), name, base)


def owners(mapping):
    result = [None] * pe_model.N_OUT
    for pin, thread in mapping.items():
        result[pin] = thread
    return result


def open_drain(pins):
    return [p in pins for p in range(pe_model.N_OUT)]


def level(uio, oe, p):
    """Level that a uio output pin drives, or None if it is released (OE = 0)."""
    return (uio >> p) & 1 if (oe >> p) & 1 else None


def value_at(changes, cycle):
    i = bisect.bisect_right(changes, (cycle, float("inf"))) - 1
    return changes[i][1]


class Bench:
    """Runs a model. Gives host words to the threads, records host words and pin changes."""

    def __init__(self, model, inputs=None):
        self.m = model
        self.inputs = inputs  # function(cycle, uo_out, uio_out, uio_oe) -> (ui_in, uio_in)
        self.to_thread = [deque() for _ in range(pe_model.N_THREADS)]
        self.from_thread = [[] for _ in range(pe_model.N_THREADS)]
        self.watches = {}

    def watch(self, name, fn):
        self.watches[name] = [fn, []]

    def changes(self, name):
        return self.watches[name][1]

    def words(self, n):
        return [w for _, w in self.from_thread[n]]

    def send(self, n, *words):
        self.to_thread[n].extend(words)

    def run(self, cycles):
        m = self.m
        watches = list(self.watches.values())
        for _ in range(cycles):
            c = m.cycle
            if self.inputs is not None:
                ui, uio = self.inputs(c, *m.pins())
                m.set_inputs(ui, uio)
            for n in range(pe_model.N_THREADS):
                q = self.to_thread[n]
                if q and m.fifo_push(n, q[0]):
                    q.popleft()
            pins = m.step()
            for n in range(pe_model.N_THREADS):
                w = m.fifo_pop(n)
                while w is not None:
                    self.from_thread[n].append((c, w))
                    w = m.fifo_pop(n)
            for fn, ch in watches:
                v = fn(*pins)
                if not ch or ch[-1][1] != v:
                    ch.append((c, v))


def status_clean(m, threads):
    return all(m.threads[n].status == 0 and m.threads[n].run for n in threads)


def assert_status_clean(m, threads):
    for n in threads:
        th = m.threads[n]
        assert th.status == 0, f"thread {n}: status {th.status} (bit 0 LATE, bit 1 ERR), {th.stop_reason}"
        assert th.run, f"thread {n} stopped: {th.stop_reason}"


# ----- UART -----


def uart_cfg(f_clk, rate):
    """Bit time of the firmware for a rate: K chunks of C cycles, B = K x C (isa 4.2 horizon)."""
    b_nom = f_clk / rate
    k = max(1, math.ceil(b_nom / HORIZON))
    c = round(b_nom / k)
    return k, c, k * c


def frame_bits(data, fmt):
    """Line bits of one frame, start bit first. fmt: 8N1, 8E1 or 8N2."""
    bits = [0] + [(data >> i) & 1 for i in range(8)]
    if fmt[1] == "E":
        bits.append(sum(bits[1:]) & 1)
    return bits + [1] * int(fmt[2])


def frame_word(bits):
    """Host word of fw/uart_tx_full.s: the line bits, then the end mark."""
    return sum(b << i for i, b in enumerate(bits)) | (1 << len(bits))


def check_tx(log, changes, b_design, b_nom, frames, label):
    """Independent receiver: start edge, bit k at start + (k + 0.5) x nominal bit time; edges at bit boundaries."""
    nbits = len(frames[0])
    starts = []
    ready = -1.0
    for i in range(1, len(changes)):
        c, v = changes[i]
        if v == 0 and changes[i - 1][1] == 1 and c > ready:
            starts.append(c)
            ready = c + (nbits - 0.5) * b_nom
    assert len(starts) == len(frames), f"{label}: {len(starts)} frames, expected {len(frames)}"
    inside = 0
    for n, (t0, bits) in enumerate(zip(starts, frames)):
        got = [value_at(changes, int(t0 + (k + 0.5) * b_nom)) for k in range(nbits)]
        assert got == bits, f"{label} frame {n}: bits {got}, expected {bits}"
        for c, _ in changes:
            if t0 <= c < t0 + nbits * b_design:
                assert (c - t0) % b_design == 0, f"{label} frame {n}: edge at {c} is not at a bit boundary"
                inside += 1
    assert inside == len(changes) - 1, f"{label}: {len(changes) - 1 - inside} pin changes outside the frames"
    gaps = {b - a for a, b in zip(starts, starts[1:])}
    assert not gaps or gaps == {nbits * b_design}, f"{label}: start-to-start {sorted(gaps)}, not back-to-back"
    err = b_nom / b_design - 1
    log.info(f"{label}: {len(frames)} frames correct, bit time {b_design} cycles (nominal {b_nom:.3f}), "
             f"rate error {err * 100:+.4f} %, back-to-back")
    assert abs(err) <= SHARE, f"{label}: rate error {err * 100:+.4f} % is more than the 1 % share"
    return err


def tx_run(f_clk, rate, fmt, data):
    k, c, b = uart_cfg(f_clk, rate)
    prog = assemble("uart_tx_full.s")
    m = pe_model.PeModel(prog.words, owner=owners({P_TX: 0}))
    bench = Bench(m)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    frames = [frame_bits(d, fmt) for d in data]
    bench.send(0, c, k, *[frame_word(bits) for bits in frames])
    bench.run(200 + (len(frames) * len(frames[0]) + 2) * b)
    return bench, m, frames, b


def uart_line(frames, bit_time, glitch=None):
    """Line changes of a sender with its own bit time: frames = [(start cycle, bits)]."""
    changes = [(0, 1)]
    if glitch is not None:
        changes += [(glitch, 0), (glitch + 3, 1)]
    for t0, bits in frames:
        for k, bit in enumerate(bits):
            c = t0 + round(k * bit_time)
            if bit != changes[-1][1]:
                changes.append((c, bit))
    return changes


def rx_bits(word, nd1):
    """Samples of one frame in a word of fw/uart_rx_full.s: start bit at bit 15 - nd1, stop bit at bit 15."""
    return [(word >> (15 - nd1 + j)) & 1 for j in range(nd1 + 1)]


def rx_run(b, h, fmt, b_tx, data, slow_k=None, glitch=False):
    """Receiver firmware with bit time b and first sample h; sender with bit time b_tx, back-to-back frames."""
    nd1 = len(frame_bits(0, fmt)) - int(fmt[2])  # data, parity, and one stop bit
    if slow_k is None:
        prog = assemble("uart_rx_full.s", ND1=nd1)
        host = [b, h]
    else:
        prog = assemble("uart_rx_slow.s", ND1=nd1, K=slow_k)
        host = [b // slow_k, h // slow_k]
    frames = []
    t = 2000
    for d in data:
        bits = frame_bits(d, fmt)
        frames.append((t, bits))
        t += round(len(bits) * b_tx)
    line = uart_line(frames, b_tx, glitch=1000 if glitch else None)
    m = pe_model.PeModel(prog.words)
    bench = Bench(m, inputs=lambda c, uo, uio, oe: (value_at(line, c) << UI_RX, 0))
    bench.send(0, *host)
    bench.run(t + 3 * b)
    words = bench.words(0)
    ok = status_clean(m, [0]) and len(words) == len(frames) and all(
        rx_bits(w, nd1) == bits[: nd1 + 1] for w, (_, bits) in zip(words, frames))
    return ok, words, m, frames, nd1


def rx_limits(b, h, nframes=8, step=0.001, stop=0.08):
    """Largest sender bit-time errors (fast: shorter bits, slow: longer bits) that the receiver accepts."""
    rng = random.Random(b * 1000 + h)
    data = [rng.randrange(256) for _ in range(nframes)]
    result = []
    for sign in (-1, 1):
        last = 0.0
        e = step
        while e <= stop + 1e-12:
            ok, *_ = rx_run(b, h, "8N1", b * (1 + sign * e), data)
            if not ok:
                break
            last = e
            e += step
        result.append(last)
    return result  # [fast limit, slow limit]


# ----- SPI -----


class SpiDevice:
    """SPI target model of one mode: samples MOSI at its sample edge, drives MISO `delay` cycles after its out edge."""

    def __init__(self, cpol, cpha, data, delay=3):
        self.cpol, self.cpha, self.delay = cpol, cpha, delay
        self.data = list(data)
        self.rx = []
        self.miso = 1
        self.pend = deque()
        self.prev = (1, cpol)
        self.nin = self.shift = self.nout = 0
        self.cs_low = 0
        self.errors = []

    def _drive(self, c):
        j = len(self.rx)
        byte = self.data[j] if j < len(self.data) else 0xFF
        self.pend.append((c + self.delay, (byte >> (7 - self.nout)) & 1))
        self.nout += 1

    def __call__(self, c, uo, uio, oe):
        while self.pend and self.pend[0][0] <= c:
            self.miso = self.pend.popleft()[1]
        cs, sck, mosi = level(uio, oe, P_CS), level(uio, oe, P_SCK), level(uio, oe, P_MOSI)
        cs = 1 if cs is None else cs
        sck = self.cpol if sck is None else sck
        pcs, psck = self.prev
        if pcs == 1 and cs == 0:
            self.cs_low += 1
            self.nin = self.shift = self.nout = 0
            if sck != self.cpol:
                self.errors.append(f"cycle {c}: SCK is {sck} at the CS fall (CPOL {self.cpol})")
            if self.cpha == 0:
                self._drive(c)
        elif cs == 0 and sck != psck:
            leading = sck != self.cpol
            if leading == (self.cpha == 0):  # sample edge
                if mosi is None:
                    self.errors.append(f"cycle {c}: MOSI is not driven at a sample edge")
                self.shift = (self.shift << 1) | (mosi or 0)
                self.nin += 1
                if self.nin == 8:
                    self.rx.append(self.shift)
                    self.nin = self.shift = self.nout = 0
            else:
                self._drive(c)
        elif pcs == 0 and cs == 1 and self.nin:
            self.errors.append(f"cycle {c}: CS high after {self.nin} bits")
        elif cs == 1 and sck != psck:
            self.errors.append(f"cycle {c}: SCK edge while CS is high")
        self.prev = (cs, sck)
        return 0, self.miso << P_MISO


SPI_WORDS = [0x03C, 0x0A5, 0x10F, 0x181]  # 2 transfers: 3 bytes, 1 byte (bit 8: CS high after)
SPI_DEV = [0xC3, 0x5A, 0xF0, 0x7E]


def spi_run(cpol, cpha, h, d, words=SPI_WORDS, dev_data=SPI_DEV):
    cl = cpol if cpha == 0 else 1 - cpol
    prog = assemble("spi_ctl.s", CPOL=cpol, CL=cl, D=d)
    m = pe_model.PeModel(prog.words, owner=owners({P_CS: 0, P_MOSI: 0, P_SCK: 0}))
    dev = SpiDevice(cpol, cpha, dev_data)
    bench = Bench(m, inputs=dev)
    bench.watch("sck", lambda uo, uio, oe: level(uio, oe, P_SCK))
    bench.watch("cs", lambda uo, uio, oe: level(uio, oe, P_CS))
    bench.send(0, h, *words)
    bench.run(400 + len(words) * (20 * h + 120))
    return bench, m, dev


def spi_ok(bench, m, dev, words=SPI_WORDS, dev_data=SPI_DEV):
    return (status_clean(m, [0]) and not dev.errors and dev.rx == [w & 0xFF for w in words]
            and [w & 0xFF for w in bench.words(0)] == dev_data[: len(words)]
            and dev.cs_low == sum(1 for w in words if w & 0x100))


def spi_half_periods(bench):
    """Times between SCK edges inside each byte (16 edges per byte that change the line)."""
    sck, cs = bench.changes("sck"), bench.changes("cs")
    edges = [c for i, (c, v) in enumerate(sck) if i and v is not None and sck[i - 1][1] is not None and v != sck[i - 1][1]]
    edges = [c for c in edges if value_at(cs, c) == 0]
    groups = [edges[i:i + 16] for i in range(0, len(edges), 16)]
    return {b - a for g in groups for a, b in zip(g, g[1:])}, len(edges)


def spi_h_min(cpol, cpha, d):
    for h in range(8, 80):
        bench, m, dev = spi_run(cpol, cpha, h, d)
        if spi_ok(bench, m, dev):
            return h
    raise AssertionError("no SCK half period up to 80 cycles works")


# ----- I2C -----


def i2c_write(byte, start=False, stop=False):
    """Two host words of fw/i2c_ctl.s for a byte that the controller sends (the target ACKs it)."""
    p = (byte << 1) | 1
    return [(int(start) << 15) | (p << 6) | (int(stop) << 5), (p & 0x1FE) << 7]


def i2c_read(ack, stop=False):
    """Two host words for a byte that the target sends; the controller then sends ACK (0) or NACK (1)."""
    p = 0x1FE | (0 if ack else 1)
    return [(p << 6) | (int(stop) << 5), (p & 1) << 7]


class I2cBus:
    """Wired-AND I2C bus with pull-ups, one target with a 7-bit address, and extra drivers for tests.

    The target ACKs its address and each written byte, sends read data, reads the ACK
    or NACK of the controller, and stretches the clock: it holds SCL low for `stretch`
    cycles from the SCL fall at the end of each ACK bit. Its SDA changes come `td`
    cycles after the SCL fall. It records START, STOP, address, written and read bytes.
    """

    def __init__(self, addr, read_data=(), stretch=0, td=6):
        self.addr, self.stretch, self.td = addr, stretch, td
        self.read_data = list(read_data)
        self.prev = (1, 1)
        self.changes = [(0, (1, 1))]
        self.events = []
        self.state, self.pos, self.shift, self.nbyte = "idle", 0, 0, 0
        self.rw = self.acked = self.ctrl_ack = 0
        self.read_i = 0
        self.sda_low = 0
        self.scl_until = -1
        self.pend = deque()
        self.other_sda = []  # (from, to) cycles: another controller pulls SDA low
        self.scl_spikes = []  # (from, to) cycles: SCL reads 1 (noise spike)
        self.sda_spikes = []  # (from, to) cycles: SDA reads 0 (noise spike)
        self.lose_at = None  # (byte number, bit 1 to 9): another controller pulls SDA low in that bit
        self.spike_after_ack = None  # (byte number, delay, width): SCL spike in the stretch after that byte
        self.sda_spike_at = None  # (byte number, bit, cycle after the SCL rise, width)

    def _drive(self, c, low):
        self.pend.append((c + self.td, int(low)))

    def lines(self, c, uio, oe):
        while self.pend and self.pend[0][0] <= c:
            self.sda_low = self.pend.popleft()[1]
        scl = int(not ((oe >> P_SCL) & 1 and not (uio >> P_SCL) & 1) and c >= self.scl_until)
        sda = int(not ((oe >> P_SDA) & 1 and not (uio >> P_SDA) & 1) and not self.sda_low)
        if any(a <= c < b for a, b in self.other_sda):
            sda = 0
        if any(a <= c < b for a, b in self.scl_spikes):
            scl = 1
        if any(a <= c < b for a, b in self.sda_spikes):
            sda = 0
        return scl, sda

    def __call__(self, c, uo, uio, oe):
        scl, sda = self.lines(c, uio, oe)
        pscl, psda = self.prev
        if (scl, sda) != self.prev:
            self.changes.append((c, (scl, sda)))
        if scl and pscl and sda != psda:
            if sda == 0:
                self.events.append((c, "START"))
                self.state, self.pos, self.shift = "addr", 0, 0
                self.pend.clear()
                self.sda_low = 0
            else:
                self.events.append((c, "STOP"))
                self.state = "idle"
        elif scl and not pscl and self.state != "idle":
            self.pos += 1
            if self.pos <= 8:
                self.shift = (self.shift << 1) | sda
            else:
                self.ctrl_ack = sda
            if self.sda_spike_at and self.sda_spike_at[:2] == (self.nbyte, self.pos):
                _, _, at, width = self.sda_spike_at
                self.sda_spikes.append((c + at, c + at + width))
        elif pscl and not scl and self.state != "idle":
            if self.lose_at and self.lose_at[0] == self.nbyte and self.pos == self.lose_at[1] - 1:
                self.other_sda.append((c + self.td, 10**9))
            if self.pos == 8:
                if self.state == "addr":
                    self.rw, a = self.shift & 1, self.shift >> 1
                    self.acked = int(a == self.addr)
                    self.events.append((c, "ADDR", a, self.rw, self.acked))
                    self._drive(c, self.acked)
                elif self.state == "write":
                    self.events.append((c, "W", self.shift))
                    self._drive(c, 1)
                elif self.state == "read":
                    self._drive(c, 0)
            elif self.pos == 9:
                if self.state == "read":
                    self.events.append((c, "R", self.read_data[self.read_i - 1], self.ctrl_ack))
                self.pos, self.shift = 0, 0
                if self.stretch:
                    self.scl_until = c + self.stretch
                if self.spike_after_ack and self.spike_after_ack[0] == self.nbyte:
                    _, at, width = self.spike_after_ack
                    self.scl_spikes.append((c + at, c + at + width))
                self.nbyte += 1
                if self.state == "addr":
                    self.state = ("read" if self.rw else "write") if self.acked else "nack"
                elif self.state == "read" and self.ctrl_ack:
                    self.state = "nack"
                if self.state == "read":
                    self.read_i += 1
                    self._drive(c, not (self.read_data[self.read_i - 1] >> 7) & 1)
                else:
                    self._drive(c, 0)
            elif self.state == "read" and 1 <= self.pos <= 7:
                self._drive(c, not (self.read_data[self.read_i - 1] >> (7 - self.pos)) & 1)
        self.prev = (scl, sda)
        return 0, (scl << P_SCL) | (sda << P_SDA)


def i2c_times(changes):
    """Bus times in cycles from the bus level changes [(cycle, (scl, sda))] (UM10204 symbols)."""
    t = {k: [] for k in ("low", "high", "period", "hd_sta", "su_sta", "su_sto", "buf", "su_dat", "hd_dat", "both")}
    last_rise = last_fall = last_start = last_stop = rise_ok = sda_change = None
    start_open = False
    prev = changes[0][1]
    for c, (scl, sda) in changes[1:]:
        pscl, psda = prev
        if scl != pscl and sda != psda:
            t["both"].append(c)
        if scl != pscl:
            if scl:
                if last_fall is not None:
                    t["low"].append(c - last_fall)
                    if sda_change is not None and sda_change >= last_fall:
                        t["su_dat"].append(c - sda_change)
                if rise_ok is not None:
                    t["period"].append(c - rise_ok)
                last_rise = rise_ok = c
            else:
                if last_rise is not None:
                    t["high"].append(c - last_rise)
                if start_open:
                    t["hd_sta"].append(c - last_start)
                    start_open = False
                last_fall = c
        elif sda != psda:
            if scl:
                if sda == 0:
                    if last_rise is not None:
                        t["su_sta"].append(c - last_rise)
                    if last_stop is not None:
                        t["buf"].append(c - last_stop)
                    last_start, start_open = c, True
                else:
                    if last_rise is not None:
                        t["su_sto"].append(c - last_rise)
                    last_stop = c
                rise_ok = None
            else:
                if last_fall is not None:
                    t["hd_dat"].append(c - last_fall)
                sda_change = c
        prev = (scl, sda)
    return t


def check_i2c_times(log, changes, mode, f_mhz, label):
    """Each measured bus time against the UM10204 minimum of the mode (I2C_LIMITS)."""
    t = i2c_times(changes)
    lim = I2C_LIMITS[mode]
    assert not t["both"], f"{label}: SCL and SDA change in the same cycle at {t['both'][:5]}"
    text = []
    for key in ("low", "high", "hd_sta", "su_sta", "su_sto", "buf", "su_dat", "hd_dat"):
        if not t[key]:
            continue
        need = math.ceil(lim[key] * f_mhz - 1e-9)
        got = min(t[key])
        text.append(f"{key} {got} (>= {need})")
        assert got >= need, f"{label}: {key} {got} cycles, UM10204 {mode} minimum {lim[key]} us = {need} cycles"
    period = min(t["period"])
    need = math.ceil(f_mhz * 1000 / lim["f_khz"] - 1e-9)
    text.append(f"SCL period {period} (>= {need}, {f_mhz * 1000 / period:.1f} kHz)")
    assert period >= need, f"{label}: SCL period {period} cycles, more than {lim['f_khz']} kHz"
    log.info(f"{label}: bus times in cycles at {f_mhz} MHz: " + ", ".join(text))


I2C_ADDR = 0x50
I2C_READ = [0xA5, 0x5A]


def i2c_script():
    """Write 0x3C, repeated START, read 2 bytes (ACK, NACK + STOP), then a NACKed address + STOP."""
    words = (i2c_write(I2C_ADDR << 1, start=True) + i2c_write(0x3C)
             + i2c_write((I2C_ADDR << 1) | 1, start=True) + i2c_read(True) + i2c_read(False, stop=True)
             + i2c_write(0x51 << 1, start=True, stop=True))
    replies = [(I2C_ADDR << 2) | 0, (0x3C << 1) | 0, (((I2C_ADDR << 1) | 1) << 1) | 0,
               (I2C_READ[0] << 1) | 0, (I2C_READ[1] << 1) | 1, ((0x51 << 1) << 1) | 1]
    events = ["START", ("ADDR", I2C_ADDR, 0, 1), ("W", 0x3C), "START", ("ADDR", I2C_ADDR, 1, 1),
              ("R", I2C_READ[0], 0), ("R", I2C_READ[1], 1), "STOP", "START", ("ADDR", 0x51, 0, 0), "STOP"]
    return words, replies, events


def i2c_events(bus):
    return [e[1] if len(e) == 2 else e[1:] for e in bus.events]


def i2c_run(mode, f_mhz, stretch, words, base=0):
    prog = assemble("i2c_ctl.s", base, **I2C_EQU[(mode, f_mhz)])
    m = pe_model.PeModel(prog.words, start_pc=(base, 0, 0, 0),
                         owner=owners({P_SCL: 0, P_SDA: 0}), open_drain=open_drain({P_SCL, P_SDA}))
    bus = I2cBus(I2C_ADDR, I2C_READ, stretch=stretch)
    bench = Bench(m, inputs=bus)
    bench.watch("own", lambda uo, uio, oe: (level(uio, oe, P_SCL), level(uio, oe, P_SDA)))
    bench.send(0, *words)
    return bench, m, bus


# ----- tests -----


@cocotb.test()
async def test_fw_full_word_count(dut):
    """Word count of the full firmware against the 128-word memory (D2, D4 item 13)."""
    sizes = {}
    for name in sorted(set(FULL + SLOW + OUTLINE)):
        prog = pe_asm.assemble_file(os.path.join(ROOT, "fw", name))
        sizes[name] = prog.size
        dut._log.info(f"fw/{name}: {prog.size} words\n" + prog.listing())
    for label, files in (("outline (M2a)", OUTLINE), ("full, UART RX for 1,526 bit/s to 1 Mbit/s", FULL),
                         ("full, UART RX for 300 bit/s to 1,525 bit/s", SLOW)):
        total = sum(sizes[f] for f in files)
        dut._log.info(f"{label}: " + " + ".join(f"{f} {sizes[f]}" for f in files)
                      + f" = {total} of {pe_model.MEM_WORDS} words, margin {pe_model.MEM_WORDS - total}")
        assert total <= pe_model.MEM_WORDS, f"{label}: {total} words do not fit"


@cocotb.test(skip=GL)
async def test_fw_full_uart_tx(dut):
    """fw/uart_tx_full.s: 300 bit/s to 1 Mbit/s at 50 MHz and 40 MHz, 8N1, 8E1, 8N2, back-to-back."""
    rng = random.Random(31)
    cases = [(50e6, 1_000_000, "8N1", 8), (50e6, 921_600, "8E1", 6), (50e6, 115_200, "8N2", 6),
             (50e6, 9_600, "8N1", 3), (50e6, 1_200, "8E1", 2), (50e6, 300, "8N1", 1),
             (40e6, 1_000_000, "8N1", 8), (40e6, 921_600, "8N1", 6), (40e6, 115_200, "8E1", 4)]
    for f_clk, rate, fmt, n in cases:
        data = [0x55, 0x00, 0xFF][:n] + [rng.randrange(256) for _ in range(max(0, n - 3))]
        bench, m, frames, b = tx_run(f_clk, rate, fmt, data)
        k, c, _ = uart_cfg(f_clk, rate)
        check_tx(dut._log, bench.changes("tx"), b, f_clk / rate, frames,
                 f"uart_tx_full {f_clk / 1e6:.0f} MHz {rate} bit/s {fmt} (K {k}, C {c})")
        assert_status_clean(m, [0])


@cocotb.test(skip=GL)
async def test_fw_full_uart_rate_error(dut):
    """Divider error B = K x C against the nominal bit time for the common rates (calculation, A1 share 1 %)."""
    rates = [300, 600, 1_200, 2_400, 4_800, 9_600, 14_400, 19_200, 38_400, 57_600, 115_200, 230_400,
             460_800, 921_600, 1_000_000]
    for f_clk in (50e6, 40e6):
        worst = (0, 0)
        for rate in rates:
            _, _, b = uart_cfg(f_clk, rate)
            err = f_clk / rate / b - 1
            if abs(err) > abs(worst[1]):
                worst = (rate, err)
        dut._log.info(f"{f_clk / 1e6:.0f} MHz: largest divider error {worst[1] * 100:+.3f} % at {worst[0]} bit/s "
                      f"({len(rates)} rates, 300 to 1,000,000 bit/s)")
        assert abs(worst[1]) <= SHARE


@cocotb.test(skip=GL)
async def test_fw_full_uart_rx_tolerance(dut):
    """fw/uart_rx_full.s at 1 Mbit/s: sender bit-time error that it accepts, for first-sample times H = x B."""
    best = {}
    for f_clk in (50e6, 40e6):
        b = round(f_clk / 1_000_000)
        rows = []
        for x in (0.30, 0.35, 0.40, 0.45, 0.50):
            h = max(12, round(x * b))
            fast, slow = rx_limits(b, h)
            rows.append((min(fast, slow), x, h, fast, slow))
            dut._log.info(f"uart_rx_full {f_clk / 1e6:.0f} MHz, 1 Mbit/s, B {b}, H {h} ({x:.2f} B): "
                          f"accepts sender bit time -{fast * 100:.1f} % to +{slow * 100:.1f} %")
        best[f_clk] = max(rows)
        _, x, h, fast, slow = best[f_clk]
        dut._log.info(f"uart_rx_full {f_clk / 1e6:.0f} MHz, 1 Mbit/s: best H {h} ({x:.2f} B): "
                      f"-{fast * 100:.1f} % to +{slow * 100:.1f} % (A1 needs +/-{TOL * 100:.1f} %)")
    h35 = round(0.35 * 50)
    fast, slow = rx_limits(50, h35)
    assert fast >= TOL and slow >= TOL, f"50 MHz, H {h35}: -{fast:.3f} to +{slow:.3f}"


@cocotb.test(skip=GL)
async def test_fw_full_uart_rx(dut):
    """fw/uart_rx_full.s and fw/uart_rx_slow.s: rates, formats, +/-3.3 % senders, glitch, back-to-back."""
    rng = random.Random(77)
    cases = [(50e6, 1_000_000, "8N1", None), (50e6, 115_200, "8E1", None), (50e6, 9_600, "8N2", None),
             (50e6, 1_526, "8N1", None), (40e6, 115_200, "8N1", None),
             (50e6, 1_200, "8N1", 2), (50e6, 300, "8E1", 6)]
    for f_clk, rate, fmt, slow_k in cases:
        b_nom = f_clk / rate
        k, c, b = uart_cfg(f_clk, rate)
        if slow_k is not None:
            assert k == slow_k, (rate, k)
        h = k * round(0.35 * c)
        n = 2 if slow_k else 6
        for err in (-TOL, TOL):
            data = [rng.randrange(256) for _ in range(n)]
            ok, words, m, frames, nd1 = rx_run(b, h, fmt, b_nom * (1 + err), data, slow_k=slow_k, glitch=True)
            name = "uart_rx_slow" if slow_k else "uart_rx_full"
            assert ok, f"{name} {rate} bit/s {fmt}, sender {err * 100:+.1f} %: words {[hex(w) for w in words]}"
            dut._log.info(f"{name} {f_clk / 1e6:.0f} MHz {rate} bit/s {fmt} (B {b} = {k} x {c}, H {h}): "
                          f"sender {err * 100:+.1f} %, {n} back-to-back frames correct, 3-cycle glitch ignored")
    for f_clk in (50e6,):
        b = round(f_clk / 1e6)
        ok, words, m, frames, nd1 = rx_run(b, round(0.35 * b), "8N1", b, [0x55, 0xAA])
        frames[1][1][9] = 0
    data = [0x3C, 0xC3]
    line_bits = [frame_bits(d, "8N1") for d in data]
    line_bits[0][9] = 0  # stop bit 0: framing error
    frames = [(2000 + i * 10 * 434, bits) for i, bits in enumerate(line_bits)]
    line = uart_line(frames, 434)
    m = pe_model.PeModel(assemble("uart_rx_full.s").words)
    bench = Bench(m, inputs=lambda c, uo, uio, oe: (value_at(line, c) << UI_RX, 0))
    bench.send(0, 434, 152)
    bench.run(2000 + 21 * 434)
    words = bench.words(0)
    assert [rx_bits(w, 9) for w in words] == [b[:10] for b in line_bits], [hex(w) for w in words]
    dut._log.info("uart_rx_full: stop bit 0 is in the word (bit 15 = 0, framing error), next frame correct")


@cocotb.test(skip=GL)
async def test_fw_full_spi(dut):
    """fw/spi_ctl.s: 4 modes, data both ways, CS, SCK idle level; smallest half period for D = 0 and D = 7."""
    for d in (0, 7):
        for cpol in (0, 1):
            for cpha in (0, 1):
                h = spi_h_min(cpol, cpha, d)
                for hh in range(h, h + 12):
                    bench, m, dev = spi_run(cpol, cpha, hh, d)
                    assert spi_ok(bench, m, dev), f"mode {cpol}{cpha} D {d}: H {hh} fails, H {h} passes"
                bench, m, dev = spi_run(cpol, cpha, h, d)
                periods, n = spi_half_periods(bench)
                assert periods == {h} and n == 16 * len(SPI_WORDS), (periods, n)
                dut._log.info(f"spi_ctl CPOL {cpol} CPHA {cpha} D {d}: smallest H {h} cycles "
                              f"(SCK {50e3 / (2 * h):.0f} kHz at 50 MHz, {40e3 / (2 * h):.0f} kHz at 40 MHz); "
                              f"H {h} to {h + 11} correct; {n} SCK edges, each {h} cycles apart in a byte")


@cocotb.test(skip=GL)
async def test_fw_full_i2c(dut):
    """fw/i2c_ctl.s: write, repeated START, read with ACK and NACK, STOP, NACKed address; UM10204 times."""
    words, replies, events = i2c_script()
    for mode, f_mhz in (("Fm", 50), ("Sm", 50), ("Fm", 40)):
        for stretch in (0, 300):
            bench, m, bus = i2c_run(mode, f_mhz, stretch, words)
            bench.run(30_000 if mode == "Fm" else 80_000)
            assert bench.words(0) == replies, f"{mode} {f_mhz}: replies {[hex(w) for w in bench.words(0)]}"
            assert i2c_events(bus) == events, f"{mode} {f_mhz}: bus events {i2c_events(bus)}"
            assert_status_clean(m, [0])
            check_i2c_times(dut._log, bus.changes, mode, f_mhz, f"i2c_ctl {mode} stretch {stretch}")
            if stretch:
                lows = i2c_times(bus.changes)["low"]
                assert max(lows) >= stretch, f"no stretched low period: {max(lows)}"
    dut._log.info("i2c_ctl: 6 bytes, 11 bus events and 6 replies correct in Fm 50 MHz, Sm 50 MHz, Fm 40 MHz, "
                  "with and without 300-cycle clock stretching")


@cocotb.test(skip=GL)
async def test_fw_full_i2c_arbitration(dut):
    """fw/i2c_ctl.s: another controller pulls SDA low in a bit where we send 1; we stop at once and report it."""
    words = i2c_write(I2C_ADDR << 1, start=True) + i2c_write(0x3C, stop=True)
    for bit in (1, 3):  # address 0xA0: bits 1 and 3 are 1
        bench, m, bus = i2c_run("Fm", 50, 0, words)
        bus.lose_at = (0, bit)
        bench.run(6000)
        got = bench.words(0)
        assert len(got) == 1 and got[0] & 0x8000, f"bit {bit}: replies {[hex(w) for w in got]}"
        own = bench.changes("own")
        t_lost = bench.from_thread[0][0][0]
        late = [(c, v) for c, v in own if c > t_lost and 0 in v]
        assert not late, f"bit {bit}: the controller drives low after the loss: {late[:4]}"
        assert own[-1][1] == (None, None), own[-1]
        assert_status_clean(m, [0])
        dut._log.info(f"i2c_ctl arbitration lost in address bit {bit}: reply 0x{got[0]:04X} (bit 15 = 1), "
                      f"SCL and SDA released from cycle {own[-1][0]}, no more low level")


@cocotb.test(skip=GL)
async def test_fw_full_i2c_spikes(dut):
    """D4 item 8 measurement: 40 ns and 60 ns spikes (2, 3 cycles at 50 MHz) on SCL and SDA, no input filter."""
    words, replies, events = i2c_script()
    results = []
    for line, width in (("SCL", 2), ("SCL", 3), ("SDA", 2), ("SDA", 3)):
        bench, m, bus = i2c_run("Fm", 50, 300, words)
        if line == "SCL":
            bus.spike_after_ack = (1, 150, width)  # in the stretch after the data byte 0x3C
        else:
            bus.sda_spike_at = (0, 1, I2C_EQU[("Fm", 50)]["TSMP"] - 1, width)  # first address bit (1): sample point
        bench.run(30_000)
        ok = bench.words(0) == replies and i2c_events(bus) == events and status_clean(m, [0])
        results.append((line, width, ok))
        dut._log.info(f"i2c_ctl Fm, {width}-cycle ({width * 20} ns) spike on {line}: "
                      f"{'transfer correct' if ok else 'transfer NOT correct'}; replies "
                      f"{[hex(w) for w in bench.words(0)]}; bus events {i2c_events(bus)[:6]}")
    assert not any(ok for _, _, ok in results), f"a spike had no effect: {results}"


def all_four(f_clk, err, rx_name="uart_rx_full.s", rate=1_000_000, slow_k=None):
    """The four protocols at the same time from one memory: thread 0 UART TX, 1 UART RX, 2 SPI, 3 I2C."""
    files = ("uart_tx_full.s", rx_name, "spi_ctl.s", "i2c_ctl.s")
    f_mhz = round(f_clk / 1e6)
    equ = {"uart_tx_full.s": {}, rx_name: dict(ND1=9, **({"K": slow_k} if slow_k else {})),
           "spi_ctl.s": dict(CPOL=0, CL=0, D=0), "i2c_ctl.s": I2C_EQU[("Fm", f_mhz)]}
    bases, image, addr = [], {}, 0
    for f in files:
        prog = assemble(f, addr, **equ[f])
        bases.append(addr)
        image.update(prog.words)
        addr += prog.size
    assert addr <= pe_model.MEM_WORDS, f"{addr} words"
    m = pe_model.PeModel(image, start_pc=tuple(bases), run=(True,) * 4,
                         owner=owners({P_TX: 0, P_CS: 2, P_MOSI: 2, P_SCK: 2, P_SCL: 3, P_SDA: 3}),
                         open_drain=open_drain({P_SCL, P_SDA}))
    k, c, b = uart_cfg(f_clk, rate)
    rng = random.Random(f_mhz)
    tx_data = [rng.randrange(256) for _ in range(12 if slow_k is None else 1)]
    rx_data = [rng.randrange(256) for _ in range(12 if slow_k is None else 1)]
    tx_frames = [frame_bits(d, "8N1") for d in tx_data]
    t, rx_frames = 600, []
    for d in rx_data:
        rx_frames.append((t, frame_bits(d, "8N1")))
        t += round(10 * b * (1 + err))
    line = uart_line(rx_frames, b * (1 + err))
    h_spi = spi_h_min(0, 0, 0)
    spi = SpiDevice(0, 0, SPI_DEV)
    words, replies, events = i2c_script()
    bus = I2cBus(I2C_ADDR, I2C_READ, stretch=300)

    def inputs(cy, uo, uio, oe):
        _, miso = spi(cy, uo, uio, oe)
        _, i2c = bus(cy, uo, uio, oe)
        return value_at(line, cy) << UI_RX, miso | i2c

    bench = Bench(m, inputs=inputs)
    bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
    bench.send(0, c, k, *[frame_word(f) for f in tx_frames])
    bench.send(1, *([b, k * round(0.35 * c)] if slow_k is None else [c, round(0.35 * c)]))
    bench.send(2, h_spi, *SPI_WORDS)
    bench.send(3, *words)
    bench.run(max(t, len(tx_frames) * 10 * b) + 3 * b + 30_000)
    return dict(m=m, bench=bench, b=b, tx_frames=tx_frames, rx_frames=rx_frames, spi=spi, bus=bus,
                replies=replies, events=events, h_spi=h_spi, words=addr, f_clk=f_clk, rate=rate)


def check_all_four(log, r, label):
    bench, m = r["bench"], r["m"]
    check_tx(log, bench.changes("tx"), r["b"], r["f_clk"] / r["rate"], r["tx_frames"], f"{label}: uart_tx")
    got = bench.words(1)
    assert [rx_bits(w, 9) for w in got] == [bits[:10] for _, bits in r["rx_frames"]], \
        f"{label}: uart_rx words {[hex(w) for w in got]}"
    assert r["spi"].rx == [w & 0xFF for w in SPI_WORDS] and not r["spi"].errors, (r["spi"].rx, r["spi"].errors)
    assert [w & 0xFF for w in bench.words(2)] == SPI_DEV
    assert bench.words(3) == r["replies"], [hex(w) for w in bench.words(3)]
    assert i2c_events(r["bus"]) == r["events"], i2c_events(r["bus"])
    check_i2c_times(log, r["bus"].changes, "Fm", round(r["f_clk"] / 1e6), f"{label}: i2c")
    assert_status_clean(m, [0, 1, 2, 3])
    log.info(f"{label}: {r['words']} words in one memory; UART TX {len(r['tx_frames'])} frames, UART RX "
             f"{len(got)} frames, SPI {len(SPI_WORDS)} bytes at H {r['h_spi']}, I2C 6 bytes with stretching: "
             f"all correct at the same time, no LATE, no ERR")


@cocotb.test(skip=GL)
async def test_fw_full_all_four(dut):
    """All four protocols at the same time, worst case of A1 section 3: UART 1 Mbit/s, sender +/-3.3 %, SPI at its
    smallest half period, I2C Fast-mode with clock stretching; and the image with fw/uart_rx_slow.s."""
    for err in (-TOL, TOL):
        r = all_four(50e6, err)
        check_all_four(dut._log, r, f"all four, 50 MHz, UART sender {err * 100:+.1f} %")
    r = all_four(40e6, 0.0)
    check_all_four(dut._log, r, "all four, 40 MHz, UART sender 0 %")
    r = all_four(50e6, TOL, rx_name="uart_rx_slow.s", rate=1_200, slow_k=2)
    check_all_four(dut._log, r, "all four with uart_rx_slow, 50 MHz, 1,200 bit/s, UART sender +3.3 %")

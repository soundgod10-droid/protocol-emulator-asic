# SPDX-License-Identifier: Apache-2.0
"""Hold-out trials: protocols that the chip was not designed for, in the model against device models.

What it does:
  Each protocol here was written after the instruction set was fixed, from its public
  documents only, with the unchanged instruction set and assembler. These tests run the
  firmware in the reference model (model/pe_model.py) against a device model written from
  the same documents, at the extremes of the device timing windows, and check every bus time
  of the firmware against the limits of the documents. The device models and the time checks
  do not use the firmware or the processor model. The word count shows which other firmware
  fits next to each program.

  1-Wire (fw/onewire.s): written from Analog Devices application note 126 ("1-Wire
  Communication Through Software", Tables 1 and 2); device model from the DS18B20 data sheet
  (Maxim 19-7487 Rev 6, 7/19: AC table page 3, reset and presence page 15, time slots page 16).

  AM2302 (fw/am2302.s) and PDI (fw/pdi.s) were chosen by a stated rule: the first two entries of the
  sigrok libsigrokdecode decoder list (decoders/, alphabetical) that read logic channels, are a link
  between two devices, are not in fw/ and not named on the competition page, can be made with 3.3 V
  pins and external parts, allow a bit time of 1 us or more, and have a public specification.
  AM2302: device model from the AM2302 data sheet (Aosong; start signal and bit times page 4, data
  format page 3, supply page 5). PDI: device model from the XMEGA AU manual (Atmel 8331F-AVR-04/2013,
  section 32: physical layer 32.3, controller and instruction set 32.5 and 32.6, registers 32.7).
"""

import os
import re
import sys
from collections import deque

import cocotb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
import pe_asm  # noqa: E402
import pe_model  # noqa: E402

GL = os.environ.get("GATES") == "yes"


def assemble(name, **equ):
    """fw/name with some .equ values changed (the host sets them for its clock when it assembles)."""
    with open(os.path.join(ROOT, "fw", name), encoding="utf-8") as f:
        text = f.read()
    for key, val in equ.items():
        pattern = re.compile(r"^(\s*\.equ\s+" + key + r"\s*,)[^;\n]*", re.M)
        text, n = pattern.subn(lambda m: f"{m.group(1)} {val} ", text)
        assert n == 1, f"{name}: .equ {key} found {n} times"
    return pe_asm.assemble(text, name, 0)


# ----- 1-Wire -----

P_OW = 4  # uio[4]: output pin 4 and input pin 4 (docs/isa.md section 5.1)

# DS18B20 data sheet, AC Electrical Characteristics (page 3), in us
T_SLOT = (60, 120)  # tSLOT
T_REC = 1  # tREC minimum
T_LOW0 = (60, 120)  # tLOW0
T_LOW1 = (1, 15)  # tLOW1
T_RSTL = 480  # tRSTL minimum
T_RSTH = 480  # tRSTH minimum

# .equ values of fw/onewire.s for each clock (MHz): the AN126 times (with C 65 us, D 5 us) in cycles.
OW_EQU = {
    50: {},  # the values written in the file
    60: dict(TA=360, TE=540, TF=3300, TC=3900, TD=300, TI2=2100, H32=919, J32=788),
}

ROM = [0x28, 0x1D, 0x39, 0x31, 0x02, 0x00, 0x00, 0xF0]  # family code 0x28, serial number, CRC byte
SCRATCH = [0x50, 0x05, 0x4B, 0x46, 0x7F, 0xFF, 0x0C, 0x10, 0x1C]  # 9 bytes (page 7 layout)


class OneWireDevice:
    """1-Wire device from the DS18B20 data sheet, not from the firmware.

    Reset (page 15): the bus low for tRSTL (480 us) or more; after the rising edge the device
    waits t_pdh (tPDHIGH, 15 to 60 us) and pulls the bus low for t_pdl (tPDLOW, 60 to 240 us).
    Write slots (page 16): it samples the bus t_s after the falling edge (window 15 to 60 us).
    Read slots (page 16): for a 0 it pulls the bus low from the falling edge for t_rd (data valid
    for tRDV = 15 us after the falling edge, page 3). Commands: 0x33 Read ROM (8 bytes out),
    0xCC Skip ROM, then 0x4E Write Scratchpad (3 bytes in: TH, TL, configuration) or
    0xBE Read Scratchpad (9 bytes out).
    """

    def __init__(self, us, t_s=30, t_pdh=30, t_pdl=120, t_rd=15, present=True):
        self.us = us
        self.t_s, self.t_pdh, self.t_pdl, self.t_rd = t_s * us, t_pdh * us, t_pdl * us, t_rd * us
        self.present = present
        self.scratch = list(SCRATCH)
        self.prev = 1
        self.fall = None
        self.pull = (0, 0)  # cycles [from, to) in which the device pulls the bus low
        self.sample_at = None
        self.state = "idle"
        self.bits = []
        self.tx = deque()
        self.count = 0
        self.received = []  # (state, byte)
        self.resets = 0
        self.master = [(0, None)]  # changes of what the controller drives: 0 or None (released)

    def _send(self, data):
        for byte in data:
            self.tx.extend((byte >> i) & 1 for i in range(8))

    def _byte(self, byte):
        self.received.append((self.state, byte))
        if self.state == "rom":
            if byte == 0x33:
                self._send(ROM)
                self.state = "tx"
            elif byte == 0xCC:
                self.state = "func"
            else:
                self.state = "idle"
        elif self.state == "func":
            if byte == 0x4E:
                self.state, self.count = "wscr", 0
            elif byte == 0xBE:
                self._send(self.scratch)
                self.state = "tx"
            else:
                self.state = "idle"
        elif self.state == "wscr":
            self.scratch[2 + self.count] = byte
            self.count += 1
            if self.count == 3:
                self.state = "idle"

    def __call__(self, c, uo, uio, oe):
        drive = 0 if (oe >> P_OW) & 1 and not (uio >> P_OW) & 1 else None
        released = drive is None and self.master[-1][1] == 0
        if drive != self.master[-1][1]:
            self.master.append((c, drive))
        if released and self.fall is not None and c - self.fall >= T_RSTL * self.us:  # end of a reset pulse
            self.resets += 1
            self.state, self.bits, self.tx, self.sample_at = "rom", [], deque(), None
            if self.present:
                self.pull = (c + self.t_pdh, c + self.t_pdh + self.t_pdl)
        dev_low = self.pull[0] <= c < self.pull[1]
        bus = 0 if drive == 0 or dev_low else 1
        if self.prev == 1 and bus == 0 and drive == 0:  # falling edge made by the controller
            self.fall = c
            if self.state == "tx" and self.tx:
                if self.tx.popleft() == 0:
                    self.pull = (c, c + self.t_rd)
                if not self.tx:
                    self.state = "idle"
            elif self.state in ("rom", "func", "wscr"):
                self.sample_at = c + self.t_s
        if self.sample_at == c:
            self.sample_at = None
            self.bits.append(bus)
            if len(self.bits) == 8:
                byte = sum(b << i for i, b in enumerate(self.bits))
                self.bits = []
                self._byte(byte)
        self.prev = bus
        return 0, bus << P_OW


def ow_check_times(changes, us):
    """Controller low and high times against the DS18B20 limits; returns the counts of each kind."""
    lows = [(c, changes[i + 1][0]) for i, (c, v) in enumerate(changes[:-1]) if v == 0]
    kinds = {"reset": 0, "low1": 0, "low0": 0}
    for i, (a, b) in enumerate(lows):
        n = b - a
        if n >= T_RSTL * us:
            kinds["reset"] += 1
            if i + 1 < len(lows):
                assert lows[i + 1][0] - b >= T_RSTH * us, f"reset high time {lows[i + 1][0] - b} cycles < tRSTH"
            continue
        if T_LOW1[0] * us <= n <= T_LOW1[1] * us:
            kinds["low1"] += 1
        elif T_LOW0[0] * us <= n <= T_LOW0[1] * us:
            kinds["low0"] += 1
        else:
            raise AssertionError(f"low time {n} cycles at {a}: neither tLOW1 nor tLOW0 nor a reset")
        if i + 1 < len(lows) and lows[i + 1][1] - lows[i + 1][0] < T_RSTL * us:
            nxt = lows[i + 1][0]
            assert nxt - b >= T_REC * us, f"recovery {nxt - b} cycles at {b} < tREC"
            assert nxt - a >= (T_SLOT[0] + T_REC) * us, f"slot {nxt - a} cycles at {a} < tSLOT + tREC"
    return kinds


def ow_run(dev, words, f_mhz):
    prog = assemble("onewire.s", **OW_EQU[f_mhz])
    owner = [None] * pe_model.N_OUT
    owner[P_OW] = 0
    od = [p == P_OW for p in range(pe_model.N_OUT)]
    m = pe_model.PeModel(prog.words, owner=owner, open_drain=od)
    to_thread, got = deque(words), []
    resets = sum(1 for w in words if w & 0x100)
    cycles = 2000 + resets * 1000 * f_mhz + (len(words) - resets) * 8 * 72 * f_mhz
    for _ in range(cycles):
        ui, uio = dev(m.cycle, *m.pins())
        m.set_inputs(ui, uio)
        if to_thread and m.fifo_push(0, to_thread[0]):
            to_thread.popleft()
        m.step()
        w = m.fifo_pop(0)
        while w is not None:
            got.append(w)
            w = m.fifo_pop(0)
    th = m.threads[0]
    assert th.status == 0 and th.run, f"thread status {th.status}, run {th.run}, {th.stop_reason}"
    return prog, got


OW_RESET = 0x100
OW_SCRIPT = ([OW_RESET, 0x33] + [0xFF] * 8
             + [OW_RESET, 0xCC, 0x4E, 0x19, 0xE6, 0x3F]
             + [OW_RESET, 0xCC, 0xBE] + [0xFF] * 9)


def ow_expected(present=True):
    p = 0 if present else 1
    scratch = list(SCRATCH)
    scratch[2:5] = [0x19, 0xE6, 0x3F]
    return [p, 0x33] + ROM + [p, 0xCC, 0x4E, 0x19, 0xE6, 0x3F] + [p, 0xCC, 0xBE] + scratch


@cocotb.test(skip=GL)
async def test_holdout_onewire(dut):
    """fw/onewire.s at 50 and 60 MHz: Read ROM, Write and Read Scratchpad, at the extremes of the device timing windows."""
    cases = [  # t_s, t_pdh, t_pdl, t_rd (us)
        (15, 15, 60, 15), (60, 60, 60, 15), (30, 30, 240, 30), (60, 15, 240, 15), (15, 60, 120, 60)]
    for f_mhz in (50, 60):
        for t_s, t_pdh, t_pdl, t_rd in cases:
            dev = OneWireDevice(f_mhz, t_s, t_pdh, t_pdl, t_rd)
            prog, got = ow_run(dev, OW_SCRIPT, f_mhz)
            label = (f"{f_mhz} MHz, device sample {t_s} us, presence wait {t_pdh} us, presence low {t_pdl} us, "
                     f"read hold {t_rd} us")
            assert got == ow_expected(), f"{label}: replies {[hex(w) for w in got]}"
            assert dev.resets == 3, f"{label}: {dev.resets} resets seen by the device"
            assert dev.scratch[2:5] == [0x19, 0xE6, 0x3F], f"{label}: scratchpad {dev.scratch}"
            kinds = ow_check_times(dev.master, f_mhz)
            dut._log.info(f"onewire {label}: {len(got)} replies correct; controller times within the DS18B20 "
                          f"limits ({kinds['reset']} resets, {kinds['low1']} write-1/read slots, "
                          f"{kinds['low0']} write-0 slots)")
        dev = OneWireDevice(f_mhz, present=False)
        _, got = ow_run(dev, [OW_RESET], f_mhz)
        assert got == [1], f"{f_mhz} MHz, no device: replies {got}"
        dut._log.info(f"onewire {f_mhz} MHz: no device on the bus gives presence reply 1")


@cocotb.test()
async def test_holdout_onewire_words(dut):
    """Memory words of fw/onewire.s, and which other firmware fits next to it in the 128 words."""
    size = {}
    for name in ("onewire.s", "uart_tx_full.s", "uart_rx_full.s", "spi_ctl.s", "i2c_ctl.s", "spi_tgt.s"):
        size[name] = pe_asm.assemble_file(os.path.join(ROOT, "fw", name)).size
    dut._log.info("words: " + ", ".join(f"{k} {v}" for k, v in size.items()))
    four = size["uart_tx_full.s"] + size["uart_rx_full.s"] + size["spi_ctl.s"] + size["i2c_ctl.s"]
    for others in (("uart_tx_full.s", "uart_rx_full.s", "spi_ctl.s"), ("uart_tx_full.s", "uart_rx_full.s", "i2c_ctl.s"),
                   ("spi_ctl.s", "i2c_ctl.s"), ("uart_tx_full.s", "uart_rx_full.s", "spi_ctl.s", "i2c_ctl.s")):
        total = size["onewire.s"] + sum(size[o] for o in others)
        dut._log.info(f"onewire.s + {' + '.join(others)}: {total} of {pe_model.MEM_WORDS} words "
                      f"({'fits' if total <= pe_model.MEM_WORDS else 'does not fit'})")
    dut._log.info(f"the four example programs use {four} words; with onewire.s {four + size['onewire.s']}")
    assert size["onewire.s"] == 40, size["onewire.s"]


# ----- AM2302 -----

P_AM = 5  # uio[5]: output pin 5 and input pin 5

# .equ values of fw/am2302.s for each clock (MHz); the file has the 60 MHz values.
AM_EQU = {
    60: {},
    50: dict(T48=2400),
}

# Data sheet examples (page 3): 65.2 %RH with 35.1 C, and a temperature of -10.1 C (bit 15 = sign).
AM_DATA = [(0x028C, 0x015F), (0x0190, 0x8065)]


def am_sum(hum, tmp):
    """Check sum: the low 8 bits of the sum of the four data bytes (data sheet page 3)."""
    return ((hum >> 8) + (hum & 0xFF) + (tmp >> 8) + (tmp & 0xFF)) & 0xFF


class Am2302Device:
    """AM2302 (DHT22) sensor from its data sheet (Aosong, copy at cdn-shop.adafruit.com, pages 3 to 5),
    not from the firmware.

    Start signal (page 4): the bus low for 1 ms or more ("1ms minimum", figure). After the host releases
    the bus the sensor waits t_go (20 to 40 us), pulls the bus low for 80 us and releases it for 80 us.
    Then 40 bits, MSB first (16 bits humidity, 16 bits temperature, 8 bits check sum, page 3): each bit
    is low for 50 us, then high for 26 to 28 us ("0", 27 here) or 70 us ("1"). The data sheet gives
    typical values only, so scale multiplies every time of the sensor (a slower or faster sensor clock).
    end_low: after the last bit the sensor pulls the bus low for 50 us. The figures on page 4 end each
    high time with the low of the next bit, so a final "0" needs this low (without it, a final "0" is
    a "1" on the bus). end_low=False is used only for a sensor that stops: nbits < 40.
    """

    def __init__(self, us, hum, tmp, scale=1.0, t_go=30, end_low=True, present=True, nbits=40):
        self.us = us
        self.data = (hum << 24) | (tmp << 8) | am_sum(hum, tmp)
        self.scale, self.t_go, self.end_low, self.present, self.nbits = scale, t_go, end_low, present, nbits
        self.lows = deque()  # sensor low intervals [from, to) in cycles
        self.host_from = None
        self.host_lows = []  # lengths of the host low pulses, cycles
        self.starts = 0
        self.conflicts = 0  # cycles in which the host pulled the bus low while the sensor did

    def _cycles(self, t_us):
        return round(t_us * self.scale * self.us)

    def _answer(self, c):
        t = c + round(self.t_go * self.us)
        self.lows.append((t, t + self._cycles(80)))
        t += self._cycles(160)
        for i in range(self.nbits):
            bit = (self.data >> (39 - i)) & 1
            self.lows.append((t, t + self._cycles(50)))
            t += self._cycles(50 + (70 if bit else 27))
        if self.end_low:
            self.lows.append((t, t + self._cycles(50)))

    def __call__(self, c, uo, uio, oe):
        host_low = bool((oe >> P_AM) & 1) and not (uio >> P_AM) & 1
        if host_low and self.host_from is None:
            self.host_from = c
        elif not host_low and self.host_from is not None:
            n = c - self.host_from
            self.host_lows.append(n)
            self.host_from = None
            if self.present and n >= 1000 * self.us and not self.lows:
                self.starts += 1
                self._answer(c)
        while self.lows and c >= self.lows[0][1]:
            self.lows.popleft()
        dev_low = bool(self.lows) and self.lows[0][0] <= c
        if host_low and dev_low:
            self.conflicts += 1
        bus = 0 if host_low or dev_low else 1
        return 0, bus << P_AM


def am_run(dev, f_mhz):
    """One reading: the host sends one word. Returns the program and the replies."""
    prog = assemble("am2302.s", **AM_EQU[f_mhz])
    owner = [None] * pe_model.N_OUT
    owner[P_AM] = 0
    od = [p == P_AM for p in range(pe_model.N_OUT)]
    m = pe_model.PeModel(prog.words, owner=owner, open_drain=od)
    m.fifo_push(0, 1)
    got = []
    for _ in range(12000 * f_mhz):  # 12 ms: start signal 2.4 ms at most, answer about 5 ms
        ui, uio = dev(m.cycle, *m.pins())
        m.set_inputs(ui, uio)
        m.step()
        w = m.fifo_pop(0)
        while w is not None:
            got.append(w)
            w = m.fifo_pop(0)
        if got and (got[0] != 0 or len(got) == 4):
            break
    th = m.threads[0]
    assert th.status == 0 and th.run, f"thread status {th.status}, run {th.run}, {th.stop_reason}"
    return prog, got


@cocotb.test(skip=GL)
async def test_holdout_am2302(dut):
    """fw/am2302.s at 50 and 60 MHz: the data sheet examples, sensor times 0.8 to 1.2 times the data sheet,
    answer after 20 to 40 us; no sensor; a sensor that stops after 20 bits."""
    cases = [  # data set, scale, t_go (us), end_low
        (0, 1.0, 30, True), (0, 0.8, 20, True), (0, 1.2, 40, True), (1, 0.8, 40, True), (1, 1.2, 20, True)]
    for f_mhz in (50, 60):
        for k, scale, t_go, end_low in cases:
            hum, tmp = AM_DATA[k]
            dev = Am2302Device(f_mhz, hum, tmp, scale=scale, t_go=t_go, end_low=end_low)
            _, got = am_run(dev, f_mhz)
            label = (f"{f_mhz} MHz, humidity 0x{hum:04X}, temperature 0x{tmp:04X}, sensor times x {scale}, "
                     f"answer after {t_go} us, end low {end_low}")
            assert got == [0, hum, tmp, am_sum(hum, tmp)], f"{label}: replies {[hex(w) for w in got]}"
            assert dev.starts == 1 and dev.conflicts == 0, f"{label}: starts {dev.starts}, conflicts {dev.conflicts}"
            start_ms = dev.host_lows[0] / (1000 * f_mhz)
            assert 1.0 <= start_ms <= 10.0, f"{label}: start signal {start_ms} ms"
            dut._log.info(f"am2302 {label}: replies correct; start signal {start_ms:.3f} ms")
        dev = Am2302Device(f_mhz, *AM_DATA[0], present=False)
        _, got = am_run(dev, f_mhz)
        assert got == [0x8000 | 41], f"{f_mhz} MHz, no sensor: replies {[hex(w) for w in got]}"
        dev = Am2302Device(f_mhz, *AM_DATA[0], end_low=False, nbits=20)
        _, got = am_run(dev, f_mhz)
        assert got == [0x8000 | 12], f"{f_mhz} MHz, sensor stops after 20 bits: replies {[hex(w) for w in got]}"
        dut._log.info(f"am2302 {f_mhz} MHz: no sensor gives 0x8029; a sensor that stops after 20 bits gives 0x800C")


# ----- PDI -----

P_CLK, P_DAT = 2, 3  # uio[2] PDI_CLK, uio[3] PDI_DATA

# .equ values of fw/pdi.s for each clock (MHz): PDI_CLK 1 MHz, 50 us of DATA high before the first clock.
PDI_EQU = {60: dict(HALF=30, TEN=3000), 50: dict(HALF=25, TEN=2500)}

PDI_KEY = [0xFF, 0x88, 0xD8, 0xCD, 0x45, 0xAB, 0x89, 0x12]  # 0x1289AB45CDD888FF (32.5.3), LSB first (32.6.2)
PDI_GUARD = [128, 64, 32, 16, 8, 4, 2, 2]  # IDLE bits for CTRL.GUARDTIME 0 to 7 (Table 32-1)


def pdi_frame(byte, parity_ok=True):
    """Host side: the 12 line bits of a DATA character, bit 0 first (32.3.3)."""
    par = (bin(byte).count("1") & 1) ^ (0 if parity_ok else 1)
    return (byte << 1) | (par << 9) | (3 << 10)


def pdi_byte(frame):
    """Host side: the data byte of a received character, after the start, parity and stop checks."""
    byte = (frame >> 1) & 0xFF
    assert frame & 1 == 0 and (frame >> 10) & 3 == 3, f"frame 0x{frame:03X}: start or stop bit wrong"
    assert (frame >> 9) & 1 == bin(byte).count("1") & 1, f"frame 0x{frame:03X}: parity wrong"
    return byte


def pdi_result(got):
    """Received bytes; None for a 0xFFFF reply (no start bit)."""
    return [None if w == 0xFFFF else pdi_byte(w) for w in got]


class PdiDevice:
    """PDI of an AVR XMEGA device from the XMEGA AU manual (Atmel 8331F-AVR-04/2013, section 32), not
    from the firmware.

    Physical layer (32.3): PDI_CLK is the RESET pin; here it is high when nobody drives it. Enable
    (32.3.1): DATA high for t_rst (the reset pulse width of the device data sheet; test values) turns
    the RESET function off; then 16 PDI_CLK cycles with DATA high, the first one not later than 100 us
    after that. A PDI_CLK slower than about 10 kHz (here: no rising edge for 100 us) turns the PDI off
    (32.3.2). Characters (32.3.3): start 0, 8 data bits LSB first, even parity, two stop bits 1;
    BREAK = 12 bits low. Both sides change DATA at the falling edge and sample it at the rising edge
    (32.3.4); the device output changes d_out cycles after the falling edge. From RX to TX the device
    sends GUARDTIME IDLE bits first, from TX to RX there is no guard time (32.3.7). DATA keeps its last
    level when nobody drives it (bus keeper, 32.3.8). A parity or frame error puts the PDI in an ERROR
    state until a BREAK (32.5.4).
    Controller (32.5.6, 32.6, Figure 32-14): LDS, STS, LD and ST with *(ptr), *(ptr++) and ptr, LDCS,
    STCS, KEY and REPEAT (the next instruction runs count + 1 times, 32.6.3); registers STATUS (NVMEN,
    set by the key), RESET (0x59 holds the device in reset) and CTRL (GUARDTIME) (32.7). A data space
    access before the key is recorded as a fault.
    """

    def __init__(self, us, t_rst=1.0, d_out=1, undriven=0, mem=None):
        self.us = us
        self.t_rst = round(t_rst * us)
        self.d_out = d_out
        self.keeper = undriven
        self.mem = dict(mem or {})
        self.c = 0
        self.faults = []
        self.prev_clk = 1
        self.hi_since = None
        self.rst_off = None
        self.first_fall = None
        self.en_clocks = 0
        self.enabled = False
        self.last_rise = None
        self.rx = []
        self.error = False
        self.tx_mode = False
        self.tx = deque()
        self.drive = None
        self.pending = None
        self.guard_code = 0
        self.nvmen = 0
        self.in_reset = 0
        self.ptr = 0
        self.repeat = 0
        self.op = None
        self.args = []
        self.count = 0
        self.st_left = 0
        self.breaks = 0
        self.frame_errors = 0
        self.received = []
        self.prog_last = None
        self.prog_change = 0
        self.setup_min = None
        self.hold_min = None

    def fault(self, text):
        if len(self.faults) < 10:
            self.faults.append(f"cycle {self.c}: {text}")

    def __call__(self, c, uo, uio, oe):
        self.c = c
        clk = (uio >> P_CLK) & 1 if (oe >> P_CLK) & 1 else 1
        prog = (uio >> P_DAT) & 1 if (oe >> P_DAT) & 1 else None
        if self.pending is not None and c >= self.pending[0]:
            self.drive = self.pending[1]
            self.pending = None
        if prog is not None and self.drive is not None and prog != self.drive:
            self.fault("programmer and device drive DATA to different levels")
        if prog != self.prog_last:
            self.prog_last = prog
            self.prog_change = c
            if self.enabled:
                h = c - self.last_rise
                self.hold_min = h if self.hold_min is None else min(self.hold_min, h)
        bus = prog if prog is not None else (self.drive if self.drive is not None else self.keeper)
        self.keeper = bus
        fall = self.prev_clk == 1 and clk == 0
        rise = self.prev_clk == 0 and clk == 1
        self.prev_clk = clk
        if not self.enabled:
            self._enable(c, bus, fall, rise)
            return 0, bus << P_DAT
        if rise:
            if prog is not None:
                s = c - self.prog_change
                self.setup_min = s if self.setup_min is None else min(self.setup_min, s)
            self.last_rise = c
            if not self.tx_mode:
                self._rx_bit(bus)
        elif c - self.last_rise > 100 * self.us:
            self.fault("no PDI_CLK rising edge for 100 us: the PDI turns off (32.3.2)")
            self.last_rise = c
        if fall and self.tx_mode:
            if self.tx:
                self.pending = (c + self.d_out, self.tx.popleft())
            else:
                self.pending = (c + self.d_out, None)
                self.tx_mode = False
        return 0, bus << P_DAT

    def _enable(self, c, bus, fall, rise):
        if bus:
            if self.hi_since is None:
                self.hi_since = c
            if self.rst_off is None and c - self.hi_since >= self.t_rst:
                self.rst_off = c
        else:
            if self.rst_off is not None:
                self.fault("DATA low in the enable sequence")
            self.hi_since = self.rst_off = None
            self.en_clocks = 0
        if fall:
            if self.rst_off is None:
                self.fault("PDI_CLK (RESET) low before the RESET function was off (32.3.1)")
            elif self.first_fall is None:
                self.first_fall = c
                if c - self.rst_off > 100 * self.us:
                    self.fault("first PDI_CLK cycle more than 100 us after the RESET function was off (32.3.1)")
        if rise and self.rst_off is not None:
            self.en_clocks += 1
            if self.en_clocks == 16:
                self.enabled = True
                self.last_rise = c

    def _rx_bit(self, bit):
        if not self.rx and bit:
            return  # IDLE bit
        self.rx.append(bit)
        if len(self.rx) < 12:
            return
        frame = sum(b << i for i, b in enumerate(self.rx))
        self.rx = []
        if frame == 0:  # BREAK
            self.breaks += 1
            self.error = False
            self.op = None
            self.repeat = 0
            return
        byte = (frame >> 1) & 0xFF
        if (frame >> 9) & 1 != bin(byte).count("1") & 1 or (frame >> 10) & 3 != 3:
            self.frame_errors += 1
            self.error = True
            return
        if not self.error:
            self.received.append(byte)
            self._byte(byte)

    def _respond(self, data):
        self.tx.extend([1] * PDI_GUARD[self.guard_code])
        for b in data:
            self.tx.extend([0] + [(b >> i) & 1 for i in range(8)] + [bin(b).count("1") & 1, 1, 1])
        self.tx_mode = True

    def _read(self, addr):
        if not self.nvmen:
            self.fault(f"data space read of 0x{addr:08X} before the key")
        return self.mem.get(addr, 0xFF)

    def _write(self, addr, data):
        if not self.nvmen:
            self.fault(f"data space write of 0x{addr:08X} before the key")
        self.mem[addr] = data

    def _byte(self, b):
        if self.op is None:
            self.op, self.args = b, []
            cmd, a, s = b >> 5, (b >> 2) & 3, b & 3
            self.count = {0: a + 1, 1: 0, 2: a + s + 2, 3: s + 1, 4: 0, 5: s + 1, 6: 1, 7: 8}[cmd]
            self.st_left = self.repeat + 1
        else:
            self.args.append(b)
        if len(self.args) < self.count:
            return
        op, args = self.op, self.args
        cmd, a, s = op >> 5, (op >> 2) & 3, op & 3

        def le(data):
            return sum(x << (8 * i) for i, x in enumerate(data))

        if cmd == 0:  # LDS
            addr = le(args)
            self._respond([self._read(addr + i) for i in range(s + 1)])
        elif cmd == 2:  # STS
            addr = le(args[:a + 1])
            for i, d in enumerate(args[a + 1:]):
                self._write(addr + i, d)
        elif cmd == 1:  # LD
            out = []
            for _ in range(self.repeat + 1):
                if a == 2:
                    out += [(self.ptr >> (8 * i)) & 0xFF for i in range(s + 1)]
                elif a == 3:
                    self.fault("LD with the reserved pointer mode")
                else:
                    out += [self._read(self.ptr + i) for i in range(s + 1)]
                    if a == 1:
                        self.ptr += s + 1
            self.repeat = 0
            self._respond(out)
        elif cmd == 3:  # ST
            if a == 2:
                for i, d in enumerate(args):
                    self.ptr = (self.ptr & ~(0xFF << (8 * i))) | (d << (8 * i))
            elif a == 3:
                self.fault("ST with the reserved pointer mode")
            else:
                for i, d in enumerate(args):
                    self._write(self.ptr + i, d)
                if a == 1:
                    self.ptr += s + 1
            self.st_left -= 1
            if self.st_left > 0:
                self.args = []
                return
            self.repeat = 0
        elif cmd == 4:  # LDCS
            self._respond([{0: self.nvmen << 1, 1: self.in_reset, 2: self.guard_code}.get(op & 0xF, 0)])
        elif cmd == 5:  # REPEAT
            self.repeat = le(args)
        elif cmd == 6:  # STCS
            reg, d = op & 0xF, args[0]
            if reg == 0 and d & 2:
                self.nvmen = 0
            elif reg == 1:
                self.in_reset = int(d == 0x59)
            elif reg == 2:
                self.guard_code = d & 7
        else:  # KEY
            if op != 0xE0:
                self.fault(f"instruction 0x{op:02X}")
            elif args == PDI_KEY:
                self.nvmen = 1
        self.op = None


def pdi_tx(*data):
    return [pdi_frame(b) for b in data]


PDI_SIG = 0x01000200  # PDIBUS address of the block that the test reads with REPEAT and LD *(ptr++)
PDI_MEM = {PDI_SIG: 0x1E, PDI_SIG + 1: 0x97, PDI_SIG + 2: 0x46}
PDI_SCRIPT = (
    pdi_tx(0x80) + [0x8001]                             # LDCS STATUS: 0x00 (NVMEN 0)
    + pdi_tx(0xE0, *PDI_KEY)                            # KEY: NVM interface on
    + pdi_tx(0x80) + [0x8001]                           # LDCS STATUS: 0x02
    + pdi_tx(0xC2, 0x05) + pdi_tx(0x82) + [0x8001]      # STCS CTRL: guard time 4 bits; LDCS CTRL: 0x05
    + pdi_tx(0x4D, 0x00, 0x01, 0x00, 0x01, 0x34, 0x12)  # STS, 4 address bytes, 2 data bytes: 0x1234 at 0x01000100
    + pdi_tx(0x0D, 0x00, 0x01, 0x00, 0x01) + [0x8002]   # LDS, 4 address bytes, 2 data bytes: 0x34, 0x12
    + pdi_tx(0x6B, 0x00, 0x02, 0x00, 0x01)              # ST ptr, 4 bytes: pointer 0x01000200
    + pdi_tx(0xA0, 0x02) + pdi_tx(0x24) + [0x8003]      # REPEAT 2; LD *(ptr++), 1 byte: 3 bytes
    + pdi_tx(0xC2, 0x06)                                # STCS CTRL: guard time 2 bits (the smallest)
    + pdi_tx(0xC1, 0x59) + pdi_tx(0x81) + [0x8001]      # STCS RESET 0x59; LDCS RESET: 0x01
    + pdi_tx(0xC1, 0x00)                                # STCS RESET 0x00: reset off
    + [pdi_frame(0x80, parity_ok=False), 0x8001]        # parity error: ERROR state, no answer (0xFFFF)
    + [0x000, 0x000]                                    # two BREAK characters: back to RX (32.5.4)
    + pdi_tx(0x80) + [0x8001]                           # LDCS STATUS: 0x02
)
PDI_REPLY = [0x00, 0x02, 0x05, 0x34, 0x12, 0x1E, 0x97, 0x46, 0x01, None, 0x02]


def pdi_run(dev, words, f_mhz, half=None):
    """Runs fw/pdi.s against dev with the host words; returns the program, the replies and thread 0."""
    equ = dict(PDI_EQU[f_mhz])
    if half is not None:
        equ["HALF"] = half
    prog = assemble("pdi.s", **equ)
    owner = [None] * pe_model.N_OUT
    owner[P_CLK] = owner[P_DAT] = 0
    m = pe_model.PeModel(prog.words, owner=owner, open_drain=[False] * pe_model.N_OUT)
    to_thread, got = deque(words), []
    want = sum(w & 0xFF for w in words if w & 0x8000)
    bits = 100 + sum(280 * (w & 0xFF) if w & 0x8000 else 14 for w in words)
    for _ in range(equ["TEN"] + bits * 2 * equ["HALF"]):
        ui, uio = dev(m.cycle, *m.pins())
        m.set_inputs(ui, uio)
        if to_thread and m.fifo_push(0, to_thread[0]):
            to_thread.popleft()
        m.step()
        w = m.fifo_pop(0)
        while w is not None:
            got.append(w)
            w = m.fifo_pop(0)
        if len(got) >= want and not to_thread:
            break
    return prog, got, m.threads[0]


@cocotb.test(skip=GL)
async def test_holdout_pdi(dut):
    """fw/pdi.s at 50 and 60 MHz (PDI_CLK 1 MHz): enable, KEY, LDCS and STCS, STS and LDS, ST ptr with
    REPEAT and LD *(ptr++), guard times 128, 4 and 2 bits, a parity error and the BREAK recovery."""
    for f_mhz in (50, 60):
        half = PDI_EQU[f_mhz]["HALF"]
        for t_rst, d_out, undriven in ((0.1, 1, 1), (1.0, 1, 0), (40.0, half - 4, 0)):
            dev = PdiDevice(f_mhz, t_rst=t_rst, d_out=d_out, undriven=undriven, mem=PDI_MEM)
            _, got, th = pdi_run(dev, PDI_SCRIPT, f_mhz)
            label = (f"{f_mhz} MHz, reset pulse width {t_rst} us, device output delay {d_out} cycles, "
                     f"DATA level before the first drive {undriven}")
            assert th.status == 0 and th.run, f"{label}: thread status {th.status}, run {th.run}, {th.stop_reason}"
            assert not dev.faults, f"{label}: {dev.faults}"
            assert pdi_result(got) == PDI_REPLY, f"{label}: replies {[hex(w) for w in got]}"
            assert dev.enabled and dev.nvmen == 1 and dev.breaks == 2 and dev.frame_errors == 1, (
                f"{label}: enabled {dev.enabled}, NVMEN {dev.nvmen}, breaks {dev.breaks}, errors {dev.frame_errors}")
            assert dev.mem[0x01000100] == 0x34 and dev.mem[0x01000101] == 0x12 and dev.ptr == PDI_SIG + 3, label
            assert dev.setup_min >= half - 2 and dev.hold_min >= half - 2, (
                f"{label}: DATA set-up {dev.setup_min}, hold {dev.hold_min} cycles")
            dut._log.info(f"pdi {label}: {len(got)} replies correct; first clock "
                          f"{(dev.first_fall - dev.rst_off) / f_mhz:.1f} us after the RESET function was off; "
                          f"DATA set-up {dev.setup_min} and hold {dev.hold_min} cycles at the rising edge")


@cocotb.test(skip=GL)
async def test_holdout_pdi_rate(dut):
    """Highest PDI_CLK of fw/pdi.s at 60 MHz: HALF is lowered by 1 cycle until the script fails."""
    f_mhz, last_ok = 60, None
    for half in range(30, 12, -1):
        dev = PdiDevice(f_mhz, mem=PDI_MEM)
        _, got, th = pdi_run(dev, PDI_SCRIPT, f_mhz, half=half)
        try:
            ok = th.status == 0 and th.run and not dev.faults and pdi_result(got) == PDI_REPLY
        except AssertionError:
            ok = False
        if not ok:
            dut._log.info(f"pdi rate: HALF {half} fails: thread status {th.status}, faults {dev.faults[:2]}, "
                          f"replies {[hex(w) for w in got]}")
            break
        last_ok = half
    assert last_ok is not None, "HALF 30 fails"
    dut._log.info(f"pdi rate: smallest HALF {last_ok} cycles: PDI_CLK {f_mhz / (2 * last_ok):.3f} MHz at {f_mhz} MHz "
                  f"({50 / (2 * last_ok):.3f} MHz at 50 MHz)")


@cocotb.test()
async def test_holdout_am2302_pdi_words(dut):
    """Memory words of fw/am2302.s and fw/pdi.s, and which other firmware fits next to them in the program memory."""
    size = {}
    for name in ("am2302.s", "pdi.s", "onewire.s", "uart_tx_full.s", "uart_rx_full.s", "spi_ctl.s", "i2c_ctl.s"):
        size[name] = pe_asm.assemble_file(os.path.join(ROOT, "fw", name)).size
    dut._log.info("words: " + ", ".join(f"{k} {v}" for k, v in size.items()))
    for sets in (("am2302.s", "uart_tx_full.s", "uart_rx_full.s"), ("am2302.s", "spi_ctl.s", "i2c_ctl.s"),
                 ("pdi.s", "uart_tx_full.s", "uart_rx_full.s"), ("pdi.s", "spi_ctl.s", "i2c_ctl.s"),
                 ("am2302.s", "pdi.s"), ("am2302.s", "onewire.s"), ("pdi.s", "onewire.s"),
                 ("am2302.s", "pdi.s", "onewire.s")):
        total = sum(size[s] for s in sets)
        dut._log.info(f"{' + '.join(sets)}: {total} of {pe_model.PROG_WORDS} program words "
                      f"({'fits' if total <= pe_model.PROG_WORDS else 'does not fit'})")
    assert size["am2302.s"] == 56 and size["pdi.s"] == 52, (size["am2302.s"], size["pdi.s"])

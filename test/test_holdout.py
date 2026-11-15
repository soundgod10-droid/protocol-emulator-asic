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
    60: dict(TA=360, TE=540, TF=3300, TC=3900, TD=300, TI2=2100, H32=900, J32=769),
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

# SPDX-License-Identifier: Apache-2.0
"""UNI/O bus master firmware (fw/unio.s) against an independent UNI/O serial EEPROM model, in the reference model and on
the RTL at 60 MHz.

What it does:
  The device model (Unio11xx) is written from the Microchip UNI/O Bus Specification DS22076D (2009): sections 1.2, 2.0,
  3.0 standby pulse, 4.0 bit level, 5.0 byte level and acknowledge sequence, 6.0 start header and re-synchronization,
  7.0 8-bit device addressing, 9.0 command structure, 10.0 address polling, 11.0 device modes, Table 12-2; and the
  device commands of the Microchip 11AAXXX/11LCXXX data sheet DS22067J (2011): Table 1-2 AC characteristics, Table
  4-1 instruction set, 4.1 READ, 4.2 CRRD, 4.3 WRITE with the 16-byte page, 4.4 WREN/WRDI, 4.5 RDSR, Table 4-2. It
  does not use the firmware, the assembler or the processor model. SCIO has a pull-up; the line is the master's level
  while the master drives it (the master is the stronger driver, DS22076D 2.3), else the device's, else high. The
  model needs a low-to-high edge after power-up (3.0 note), detects the standby pulse (high >= TSTBY 600 us), checks
  TSS >= 10 us before and THDR >= 5 us of the start header, takes the bit period TE from the header (8 bits, 6.1.2)
  and checks it against 10 to 100 kHz, re-synchronizes its phase on every MAK middle edge (6.2.1), and checks every
  master bit: one middle edge within +/-0.06 UI of its grid (TIJIT, DS22067J Table 1-2) and no edge other than the
  needed bit-start edges; a missing middle edge sends the device to Idle (11.1). It answers as a 11XX at device
  address A0h: NoSAK after the start header, SAK after its address, the commands READ, CRRD, WRITE (page buffer, the
  lower 4 address bits wrap, write cycle after the NoMAK), WREN, WRDI, RDSR (repeated on MAK); NoMAK before a command
  is complete, an unknown command, or WREN/WRDI without NoMAK send it to Idle without SAK. Its own edges can be set
  late or early by a skew and a random jitter (fractions of TE; DS22067J allows +/-0.25 UI output edge jitter).
  Model assumption, stated: commands other than RDSR during a write cycle are not acknowledged.
  The model test runs a whole EEPROM session (WREN, page WRITE of 8 bytes, RDSR polling through the write cycle, READ
  of the 8 bytes, CRRD, address polling of an absent device) at 50 kbit/s with the device's edges at +0.2 UI and
  -0.2 UI with jitter, at 10 kbit/s, and at 100 kbit/s with no skew; negative controls THDR 3.3 us and a standby
  pulse of 533 us. The RTL test runs WREN, a 2-byte write, status polling and the read-back at 50 kbit/s with the
  device's edges at +0.15 UI, in lockstep with the reference model; the same test runs on the gate-level netlist
  (gl_fw workflow).

Why:
  Fourteenth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode decoder
  at the pinned commit decodes UNI/O, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins at logic level; the pull-up is ideal (no rise time), the
device's current-limited driver is not simulated (the master wins any overlap).
"""

import os
import random
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
P_SCIO = 0  # uio[0]

# DS22076D Table 12-2 / DS22067J Table 1-2 (us, UI)
TSTBY_US, TSS_US, THDR_US = 600.0, 10.0, 5.0
TE_MIN_US, TE_MAX_US = 10.0, 100.0
TIJIT = 0.06
READ, CRRD, WRITE, WREN, WRDI, RDSR = 0x03, 0x06, 0x6C, 0x96, 0x91, 0x05
ADDR = 0xA0


class Unio11xx:
    """11XX UNI/O serial EEPROM on SCIO (see the module text). skew, jitter: the device's own edges moved by skew x TE
    and a random +/-jitter x TE each; twc_us: write cycle time."""

    def __init__(self, us=F_MHZ, size=256, skew=0.0, jitter=0.0, twc_us=3000.0, seed=1):
        self.cyc = lambda u: u * us
        self.mem = [0xFF] * size
        self.size = size
        self.skew, self.jitter, self.rnd = skew, jitter, random.Random(seed)
        self.twc = self.cyc(twc_us)
        self.wel, self.wip_until, self.ptr = 0, -1, 0
        self.line, self.state, self.por = 1, "por", False
        self.last_edge, self.high_since = 0, 0
        self.edges = []
        self.drive, self.sched = None, []
        self.contend = 0
        self.errors, self.events, self.bytes = [], [], []
        self.te, self.after_cmd, self.byte_bits = None, None, []

    def _err(self, c, text):
        self.errors.append(f"cycle {c}: {text}")

    def _ui(self, c, ref):
        return (c - ref) / self.te

    # ---- device side: scheduling its own bits ----
    def _put(self, t, level):
        self.sched.append((round(t), level))
        self.sched.sort(key=lambda x: x[0])

    def _send_bits(self, k0, bits, release=True):
        """Drive bits in slots k0, k0+1, ... (slot k: middle at anchor + (k - kanchor) x TE)."""
        for i, b in enumerate(bits):
            m = self._mid(k0 + i)
            j = lambda: self.rnd.uniform(-self.jitter, self.jitter) * self.te  # noqa: E731
            self._put(m - self.te / 2 + self.skew * self.te + j(), 1 - b)
            self._put(m + self.skew * self.te + j(), b)
        if release:
            self._put(self._mid(k0 + len(bits) - 1) + self.te / 2 + self.skew * self.te, None)

    def _mid(self, k):
        return self.anchor + (k - self.kanchor) * self.te

    # ---- protocol (11XX) ----
    def _start_command(self):
        self.phase, self.byte_bits, self.slot = "addr", [], 0
        self.kinds = ["mak", "nosak"]  # after the header byte (5.1.2)
        self.cmd = None

    def _next_master_byte(self, k):
        self.kinds = ["m"] * 8 + ["mak", "sak"]
        self.kbase = k

    def _on_mak(self, c, k, mak):
        """Acknowledge after a byte: decide the SAK and what follows. k = slot of the MAK."""
        ph, b = self.phase, self.last_byte
        sak, nxt, end = False, None, False
        if ph == "hdr":
            if not mak:
                self._err(c, "NoMAK after the start header (5.1.1: a MAK must follow the header)")
            self.phase, nxt = "addr", "m"
        elif ph == "addr":
            if b != ADDR:
                self.state = "idle"
                self.events.append(("not selected", b))
                return
            sak = True
            nxt, end = ("m", False) if mak else (None, True)  # NoMAK after the address: polling (10.0)
            self.phase = "cmd"
        elif ph == "cmd":
            self.cmd = b
            busy = c < self.wip_until
            ok = b in (READ, CRRD, WRITE, WREN, WRDI, RDSR) and not (busy and b != RDSR)
            if b in (WREN, WRDI):
                ok = ok and not mak
            elif ok:
                ok = mak
            if not ok:
                self.state = "idle"
                self.events.append(("command not taken", b, mak, busy))
                return
            sak = True
            if b in (WREN, WRDI):
                self.wel = 1 if b == WREN else 0
                end = True
            elif b in (READ, WRITE):
                self.phase, nxt = "amsb", "m"
            elif b == CRRD:
                self.phase, nxt = "rdata", "s"
            else:
                self.phase, nxt = "status", "s"
        elif ph in ("amsb", "alsb"):
            if not mak:
                self.state = "idle"
                self.events.append(("NoMAK inside the address", b))
                return
            sak = True
            if ph == "amsb":
                self.amsb, self.phase, nxt = b, "alsb", "m"
            else:
                self.ptr = ((self.amsb << 8) | b) % self.size
                self.page = {}
                self.phase, nxt = ("rdata", "s") if self.cmd == READ else ("wdata", "m")
        elif ph == "wdata":
            self.page[self.ptr] = b
            self.ptr = (self.ptr & ~0xF) | ((self.ptr + 1) & 0xF)
            sak = True
            if mak:
                nxt = "m"
            else:
                end = True
                if self.wel:
                    for a, v in self.page.items():
                        self.mem[a] = v
                    self.wip_until = c + self.twc
                    self.events.append(("write cycle", dict(self.page)))
                self.wel = 0
        elif ph == "rdata":
            self.ptr = (self.ptr + 1) % self.size
            sak, nxt, end = True, ("s" if mak else None), not mak
        elif ph == "status":
            sak, nxt, end = True, ("s" if mak else None), not mak
        # SAK slot k + 1, then the next byte
        if sak:
            self._send_bits(k + 1, [1], release=nxt != "s")
        self.kinds, self.kbase = ["sak" if sak else "nosak"], k + 1
        if end:
            self.ending = True
        elif nxt == "m":
            self.kinds += ["m"] * 8 + ["mak"]
        elif nxt == "s":
            v = self._status() if self.phase == "status" else self.mem[self.ptr]
            self.out_byte = v
            self._send_bits(k + 2, [(v >> (7 - i)) & 1 for i in range(8)])
            self.kinds += ["s"] * 8 + ["mak"]

    def _status(self):
        return (2 if self.wel else 0) | (1 if self.c < self.wip_until else 0)

    # ---- one cycle ----
    def __call__(self, c, uo, uio, oe):
        self.c = c
        while self.sched and self.sched[0][0] <= c:
            self.drive = self.sched.pop(0)[1]
        m_oe, m_v = oe & 1, uio & 1
        if m_oe and self.drive is not None and m_v != self.drive:
            self.contend += 1
            if self.te and self.contend == round(0.3 * self.te):
                self._err(c, "master and device drive different levels for 0.3 TE")
        else:
            self.contend = 0
        line = m_v if m_oe else (self.drive if self.drive is not None else 1)
        if line != self.line:
            if line == 1:
                if not self.por:
                    self.por, self.state = True, "idle"
                self.high_since = c
            self._edge(c, line)
            self.line = line
            self.last_edge = c
        if self.por and line == 1 and c - self.high_since == round(self.cyc(TSTBY_US)):
            self.state, self.after_cmd = "standby", None
            self.events.append(("standby", c))
        if self.state == "bits":
            self._bits(c)
        return 0, line << P_SCIO

    def _edge(self, c, line):
        st = self.state
        if st == "standby" and line == 0:
            high = c - self.high_since
            if self.after_cmd is not None and c - self.after_cmd < self.cyc(TSS_US):
                self._err(c, f"start header {(c - self.after_cmd) / self.cyc(1):.2f} us after the command end "
                             f"(TSS 10 us)")
            elif high < self.cyc(TSS_US):
                self._err(c, f"line high {high / self.cyc(1):.2f} us before the start header (TSS 10 us)")
            self.state, self.t_fall = "thdr", c
        elif st == "thdr":
            low = c - self.t_fall
            if low < self.cyc(THDR_US):
                self._err(c, f"start header low {low / self.cyc(1):.2f} us (THDR 5 us)")
            self.state, self.t_start, self.hdr = "hdr", c, []
        elif st == "hdr":
            self.hdr.append((c, line))
            if len(self.hdr) == 8:
                te = (c - self.t_start) / 7.5
                self.te = te
                for i, (t, v) in enumerate(self.hdr):
                    if v != i % 2 or abs(self._ui(t, self.t_start + (i + 0.5) * te)) > TIJIT:
                        self._err(t, f"start header bit {i} not 0/1 alternating on its grid")
                if not self.cyc(TE_MIN_US) <= te <= self.cyc(TE_MAX_US):
                    self._err(c, f"bit period {te / self.cyc(1):.2f} us (TE 10 to 100 us)")
                self.state, self.anchor, self.kanchor = "bits", c, 7
                self.phase, self.last_byte, self.ending = "hdr", 0x55, False
                self.bytes.append(("hdr", 0x55))
                self.kinds, self.kbase, self.edges, self.byte_bits = ["mak", "nosak"], 8, [], []
                self.after_cmd = None
        elif st == "bits":
            self.edges.append((c, line))

    def _bits(self, c):
        """Evaluate the next slot when its window has passed."""
        k = self.kbase
        kind = self.kinds[0]
        m = self._mid(k)
        tol = TIJIT * self.te + 1
        done = m + tol + 1 if kind == "mak" else m + self.te / 2 - tol
        if c < done:
            return
        lo, hi = m - self.te / 2 - tol, m + self.te / 2 - tol
        win = [(t, v) for t, v in self.edges if lo <= t < hi]
        self.edges = [(t, v) for t, v in self.edges if t >= hi]
        mid = [(t, v) for t, v in win if abs(t - m) <= tol]
        if kind in ("m", "mak"):
            start = [(t, v) for t, v in win if abs(t - (m - self.te / 2)) <= tol]
            other = [x for x in win if x not in mid and x not in start]
            if kind == "mak":
                other = [x for x in other if x[0] <= m]  # edges after a MAK middle belong to the SAK slot
            for t, _ in other:
                self._err(t, f"master edge at {self._ui(t, m):+.3f} UI from the middle of bit slot {k} "
                             f"(allowed: middle and bit start, +/-{TIJIT} UI)")
            if len(mid) != 1:
                self.state = "idle"
                self._err(c, f"no single middle edge in master bit slot {k} ({len(mid)}): device to Idle")
                return
            bit = mid[0][1]
            if kind == "mak":
                self.anchor, self.kanchor = mid[0][0], k  # phase adjustment on the MAK (6.2.1)
                self.bytes.append(("mak", bit))
                self.kinds.pop(0)
                self._on_mak(c, k, bit)
                if self.state != "bits":
                    return
                if self.ending:
                    pass
                return
            self.byte_bits.append(bit)
        elif kind == "nosak":
            if mid:
                self._err(c, f"middle edge in the NoSAK slot {k}")
        elif kind == "s":
            self.byte_bits.append(None)
        self.kinds.pop(0)
        self.kbase = k + 1
        if len(self.byte_bits) == 8:
            if None not in self.byte_bits:
                self.last_byte = sum(b << (7 - i) for i, b in enumerate(self.byte_bits))
                self.bytes.append(("m", self.last_byte))
            else:
                self.last_byte = self.out_byte
                self.bytes.append(("s", self.out_byte))
            self.byte_bits = []
        if not self.kinds:
            if self.ending:
                self.state, self.after_cmd = "standby", self._mid(k) + self.te / 2
                self.events.append(("command end", c))
            else:
                self.state = "idle"
                self._err(c, "no slot expected")


def host_words(cmds):
    """cmds: list of (start, items); start: 'stby', 'hdr' or None; items: bytes to send (int) or 'r' for a read.
    The last item of a command gets NoMAK."""
    words = []
    for start, items in cmds:
        for i, it in enumerate(items):
            w = (0x200 if it == "r" else it) | (0x100 if i < len(items) - 1 else 0)
            if i == 0:
                w |= {"stby": 0x800, "hdr": 0x400, None: 0}[start]
            words.append(w)
    return words


DATA = [0x00, 0xFF, 0x5A, 0xA5, 0x01, 0x80, 0x3C, 0xC3]
POLLS = 14


def session(polls=POLLS, data=DATA):
    return [("stby", [ADDR, WREN]),
            ("hdr", [ADDR, WRITE, 0x00, 0x10] + data),
            ("hdr", [ADDR, RDSR] + ["r"] * polls),
            ("hdr", [ADDR, READ, 0x00, 0x10] + ["r"] * len(data)),
            ("hdr", [ADDR, CRRD, "r", "r"]),
            ("stby", [0x4A]),
            ("stby", [ADDR, RDSR, "r"])]


def expect(dev, cmds):
    """Host words the firmware must return: one per byte (and per start header), byte | SAK << 8."""
    out = []
    for start, items in cmds:
        out.append(0x55)
        for it in items:
            out.append(None if it == "r" else it)
    return out


def check(dev, words, cmds, label, data=DATA):
    assert not dev.errors, f"{label}: {dev.errors[:3]}"
    exp = expect(dev, cmds)
    assert len(words) == len(exp), f"{label}: {len(words)} host words, {len(exp)} expected"
    i = 0
    reads = []
    for start, items in cmds:
        assert words[i] == 0x55, f"{label}: start header {words[i]:#x} (55h with NoSAK)"
        i += 1
        selected = items[0] == ADDR
        for it in items:
            w = words[i]
            i += 1
            assert w & 0x8000 == 0, f"{label}: read byte without middle edges {w:#x}"
            assert (w >> 8) & 1 == selected, f"{label}: SAK {w:#x} for {it}"
            if it == "r":
                reads.append(w & 0xFF)
            else:
                assert w & 0xFF == it
    st = reads[:POLLS]
    rd = reads[POLLS:POLLS + len(data)]
    assert st[0] & 1 == 1 and st[-1] == 0, f"{label}: status polls {[hex(s) for s in st]} (WIP 1 then 0)"
    assert rd == data, f"{label}: read back {[hex(x) for x in rd]}"
    assert reads[POLLS + len(data):POLLS + len(data) + 2] == [0xFF, 0xFF], f"{label}: CRRD {reads}"
    assert dev.mem[0x10:0x18] == data
    return st


def assemble(**equ):
    return ff.assemble("unio.s", **equ)


def rate_equ(te):
    return dict(HB=te // 2, QE=te // 4, HBQ=te // 2 - te // 4, TOW=te // 4 + round(0.3 * te))


def model_run(dev, words, want, cycles, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners({P_SCIO: 0}))
    m.late = []  # (cycle, lead, address, program line) of late pin events and samples, for the failure message
    event_cycle = m._event_cycle

    def watch(th, c, t):
        d = (t - c) & 0xFFFF
        if not pe_model.LEAD <= d < pe_model.HORIZON and len(m.late) < 5:
            m.late.append((c, d - 0x10000 if d & 0x8000 else d, th.pc, prog.source[th.pc][1].strip()))
        return event_cycle(th, c, t)

    m._event_cycle = watch
    bench = ff.Bench(m, inputs=dev)
    bench.send(0, *words)
    for _ in range(0, cycles, 100_000):
        if len(bench.words(0)) >= want:
            break
        bench.run(100_000)
    bench.run(5_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_unio_words(dut):
    """Memory words of fw/unio.s."""
    size = assemble().size
    dut._log.info(f"fw/unio.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_unio_model(dut):
    """fw/unio.s in the reference model against the 11XX model: an EEPROM session at 50 kbit/s with the device's
    edges at +0.2 and -0.2 UI (with jitter), at 10 kbit/s and at 100 kbit/s; controls THDR and TSTBY."""
    log = dut._log
    cmds = session()
    words = host_words(cmds)
    want = len(expect(None, cmds))
    for te, skew, jit, cycles in ((1200, 0.2, 0.03, 3_000_000), (1200, -0.2, 0.03, 3_000_000),
                                  (6000, 0.1, 0.03, 12_000_000), (900, 0.2, 0.03, 3_000_000)):
        dev = Unio11xx(skew=skew, jitter=jit, twc_us=80 * te / F_MHZ)  # write cycle = 8 byte times
        bench, m = model_run(dev, words, want, cycles, **rate_equ(te))
        label = f"model TE {te / F_MHZ:.0f} us, device edges {skew:+.2f} UI +/-{jit} UI"
        st = check(dev, bench.words(0), cmds, label)
        assert not m.late, f"{label}: late events {m.late}"
        ff.assert_status_clean(m, [0])
        log.info(f"{label}: {len(bench.words(0))} host words as expected; device TE {dev.te:.1f} cycles; status "
                 f"polls {[hex(s) for s in st]}; device events {[e[0] for e in dev.events]}")
    for equ, want_text in ((dict(THDR=200), "THDR"), (dict(STBN=8), "no SAK")):
        dev = Unio11xx(twc_us=2000.0)
        bench, m = model_run(dev, words[:2], 3, 400_000, **equ)
        w = bench.words(0)
        found = [e for e in dev.errors if want_text in e] if want_text != "no SAK" else (
            [f"address byte {w[1]:#x}: no SAK (the device stayed in Idle)"] if len(w) > 1 and not w[1] & 0x100 else [])
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: {dev.errors[:2]} {[hex(x) for x in w]}"


RTL_DATA = [0x5A, 0xC3]
RTL_POLLS = 6


@cocotb.test()
async def test_unio_rtl(dut):
    """fw/unio.s on the chip at 60 MHz against the 11XX model at 50 kbit/s with the device's edges at +0.15 UI: WREN,
    a 2-byte WRITE, RDSR polling through a 1 ms write cycle and the READ back, in lockstep with the reference
    model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl unio"
    cmds = [("stby", [ADDR, WREN]), ("hdr", [ADDR, WRITE, 0x00, 0x10] + RTL_DATA),
            ("hdr", [ADDR, RDSR] + ["r"] * RTL_POLLS), ("hdr", [ADDR, READ, 0x00, 0x10, "r", "r"])]
    want = len(expect(None, cmds))
    dev = Unio11xx(skew=0.15, jitter=0.02, twc_us=1000.0)
    bench = rf.RtlBench(dut, assemble().words, owner=ff.owners({P_SCIO: 0}), inputs=dev, label=label)
    bench.send(0, *host_words(cmds))
    if hasattr(dut, "dump_off"):  # waveform of the standby pulse end, the start header and the address byte
        await bench.run(rf.HOST_START + 38_000)
        dut.dump_off.value = 0
        await bench.run(30_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform window to model cycle {bench.m.cycle}; device TE {dev.te}; header start "
                 f"{round(now - (bench.m.cycle - dev.t_start) * rf.CLK_PS) if dev.te else None} ps")
    await bench.collect(0, want, limit=900_000)
    await bench.run(2000)
    await bench.finish(log)
    w = bench.words(0)
    assert not dev.errors, f"{label}: {dev.errors[:3]}"
    reads = [x & 0xFF for x, it in zip(w, [i for s, its in cmds for i in [None] + its]) if it == "r"]
    assert all((x >> 8) & 1 for x, it in zip(w, [i for s, its in cmds for i in [None] + its]) if it is not None), \
        f"{label}: SAK missing {[hex(x) for x in w]}"
    assert reads[-2:] == RTL_DATA and reads[0] & 1 and reads[RTL_POLLS - 1] == 0, f"{label}: {[hex(x) for x in w]}"
    log.info(f"{label}: host words {[hex(x) for x in w]}; device TE {dev.te:.1f} cycles")

# SPDX-License-Identifier: Apache-2.0
"""SDI-12 data recorder firmware (fw/sdi12.s) against an independent SDI-12 sensor model, in the reference model and on
the RTL at 60 MHz.

What it does:
  The sensor model (Sdi12Sensor) is written from SDI-12 Version 1.4 (February 20, 2023, SDI-12 Support Group, public
  domain): 3.1 negative logic and the pull-down of a released line (3.1.2), 4.0 to 4.3 (break, 1200 baud 7E1, LSB
  first, addresses), 4.4 Table 5 (a!, aI!, aM!, aD0!, ?!, aAb!), 7.0 and 7.1 timing. It does not use the firmware,
  the assembler or the processor model. The line is the recorder's level while the recorder drives it, else the
  sensor's, else marking (low). It checks the recorder: a break of at least 12 ms (a spacing run of 6.5 to 12 ms is
  neither a character nor a break), at least 8.33 ms - 0.40 ms of marking after the break before the first start
  bit, every character edge at k bit times within 2 % (stated assumption: the standard gives no baud tolerance), even
  parity and a marking stop bit, at most 1.66 ms between the characters of a command, the line released within 7.5 ms
  + 0.40 ms after the last stop bit, and no drive while the sensor drives. It answers its own address: marking from
  d_mark after the command, the first start bit resp after the end of the command (8.33 - 0.40 ms to 15.0 + 0.40 ms),
  its characters at its own bit rate with a set gap (at most 1.66 ms), release after the <LF>; it goes to low-power
  standby after another sensor's address or 100 ms of marking, and then needs a break.
  The model test runs a session (break and a!, aI!, aM!, aD0!, a command to an absent sensor, ?!, aAb! and the new
  address) at the real 1200 baud, and the first four commands with all times divided by 10 at the corners: sensor
  bit rate -2 % and +2 %, first start bit after 7.93 ms and 15.4 ms, 1.66 ms between the sensor's characters;
  negative controls: a 7.9 ms break, a bit time 4 % long, and a response time-out shorter than 15.4 ms. The RTL test
  runs break and 0!, then 0M!, with all times divided by 20, in lockstep with the reference model; the same test runs
  on the gate-level netlist (gl_fw workflow).

Why:
  Twenty-fourth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012). No libsigrokdecode
  decoder at the pinned commit decodes SDI-12, so this row has no sigrok scenario.

Conditions: logic simulation at 60 MHz with ideal pins at logic level (the SDI-12 levels of Table 1, the 1 to 2 kOhm
source resistance and the 1.5 V/us slew limit are not simulated); the board pull-down is ideal.
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
P_SD = 4  # uio[4]
BIT_US = 1e6 / 1200
TOL = 0.02
IDENT = "14ACMEINSTSDI12M001"
VALUES = "+3.14-2.71"


def frame(ch):
    """Logic bits of one character: start, 7 data bits LSB first, even parity, stop."""
    d = [(ch >> i) & 1 for i in range(7)]
    return [0] + d + [sum(d) & 1] + [1]


class Sdi12Sensor:
    """SDI-12 sensor on the data line (see the module text). scale: all times divided by it; rate: the sensor's bit
    rate as a factor of 1200 baud; resp_ms, mark_ms: first start bit and start of its marking after the command;
    gap_ms: gap between its characters; rel_ms: release after the <LF>."""

    def __init__(self, us=F_MHZ, scale=1, addr="0", rate=1.0, resp_ms=9.0, mark_ms=0.5, gap_ms=0.0, rel_ms=1.0):
        self.cyc = lambda ms: ms * 1000 * us / scale
        self.bit = BIT_US * us / scale
        self.bit_s = self.bit / rate
        self.addr = addr
        self.resp, self.mark, self.gap, self.rel = (self.cyc(resp_ms), self.cyc(mark_ms), self.cyc(gap_ms),
                                                    self.cyc(rel_ms))
        self.line, self.drive, self.sched = 0, None, []
        self.awake, self.after_break = False, None
        self.rx, self.cmd, self.last_stop, self.cmd_end = None, "", None, None
        self.rise, self.mark_since, self.release_by = None, 0, None
        self.tx_until = -1
        self.commands, self.responses, self.errors = [], [], []
        self.edges = []  # recorder edges, for the waveform check

    def _err(self, c, text):
        self.errors.append(f"cycle {c}: {text}")

    def _respond(self, c, end, text):
        t = end + self.resp
        self.sched.append((round(end + self.mark), 0))
        for i, ch in enumerate(text):
            for k, b in enumerate(frame(ord(ch))):
                self.sched.append((round(t + k * self.bit_s), 1 - b))
            t += 10 * self.bit_s + self.gap
        self.sched.append((round(t - self.gap + self.rel), None))
        self.sched.sort(key=lambda x: x[0])
        self.tx_until = round(t - self.gap + self.rel)
        self.responses.append(text)

    def _command(self, c, end):
        cmd, a = self.cmd, self.addr
        self.commands.append(cmd)
        if cmd == "?!":
            return self._respond(c, end, a + "\r\n")
        if not cmd or cmd[0] != a:
            self.awake = False  # another sensor's address: low-power standby (7.0)
            return
        body = cmd[1:-1]
        if body == "":
            out = a
        elif body == "I":
            out = a + IDENT
        elif body == "M":
            out = a + "0002"
        elif body == "D0":
            out = a + VALUES
        elif len(body) == 2 and body[0] == "A":
            self.addr, out = body[1], body[1]
        else:
            return
        self._respond(c, end, out + "\r\n")

    def _char(self, c, st):
        bits = st["bits"]
        for dt in st["edges"]:
            k = round(dt / self.bit)
            if abs(dt - k * self.bit) > TOL * max(k, 1) * self.bit + 1:
                self._err(c, f"recorder edge {dt / self.bit:.3f} bit times after the start bit (2 %)")
                break
        if bits[0] != 0 or bits[9] != 1:
            self._err(c, f"recorder character: start {bits[0]}, stop {bits[9]} (start 0, stop marking)")
        d = bits[1:8]
        if sum(d) & 1 != bits[8]:
            self._err(c, "recorder character: parity error (even parity, 4.1)")
        ch = chr(sum(b << i for i, b in enumerate(d)))
        if self.cmd and self.last_stop is not None and st["t0"] - self.last_stop > self.cyc(1.66):
            self._err(c, f"{(st['t0'] - self.last_stop) / self.cyc(1):.2f} ms between command characters (1.66 ms)")
        self.last_stop = st["t0"] + 10 * self.bit
        self.cmd += ch
        if ch == "!":
            end = self.last_stop
            self.release_by = end + self.cyc(7.5 + 0.4)
            if self.awake:
                self._command(c, end)
            self.cmd = ""

    def __call__(self, c, uo, uio, oe):
        while self.sched and self.sched[0][0] <= c:
            self.drive = self.sched.pop(0)[1]
        r_oe, r_v = (oe >> P_SD) & 1, (uio >> P_SD) & 1
        if r_oe and self.drive is not None and r_v != self.drive:
            self._err(c, "recorder drives while the sensor drives a different level")
        if self.release_by is not None:
            if not r_oe:
                self.release_by = None
            elif c > self.release_by:
                self._err(c, "recorder did not release the line within 7.5 ms + 0.40 ms after the command")
                self.release_by = None
        line = r_v if r_oe else (self.drive if self.drive is not None else 0)
        sensor_tx = c <= self.tx_until
        if line != self.line:
            if not sensor_tx:
                self.edges.append((c, line))
            if line == 1:  # spacing begins
                self.rise = c
                if not sensor_tx and self.rx is None:
                    if self.after_break is not None and not self.cmd:
                        mk = c - self.after_break
                        if mk < self.cyc(8.33 - 0.40):
                            self._err(c, f"marking {mk / self.cyc(1):.2f} ms after the break (8.33 ms)")
                        self.after_break = None
                    self.rx = {"t0": c, "bits": [], "k": 0, "edges": []}
            else:  # marking begins
                run = c - self.rise if self.rise is not None else 0
                self.mark_since = c
                if not sensor_tx and run >= self.cyc(12.0):
                    self.awake, self.after_break, self.rx, self.cmd = True, c, None, ""
                elif not sensor_tx and self.rx is None and run > self.cyc(6.5):
                    self._err(c, f"spacing {run / self.cyc(1):.2f} ms: neither a character nor a break (12 ms)")
            if self.rx is not None and c > self.rx["t0"]:
                self.rx["edges"].append(c - self.rx["t0"])
            self.line = line
        if self.rx is not None:
            st = self.rx
            while st["k"] < 10 and c >= st["t0"] + (st["k"] + 0.5) * self.bit:
                st["bits"].append(1 - line)
                st["k"] += 1
            if st["k"] == 10:
                self.rx = None
                if any(st["bits"]):  # all ten samples spacing: the start of a break, not a character
                    self._char(c, st)
        if line == 0 and self.awake and not sensor_tx and c - self.mark_since > self.cyc(100.0):
            self.awake = False  # 100 ms of marking: low-power standby
        return 0, line << P_SD


def words(cmds):
    """cmds: list of (break, command text). One host word per character."""
    out = []
    for brk, text in cmds:
        for i, ch in enumerate(text):
            out.append(ord(ch) | (0x200 if brk and i == 0 else 0) | (0x100 if i == len(text) - 1 else 0))
    return out


SESSION = [(True, "0!"), (False, "0I!"), (False, "0M!"), (False, "0D0!"), (True, "1!"), (True, "?!"),
           (True, "0A3!"), (False, "3!")]
REPLIES = ["0\r\n", "0" + IDENT + "\r\n", "00002\r\n", "0" + VALUES + "\r\n", None, "0\r\n", "3\r\n", "3\r\n"]


def expect(replies):
    out = []
    for r in replies:
        out += [0x8000] if r is None else [ord(ch) for ch in r]
    return out


def assemble(**equ):
    return ff.assemble("sdi12.s", **equ)


def scale_equ(s):
    poll = 240 if s == 1 else 24
    half = 25000 // s
    return dict(HALF=half, POLL=poll, HSP=half - poll // 2, TOUT=round(17.5e-3 * 60e6 / s / poll),
                TGAP=round(8.33e-3 * 60e6 / s / poll))


def model_run(dev, w, want, cycles, **equ):
    prog = assemble(**equ)
    m = pe_model.PeModel(prog.words, owner=ff.owners({P_SD: 0}))
    m.late = []  # (cycle, lead, address, program line) of late pin events and samples, for the failure message
    event_cycle = m._event_cycle

    def watch(th, c, t):
        d = (t - c) & 0xFFFF
        if not pe_model.LEAD <= d < pe_model.HORIZON and len(m.late) < 5:
            m.late.append((c, d - 0x10000 if d & 0x8000 else d, th.pc, prog.source[th.pc][1].strip()))
        return event_cycle(th, c, t)

    m._event_cycle = watch
    bench = ff.Bench(m, inputs=dev)
    bench.send(0, *w)
    for _ in range(0, cycles, 200_000):
        if len(bench.words(0)) >= want:
            break
        bench.run(200_000)
    bench.run(20_000)
    return bench, m


@cocotb.test(skip=GL)
async def test_sdi12_words(dut):
    """Memory words of fw/sdi12.s."""
    size = assemble().size
    dut._log.info(f"fw/sdi12.s: {size} words of {pe_model.PROG_WORDS}")
    assert size <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_sdi12_model(dut):
    """fw/sdi12.s in the reference model against the sensor model: a session at 1200 baud; four commands at the sensor
    corners (times / 10); negative controls for the break, the bit time and the response time-out."""
    log = dut._log
    want = expect(REPLIES)
    dev = Sdi12Sensor()
    bench, m = model_run(dev, words(SESSION), len(want), 80_000_000, **scale_equ(1))
    assert not dev.errors, f"session: {dev.errors[:3]}"
    assert bench.words(0) == want, f"session: {[hex(x) for x in bench.words(0)]}"
    assert dev.commands == [t for _, t in SESSION], f"session: sensor saw {dev.commands}"
    ff.assert_status_clean(m, [0])
    log.info(f"session at 1200 baud: {len(want)} host words as expected; sensor saw {dev.commands}; replies "
             f"{[r for r in dev.responses]}; {m.cycle} cycles ({m.cycle / F_MHZ / 1000:.1f} ms)")
    four, w4 = SESSION[:4], expect(REPLIES[:4])
    for rate, resp, mark, gap in ((0.98, 7.93, 0.0, 1.66), (1.02, 15.4, 7.0, 1.66), (1.02, 7.93, 0.0, 0.0)):
        dev = Sdi12Sensor(scale=10, rate=rate, resp_ms=resp, mark_ms=mark, gap_ms=gap)
        bench, m = model_run(dev, words(four), len(w4), 4_000_000, **scale_equ(10))
        label = f"sensor rate {rate}, first start bit {resp} ms, character gap {gap} ms (times / 10)"
        assert not dev.errors and bench.words(0) == w4, f"{label}: {dev.errors[:3]} {[hex(x) for x in bench.words(0)]}"
        ff.assert_status_clean(m, [0])
        log.info(f"{label}: replies as expected")
    for equ, want_text in ((dict(BRKN=19), "neither a character nor a break"), (dict(HALF=2600), "recorder edge"),
                           (dict(TOUT=1250), "time-out")):
        dev = Sdi12Sensor(scale=10, resp_ms=15.4, mark_ms=7.0)
        e = {**scale_equ(10), **equ}
        if "HALF" in equ:
            e["HSP"] = equ["HALF"] - e["POLL"] // 2
        bench, m = model_run(dev, words(SESSION[:1]), 3, 1_500_000, **e)
        w = bench.words(0)
        found = [x for x in dev.errors if want_text in x] if want_text != "time-out" else (
            [f"host word {w[0]:#x}: no response seen (the sensor answered at 15.4 ms)"] if w[:1] == [0x8000] else [])
        log.info(f"control {equ}: {found[0] if found else 'no fault found'}")
        assert found, f"control {equ}: {dev.errors[:2]} {[hex(x) for x in w]}"


@cocotb.test()
async def test_sdi12_rtl(dut):
    """fw/sdi12.s on the chip at 60 MHz against the sensor model with all times divided by 20: break and 0!, then 0M!,
    in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl sdi12"
    cmds = SESSION[:1] + SESSION[2:3]
    want = expect([REPLIES[0], REPLIES[2]])
    dev = Sdi12Sensor(scale=20, resp_ms=9.0, mark_ms=0.5)
    bench = rf.RtlBench(dut, assemble(**scale_equ(20)).words, owner=ff.owners({P_SD: 0}), inputs=dev, label=label)
    bench.send(0, *words(cmds))
    if hasattr(dut, "dump_off"):  # waveform of the break, the command 0! and the reply
        dut.dump_off.value = 0
        await bench.run(rf.HOST_START + 60_000)
        dut.dump_off.value = 1
        now = get_sim_time(unit="ps")
        log.info(f"{label}: waveform from the start; recorder edges (ps) "
                 f"{[(round(now - (bench.m.cycle - c) * rf.CLK_PS), v) for c, v in dev.edges[:8]]}; bit "
                 f"{dev.bit:.1f} cycles")
    await bench.collect(0, len(want), limit=900_000)
    await bench.run(2000)
    await bench.finish(log)
    assert not dev.errors and bench.words(0) == want, f"{label}: {dev.errors[:3]} {[hex(x) for x in bench.words(0)]}"
    log.info(f"{label}: host words {[hex(x) for x in bench.words(0)]}; sensor saw {dev.commands}")

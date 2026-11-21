# SPDX-License-Identifier: Apache-2.0
"""LIN 2.2A commander and responder firmware (fw/lin_cmd.s, fw/lin_resp.s) against an independent LIN node model, in the
reference model and on the RTL at 60 MHz.

What it does:
  The node model (LinNode) is written from the LIN Specification Package Revision 2.2A (LIN Consortium, 31 December
  2010): protocol specification 2.3.1 (frame structure: break of at least 13 nominal bits and a delimiter of at least
  one bit, sync byte 0x55, protected identifier with the parity bits of equations (1) and (2), byte fields LSB first
  with start and stop bit, 1 to 8 data bytes, checksum: inverted 8-bit sum with carry, classic for identifiers 60 and
  61, enhanced otherwise), 2.3.2 (THeader_Maximum = 1.4 x 34 bits, TResponse_Maximum = 1.4 x 10 (N + 1) bits; tools
  check them), Table 2.4 (protected identifiers) and Table 2.5 (checksum example), PDF pages 29 to 33, 54, 55; physical
  layer 6.3 Table 6.1 (bit rate tolerance: commander 0.5 %, responder without synchronization 1.5 %), page 113. It does
  not use the firmware, the assembler or the processor model. The bus is the wired AND of the chip's LIN_TX
  (uo_out[4]) and the node's output; the chip reads the bus on LIN_RX (ui_in[3]).
    - As a responder (the chip is the commander) the node detects each break at 11 of its own bit times, checks the
      break (13 nominal bits), the delimiter (one bit), the sync byte, the identifier parity, THeader_Maximum, the
      position of every edge of the chip's bytes (0.5 %), then publishes or receives the response; a response it
      receives must have a correct checksum and end within TResponse_Maximum.
    - As a commander (the chip is a responder) it sends the headers of a schedule at its own bit time, publishes its
      own responses, and checks the chip's responses (checksum, edges within 1.5 %, TResponse_Maximum) and that the
      chip stays silent on identifiers it does not have.
  The model test runs both programs with several frame types, with the other node's bit time at the limits of Table
  6.1, with a wrong checksum, and with no response; negative controls change the commander's break length and bit
  time and the responder's bit time beyond the limits. The RTL tests run both programs on the chip in lockstep with
  the reference model; the same tests run on the gate-level netlist (gl_fw workflow).

Why:
  Fourth row of the pre-freeze list P (flex/BLIND.md, record of beacon round 34159012), with both roles of the row.

Conditions: logic simulation at 60 MHz with ideal pins; the LIN transceiver (TXD/RXD, the 12 V bus, its slopes and
the loop delay) is not simulated: LIN_RX shows the wired AND of both nodes in the same clock cycle.
"""

import bisect
import os
import sys
from collections import deque

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
P_TX = 12  # LIN_TX = uo_out[4]
UI_RX = 3  # LIN_RX = ui_in[3]
BIT = F_MHZ * 1e6 / 19200  # nominal bit time in cycles (19,200 bit/s; physical layer 6.3: 1 to 20 kbit/s)
TOL_MASTER = 0.005  # Table 6.1 FTOL_RES_MASTER
TOL_SLAVE = 0.015  # Table 6.1 FTOL_RES_SLAVE


def lin_pid(fid):
    """Protected identifier, equations (1) and (2)."""
    b = [(fid >> i) & 1 for i in range(6)]
    p0 = b[0] ^ b[1] ^ b[2] ^ b[4]
    p1 = 1 - (b[1] ^ b[3] ^ b[4] ^ b[5])
    return fid | p0 << 6 | p1 << 7


def lin_checksum(fid, data):
    """Inverted 8-bit sum with carry (2.3.1.5): classic (data only) for 60 and 61, else enhanced (with the PID)."""
    s = 0 if fid in (60, 61) else lin_pid(fid)
    for d in data:
        s += d
        if s >= 256:
            s -= 255
    return (~s) & 0xFF


class LinNode:
    """One LIN node on the bus with the chip (see the module text).

    role "responder": publish {id: data} it sends, subscribe {id: N} it receives from the chip; corrupt {id}: its
    response for id has a wrong checksum. role "commander": schedule [(id, what)] with what = data (it publishes),
    N > 0 (the chip should publish N bytes) or 0 (nobody answers: the chip must stay silent); corrupt as above.
    bit: this node's bit time in cycles. frames: (id, bytes with checksum, checksum good, publisher).
    """

    def __init__(self, role, bit=BIT, publish=None, subscribe=None, schedule=(), start=0, corrupt=()):
        self.role, self.bit = role, bit
        self.publish, self.subscribe = dict(publish or {}), dict(subscribe or {})
        self.schedule, self.corrupt = list(schedule), set(corrupt)
        self.out = self.bus = 1
        self.c = 0
        self.hist, self.hist_t = [], []
        self.drive = deque()
        self.frames, self.errors = [], []
        self.done = False
        self.gen = self._responder() if role == "responder" else self._commander(start)

    def __call__(self, c, uo, uio, oe):
        while self.drive and self.drive[0][0] <= c:
            self.out = self.drive.popleft()[1]
        bus = ((uo >> 4) & 1) & self.out
        if bus != self.bus:
            self.hist.append((c, bus))
            self.hist_t.append(c)
        self.bus, self.c = bus, c
        if not self.done:
            try:
                next(self.gen)
            except StopIteration:
                self.done = True
        return bus << UI_RX, 0

    # ----- primitives -----

    def _until(self, t):
        while self.c < t:
            yield

    def _fall(self, limit):
        end, prev = self.c + limit, self.bus
        while self.c < end:
            yield
            if prev == 1 and self.bus == 0:
                return self.c
            prev = self.bus
        return None

    def _byte(self, t0, tol, who):
        """A byte field from the falling edge t0: samples in the middle of each bit at this node's bit time; every edge
        of the sender must lie at k nominal bit times from t0 within tol."""
        vals = []
        for k in range(10):
            yield from self._until(t0 + (k + 0.5) * self.bit)
            vals.append(self.bus)
        lo = bisect.bisect_right(self.hist_t, t0)
        hi = bisect.bisect_left(self.hist_t, t0 + 9.5 * BIT)
        for c, _ in self.hist[lo:hi]:
            d = c - t0
            k = round(d / BIT)
            if k == 0 or abs(d - k * BIT) > tol * k * BIT + 1:
                self.errors.append(f"cycle {c}: edge of the {who} {d / BIT:.4f} bit times after the start edge "
                                   f"(bit rate tolerance {tol * 100:.1f} %, Table 6.1)")
        if vals[0] != 0 or vals[9] != 1:
            self.errors.append(f"cycle {t0}: byte field of the {who} without start or stop bit")
        return sum(vals[1 + i] << i for i in range(8))

    def _send(self, data, t):
        for b in data:
            for k, v in enumerate([0] + [(b >> i) & 1 for i in range(8)] + [1]):
                self.drive.append((round(t + k * self.bit), v))
            t += 10 * self.bit
        return t

    def _response(self, fid, n, hdr_end, tol, who):
        """Receives n data bytes and the checksum from `who`; checks the checksum and TResponse_Maximum."""
        got, end = [], hdr_end
        for i in range(n + 1):
            b0 = yield from self._fall(140 * BIT)
            if b0 is None:
                self.errors.append(f"cycle {self.c}: identifier 0x{fid:02X}: byte {i} of the response missing")
                return got, False
            got.append((yield from self._byte(b0, tol, who)))
            end = b0 + 10 * BIT
        ok = got[-1] == lin_checksum(fid, got[:-1])
        if (end - hdr_end) > 1.4 * 10 * (n + 1) * BIT:
            self.errors.append(f"identifier 0x{fid:02X}: response {(end - hdr_end) / BIT:.1f} bit times "
                               f"(TResponse_Maximum {1.4 * 10 * (n + 1):.1f})")
        return got, ok

    # ----- roles -----

    def _responder(self):
        while True:
            fe = yield from self._fall(10**12)
            while self.bus == 0:
                yield
            low = self.c - fe
            if low < 11 * self.bit:  # 2.3.1.1: break detection threshold of 11 local bit times
                continue
            if low < 13 * BIT:
                self.errors.append(f"cycle {fe}: break of {low / BIT:.2f} bit times (2.3.1.1: at least 13)")
            rise = self.c
            s = yield from self._fall(40 * BIT)
            if s is None:
                self.errors.append(f"cycle {rise}: no sync byte after the break")
                continue
            if s - rise < BIT:
                self.errors.append(f"cycle {s}: break delimiter {(s - rise) / BIT:.2f} bit times (at least 1)")
            sync = yield from self._byte(s, TOL_MASTER, "commander")
            p = yield from self._fall(40 * BIT)
            if sync != 0x55 or p is None:
                self.errors.append(f"cycle {s}: sync byte 0x{sync:02X} or no protected identifier")
                continue
            pid = yield from self._byte(p, TOL_MASTER, "commander")
            fid = pid & 63
            if pid != lin_pid(fid):
                self.errors.append(f"cycle {p}: protected identifier 0x{pid:02X}: parity error")
            hdr_end = p + 10 * BIT
            if hdr_end - fe > 1.4 * 34 * BIT:
                self.errors.append(f"cycle {fe}: header {(hdr_end - fe) / BIT:.1f} bit times (THeader_Maximum 47.6)")
            if fid in self.publish:
                data = list(self.publish[fid])
                data.append(lin_checksum(fid, data) ^ (1 if fid in self.corrupt else 0))
                t = self._send(data, p + 10.5 * self.bit)
                yield from self._until(t)
                self.frames.append((fid, data, fid not in self.corrupt, "node"))
            elif fid in self.subscribe:
                got, ok = yield from self._response(fid, self.subscribe[fid], hdr_end, TOL_MASTER, "commander")
                self.frames.append((fid, got, ok, "chip"))
            else:
                self.frames.append((fid, [], None, "none"))

    def _commander(self, start):
        yield from self._until(start)
        for fid, what in self.schedule:
            t = self.c + 2
            self.drive.extend([(t, 0), (round(t + 13 * self.bit), 1)])  # break 13 bits, delimiter 2 bits
            s = t + 15 * self.bit
            hdr_end = self._send([0x55, lin_pid(fid)], s)
            yield from self._until(hdr_end)
            if isinstance(what, list):
                data = list(what) + [lin_checksum(fid, what) ^ (1 if fid in self.corrupt else 0)]
                end = self._send(data, hdr_end + self.bit)
                yield from self._until(end)
                self.frames.append((fid, data, fid not in self.corrupt, "node"))
            elif what == 0:
                e = yield from self._fall(30 * BIT)
                if e is not None:
                    self.errors.append(f"cycle {e}: identifier 0x{fid:02X}: a response where none is due")
                self.frames.append((fid, [], None, "none"))
            else:
                got, ok = yield from self._response(fid, what, hdr_end, TOL_SLAVE, "responder")
                self.frames.append((fid, got, ok, "chip"))
            yield from self._until(self.c + 10 * self.bit)


# ----- host words and frames -----

DATA8 = [0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF]
EXAMPLE = [0x4A, 0x55, 0x93, 0xE5]  # Table 2.5


def cmd_header(fid, n, pub):
    return fid | n << 8 | (1 << 12 if pub else 0)


def assemble(name, **equ):
    return ff.assemble(name, **equ)


def cmd_case(short=False):
    """Commander firmware against a responder node: frames, host words, the node, the expected host words."""
    if short:
        sched = [(0x22, "pub", [0x12, 0x34]), (0x10, "sub", 4)]
    else:
        sched = [(0x22, "pub", [0x12, 0x34]), (0x10, "sub", 4), (0x3C, "pub", DATA8), (0x3D, "sub", 8),
                 (0x11, "sub", 4), (0x05, "sub", 2)]
    words, want = [], []
    publish = {0x10: EXAMPLE, 0x3D: DATA8, 0x11: [0xAA, 0x55, 0x00, 0xFF]}
    subscribe = {0x22: 2, 0x3C: 8}
    for fid, kind, x in sched:
        if kind == "pub":
            words += [cmd_header(fid, len(x), True)] + list(x)
        else:
            words.append(cmd_header(fid, x, False))
            if fid in publish:
                want.append((list(publish[fid]), 1 if fid == 0x11 else 0))  # 0x11: wrong checksum from the node
            else:
                want.append((None, 2))  # nobody answers
    return sched, words, publish, subscribe, want


def cmd_results(words, sched):
    """Host words of the commander split by frame: (data, status) for each frame that a responder publishes."""
    out, i = [], 0
    for fid, kind, x in sched:
        if kind == "sub":
            out.append((words[i:i + x], words[i + x] if i + x < len(words) else None))
            i += x + 1
    return out


def cmd_check(node, words, sched, want, label):
    assert not node.errors, f"{label}: {node.errors[:3]}"
    got = cmd_results(words, sched)
    assert len(got) == len(want), f"{label}: words {words}"
    for (d, st), (wd, wst) in zip(got, want):
        assert st == wst and (wd is None or d == wd), f"{label}: frame result {d}, status {st}; expected {wd}, {wst}"
    for fid, kind, x in sched:
        fr = [f for f in node.frames if f[0] == fid]
        assert fr, f"{label}: the node saw no header for 0x{fid:02X}"
        if kind == "pub":
            assert fr[0][1][:-1] == list(x) and fr[0][2], f"{label}: 0x{fid:02X} at the node: {fr[0]}"


def cmd_model(bit=BIT, short=False, corrupt=(0x11,), **equ):
    sched, words, publish, subscribe, want = cmd_case(short)
    node = LinNode("responder", bit=bit, publish=publish, subscribe=subscribe, corrupt=corrupt)
    m = pe_model.PeModel(assemble("lin_cmd.s", **equ).words, owner=ff.owners({P_TX: 0}))
    bench = ff.Bench(m, inputs=node)
    bench.send(0, *words)
    nframes = len(sched)
    for _ in range(400):
        if len(node.frames) >= nframes and not bench.to_thread[0]:
            break
        bench.run(50_000)
    bench.run(20_000)
    return bench, m, node, sched, want


def resp_case(short=False):
    """Responder firmware against a commander node: table words, schedule, expected host words."""
    words = ([cmd_header(0x10, 4, True)] + EXAMPLE + [cmd_header(0x3D, 8, True)] + DATA8
             + [cmd_header(0x22, 2, False), cmd_header(0x3C, 8, False)])
    if short:
        sched = [(0x10, 4), (0x22, [0x12, 0x34])]
        want = [0x22, 0x12, 0x34]
        corrupt = ()
    else:
        sched = [(0x10, 4), (0x22, [0x12, 0x34]), (0x3C, DATA8), (0x3D, 8), (0x07, [0x01]), (0x09, 0),
                 (0x22, [0x56, 0x78])]
        want = [0x22, 0x12, 0x34, 0x3C] + DATA8 + [0x22 | 1 << 8, 0x56, 0x78]
        corrupt = (0x22,)
    return words, sched, want, corrupt


def resp_model(bit=BIT, short=False, **equ):
    words, sched, want, corrupt = resp_case(short)
    if not short:  # the corrupted frame is the last 0x22 only: give it separately
        node = LinNode("commander", bit=bit, schedule=sched[:-1], start=8000)
    else:
        node = LinNode("commander", bit=bit, schedule=sched, start=8000)
    m = pe_model.PeModel(assemble("lin_resp.s", **equ).words, owner=ff.owners({P_TX: 0}))
    bench = ff.Bench(m, inputs=node)
    bench.send(0, *words)
    for _ in range(400):
        if node.done:
            break
        bench.run(50_000)
    if not short:
        node2 = LinNode("commander", bit=bit, schedule=sched[-1:], start=m.cycle + 2000, corrupt=corrupt)
        node2.frames, node2.errors = node.frames, node.errors
        bench.inputs = node2
        for _ in range(100):
            if node2.done:
                break
            bench.run(50_000)
        node = node2
    bench.run(20_000)
    return bench, m, node, sched, want


def resp_check(node, words, sched, want, label):
    assert not node.errors, f"{label}: {node.errors[:3]}"
    assert words == want, f"{label}: host words {words}"
    for fid, what in sched:
        fr = [f for f in node.frames if f[0] == fid]
        if not isinstance(what, list) and what:
            assert fr and fr[0][2], f"{label}: the chip's response to 0x{fid:02X}: {fr[:1]}"


@cocotb.test(skip=GL)
async def test_lin_words(dut):
    """Memory words of fw/lin_cmd.s and fw/lin_resp.s (both fit together)."""
    a, b = assemble("lin_cmd.s").size, assemble("lin_resp.s").size
    dut._log.info(f"fw/lin_cmd.s: {a} words, fw/lin_resp.s: {b} words; together {a + b} of {pe_model.PROG_WORDS}")
    assert a + b <= pe_model.PROG_WORDS


@cocotb.test(skip=GL)
async def test_lin_model(dut):
    """fw/lin_cmd.s and fw/lin_resp.s in the reference model against the LIN 2.2A node model."""
    log = dut._log
    # the node model against the specification's own examples
    table = {0x25: 0x25, 0x26: 0xA6, 0x27: 0xE7, 0x28: 0xA8, 0x3B: 0xFB, 0x3C: 0x3C, 0x3D: 0x7D, 0x3F: 0xBF}
    assert all(lin_pid(i) == p for i, p in table.items()), "lin_pid against Table 2.4"
    assert lin_checksum(60, EXAMPLE) == 0xE6, "classic checksum against Table 2.5"
    # commander
    for scale in (1.0, 1 - TOL_SLAVE, 1 + TOL_SLAVE):
        bench, m, node, sched, want = cmd_model(bit=BIT * scale)
        cmd_check(node, bench.words(0), sched, want, f"model commander, responder bit time x {scale}")
        ff.assert_status_clean(m, [0])
        log.info(f"model commander, responder bit time x {scale:.3f}: {len(node.frames)} frames "
                 f"({', '.join(f'0x{f[0]:02X}' for f in node.frames)}), chip results {cmd_results(bench.words(0), sched)}")
    for equ, want_txt in ((dict(BRK=12), "at least 13"), (dict(BIT=3150), "tolerance 0.5 %")):
        bench, m, node, sched, _ = cmd_model(short=True, **equ)
        log.info(f"control commander {equ}: {node.errors[0] if node.errors else 'no fault found'}")
        assert node.errors and want_txt in node.errors[0], f"control {equ}: got {node.errors[:2]}"
    # responder
    for scale in (1.0, 1 - TOL_MASTER, 1 + TOL_MASTER):
        bench, m, node, sched, want = resp_model(bit=BIT * scale)
        resp_check(node, bench.words(0), sched, want, f"model responder, commander bit time x {scale}")
        ff.assert_status_clean(m, [0])
        log.info(f"model responder, commander bit time x {scale:.3f}: {len(node.frames)} frames, chip words "
                 f"{[hex(w) for w in bench.words(0)]}")
    bench, m, node, sched, _ = resp_model(short=True, BIT=3200)
    log.info(f"control responder {{'BIT': 3200}}: {node.errors[0] if node.errors else 'no fault found'}")
    assert node.errors and "tolerance 1.5 %" in node.errors[0], f"control responder: got {node.errors[:2]}"


@cocotb.test()
async def test_lin_cmd_rtl(dut):
    """fw/lin_cmd.s on the chip at 60 MHz against a LIN responder node: a frame the chip publishes and one it receives,
    in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl lin_cmd"
    sched, words, publish, subscribe, want = cmd_case(short=True)
    node = LinNode("responder", publish=publish, subscribe=subscribe)
    bench = rf.RtlBench(dut, assemble("lin_cmd.s").words, owner=ff.owners({P_TX: 0}), inputs=node, label=label)
    bench.send(0, *words)
    for _ in range(100):
        if len(node.frames) >= len(sched) and len(bench.words(0)) >= 5:
            break
        await bench.run(20_000)
    await bench.run(10_000)
    await bench.finish(log)
    cmd_check(node, bench.words(0), sched, want, label)
    log.info(f"{label}: frames at the node {[(hex(f[0]), [hex(b) for b in f[1]], f[2], f[3]) for f in node.frames]}; "
             f"chip results {cmd_results(bench.words(0), sched)}")


@cocotb.test()
async def test_lin_resp_rtl(dut):
    """fw/lin_resp.s on the chip at 60 MHz against a LIN commander node: a frame the chip publishes and one it
    receives, in lockstep with the reference model."""
    rf.start_clock(dut)
    log = dut._log
    label = "rtl lin_resp"
    words, sched, want, _ = resp_case(short=True)
    node = LinNode("commander", schedule=sched, start=rf.HOST_START + 8000)
    bench = rf.RtlBench(dut, assemble("lin_resp.s").words, owner=ff.owners({P_TX: 0}), inputs=node, label=label)
    bench.send(0, *words)
    # waveform of the first header and the chip's response (tb.fst), for review; the Verilator coverage build has no
    # dump and no dump_off (test/tb.v), so there the test runs without it
    if hasattr(dut, "dump_off"):
        await bench.run(rf.HOST_START + 7000)
        dut.dump_off.value = 0
        await bench.run(300_000)
        dut.dump_off.value = 1
    for _ in range(100):
        if node.done:
            break
        await bench.run(20_000)
    await bench.collect(0, len(want))
    await bench.finish(log)
    resp_check(node, bench.words(0), sched, want, label)
    log.info(f"{label}: frames at the node {[(hex(f[0]), [hex(b) for b in f[1]], f[2], f[3]) for f in node.frames]}; "
             f"chip words {[hex(w) for w in bench.words(0)]}")

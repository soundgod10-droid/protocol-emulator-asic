# SPDX-License-Identifier: Apache-2.0
"""Protocol firmware on the chip: each protocol of fw/ against its independent protocol model, at 60 MHz.

What it does:
  Each test loads a firmware image into the chip through the host SPI port as a host does
  (docs/host_port.md: WRITE_MEM, WRITE_CFG, START) and runs it against the same protocol model as the
  model test of that firmware (test_fw_full.py, test_flex.py, test_holdout.py), in closed loop on the chip
  pins: in each clock cycle the protocol model reads uo_out[6:0], uio_out and uio_oe of the chip and gives
  ui_in[4:0] and uio_in. The host words go through the SPI port, by the host rules of docs/host_port.md
  sections 9 and 10: a loop of READ_STATUS, then a PUSH of at most 4 - count words to each thread that has
  words to get, and a PULL of the out-FIFO count of each thread, at SCK 10 MHz (6 clock cycles). The host
  reacts to the chip as a real host does (the transactions are made at run time from the status bytes).
  The reference model (model/pe_model.py) runs in lockstep, with the same inputs and the same host
  operations in the same clock cycles: in each cycle the chip pins must equal the model pins, each word
  that the host reads on HOST_MISO must equal the model word, and at the end the RUN, LATE and ERR bits
  that the host reads must equal those of the model. Pass or fail of the protocol comes from the protocol
  model and its checks (the functions of the model tests), applied to the chip pins and to the words that
  the host read from the chip.

Why:
  The model tests run every protocol in the reference model only. These tests run the same firmware on the
  RTL and, in the gate-level job, on the layout netlist, so each protocol result is a result of the chip.

Clock: 60 MHz (16.668 ns, the declared clock of info.yaml); the firmware constants are those for 60 MHz.
The model tests keep the rate sweeps; each test here runs the standard rate and the worst cases of its
model test that fit in about one million clock cycles.
"""

import math
import os
import random
import sys
from collections import deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "model"))
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "test"))
import host_spi as hs  # noqa: E402
import pe_asm  # noqa: E402
import pe_model  # noqa: E402
import test_flex as fx  # noqa: E402
import test_fw_full as ff  # noqa: E402
import test_holdout as ho  # noqa: E402

GL = os.environ.get("GATES") == "yes"
F_CLK = 60e6  # declared clock (info.yaml clock_hz)
F_MHZ = 60
CLK_PS = 16668  # clock period in the simulation (60 MHz rounded down)
SCK_PERIOD = 6  # host SCK period in clock cycles: 100 ns at 60 MHz, the rated 10 MHz (docs/host_port.md 3)
RESET_PINS = (0x7F, 0xFF, 0x00)  # isa 2.4: V = 1, OE = 0 on all output pins
HOST_START = 3000  # cycles after START before a protocol model starts: the host has loaded the first words

# I2C .equ values at 60 MHz. Fm: test_fw_full.py (the 50 MHz values x 1.2, TLOW 92). Sm: the 50 MHz values x 1.2,
# rounded up; HD and TSMP are cycle counts of the firmware, not bus times. check_i2c_times checks each bus time.
I2C_EQU60 = {
    "Fm": ff.I2C_EQU[("Fm", 60)],
    "Sm": dict(HD=4, TLOW=312, TSMP=16, THIGH2=266, TSUSTA=282, THDSTA=246, TSUSTO=240, TBUF=288),
}


def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PS, unit="ps").start())
    try:
        dut.dump_off.value = 1  # no waveform dump during these long tests (test/tb.v); not in the Verilator build
    except AttributeError:
        pass


def bits_word(bits):
    return int("".join(map(str, bits)), 2)


class RtlBench:
    """The chip with a real host on the SPI pins, a protocol model on the other pins, and the model in lockstep.

    Same interface as test_fw_full.Bench (send, words, from_thread, watch, changes, m, run), so the protocol
    checks of the model tests apply unchanged; run() is a coroutine. send_group() gives words that the host
    must write in one PUSH transaction (for example a PDI instruction and the receive word after it).
    """

    def __init__(self, dut, image, start_pc=(0, 0, 0, 0), run=(True, False, False, False), owner=None, od=None,
                 inputs=None, label="rtl"):
        words = [0] * pe_model.MEM_WORDS
        used = 0
        for a, w in (image.items() if isinstance(image, dict) else enumerate(image)):
            words[a] = w & 0xFFFF
            used = max(used, a + 1)
        self.dut, self.label, self.inputs = dut, label, inputs
        self.image = words
        self.used = used  # the host writes the words that the firmware uses (docs/host_port.md 8, rule 6)
        self.start_pc = tuple(start_pc)
        self.run_mask = tuple(bool(x) for x in run)
        self.owner = list(owner) if owner is not None else [None] * pe_model.N_OUT
        self.od = [bool(x) for x in od] if od is not None else [False] * pe_model.N_OUT
        self.m = pe_model.PeModel(words, self.start_pc, self.run_mask, self.owner, self.od)
        self.to_thread = [deque() for _ in range(pe_model.N_THREADS)]  # word groups not yet written
        self.from_thread = [[] for _ in range(pe_model.N_THREADS)]  # (model cycle, word) that the host read
        self.watches = {}
        self.sched = hs.Schedule(SCK_PERIOD)
        self.t = 0  # test-bench cycle; model cycle = t - t0
        self.t0 = None
        self.ops = {}  # test-bench cycle -> model host operations at the end of that cycle
        self.miso = {}  # (transaction, byte) -> MISO bits that the host took
        self.need = set()  # transactions whose MISO bytes the host reads
        self.open = deque()  # transactions in order: (number, info)
        self.counts, self.model_words = {}, {}
        self.plan = deque()
        self.status = None
        self.want_final, self.final_k, self.final_status = False, None, None
        self.stats = dict(STATUS=0, PUSH=0, PULL=0, pushed=0, pulled=0, compared=0)

    # ----- interface of test_fw_full.Bench -----

    def watch(self, name, fn):
        self.watches[name] = [fn, []]

    def changes(self, name):
        return self.watches[name][1]

    def words(self, n):
        return [w for _, w in self.from_thread[n]]

    def send(self, n, *words):
        for w in words:
            self.to_thread[n].append([w & 0xFFFF])

    def send_group(self, n, *words):
        assert len(words) <= pe_model.FIFO_DEPTH, "a group must fit in the in-FIFO"
        self.to_thread[n].append([w & 0xFFFF for w in words])

    # ----- host -----

    def _add(self, t, out, kind, n=None, words=None, nwords=0):
        s = self.sched
        if len(s.pins) < t:
            s.idle(t - len(s.pins))
        base = len(s.pins)
        k = s.add(out, kind)
        half = SCK_PERIOD // 2

        def act(byte):  # the port acts on a byte 2 cycles after the rising SCK edge of its last bit
            return base + 2 + (8 * byte + 7) * SCK_PERIOD + half + s.SYNC

        if kind == "PUSH":
            for j, w in enumerate(words):
                self.ops.setdefault(act(3 + 2 * j), []).append(("push", n, w, k, j))
            self.stats["pushed"] += len(words)
        elif kind == "PULL":
            self.ops.setdefault(act(1), []).append(("count", n, 0, k, 0))
            for j in range(nwords):
                self.ops.setdefault(act(2 + 2 * j), []).append(("pop", n, 0, k, j))
            self.need.add(k)
        elif kind == "STATUS":
            self.need.add(k)
            if self.want_final and self.final_k is None:
                self.final_k = k
        if kind in self.stats:
            self.stats[kind] += 1
        self.open.append((k, dict(kind=kind, n=n, nwords=nwords, nbytes=len(out), end=len(s.pins))))

    def _bytes(self, k, nbytes):
        out = []
        for b in range(nbytes):
            bits = self.miso.pop((k, b), [])
            assert len(bits) == 8, f"{self.label}: transaction {k} byte {b}: {len(bits)} MISO bits"
            out.append(bits_word(bits))
        return out

    def _decode(self, k, info):
        self.need.discard(k)
        if info["kind"] == "STATUS":
            got = self._bytes(k, info["nbytes"])
            assert got[0] == 0, f"{self.label}: READ_STATUS command byte MISO 0x{got[0]:02X}"
            self.status = hs.parse_status(got[1:8])
            if k == self.final_k:
                self.final_status = self.status
            self.plan = deque(self._plan(self.status))
        elif info["kind"] == "PULL":
            got = self._bytes(k, info["nbytes"])
            n, nw = info["n"], info["nwords"]
            cnt = self.counts.pop(k)
            mw = self.model_words.pop(k, [])
            words = hs.bytes_to_words(got[3:])
            want = mw + [0] * (nw - len(mw))
            assert got[:2] == [0, 0] and got[2] == cnt and words == want, (
                f"{self.label}: PULL of thread {n}: MISO {[f'{b:02X}' for b in got]}, model count {cnt}, "
                f"words {[f'{w:04X}' for w in want]}")
            for w in words[:min(cnt, nw)]:
                self.from_thread[n].append((info["end"] - self.t0, w))
            self.stats["pulled"] += min(cnt, nw)

    def _plan(self, st):
        """Host transactions from the status bytes (docs/host_port.md 9, 10): PUSH at most 4 - count words to
        each thread that has words to get (whole groups only), PULL the out-FIFO count of each thread."""
        plan = []
        for n in range(pe_model.N_THREADS):
            free = pe_model.FIFO_DEPTH - st["in_cnt"][n]
            q, words = self.to_thread[n], []
            while q and len(words) + len(q[0]) <= free:
                words += q.popleft()
            if words:
                plan.append(("PUSH", n, words))
        for n in range(pe_model.N_THREADS):
            if st["out_cnt"][n]:
                # All 4 word slots: the port takes the next word when it loads it into MISO, at the end of the byte
                # before it (docs/host_port.md 9), so a PULL that ends before the K-th word loses a word.
                plan.append(("PULL", n, pe_model.FIFO_DEPTH))
        return plan

    def _host(self, t):
        while self.open and self.open[0][1]["end"] <= t:
            k, info = self.open.popleft()
            self._decode(k, info)
        if self.plan:
            kind, n, arg = self.plan.popleft()
            if kind == "PUSH":
                self._add(t, hs.t_push(n, arg), "PUSH", n, words=arg)
            else:
                self._add(t, hs.t_pull(n, arg), "PULL", n, nwords=arg)
        else:
            self._add(t, [hs.READ_STATUS] + [0] * hs.STATUS_BYTES, "STATUS")

    def _before(self, op):
        kind, n, _, k, j = op
        m = self.m
        if kind == "push":
            assert len(m.fifo_in[n]) < pe_model.FIFO_DEPTH, f"{self.label}: the host pushed to a full FIFO of thread {n}"
            return True
        if kind == "count":
            self.counts[k] = len(m.fifo_out[n])
            return self.counts[k]
        return m.fifo_out[n][0] if j < self.counts[k] else None

    def _after(self, op, before):
        kind, n, data, k, _ = op
        if kind == "push":
            self.m.fifo_in[n].append(data)
        elif kind == "pop" and before is not None:
            assert self.m.fifo_out[n].popleft() == before
            self.model_words.setdefault(k, []).append(before)

    # ----- chip -----

    async def _start(self):
        dut = self.dut
        dut.ena.value = 1
        dut.ui_in.value = 1 << hs.CS_N_BIT
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        await ClockCycles(dut.clk, 5)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1
        await ClockCycles(dut.clk, 4)
        s = self.sched
        s.idle(2)
        head, pins = hs.config_words(self.start_pc, self.run_mask, self.owner, self.od)
        for out in (hs.t_write_mem(0, self.image), hs.t_write_cfg(0, head), hs.t_write_cfg(16, pins)):
            s.add(out, "load")
        k = s.add([hs.START], "START")
        self.t0 = s.act_cycle(k, 0) + 2  # docs/host_port.md 8: cycle 0 is 2 cycles after the START act
        self.started = True

    async def run(self, cycles):
        """Runs `cycles` model cycles (the load before model cycle 0 comes first)."""
        if self.t0 is None:
            await self._start()
        dut, m, s = self.dut, self.m, self.sched
        out_h, ui_h, uin_h = dut.out_pins, dut.ui_in, dut.uio_in
        fe = FallingEdge(dut.clk)
        watches = list(self.watches.values())
        inputs, t0 = self.inputs, self.t0
        target = m.cycle + cycles
        last_ui = last_uin = None
        while m.cycle < target:
            await fe
            t = self.t
            v = out_h.value.to_unsigned()  # {uo_out, uio_out, uio_oe} (test/tb.v)
            uo_v, uio_v, oe_v = v >> 16, (v >> 8) & 0xFF, v & 0xFF
            smp = s.samples.get(t)
            if smp is not None and smp[0] in self.need:
                self.miso.setdefault(smp[:2], []).append((uo_v >> hs.MISO_BIT) & 1)
            if t >= t0 and t >= len(s.pins):
                self._host(t)
            sck, cs_n, mosi = s.pins[t] if t < len(s.pins) else (0, 1, 0)
            uo7 = uo_v & 0x7F
            if t >= t0:
                c = t - t0
                ui, uin = inputs(c, uo7, uio_v, oe_v) if inputs is not None else (0, 0)
                ops = self.ops.pop(t, None)
                acts = [(op, self._before(op)) for op in ops] if ops else ()
                m.set_inputs(ui, uin)
                exp = m.step()
                for op, before in acts:
                    self._after(op, before)
                for fn, ch in watches:
                    v = fn(uo7, uio_v, oe_v)
                    if not ch or ch[-1][1] != v:
                        ch.append((c, v))
            else:
                ui = uin = 0
                exp = RESET_PINS
            ui_v = (sck << hs.SCK_BIT) | (cs_n << hs.CS_N_BIT) | (mosi << hs.MOSI_BIT) | (ui & 0x1F)
            if ui_v != last_ui:
                ui_h.value = last_ui = ui_v
            if uin != last_uin:
                uin_h.value = last_uin = uin & 0xFF
            if (uo7, uio_v, oe_v) != exp:
                where = f"model cycle {t - t0}" if t >= t0 else f"load cycle {t}"
                raise AssertionError(
                    f"{self.label} {where}: chip uo_out[6:0] {uo7:07b} uio_out {uio_v:08b} uio_oe {oe_v:08b}, "
                    f"model {exp[0]:07b} {exp[1]:08b} {exp[2]:08b} (model PCs {[th.pc for th in m.threads]})")
            self.stats["compared"] += 1
            self.t = t + 1

    def diag(self):
        """State for a fault message: model cycle, PCs, FIFOs, the last status the host read, the host queues."""
        m = self.m
        return (f"model cycle {m.cycle}, PCs {[th.pc for th in m.threads]}, run {[th.run for th in m.threads]}, "
                f"model in-FIFOs {[list(map(hex, q)) for q in m.fifo_in]}, out-FIFOs {[list(map(hex, q)) for q in m.fifo_out]}; "
                f"last chip status {self.status}; host queue {[list(q) for q in self.to_thread]}, plan {list(self.plan)}, "
                f"transactions {dict((k, v) for k, v in self.stats.items())}")

    async def collect(self, n, count, limit=20_000):
        """Runs until the host has read `count` words of thread n, at most `limit` more cycles (the host polls
        the status: a word that the thread pushes comes to the host some hundred cycles later)."""
        for _ in range(limit // 200):
            if len(self.from_thread[n]) >= count:
                return
            await self.run(200)

    async def finish(self, log=None):
        """One more READ_STATUS after the host's planned transactions: RUN, LATE and ERR of each thread as the host
        reads them must equal the model's, with no command error and no dropped PUSH."""
        self.want_final, self.final_k, self.final_status = True, None, None
        for _ in range(400):
            if self.final_status is not None:
                break
            await self.run(256)
        st = self.final_status
        assert st is not None, f"{self.label}: no final READ_STATUS"
        model = [(int(th.run), th.status & 1, (th.status >> 1) & 1) for th in self.m.threads]
        chip = [(st["run"][n], st["late"][n], st["err"][n]) for n in range(pe_model.N_THREADS)]
        assert chip == model, f"{self.label}: chip (RUN, LATE, ERR) {chip}, model {model}"
        assert st["cmd_err"] == 0 and st["push_drop"] == 0, f"{self.label}: status {st}"
        if log is not None:
            s = self.stats
            log.info(f"{self.label}: {self.m.cycle} cycles in lockstep with the model ({s['compared']} pin "
                     f"comparisons with the load); host: {s['STATUS']} READ_STATUS, {s['PUSH']} PUSH ({s['pushed']} "
                     f"words), {s['PULL']} PULL ({s['pulled']} words checked on MISO); RUN, LATE, ERR as the model "
                     f"{chip}")
        return st


# ----- host port: PULL length rule -----


async def spi_load(dut, host, image, owner=None, od=None):
    """Chip reset, then WRITE_MEM of 128 words, WRITE_CFG (thread 0 runs from 0), START, with the real-time host."""
    dut.ena.value = 1
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 4)
    words = [0] * pe_model.MEM_WORDS
    for a, w in image.items():
        words[a] = w
    head, pins = hs.config_words((0, 0, 0, 0), (True, False, False, False), owner or [None] * pe_model.N_OUT,
                                 od or [False] * pe_model.N_OUT)
    await host.write_mem(0, words)
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    await host.start()


@cocotb.test()
async def test_rtl_pull_rule(dut):
    """docs/host_port.md 9 on the chip, with the independent real-time host model (test/host_spi.py SpiTimed,
    SCK 100 ns): the port takes the next out-FIFO word when it loads it into MISO, at the end of the byte before it.
    Thread 0 pushes 3 words. A PULL that the host ends after 1 word takes 2 words: the second is lost (count 1, then
    the third word). A PULL that clocks all K words gives all 3 and leaves the FIFO empty."""
    start_clock(dut)
    log = dut._log
    prog = pe_asm.assemble("LDI r0, 0x11\nPUSH r0\nLDI r0, 0x22\nPUSH r0\nLDI r0, 0x33\nPUSH r0\nHALT", "pull_rule", 0)
    host = hs.SpiTimed(dut, period_ns=100)
    await spi_load(dut, host, prog.words)
    await ClockCycles(dut.clk, 200)
    st = await host.read_status()
    assert st["out_cnt"][0] == 3, st
    first, k1 = await host.pull(0, max_words=1)
    st = await host.read_status()
    rest, k2 = await host.pull(0)
    log.info(f"PULL ended after 1 word: count {k1}, words {[hex(w) for w in first]}; then status out-FIFO "
             f"{st['out_cnt'][0]}; next PULL count {k2}, words {[hex(w) for w in rest]}")
    assert (k1, first, st["out_cnt"][0], k2, rest) == (3, [0x11], 1, 1, [0x33]), (k1, first, st, k2, rest)
    await spi_load(dut, host, prog.words)
    await ClockCycles(dut.clk, 200)
    words, k = await host.pull(0, max_words=4)
    st = await host.read_status()
    log.info(f"PULL of all K words: count {k}, words {[hex(w) for w in words]}; then status out-FIFO {st['out_cnt'][0]}")
    assert (k, words, st["out_cnt"][0]) == (3, [0x11, 0x22, 0x33], 0), (k, words, st)


# ----- UART -----


@cocotb.test()
async def test_rtl_uart_tx(dut):
    """fw/uart_tx_full.s on the chip at 60 MHz: 1 Mbit/s 8N1, 115,200 bit/s 8N1 and 9,600 bit/s 8E1, back-to-back
    frames, against the independent receiver of test_fw_full.check_tx (each bit at its middle, every edge on a
    bit boundary, start-to-start exactly one frame time, divider error within 1 %)."""
    start_clock(dut)
    log = dut._log
    rng = random.Random(31)
    prog = ff.assemble("uart_tx_full.s")
    for rate, fmt, n in ((1_000_000, "8N1", 8), (115_200, "8N1", 6), (9_600, "8E1", 2)):
        data = ([0x55, 0x00, 0xFF] + [rng.randrange(256) for _ in range(n)])[:n]
        k, c, b = ff.uart_cfg(F_CLK, rate)
        label = f"rtl uart_tx_full 60 MHz {rate} bit/s {fmt} (K {k}, C {c})"
        bench = RtlBench(dut, prog.words, owner=ff.owners({ff.P_TX: 0}), label=label)
        bench.watch("tx", lambda uo, uio, oe: (uo >> 4) & 1)
        frames = [ff.frame_bits(d, fmt) for d in data]
        bench.send(0, c, k, *[ff.frame_word(f) for f in frames])
        await bench.run(HOST_START + (len(frames) * len(frames[0]) + 2) * b)
        ff.check_tx(log, bench.changes("tx"), b, F_CLK / rate, frames, label)
        await bench.finish(log)


async def rtl_rx(dut, b, h, fmt, b_tx, data, slow_k=None, glitch=True, label=""):
    """fw/uart_rx_full.s (or uart_rx_slow.s) with a sender of bit time b_tx; the words the host reads must hold
    the sent bits (test_fw_full.rx_run on the chip). A 3-cycle low glitch comes 1,000 cycles before the frames."""
    nd1 = len(ff.frame_bits(0, fmt)) - int(fmt[2])
    if slow_k is None:
        prog = ff.assemble("uart_rx_full.s", ND1=nd1)
        host = [b, h]
    else:
        prog = ff.assemble("uart_rx_slow.s", ND1=nd1, K=slow_k)
        host = [b // slow_k, h // slow_k]
    frames, t = [], HOST_START
    for d in data:
        bits = ff.frame_bits(d, fmt)
        frames.append((t, bits))
        t += round(len(bits) * b_tx)
    line = ff.uart_line(frames, b_tx, glitch=HOST_START - 1000 if glitch else None)
    bench = RtlBench(dut, prog.words, inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << ff.UI_RX, 0), label=label)
    bench.send(0, *host)
    await bench.run(t + 3 * b)
    await bench.collect(0, len(frames))
    words = bench.words(0)
    assert len(words) == len(frames) and all(ff.rx_bits(w, nd1) == bits[:nd1 + 1] for w, (_, bits) in zip(words, frames)), (
        f"{label}: words {[hex(w) for w in words]}, sent {[bits for _, bits in frames]}")
    await bench.finish(dut._log)


@cocotb.test()
async def test_rtl_uart_rx(dut):
    """fw/uart_rx_full.s and fw/uart_rx_slow.s on the chip at 60 MHz: 1 Mbit/s 8N1 and 115,200 bit/s 8E1 with
    senders at -3.3 % and +3.3 % bit time, back-to-back frames and a glitch; 1,200 bit/s with the slow receiver;
    a frame with a stop bit 0 (framing error in the word) and a correct next frame."""
    start_clock(dut)
    log = dut._log
    rng = random.Random(77)
    for rate, fmt, n, slow_k, errs in ((1_000_000, "8N1", 6, None, (-ff.TOL, ff.TOL)),
                                       (115_200, "8E1", 4, None, (-ff.TOL, ff.TOL)),
                                       (1_200, "8N1", 1, 2, (ff.TOL,))):
        k, c, b = ff.uart_cfg(F_CLK, rate)
        if slow_k is not None:
            assert k == slow_k, (rate, k)
        h = ff.rx_h(b) if k == 1 else k * round(9 * c / 19)
        for err in errs:
            name = "uart_rx_slow" if slow_k else "uart_rx_full"
            label = f"rtl {name} 60 MHz {rate} bit/s {fmt} (B {b}, H {h}), sender {err * 100:+.1f} %"
            await rtl_rx(dut, b, h, fmt, (F_CLK / rate) * (1 + err), [rng.randrange(256) for _ in range(n)],
                         slow_k=slow_k, label=label)
            log.info(f"{label}: {n} frames correct, glitch ignored")
    b = ff.uart_cfg(F_CLK, 115_200)[2]
    data = [0x3C, 0xC3]
    line_bits = [ff.frame_bits(d, "8N1") for d in data]
    line_bits[0][9] = 0  # stop bit 0: framing error
    line_bits[0].append(1)  # one idle bit, so that the next start bit has a falling edge
    frames = [(HOST_START, line_bits[0]), (HOST_START + 11 * b, line_bits[1])]
    line = ff.uart_line(frames, b)
    label = "rtl uart_rx_full 60 MHz 115,200 bit/s, stop bit 0"
    bench = RtlBench(dut, ff.assemble("uart_rx_full.s").words,
                     inputs=lambda c, uo, uio, oe: (ff.value_at(line, c) << ff.UI_RX, 0), label=label)
    bench.send(0, b, ff.rx_h(b))
    await bench.run(HOST_START + 21 * b + 3 * b)
    await bench.collect(0, 2)
    words = bench.words(0)
    assert [ff.rx_bits(w, 9) for w in words] == [x[:10] for x in line_bits], [hex(w) for w in words]
    await bench.finish(log)
    log.info(f"{label}: stop bit 0 is in the word (bit 15 = 0), next frame correct")


# ----- SPI -----


@cocotb.test()
async def test_rtl_spi_ctl(dut):
    """fw/spi_ctl.s on the chip in all four modes, D 0 and D 7, at the smallest half period H that the model finds
    (test_fw_full.spi_h_min), against the independent SPI device model: data both ways, CS periods, every SCK
    half period in a byte exactly H cycles."""
    start_clock(dut)
    log = dut._log
    for d in (0, 7):
        for cpol in (0, 1):
            for cpha in (0, 1):
                h = ff.spi_h_min(cpol, cpha, d)
                cl = cpol if cpha == 0 else 1 - cpol
                prog = ff.assemble("spi_ctl.s", CPOL=cpol, CL=cl, D=d)
                dev = ff.SpiDevice(cpol, cpha, ff.SPI_DEV)
                label = f"rtl spi_ctl CPOL {cpol} CPHA {cpha} D {d} H {h} (SCK {F_CLK / 2e3 / h:.0f} kHz at 60 MHz)"
                bench = RtlBench(dut, prog.words, owner=ff.owners({ff.P_CS: 0, ff.P_MOSI: 0, ff.P_SCK: 0}),
                                 inputs=dev, label=label)
                bench.watch("sck", lambda uo, uio, oe: ff.level(uio, oe, ff.P_SCK))
                bench.watch("cs", lambda uo, uio, oe: ff.level(uio, oe, ff.P_CS))
                bench.send(0, h, *ff.SPI_WORDS)
                await bench.run(HOST_START + len(ff.SPI_WORDS) * (20 * h + 120) + 2000)
                await bench.collect(0, len(ff.SPI_WORDS))
                fault = ff.spi_fault(bench, bench.m, dev)
                assert fault is None, f"{label}: {fault}; {bench.diag()}"
                periods, n = ff.spi_half_periods(bench)
                assert periods == {h} and n == 16 * len(ff.SPI_WORDS), (label, periods, n)
                await bench.finish(log)


@cocotb.test()
async def test_rtl_spi_tgt(dut):
    """fw/spi_tgt.s on the chip in all four modes at 1 MHz (half period 30 cycles at 60 MHz) with CS lead 30,
    against the independent SPI controller model (test_fw_full.SpiController): MISO bytes at the sample edges,
    the MOSI bytes in the host words, MISO released while CS is high."""
    start_clock(dut)
    log = dut._log
    for mode in range(4):
        prog = pe_asm.assemble(ff.spi_tgt_text(mode), "spi_tgt.s", 0)
        ctl = ff.SpiController(mode, 30, [mosi for mosi, _ in ff.SPI_TGT_XFERS], lead=30, gap=3000, t0=HOST_START + 1000)
        label = f"rtl spi_tgt mode {mode} half period 30 (1 MHz at 60 MHz)"
        bench = RtlBench(dut, prog.words, owner=ff.owners({ff.P_MISO: 0}), inputs=ctl, label=label)
        for _, miso in ff.SPI_TGT_XFERS:
            bench.send(0, *[byte << 2 for byte in miso], 0)
        await bench.run(ctl.end + 1000)
        await bench.collect(0, sum(len(mosi) for mosi, _ in ff.SPI_TGT_XFERS))
        fault = ff.spi_tgt_fault(bench, bench.m, ctl)
        assert fault is None, f"{label}: {fault}"
        su, hold, rel, _ = ctl.miso_timing()
        log.info(f"{label}: correct; MISO set-up >= {su}, hold >= {hold} cycles, released <= {rel} cycles after CS")
        await bench.finish(log)


# ----- I2C -----


def i2c_bench(dut, mode, stretch, label):
    prog = ff.assemble("i2c_ctl.s", 0, **I2C_EQU60[mode])
    bus = ff.I2cBus(ff.I2C_ADDR, ff.I2C_READ, stretch=stretch)
    bench = RtlBench(dut, prog.words, owner=ff.owners({ff.P_SCL: 0, ff.P_SDA: 0}),
                     od=ff.open_drain({ff.P_SCL, ff.P_SDA}), inputs=bus, label=label)
    bench.watch("own", lambda uo, uio, oe: (ff.level(uio, oe, ff.P_SCL), ff.level(uio, oe, ff.P_SDA)))
    return bench, bus


def send_pairs(bench, words):
    for i in range(0, len(words), 2):
        bench.send_group(0, *words[i:i + 2])


@cocotb.test()
async def test_rtl_i2c(dut):
    """fw/i2c_ctl.s on the chip at 60 MHz, Fast-mode and Standard-mode, with and without clock stretching (360
    cycles = 6 us): write, repeated START, read with ACK and NACK, STOP, NACKed address, against the I2C target
    model; every bus time against the UM10204 Rev. 7.0 Table 11 minimums (test_fw_full.check_i2c_times).
    Arbitration lost in address bits 1 and 3. Spikes of 3 samples (the longest a spike under 50 ns gives at
    60 MHz) on SCL and SDA through the chip's input filter."""
    start_clock(dut)
    log = dut._log
    words, replies, events = ff.i2c_script()
    for mode in ("Fm", "Sm"):
        for stretch in (0, 360):
            label = f"rtl i2c_ctl {mode} 60 MHz stretch {stretch}"
            bench, bus = i2c_bench(dut, mode, stretch, label)
            send_pairs(bench, words)
            await bench.run(HOST_START + (36_000 if mode == "Fm" else 100_000))
            await bench.collect(0, len(replies))
            assert bench.words(0) == replies, f"{label}: replies {[hex(w) for w in bench.words(0)]}"
            assert ff.i2c_events(bus) == events, f"{label}: bus events {ff.i2c_events(bus)}"
            ff.check_i2c_times(log, bus.changes, mode, F_MHZ, label)
            if stretch:
                assert max(ff.i2c_times(bus.changes)["low"]) >= stretch, f"{label}: no stretched low period"
            await bench.finish(log)
    for bit in (1, 3):  # address 0xA0: bits 1 and 3 are 1
        label = f"rtl i2c_ctl Fm arbitration lost in address bit {bit}"
        bench, bus = i2c_bench(dut, "Fm", 0, label)
        bus.lose_at = (0, bit)
        send_pairs(bench, ff.i2c_write(ff.I2C_ADDR << 1, start=True))
        await bench.run(HOST_START + 7200)
        got = bench.words(0)
        assert len(got) == 1 and got[0] & 0x8000, f"{label}: replies {[hex(w) for w in got]}"
        own = bench.changes("own")
        assert own[-1][1] == (None, None), f"{label}: pins at the end {own[-1]}"
        await bench.finish(log)
    for line, places in (("SCL", (168, 180)), ("SDA", (10, 20))):
        for at in places:
            label = f"rtl i2c_ctl Fm stretch 360, 3-sample spike on {line} at {at}"
            bench, bus = i2c_bench(dut, "Fm", 360, label)
            if line == "SCL":
                bus.spike_after_ack = (1, at, 3)  # SCL reads 1 inside the stretch after the data byte 0x3C
            else:
                bus.sda_spike_at = (0, 1, at, 3)  # SDA reads 0 in the first address bit (the controller sends 1)
            send_pairs(bench, words)
            await bench.run(HOST_START + 36_000)
            await bench.collect(0, len(replies))
            assert bench.words(0) == replies and ff.i2c_events(bus) == events, (
                f"{label}: replies {[hex(w) for w in bench.words(0)]}, events {ff.i2c_events(bus)}")
            await bench.finish(log)


# ----- CAN -----


def can_bench(dut, bt, sp, node_frames=(), node_ppm=0, node_start=0, delay=0, label=""):
    prog = pe_asm.assemble(ff.source("can.s", BT=bt, SP=sp), "can.s", 0)
    nbt = bt * (1 + node_ppm * 1e-6)
    node = fx.CanNode(nbt, round(0.75 * nbt), sjw=max(1, round(nbt / 10)), frames=[dict(f) for f in node_frames],
                      start=node_start)
    bench = RtlBench(dut, prog.words, start_pc=(0, prog.symbols["rx"][0], 0, 0), run=(True, True, False, False),
                     owner=ff.owners({fx.P_TXA: 0, fx.P_TXB: 1}), od=fx.od((fx.P_TXA, fx.P_TXB)),
                     inputs=fx.CanBus([node], delay), label=label)
    return bench, node


async def can_send_all(bench, frames, bt, limit_bits=600):
    """test_flex.can_send_all on the chip: each frame, its status word, the frame again after a lost status."""
    log = []
    for i, f in enumerate(frames):
        words, ackbit = fx.can_tx_words(f)
        for _ in range(4):
            n0 = len(bench.words(0))
            bench.send(0, *words)
            waited = 0
            while len(bench.words(0)) == n0 and waited < limit_bits:
                await bench.run(int(10 * bt))
                waited += 10
            if len(bench.words(0)) == n0:
                log.append((i, None, ackbit))
                return log
            s = bench.words(0)[-1]
            log.append((i, s, ackbit))
            if s != 0xFFFF:
                break
    return log


@cocotb.test()
async def test_rtl_can(dut):
    """fw/can.s (2 threads) on the chip at 250 kbit/s (BT 240 cycles at 60 MHz, sample point 55 %) against the
    independent CAN node model (Bosch CAN 2.0 Part A): the chip sends 5 frames to a node at +0.5 % clock with a
    10-cycle transceiver loop delay; the node at -0.5 % sends 2 frames to the chip; one arbitration that the chip
    loses (it takes the winner's frame, then sends its own)."""
    start_clock(dut)
    log = dut._log
    bt = round(F_CLK / 250_000)
    sp = round(0.55 * bt)
    label = f"rtl can 250 kbit/s BT {bt} SP {sp}, chip to node +0.5 %, delay 10"
    bench, node = can_bench(dut, bt, sp, node_ppm=5000, delay=10, label=label)
    await bench.run(HOST_START)
    sent = await can_send_all(bench, fx.CAN_FRAMES, bt)
    await bench.run(int(20 * bt))
    fault = fx.can_fault(bench, bench.m, node, sent, fx.CAN_FRAMES)
    assert fault is None, f"{label}: {fault}"
    await bench.finish(log)
    label = f"rtl can 250 kbit/s BT {bt} SP {sp}, node -0.5 % to chip, delay 10"
    bench, node = can_bench(dut, bt, sp, node_frames=fx.NODE_FRAMES, node_ppm=-5000, node_start=HOST_START, delay=10,
                            label=label)
    await bench.run(HOST_START + int(300 * bt))
    fault = fx.can_fault(bench, bench.m, node, node_frames=fx.NODE_FRAMES)
    assert fault is None, f"{label}: {fault}"
    await bench.finish(log)
    label = f"rtl can 250 kbit/s BT {bt} SP {sp}, arbitration, node +0.5 %, delay 10"
    node_f = [dict(ident=0x700, data=[0x11]), dict(ident=0x3C1, data=[0x44])]
    chip_f = [dict(ident=0x3C2, data=[0x22, 0x33])]
    bench, node = can_bench(dut, bt, sp, node_frames=node_f, node_ppm=5000, node_start=HOST_START, delay=10, label=label)
    await bench.run(HOST_START + int(30 * bt))
    sent = await can_send_all(bench, chip_f, bt)
    await bench.run(int(20 * bt))
    fault = fx.can_fault(bench, bench.m, node, sent, chip_f, node_f, expect_lost=1)
    assert fault is None, f"{label}: {fault}"
    await bench.finish(log)


# ----- JTAG -----


async def rtl_jtag(dut, name, h1, h2):
    """test_flex.jtag_run_a (fw/jtag.s) or jtag_run_b (fw/jtag_tap.s) on the chip; returns the bench."""
    prog = fx.assemble(name, H1=h1, H2=h2)
    devs = fx.jtag_devices()
    label = f"rtl {name} H1 {h1} H2 {h2} (TCK {F_CLK / (h1 + h2) / 1e6:.2f} MHz at 60 MHz)"
    bench = RtlBench(dut, prog.words, owner=ff.owners({fx.P_TCK: 0, fx.P_TMS: 0, fx.P_TDI: 0}),
                     inputs=fx.JtagChain(devs), label=label)
    await bench.run(50)
    if name == "jtag.s":
        for k, ((tms, tdi, idx), want) in enumerate(fx.jtag_steps()):
            n = len(tms)
            words = [n] + [w for g in range(0, n, 16) for w in (fx.lsb_word(tms[g:g + 16]), fx.lsb_word(tdi[g:g + 16]))]
            n0 = len(bench.words(0))
            bench.send(0, *words)
            need = n0 + (n + 15) // 16
            for _ in range(200):
                if len(bench.words(0)) >= need:
                    break
                await bench.run(n * (h1 + h2) // 8 + 400)
            got = bench.words(0)[n0:]
            assert len(got) == need - n0, f"{label}: step {k}: {len(got)} TDO words"
            tdo = fx.jtag_tdo(got, n)
            assert [tdo[i] for i in idx] == want, f"{label}: step {k}: TDO {[tdo[i] for i in idx]}, expected {want}"
    else:
        for k, ops in enumerate(fx.jtag_ops()):
            for cmd, tdi, want in ops:
                n = len(tdi)
                n0 = len(bench.words(0))
                bench.send(0, cmd, *[fx.lsb_word(tdi[g:g + 16]) for g in range(0, n, 16)])
                need = n0 + (n + 15) // 16
                for _ in range(200):
                    if len(bench.words(0)) >= need:
                        break
                    await bench.run(max(n, 8) * (h1 + h2) // 8 + 400)
                await bench.run(12 * (h1 + h2))
                got = bench.words(0)[n0:]
                assert len(got) == need - n0, f"{label}: step {k}: {len(got)} TDO words"
                assert fx.jtag_tdo(got, n) == want, f"{label}: step {k}: TDO {fx.jtag_tdo(got, n)}, expected {want}"
    await bench.run(400)
    for d in devs:
        assert not d.errors, f"{label}: device errors {d.errors[:3]}"
    assert devs[1].updates == [0xA53, 0], f"{label}: device 1 updates {devs[1].updates}"
    await bench.finish(dut._log)


@cocotb.test()
async def test_rtl_jtag(dut):
    """fw/jtag.s and fw/jtag_tap.s on the chip with the defaults H1 12, H2 28 (TCK 1.5 MHz at 60 MHz), against two
    independent TAP models in a chain (TI SSYA002C): both IDCODEs after Test-Logic-Reset, an IR scan (USER and
    BYPASS), a DR scan through Pause-DR with update (jtag.s), a read-back, reset; TMS/TDI set-up and hold, TCK low
    and high times and TDO delay checked by the models."""
    start_clock(dut)
    for name in ("jtag.s", "jtag_tap.s"):
        await rtl_jtag(dut, name, 12, 28)


# ----- SWD -----


class RtlSwdHost:
    """test_flex.SwdHost on the chip: a transaction's words go in one PUSH."""

    def __init__(self, bench, period):
        self.b, self.period = bench, period

    async def _wait(self, n):
        for _ in range(400):
            if len(self.b.words(0)) >= n:
                return True
            await self.b.run(8 * self.period)
        return False

    async def raw(self, bits):
        self.b.send(0, *fx.swd_raw(bits))
        await self.b.run((len(bits) + 8) * self.period + 2400)

    async def xfer(self, apndp, rnw, a, value=0, bad_parity=False):
        n0 = len(self.b.words(0))
        words = [fx.swd_request(apndp, rnw, a) | rnw << 10]
        if not rnw:
            words += [value & 0xFFFF, value >> 16, fx.parity(value) ^ int(bad_parity)]
        self.b.send_group(0, *words)
        if not await self._wait(n0 + 1):
            return None, None, None
        ack = self.b.words(0)[n0] >> 13
        if not (rnw and ack == 0b001):
            await self.b.run(60 * self.period)
            return ack, None, None
        if not await self._wait(n0 + 4):
            return ack, None, None
        lo, hi, p = self.b.words(0)[n0 + 1:n0 + 4]
        v = lo | hi << 16
        return ack, v, fx.parity(v) == (p >> 14) & 1


@cocotb.test()
async def test_rtl_swd(dut):
    """fw/swd.s on the chip with the defaults H1 12, H2 20 (SWCLK 1.875 MHz at 60 MHz), against the independent
    SW-DP target model (Arm IHI0031H): JTAG-to-SWD and line reset, DPIDR read, SELECT write, AP write with two
    WAITs and retries, posted AP read and RDBUFF, a write with a parity error, the reset-state rule."""
    start_clock(dut)
    h1, h2 = 12, 20
    prog = fx.assemble("swd.s", H1=h1, H2=h2)
    tgt = fx.SwdTarget()
    label = f"rtl swd.s H1 {h1} H2 {h2} (SWCLK {F_CLK / (h1 + h2) / 1e6:.3f} MHz at 60 MHz)"
    bench = RtlBench(dut, prog.words, owner=ff.owners({fx.P_SWCLK: 0, fx.P_SWDIO: 0}), inputs=fx.SwdLine(tgt),
                     label=label)
    host = RtlSwdHost(bench, h1 + h2)
    await bench.run(50)
    await host.raw(fx.SWD_CONNECT)
    got = await host.xfer(0, 1, 0x0)
    assert got == (0b001, tgt.dpidr, True), f"{label}: DPIDR read {got}"
    assert (await host.xfer(0, 0, 0x8, 0x000000F0))[0] == 0b001, f"{label}: SELECT write: no OK"
    tgt.waits = 2
    acks = [(await host.xfer(1, 0, 0x4, 0xCAFEF00D))[0] for _ in range(3)]
    assert acks == [0b010, 0b010, 0b001] and tgt.ap.get((0xF0, 0x4)) == 0xCAFEF00D, f"{label}: ACKs {acks}, AP {tgt.ap}"
    first = await host.xfer(1, 1, 0x4)
    last = await host.xfer(0, 1, 0xC)
    assert first[0] == 0b001 and last == (0b001, 0xCAFEF00D, True), f"{label}: AP read {first}, RDBUFF {last}"
    bad = await host.xfer(1, 0, 0x4, 0x12345678, bad_parity=True)
    await host.xfer(1, 1, 0x4)
    after = await host.xfer(0, 1, 0xC)
    assert bad[0] == 0b001 and after[1] == 0xCAFEF00D and tgt.log[-3][-1] is False, (
        f"{label}: write with parity error {bad}, then RDBUFF {after}, log {tgt.log[-3:]}")
    await host.raw(fx.SWD_LINE_RESET)
    e0 = tgt.protocol_errors
    noresp = await host.xfer(1, 1, 0x0)
    e1 = tgt.protocol_errors
    await host.raw(fx.SWD_LINE_RESET)
    again = await host.xfer(0, 1, 0x0)
    assert noresp[0] == 0b111 and again == (0b001, tgt.dpidr, True) and e1 == e0 + 1, (
        f"{label}: reset state: AP read ACK {noresp}, then DPIDR {again}")
    assert not tgt.errors, f"{label}: target errors {tgt.errors[:3]}"
    await bench.finish(dut._log)


# ----- PS/2 -----

PS2_PERIOD = 4800  # device clock 12.5 kHz at 60 MHz
PS2_DEV_EQU = dict(TSU=900, TLO=2400, THI=1500)  # fw/ps2_dev.s 12.5 kHz at 60 MHz (the 50 MHz values x 1.2)


@cocotb.test()
async def test_rtl_ps2(dut):
    """fw/ps2_host.s against the independent PS/2 device model (12.5 kHz device clock): three frames, then a
    command with request to send, acknowledge and the device answer; fw/ps2_dev.s (12.5 kHz) against the
    independent PS/2 system model: five frames, one of them inhibited by the system before the 10th clock (line
    contention: stopped and sent again), then a system command with acknowledge (IBM PS/2 Technical Reference,
    Keyboards, pages 38 to 40)."""
    start_clock(dut)
    log = dut._log
    frames = fx.PS2_BYTES[:3]
    prog = fx.assemble("ps2_host.s", LATE="st", RTS=4800)
    dev = fx.Ps2Device(PS2_PERIOD, frames, gap=3600, rts_delay=600, start=HOST_START + 1000)
    label = "rtl ps2_host.s 12.5 kHz device clock"
    bench = RtlBench(dut, prog.words, owner=ff.owners({fx.P_PCLK: 0, fx.P_PDATA: 0}), od=fx.od((fx.P_PCLK, fx.P_PDATA)),
                     inputs=fx.Ps2Bus(dev), label=label)
    await bench.run(HOST_START + 1000 + len(frames) * (12 * PS2_PERIOD + 3800))
    words = list(bench.words(0))
    got = [fx.ps2_decode(fx.ps2_word_bits(w)) if w >= 32 else (None, False) for w in words]
    assert got == [(b, True) for b in frames], f"{label}: received {got}, device sent {dev.sent}"
    bench.send(0, fx.ps2_cmd_word(0xED))
    await bench.run(HOST_START + 4800 + 13 * PS2_PERIOD + 720 + 12 * PS2_PERIOD + 4800)
    rest = bench.words(0)[len(words):]
    assert rest and (rest[0] >> 15) & 1 == 0 and dev.received == [(0xED, True)], (
        f"{label}: command: words {[hex(w) for w in rest]}, device received {dev.received}, errors {dev.errors[:2]}")
    assert len(rest) >= 2 and fx.ps2_decode(fx.ps2_word_bits(rest[1])) == (0xFA, True), f"{label}: answer {rest}"
    assert not dev.errors and not dev.stopped, f"{label}: device errors {dev.errors[:2]}, stopped {dev.stopped}"
    await bench.finish(log)

    prog = fx.assemble("ps2_dev.s", **PS2_DEV_EQU)
    host = fx.Ps2Host(t_rts=4800, tchg=120, inhibit_frame=1, inhibit_at=5, inhibit_delay=240, inhibit_len=3000)
    label = "rtl ps2_dev.s 12.5 kHz, frame 1 inhibited at falling edge 5"
    bench = RtlBench(dut, prog.words, owner=ff.owners({fx.P_PCLK: 0, fx.P_PDATA: 0}), od=fx.od((fx.P_PCLK, fx.P_PDATA)),
                     inputs=fx.Ps2Bus(host), label=label)
    await bench.run(200)
    stops = 0
    sent = (0xAA, 0xFA, 0x12, 0xF0, 0x12)
    for b in sent:
        word = sum(bit << i for i, bit in enumerate(fx.ps2_frame(b)))
        for _ in range(3):
            n0 = len(bench.words(0))
            bench.send(0, word)
            for _ in range(100):
                await bench.run(2400)
                if len(bench.words(0)) > n0:
                    break
            if bench.words(0)[n0:] == [0]:
                break
            stops += 1
            await bench.run(12_000)
    assert host.frames == [(b, True) for b in sent], f"{label}: system received {host.frames}, stops {stops}"
    host.cmd, host.cmd_at = 0xF4, bench.m.cycle + 3600
    n0 = len(bench.words(0))
    await bench.run(3600 + 5040 + 14 * 7200 + HOST_START)
    got = bench.words(0)[n0:]
    assert host.ack == 0 and len(got) == 1 and fx.ps2_decode(fx.ps2_word_bits(got[0])) == (0xF4, True), (
        f"{label}: command: acknowledge {host.ack}, words {[hex(w) for w in got]}")
    assert stops == 1, f"{label}: {stops} frames stopped by the system, expected 1"
    log.info(f"{label}: clock period {min(host.periods)} to {max(host.periods)} cycles")
    await bench.finish(log)


# ----- timed trigger -----


@cocotb.test()
async def test_rtl_trigger(dut):
    """fw/trigger.s on the chip: UART frames at 115,200 bit/s (B 521 at 60 MHz), D 240; the pulse delay from the
    start edge of each matching frame at 4 start phases must be the same (0 cycles of jitter) and equal
    B / 2 + 8 B + D + 3 (synchronizer 2 cycles, event 1)."""
    start_clock(dut)
    log = dut._log
    b, d, match = 521, 240, 0x5A
    data = [0x11, match, 0xA5, match]
    lat = []
    for off in range(4):
        starts = [HOST_START + off + i * 12 * b for i in range(4)]
        line = ff.uart_line([(s, ff.frame_bits(x, "8N1")) for s, x in zip(starts, data)], b)
        label = f"rtl trigger.s B {b} D {d} start phase {off}"
        bench = RtlBench(dut, fx.assemble("trigger.s").words, owner=ff.owners({13: 0}),
                         inputs=lambda c, uo, uio, oe, line=line: (ff.value_at(line, c) << 3, 0), label=label)
        bench.watch("trg", lambda uo, uio, oe: (uo >> 5) & 1)
        bench.send(0, b, match, d)
        await bench.run(starts[-1] + 12 * b + d + 500)
        await bench.collect(0, 2)
        rises = [c for i, (c, v) in enumerate(bench.changes("trg")) if v == 1 and i > 0]
        assert len(rises) == 2 and bench.words(0) == [match, match], f"{label}: pulses {rises}, words {bench.words(0)}"
        lat += [rises[0] - starts[1], rises[1] - starts[3]]
        await bench.finish(log)
    want = b // 2 + 8 * b + d + 3
    log.info(f"rtl trigger.s: pulse delay from the start edge {sorted(set(lat))} cycles over {len(lat)} matches; "
             f"formula {want}")
    assert set(lat) == {want}, f"pulse delays {lat}, expected {want} at every phase"


# ----- hold-out trials -----


@cocotb.test()
async def test_rtl_onewire(dut):
    """fw/onewire.s on the chip at 60 MHz (OW_EQU[60]) against the independent DS18B20 device model at its slowest
    sample time and its shortest presence wait (sample 60 us, presence wait 15 us, presence low 240 us, read hold
    15 us): Read ROM, Write Scratchpad, Read Scratchpad; every controller low and high time within the data sheet
    limits; no device gives presence reply 1."""
    start_clock(dut)
    log = dut._log
    prog = ho.assemble("onewire.s", **ho.OW_EQU[F_MHZ])
    owner = ff.owners({ho.P_OW: 0})
    od = ff.open_drain({ho.P_OW})
    dev = ho.OneWireDevice(F_MHZ, t_s=60, t_pdh=15, t_pdl=240, t_rd=15)
    label = "rtl onewire.s 60 MHz, device sample 60 us, presence wait 15 us, presence low 240 us, read hold 15 us"
    bench = RtlBench(dut, prog.words, owner=owner, od=od, inputs=dev, label=label)
    bench.send(0, *ho.OW_SCRIPT)
    resets = sum(1 for w in ho.OW_SCRIPT if w & 0x100)
    await bench.run(HOST_START + resets * 1000 * F_MHZ + (len(ho.OW_SCRIPT) - resets) * 8 * 72 * F_MHZ)
    await bench.collect(0, len(ho.ow_expected()))
    assert bench.words(0) == ho.ow_expected(), f"{label}: replies {[hex(w) for w in bench.words(0)]}"
    assert dev.resets == 3 and dev.scratch[2:5] == [0x19, 0xE6, 0x3F], (dev.resets, dev.scratch)
    kinds = ho.ow_check_times(dev.master, F_MHZ)
    log.info(f"{label}: {len(bench.words(0))} replies correct; controller times within the DS18B20 limits {kinds}")
    await bench.finish(log)
    dev = ho.OneWireDevice(F_MHZ, present=False)
    label = "rtl onewire.s 60 MHz, no device"
    bench = RtlBench(dut, prog.words, owner=owner, od=od, inputs=dev, label=label)
    bench.send(0, ho.OW_RESET)
    await bench.run(HOST_START + 1000 * F_MHZ + 2000)
    assert bench.words(0) == [1], f"{label}: replies {bench.words(0)}"
    await bench.finish(log)


@cocotb.test()
async def test_rtl_am2302(dut):
    """fw/am2302.s on the chip at 60 MHz against the independent AM2302 sensor model: the data sheet example with a
    negative temperature from a sensor with all times x 1.2 that answers after 40 us; no sensor (reply 0x8029); a
    sensor that stops after 20 bits (reply 0x800C)."""
    start_clock(dut)
    log = dut._log
    prog = ho.assemble("am2302.s", **ho.AM_EQU[F_MHZ])
    owner = ff.owners({ho.P_AM: 0})
    od = ff.open_drain({ho.P_AM})
    hum, tmp = ho.AM_DATA[1]
    for dev, want, label in (
            (ho.Am2302Device(F_MHZ, hum, tmp, scale=1.2, t_go=40), [0, hum, tmp, ho.am_sum(hum, tmp)],
             "rtl am2302.s 60 MHz, -10.1 C example, sensor times x 1.2, answer after 40 us"),
            (ho.Am2302Device(F_MHZ, hum, tmp, present=False), [0x8000 | 41], "rtl am2302.s 60 MHz, no sensor"),
            (ho.Am2302Device(F_MHZ, hum, tmp, end_low=False, nbits=20), [0x8000 | 12],
             "rtl am2302.s 60 MHz, sensor stops after 20 bits")):
        bench = RtlBench(dut, prog.words, owner=owner, od=od, inputs=dev, label=label)
        bench.send(0, 1)
        for _ in range(12000 * F_MHZ // 10_000 + 2):
            await bench.run(10_000)
            got = bench.words(0)
            if got and (got[0] != 0 or len(got) == 4):
                break
        assert bench.words(0) == want, f"{label}: replies {[hex(w) for w in bench.words(0)]}"
        if dev.present and dev.nbits == 40:
            assert dev.starts == 1 and dev.conflicts == 0, (dev.starts, dev.conflicts)
            start_ms = dev.host_lows[0] / (1000 * F_MHZ)
            assert 1.0 <= start_ms <= 10.0, f"{label}: start signal {start_ms} ms"
            log.info(f"{label}: replies correct, start signal {start_ms:.3f} ms")
        await bench.finish(log)


@cocotb.test()
async def test_rtl_pdi(dut):
    """fw/pdi.s on the chip at 60 MHz (PDI_CLK 1 MHz, PDI_EQU[60]) against the independent XMEGA PDI device model
    (reset pulse width 1 us, output delay 1 cycle): enable, KEY, LDCS and STCS, STS and LDS, ST ptr with REPEAT and
    LD *(ptr++), guard times 128, 4 and 2 bits, a parity error and the BREAK recovery; DATA set-up and hold at the
    rising edge. Each receive word goes in one PUSH with the instruction before it (fw/pdi.s host rule)."""
    start_clock(dut)
    log = dut._log
    half = ho.PDI_EQU[F_MHZ]["HALF"]
    dev = ho.PdiDevice(F_MHZ, t_rst=1.0, d_out=1, undriven=0, mem=ho.PDI_MEM)
    label = "rtl pdi.s 60 MHz, reset pulse width 1 us, device output delay 1 cycle"
    bench = RtlBench(dut, ho.assemble("pdi.s", **ho.PDI_EQU[F_MHZ]).words,
                     owner=ff.owners({ho.P_CLK: 0, ho.P_DAT: 0}), inputs=dev, label=label)
    script = list(ho.PDI_SCRIPT)
    i = 0
    while i < len(script):
        if i + 1 < len(script) and script[i + 1] & 0x8000:
            bench.send_group(0, script[i], script[i + 1])
            i += 2
        else:
            bench.send(0, script[i])
            i += 1
    want = sum(w & 0xFF for w in script if w & 0x8000)
    for _ in range(200):
        await bench.run(5000)
        if len(bench.words(0)) >= want and not any(bench.to_thread):
            break
    got = bench.words(0)
    assert not dev.faults, f"{label}: {dev.faults}"
    assert ho.pdi_result(got) == ho.PDI_REPLY, f"{label}: replies {[hex(w) for w in got]}"
    assert dev.enabled and dev.nvmen == 1 and dev.breaks == 2 and dev.frame_errors == 1, (
        f"{label}: enabled {dev.enabled}, NVMEN {dev.nvmen}, breaks {dev.breaks}, errors {dev.frame_errors}")
    assert dev.mem[0x01000100] == 0x34 and dev.mem[0x01000101] == 0x12 and dev.ptr == ho.PDI_SIG + 3, label
    assert dev.setup_min >= half - 2 and dev.hold_min >= half - 2, (dev.setup_min, dev.hold_min)
    log.info(f"{label}: {len(got)} replies correct; DATA set-up {dev.setup_min} and hold {dev.hold_min} cycles")
    await bench.finish(log)

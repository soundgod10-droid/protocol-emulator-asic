"""10BASE-T station firmware (fw/eth_10bt.s, data fw/eth_10bt_data.py) in the chip, against the link-partner
model of test/linemodel/tenbaset.py (written from IEEE 802.3 clauses 3, 4, 7 and 14, not from the RTL or the
firmware).

Line model: the partner drives the receive pair as three levels (+1, -1, 0). The board's receive slicer gives
ui_in[0] = 1 for +1, the squelch comparator ui_in[1] = 1 for a non-zero level. The chip drives TD+ (uio0),
TD- (uio1) and OE (uio2); the line level is +1 for TD+ high, -1 for TD- high and 0 while OE is low. There is
no transmit network, magnetics or cable: a logic simulation at 60 MHz. Every chip packet and link pulse is
checked by the partner model (Manchester bits, preamble and SFD, FCS, rate, jitter, TP_IDL, pulse width,
interframe gap); the received-frame verdicts come from its expected_rx().

test_eth_dev_link loads the link timers divided by 16 (fw/eth_10bt_data.py scale), so the link state machine
runs in milliseconds; its times are multiplied by 16 before they are compared with the 802.3 ranges.
test_eth_dev_nlp_period runs the real timers.
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Lock, Timer
from cocotb.utils import get_sim_time

try:
    from cocotb.triggers import ValueChange
except ImportError:                      # older cocotb
    from cocotb.triggers import Edge as ValueChange

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "fw"))
import eth_10bt_data  # noqa: E402
import host_spi as hs  # noqa: E402
import pe_asm  # noqa: E402
from linemodel import tenbaset as T  # noqa: E402
from linemodel.wave import Wave  # noqa: E402

FW = os.path.join(ROOT, "fw", "eth_10bt.s")
CHIP_CLK_PS = 16666
PIN_HDRV, PIN_HETH = 1 << 4, 1 << 5
BT = T.BT_PS
MAC_DUT = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])
MAC_PARTNER = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x02])
EV_PASS, EV_FAIL, EV_SENT, EV_NOTSENT, EV_DISCARD = 1, 2, 3, 4, 5


def helper_pin(signal):
    """Pin configuration word: 10BASE-Te helper drive, signal 0 TXA (TD+), 1 TXB (TD-), 4 OE."""
    return PIN_HDRV | PIN_HETH | (signal << 6)


def words_of(b):
    b = bytes(b) + (b"\0" if len(b) % 2 else b"")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


class Line:
    """Receive pair driven by the partner model; transmit pair captured from the chip pins."""

    def __init__(self, dut, host):
        self.dut, self.host = dut, host
        self.rx_level = 0
        self.tx_level = 0
        self.tx = []                      # chip line level changes (absolute ps, level)
        self.rx_log = []                  # partner bursts (start, end) absolute ps
        self._apply()
        cocotb.start_soon(self._watch())

    def _apply(self):
        lv = self.rx_level
        self.host.inputs = (self.host.inputs & ~0x3) | (1 if lv > 0 else 0) | ((1 if lv != 0 else 0) << 1)
        self.host._drive()

    def set_rx(self, lv):
        if lv != self.rx_level:
            self.rx_level = lv
            self._apply()

    def _level(self):
        v, oe = self.dut.uio_out.value, self.dut.uio_oe.value
        if not (v.is_resolvable and oe.is_resolvable):
            return 0
        v, oe = v.to_unsigned(), oe.to_unsigned()
        if (oe & 0x7) != 0x7 or not (v >> 2) & 1:
            return 0
        tdp, tdm = v & 1, (v >> 1) & 1
        return 1 if (tdp and not tdm) else (-1 if (tdm and not tdp) else 0)

    async def _watch(self):
        while True:
            await ValueChange(self.dut.uio_out)
            lv = self._level()
            if lv != self.tx_level:
                self.tx_level = lv
                self.tx.append((get_sim_time("ps"), lv))

    async def send(self, wave):
        """Drive a partner wave from now; returns its start time (absolute ps)."""
        start = get_sim_time("ps")
        self.set_rx(wave.initial)
        for t, lv in wave.edges:
            dt = start + t - get_sim_time("ps")
            if dt >= 1:
                await Timer(round(dt), unit="ps")
            self.set_rx(lv)
        dt = start + wave.end - get_sim_time("ps")
        if dt >= 1:
            await Timer(round(dt), unit="ps")
        self.set_rx(0)
        last = max([t for t, lv in wave.edges if lv != 0] + [0])
        nz = [t for t, lv in wave.edges if lv == 0]
        self.rx_log.append((start, start + (nz[-1] if nz else wave.end)))
        return start

    def tx_wave(self, t0, t1):
        """Chip transmit levels in [t0, t1) (absolute ps) as a Wave with times relative to t0."""
        init = 0
        for t, lv in self.tx:
            if t < t0:
                init = lv
        return Wave(init, [(t - t0, lv) for t, lv in self.tx if t0 <= t < t1], end=t1 - t0)


class HostPort:
    """Host side of the thread 0 FIFOs: a reader that splits received frames and events, and a writer."""

    def __init__(self, host):
        self.host = host
        self.lock = Lock()
        self.events = []                  # (ps, word)
        self.frames = []                  # (ps, bytes)
        self._buf = []
        self.running = True
        cocotb.start_soon(self._poll())

    async def _poll(self):
        while self.running:
            async with self.lock:
                words, _ = await self.host.pull(0)
            if words:
                self._buf += words
                self._parse()
            else:
                await Timer(10, unit="us")

    def _parse(self):
        t = get_sim_time("ps")
        while self._buf:
            w = self._buf[0]
            if w & 0x8000:
                n = w & 0x7FFF
                nw = (n + 1) // 2
                if len(self._buf) < 1 + nw:
                    return
                data = b"".join(x.to_bytes(2, "little") for x in self._buf[1:1 + nw])[:n]
                self.frames.append((t, data))
                del self._buf[:1 + nw]
            else:
                self.events.append((t, w))
                del self._buf[0]

    async def send_frame(self, frame):
        words = [len(frame)] + words_of(frame)
        i = 0
        while i < len(words):
            async with self.lock:
                st = await self.host.read_status()
                free = 4 - st["in_cnt"][0]
                if free > 0:
                    await self.host.push(0, words[i:i + free])
                    i += free
            await Timer(5, unit="us")

    async def wait(self, cond, timeout_us, step_us=20):
        t_end = get_sim_time("us") + timeout_us
        while not cond():
            assert get_sim_time("us") < t_end, "timeout waiting for the chip (events %s, frames %d)" % (
                [hex(w) for _, w in self.events], len(self.frames))
            await Timer(step_us, unit="us")

    def codes(self):
        return [w for _, w in self.events]


async def eth_chip(dut, scale=1, seed=0xACE1):
    prog = pe_asm.assemble_file(FW)
    v_first = prog.symbols["V_BASE"][0]
    assert prog.size <= v_first, "firmware %d words overlaps the variables at %d" % (prog.size, v_first)
    mem, info = eth_10bt_data.build(prog.symbols, scale=scale, seed=seed)
    cocotb.start_soon(Clock(dut.clk, CHIP_CLK_PS, unit="ps").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    host = hs.SpiTimed(dut)
    line = Line(dut, host)
    await host.write_mem(0, prog.image()[:prog.size])
    for addr, words in mem:
        await host.write_mem(addr, words)
    head, _ = hs.config_words((0, 0, 0, 0), (True, False, False, False), [None] * 15, [False] * 15)
    pins = [0] * 15
    pins[0], pins[1], pins[2] = helper_pin(0), helper_pin(1), helper_pin(4)
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    await host.start()
    t_start = get_sim_time("ps")
    port = HostPort(host)
    await ClockCycles(dut.clk, 200)
    dut._log.info("fw/eth_10bt.s: %d program words (variables from %d); timers %s units (scale %d)" %
                  (prog.size, v_first, info["timers_units"], scale))
    return host, line, port, info, t_start


def frame_of(n_octets, seed, ethertype=0x88B5):
    """A frame of n_octets with its FCS (DA, SA, local experimental EtherType, random data)."""
    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(n_octets - 18))
    return T.build_frame(MAC_DUT, MAC_PARTNER, ethertype, payload)


def partner_wave(frame, ppm=0.0, jitter_ps=0.0, seed=1, extra_bits=()):
    w = T.manchester_wave(T.packet_bits(frame, extra_bits=extra_bits), ppm=ppm)
    return T.zero_crossing_jitter(w, jitter_ps, seed) if jitter_ps else w


@cocotb.test()
async def test_eth_dev_rx(dut):
    """Receive: frames from the partner at the receiver limits (+/-100 ppm, 13.5 ns zero-crossing jitter,
    IEEE 802.3 7.3.2 and 14.3.1.3.1): 64 and 1,518 octets are delivered to the host without the FCS; a frame
    with 4 dribble bits is delivered (truncated to the octet, 4.2.4.2.1); an FCS error and a 46-octet runt are
    discarded with their reasons. Each verdict must equal the partner model's expected_rx()."""
    host, line, port, info, _ = await eth_chip(dut)
    good = frame_of(64, 1)
    bad = bytearray(frame_of(64, 2))
    bad[30] ^= 0x10
    cases = [
        ("64 octets, +100 ppm, 13.5 ns", frame_of(64, 3), 100.0, 13500.0, ()),
        ("1518 octets, -100 ppm, 13.5 ns", frame_of(1518, 4), -100.0, 13500.0, ()),
        ("64 octets + 4 dribble bits", good, 0.0, 0.0, (1, 0, 1, 1)),
        ("FCS error", bytes(bad), 0.0, 0.0, ()),
        ("46-octet runt", T.build_frame(MAC_DUT, MAC_PARTNER, 0x88B5, bytes(28), pad=False), 0.0, 0.0, ()),
    ]
    results = []
    for k, (label, frame, ppm, jit, extra) in enumerate(cases):
        exp, why, ref = T.expected_rx(frame, extra_bits=extra, jitter_ps=jit, ppm=ppm)
        n_f, n_e = len(port.frames), len(port.events)
        await line.send(partner_wave(frame, ppm, jit, seed=10 + k, extra_bits=extra))
        await port.wait(lambda: len(port.frames) > n_f or len(port.events) > n_e, 20000)
        await Timer(200, unit="us")
        new_f, new_e = port.frames[n_f:], [w for _, w in port.events[n_e:]]
        if exp == "DELIVER":
            assert len(new_f) == 1 and new_f[0][1] == frame[:-4] and not new_e, \
                "%s: expected delivery (%s), got frames %d events %s" % (label, why, len(new_f), new_e)
            results.append("%s: delivered (%d octets)" % (label, len(new_f[0][1])))
        else:
            reason = 1 if "minFrameSize" in why else 2
            assert not new_f and new_e == [EV_DISCARD | (reason << 8)], \
                "%s: expected discard (%s), got frames %d events %s" % (label, why, len(new_f), [hex(w) for w in new_e])
            results.append("%s: discarded, reason %d (%s, %s)" % (label, reason, why, ref))
    port.running = False
    dut._log.info("RX: " + "; ".join(results))


@cocotb.test()
async def test_eth_dev_tx(dut):
    """Transmit: frames from the host of 42 octets (padded to 60, 4.2.3.3), 60 and 408 octets (the largest the
    buffer holds) go out with preamble, SFD and FCS (CRC-32 by the helper); a 409-octet frame is refused
    (event 4). Every packet is decoded by the partner model: no violation (bits, preamble, SFD, FCS, rate,
    jitter, TP_IDL); the gaps between packets are at least 96 BT."""
    host, line, port, info, t0 = await eth_chip(dut)
    rng = random.Random(7)
    sent = []
    for n in (42, 60, 408):
        body = MAC_PARTNER + MAC_DUT + (0x88B5).to_bytes(2, "big") + bytes(rng.randrange(256) for _ in range(n - 14))
        n_e = len(port.events)
        await port.send_frame(body)
        await port.wait(lambda: len(port.events) > n_e, 5000)
        assert port.codes()[n_e:] == [EV_SENT], "events %s" % [hex(w) for w in port.codes()[n_e:]]
        sent.append(body + bytes(max(0, 60 - len(body))))
    n_e = len(port.events)
    await port.send_frame(bytes(409))
    await port.wait(lambda: len(port.events) > n_e, 5000)
    assert port.codes()[n_e:] == [EV_NOTSENT], "events %s" % [hex(w) for w in port.codes()[n_e:]]
    await Timer(100, unit="us")
    port.running = False
    t1 = get_sim_time("ps")
    evs = T.decode_tx(line.tx_wave(t0, t1))
    pk = [e for e in evs if e.kind == "packet"]
    assert len(pk) == 3, "packets on the line: %d (%s)" % (len(pk), [e.kind for e in evs])
    for e, body in zip(pk, sent):
        assert not e.violations, "packet violations: %s" % e.violations
        assert e.frame[:-4] == body, "frame on the line differs from the host frame"
    v = T.check_tx_timing(pk, t1 - t0, check_link=False)
    assert not [x for x in v if x[0] == "IPG"], v
    dut._log.info("TX: %s" % "; ".join(
        "%d octets + FCS, preamble %d bits, rate %+.1f ppm, jitter %.0f ps, TP_IDL HI %.2f BT" % (
            len(e.frame) - 4, e.preamble_bits, e.measures["rate_ppm"], e.measures["jitter_ps"],
            e.measures["tpidl_hi_bt"]) for e in pk))


@cocotb.test()
async def test_eth_dev_collision(dut):
    """Collision (half duplex): the partner starts a burst (56 preamble bits, then 32 jam bits) as soon as the
    chip starts a frame. The chip must stop its frame with the 32-bit jam (helper), back off (truncated binary
    exponential backoff, 4.2.3.2.5), wait for the end of the partner burst plus 96 BT (4.2.3.2.2) and send
    the frame again, complete and valid (event 3, not event 4)."""
    host, line, port, info, t0 = await eth_chip(dut)
    body = MAC_PARTNER + MAC_DUT + (0x88B5).to_bytes(2, "big") + bytes(range(46))
    n_tx = len(line.tx)
    cocotb.start_soon(port.send_frame(body))
    while len(line.tx) == n_tx:
        await Timer(100, unit="ns")
    t_dut0 = line.tx[n_tx][0]
    burst = T.manchester_wave([(i + 1) % 2 for i in range(56)] + [1, 0] * 16)
    t_p0 = await line.send(burst)
    t_p1 = line.rx_log[-1][1]
    n_e = len(port.events)
    await port.wait(lambda: EV_SENT in port.codes() or EV_NOTSENT in port.codes(), 5000)
    await Timer(100, unit="us")
    port.running = False
    t1 = get_sim_time("ps")
    assert EV_SENT in port.codes() and EV_NOTSENT not in port.codes(), [hex(w) for w in port.codes()]
    evs = T.decode_tx(line.tx_wave(t0, t1))
    bursts = [e for e in evs if e.kind != "pulse"]
    assert len(bursts) == 2, "chip bursts: %s" % [(e.kind, round((e.t1 - e.t0) / BT, 1)) for e in evs]
    first, second = bursts
    first_bt = (first.t1 - first.t0) / BT
    full_bt = 64 + 64 * 8 / 1.0
    assert first_bt < 200, "first burst %.1f BT: not stopped by the collision" % first_bt
    jam_end_bt = (t0 + first.t1 - t_p0) / BT
    assert jam_end_bt >= 32, "first burst ended %.1f BT after the partner start (< 32 jam bits)" % jam_end_bt
    gap_bt = (t0 + second.t0 - t_p1) / BT
    assert gap_bt >= 96, "retry %.1f BT after the end of the partner burst (< 96 BT)" % gap_bt
    assert not second.violations and second.frame[:-4] == body, second.violations
    dut._log.info("collision: chip frame started %.2f us after the host load began; partner burst %.1f BT; "
                  "first chip burst %.1f BT (ends %.1f BT after the partner start); retry %.1f BT after the "
                  "partner burst ended; retry valid (%d octets + FCS); events %s" % (
                      (t_dut0 - t0) / 1e6, (t_p1 - t_p0) / BT, first_bt, jam_end_bt, gap_bt,
                      len(second.frame) - 4, [hex(w) for w in port.codes()]))


@cocotb.test()
async def test_eth_dev_link(dut):
    """Link Integrity Test (Figure 14-6, 14.2.1.7) with the link timers divided by 16 (see the module text):
    power on in LINK TEST PASS; with no activity the link fails after link_loss (must be 50 to 150 ms at the
    real scale); a frame from the host is then not sent (event 4); link pulses 1.6 ms apart (real scale; less
    than link_test_min 2 ms) keep it failed; 4 pulses 16 ms apart (real scale) bring LINK TEST PASS (lc_max 4,
    allowed 2 to 10); after a new link loss a received frame brings LINK TEST PASS at once (RD = active) and
    is not delivered (reason 4). The chip's own link pulses: width inside Figure 14-13, period 8 to 24 ms
    (real scale). Every state the chip reports must be allowed by the partner model's expected_link_state()."""
    S = 16
    host, line, port, info, t0 = await eth_chip(dut, scale=S)
    ms = 1e9 / S                                       # ps of one real-scale millisecond in this test

    def t_real_ms(t):
        return (t - t0) / ms

    await port.wait(lambda: EV_FAIL in port.codes(), 200000 / S * 2)
    t_fail1 = port.events[port.codes().index(EV_FAIL)][0]
    assert 50 <= t_real_ms(t_fail1) <= 150 + 0.5, "link fail after %.2f ms (real scale)" % t_real_ms(t_fail1)
    n_e = len(port.events)
    await port.send_frame(bytes(60))
    await port.wait(lambda: len(port.events) > n_e, 2000)
    assert port.codes()[n_e:] == [EV_NOTSENT], [hex(w) for w in port.codes()[n_e:]]
    rd = []                                           # partner activity for the model, real-scale ps
    pulse = T.nlp_wave()
    n_e = len(port.events)
    for _ in range(4):                                # early pulses: 1.6 ms (real) apart
        ts = await line.send(pulse)
        rd.append(("pulse", (ts - t0) * S))
        await Timer(round(1.6 * ms), unit="ps")
    assert EV_PASS not in port.codes()[n_e:], "early pulses brought the link up"
    for _ in range(4):                                # 16 ms (real) apart
        await Timer(round(16 * ms), unit="ps")
        ts = await line.send(pulse)
        rd.append(("pulse", (ts - t0) * S))
    await port.wait(lambda: EV_PASS in port.codes()[n_e:], 2000)
    t_pass = port.events[n_e + port.codes()[n_e:].index(EV_PASS)][0]
    exp = T.expected_link_state(rd, (t_pass - t0) * S + 1e6)
    assert exp in ("PASS", "EITHER"), "chip reports LINK TEST PASS where a compliant MAU must be in %s" % exp
    exp_before = T.expected_link_state(rd, (rd[4][1] - 1e6))
    assert exp_before in ("FAIL", "EITHER"), exp_before
    n_e = len(port.events)
    await port.wait(lambda: EV_FAIL in port.codes()[n_e:], 200000 / S * 2)
    t_fail2 = port.events[n_e + port.codes()[n_e:].index(EV_FAIL)][0]
    n_e = len(port.events)
    await line.send(partner_wave(frame_of(64, 9)))
    await port.wait(lambda: len(port.events) >= n_e + 2, 2000)
    assert port.codes()[n_e:n_e + 2] == [EV_PASS, EV_DISCARD | (4 << 8)], [hex(w) for w in port.codes()[n_e:]]
    assert not port.frames, "a frame received in LINK TEST FAIL was delivered"
    port.running = False
    t1 = get_sim_time("ps")
    evs = T.decode_tx(line.tx_wave(t0, t1))
    pulses = [e for e in evs if e.kind == "pulse"]
    assert pulses and all(not e.violations for e in pulses), [e.violations for e in pulses if e.violations]
    assert len(pulses) == len(evs), "the chip sent something other than link pulses: %s" % [e.kind for e in evs]
    gaps = [(b.t0 - a.t1) * S / 1e9 for a, b in zip(pulses, pulses[1:])]
    assert all(8 <= g <= 24 for g in gaps), "pulse silences (real ms): %s" % gaps
    widths = sorted(set(round(e.measures["width_ps"]) for e in pulses))
    dut._log.info("link (timers / %d, times at the real scale): fail after %.2f ms; frame refused in FAIL; "
                  "4 pulses 1.6 ms apart: no pass; pass %.2f ms after the 4th pulse 16 ms apart (lc_max 4); "
                  "second fail %.2f ms after the last pulse; frame in FAIL: pass + discard reason 4; chip "
                  "pulses %d, width %s ps, silence %.3f-%.3f ms" % (
                      S, t_real_ms(t_fail1), (t_pass - t0 - rd[-1][1] / S) / ms,
                      (t_fail2 - t0 - rd[-1][1] / S) / ms, len(pulses), widths, min(gaps), max(gaps)))


@cocotb.test()
async def test_eth_dev_nlp_period(dut):
    """Link test pulses at the real timer lengths: with no frames the chip sends a pulse every 16 ms
    (silence 8 to 24 ms, 14.2.1.1); each pulse inside Figure 14-13 (85-137.5 ns at 2.8 V, test/linemodel)."""
    host, line, port, info, t0 = await eth_chip(dut)
    port.running = False
    await Timer(33, unit="ms")
    t1 = get_sim_time("ps")
    evs = T.decode_tx(line.tx_wave(t0, t1))
    assert len(evs) == 2 and all(e.kind == "pulse" and not e.violations for e in evs), \
        [(e.kind, e.violations) for e in evs]
    first_ms = evs[0].t0 / 1e9
    gap_ms = (evs[1].t0 - evs[0].t1) / 1e9
    assert 8 <= gap_ms <= 24, "silence %.4f ms" % gap_ms
    dut._log.info("NLP: first pulse %.4f ms after START, silence %.4f ms, widths %s ps" % (
        first_ms, gap_ms, [round(e.measures["width_ps"]) for e in evs]))

# SPDX-License-Identifier: Apache-2.0
"""USB low-speed device and 10BASE-T station firmware in the reference model with the line-helper model.

What it does:
  These tests run fw/usb_dev.s and fw/eth_10bt.s in the reference model (model/pe_model.py) with the
  port-level model of the two line helpers (test/slu_model.py HelperChip), in closed loop with the per-cycle
  device models of test/linemodel/cycle_devices.py: the USB host session (UsbHostPort) and the 10BASE-T link
  partner (TenBaseTPort), both written from the standards, not from the RTL or the firmware. The checks are
  those of the RTL tests test/test_usb_dev.py and test/test_eth_dev.py (no USB 2.0 or 802.3 violation in any
  chip packet, the host results, the events to the host). No HDL is simulated.

  Each procedure (*_proc) is written once as a generator: it makes its bench with the factory it is given,
  attaches the device model and yields (bench, done, limit, part) to have the bench run in parts of `part`
  cycles until done() or `limit` cycles; between parts it may give host words, as a host does. drive_model()
  runs it on the model bench (test_fw_full.Bench); test/test_helpers_rtl.py runs the same procedure on the chip
  RTL with the model in lockstep. The procedure returns its check function's text.

Why:
  The model test benches and the browser simulator (sim/py/examples.py) run the reference model. The same
  procedures on the RTL, with the pins and every helper register compared in every cycle, show that the runs
  that the simulator shows are the RTL's runs for the same inputs.

Conditions: logic levels at a 60 MHz clock; the device models change their outputs at clock edges (one clock
period of quantization, test/linemodel/cycle_devices.py); no transceiver, cable, magnetics or comparator delay.
Simulation-speed shortcuts of test_usb_dev.py: bus reset 20 us, reset recovery 100 us.
"""

import os
import random
import sys

import cocotb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(ROOT, "model"), os.path.join(ROOT, "asm"), os.path.join(ROOT, "fw"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import eth_10bt_data  # noqa: E402
import pe_model  # noqa: E402
import slu_model  # noqa: E402
import test_fw_full as ff  # noqa: E402
import usb_dev_data  # noqa: E402
from linemodel import cycle_devices as CD  # noqa: E402
from linemodel import tenbaset as T  # noqa: E402
from linemodel import usb_ch9 as C  # noqa: E402
from linemodel import usb_host as H  # noqa: E402
from linemodel import usb_ls as L  # noqa: E402

GL = os.environ.get("GATES") == "yes"
CLK_PS = 16666                            # 60 MHz, as the RTL tests
RESET_PS, RECOVERY_PS = 20e6, 100e6       # test_usb_dev.py simulation-speed shortcuts
EV_RESET, EV_SUSPEND, EV_RESUME, EV_CONFIG, EV_LED, EV_EP1, EV_ADDR = 1, 2, 3, 4, 5, 6, 7
EV_PASS, EV_FAIL, EV_SENT, EV_NOTSENT, EV_DISCARD = 1, 2, 3, 4, 5
START_WAIT = 400                          # cycles after START before the USB host acts (test_usb_dev.usb_chip)


def helper_pin(signal, eth=False):
    """Pin configuration word of a helper pin (docs/host_port.md): helper drive, helper, signal."""
    return slu_model.PIN_HDRV | (slu_model.PIN_HETH if eth else 0) | (signal << 6)


USB_PINS = {0: helper_pin(0), 1: helper_pin(1), 2: helper_pin(5)}           # VPO, VMO, OE# (test_usb_dev)
ETH_PINS = {0: helper_pin(0, True), 1: helper_pin(1, True), 2: helper_pin(4, True)}  # TD+, TD-, OE


def image_of(prog, mem):
    """Program words and the data-memory words that the host writes before START."""
    image = dict(prog.words)
    for addr, words in mem:
        for i, w in enumerate(words):
            image[addr + i] = w
    return image


def helper_cfg(pins):
    cfg = [None] * pe_model.N_OUT
    for p, w in pins.items():
        cfg[p] = w
    return cfg


def model_bench(image, pins, helpers, inputs, label):
    """Bench factory of the model runs: HelperChip on test_fw_full.Bench."""
    return ff.Bench(slu_model.HelperChip(image, helper_cfg=helper_cfg(pins), helpers=helpers), inputs=inputs)


def run_until(bench, done, limit, part=6000):
    """Bench.run in parts until done() or `limit` cycles (the loop of the hold-out tests)."""
    m = bench.m
    while m.cycle < limit and not done():
        bench.run(min(part, limit - m.cycle))


def drive_model(proc):
    """Runs a procedure generator on the model bench; returns its result."""
    try:
        req = next(proc)
        while True:
            run_until(*req)
            req = proc.send(None)
    except StopIteration as stop:
        return stop.value


def run_for(bench, cycles, part=6000):
    """A run request of `cycles` more cycles."""
    return bench, (lambda: False), bench.m.cycle + cycles, part


# ----- USB low-speed device -----

def usb_setup():
    prog = ff.assemble("usb_dev.s")
    v_first = prog.symbols["V_BASE"][0]
    assert prog.size <= v_first, "firmware %d words overlaps the variables at %d" % (prog.size, v_first)
    mem, info = usb_dev_data.build(prog.symbols)
    return prog, mem, info


def usb_events_expected(addr=5):
    return [EV_RESET, EV_ADDR | (addr << 8), EV_CONFIG | (1 << 8)]


def usb_packets(s):
    ipd = [e.measures["ipd_bits"] for e in s.log if "ipd_bits" in e.measures]
    eop = [e.measures["eop_se0_ps"] for e in s.log if "eop_se0_ps" in e.measures]
    if not ipd:
        return "no device packets"
    return ("%d device packets, reply %.3f to %.3f bit times after the host EOP, EOP SE0 %.0f to %.0f ns" % (
        len(ipd), min(ipd), max(ipd), min(eop) / 1e3, max(eop) / 1e3))


def usb_clean(s, dev):
    assert dev.done, "the host session did not end"
    assert not s.violations, "USB 2.0 violations: %s" % s.violations[:6]
    assert not dev.collisions, "host and device drove the bus at the same time: %s" % dev.collisions[:4]


def usb_enum_proc(make_bench, ppm=0.0, seed=None, addr=5, limit=1_200_000):
    """test_usb_dev.enumerate_once with the per-cycle host: bus reset, GET_DESCRIPTOR(device) at address 0,
    SET_ADDRESS, device, configuration, strings 0/1/2, device_qualifier (STALL), SET_CONFIGURATION 1."""
    prog, mem, info = usb_setup()
    s = H.UsbHostSession(ppm=ppm, stress_seed=seed)

    def session():
        yield from s.wait(START_WAIT * CLK_PS)
        return (yield from s.enumerate(addr=addr, reset_ps=RESET_PS, recovery_ps=RECOVERY_PS))
    dev = CD.UsbHostPort(session(), clk_ps=CLK_PS)
    bench = make_bench(image_of(prog, mem), USB_PINS, ("usb",), dev, "USB enumeration")
    yield bench, (lambda: dev.done), limit, 6000
    yield bench, (lambda: len(bench.words(0)) >= 3), bench.m.cycle + 20_000, 2000   # the events reach the host
    usb_clean(s, dev)
    rep = dev.report
    assert rep["strings"] == {1: usb_dev_data.MANUFACTURER, 2: usb_dev_data.PRODUCT}, rep["strings"]
    assert rep["device"]["idVendor"] == usb_dev_data.VID and rep["config"]["endpoints"] == [(0x81, 3, 8, 10)]
    got = bench.words(0)
    assert got == usb_events_expected(addr), "events %s" % [hex(w) for w in got]
    ff.assert_status_clean(bench.m, [0])
    return ("enumerated: address %d, configuration 1, strings %r; %d host checks, no violation; %s; events %s" % (
        addr, rep["strings"], len(s.log), usb_packets(s), [hex(w) for w in got]))


def usb_errors_proc(make_bench, limit=600_000):
    """test_usb_dev.test_usb_dev_errors with the per-cycle host: SETUP token with a bad CRC5, with a bad PID check,
    setup data with a bad CRC16, with a stuffing error, SETUP to address 9, IN with a bad CRC5 get no answer; then
    GET_DESCRIPTOR(device) still works."""
    prog, mem, info = usb_setup()
    s = H.UsbHostSession()
    gd = C.get_descriptor(C.DT_DEVICE, 0, 18)
    out = {}

    def session():
        yield from s.wait(START_WAIT * CLK_PS)
        yield from s.reset(RESET_PS, RECOVERY_PS)
        cases = [dict(tok_flags={"bad_crc": True}), dict(tok_flags={"bad_pid": True}),
                 dict(data_flags={"bad_crc": True}), dict(data_inj=L.Inject(stuff_violation=True))]
        for kw in cases:
            yield from s.setup(0, 0, gd, expect_ack=False, **kw)
            yield from s.gap(20)
        yield from s.setup(9, 0, gd, expect_ack=False)
        yield from s.gap(20)
        yield from s.in_(0, 0, tok_flags={"bad_crc": True}, expect=None)
        yield from s.gap(20)
        out["dev"], out["stall"] = yield from s.control_read(0, gd)
    dev = CD.UsbHostPort(session(), clk_ps=CLK_PS)
    bench = make_bench(image_of(prog, mem), USB_PINS, ("usb",), dev, "USB damaged packets")
    yield bench, (lambda: dev.done), limit, 6000
    yield run_for(bench, 2000)
    usb_clean(s, dev)
    silent = [e.what for e in s.log if e.what.endswith("no reply")]
    assert len(silent) == 6, "silent checks: %s" % silent
    assert out["dev"] == usb_dev_data.DEVICE and not out["stall"], out
    ff.assert_status_clean(bench.m, [0])
    return ("6 damaged or foreign packets, no answer to any; then GET_DESCRIPTOR(device) correct; %s; events %s" % (
        usb_packets(s), [hex(w) for w in bench.words(0)]))


# ----- 10BASE-T station -----

MAC_DUT = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])       # test_eth_dev.py
MAC_PARTNER = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x02])
ETHERTYPE = 0x88B5                                          # local experimental EtherType
RX_AT = 6000                              # cycle of the partner frame start (the firmware is ready after about 1,300)


def eth_setup(scale=1, seed=0xACE1):
    prog = ff.assemble("eth_10bt.s")
    v_first = prog.symbols["V_BASE"][0]
    assert prog.size <= v_first, "firmware %d words overlaps the variables at %d" % (prog.size, v_first)
    mem, info = eth_10bt_data.build(prog.symbols, scale=scale, seed=seed)
    return prog, mem, info


def eth_frame(n_octets, seed):
    """test_eth_dev.frame_of: a frame of n_octets with its FCS (DA, SA, EtherType, data from a seeded generator)."""
    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(n_octets - 18))
    return T.build_frame(MAC_DUT, MAC_PARTNER, ETHERTYPE, payload)


def eth_tx_body(tx_octets=60, seed=7):
    """A frame that the host gives to send (test_eth_dev.test_eth_dev_tx): DA partner, SA chip, EtherType, data."""
    rng = random.Random(seed)
    return MAC_PARTNER + MAC_DUT + ETHERTYPE.to_bytes(2, "big") + bytes(rng.randrange(256) for _ in range(tx_octets - 14))


def eth_words(frame):
    """Host words of a frame for fw/eth_10bt.s: the length, then the octets, low octet first."""
    b = bytes(frame) + (b"\0" if len(frame) % 2 else b"")
    return [len(frame)] + [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def eth_host_split(words):
    """Thread-to-host words of fw/eth_10bt.s: frames (0x8000 + length, then the octets) and event words."""
    frames, events, i = [], [], 0
    while i < len(words):
        w = words[i]
        if w & 0x8000:
            n = w & 0x7FFF
            nw = (n + 1) // 2
            if i + 1 + nw > len(words):
                break
            frames.append(b"".join(x.to_bytes(2, "little") for x in words[i + 1:i + 1 + nw])[:n])
            i += 1 + nw
        else:
            events.append(w)
            i += 1
    return frames, events


def eth_rxtx_proc(make_bench, rx_octets=64, tx_octets=60, limit=400_000):
    """The partner sends a frame (test_eth_dev.test_eth_dev_rx case 1 without drift: per-cycle input); after its
    delivery the host gives a frame to send (test_eth_dev.test_eth_dev_tx). Checks: the frame delivered without
    its FCS as the partner model expects; the host frame on the line once, decoded by the partner model with no
    violation; event 3 (sent)."""
    prog, mem, info = eth_setup()
    dev = CD.TenBaseTPort(clk_ps=CLK_PS)
    rx = eth_frame(rx_octets, 3)
    dev.send(T.manchester_wave(T.packet_bits(rx)), RX_AT)
    bench = make_bench(image_of(prog, mem), ETH_PINS, ("eth",), dev, "10BASE-T receive and send")
    yield bench, (lambda: eth_host_split(bench.words(0))[0]), limit, 2000
    body = eth_tx_body(tx_octets)
    bench.send(0, *eth_words(body))
    yield bench, (lambda: EV_SENT in eth_host_split(bench.words(0))[1]), limit, 2000
    yield run_for(bench, 6000)            # the packet's TP_IDL and the idle line after it
    exp, why, _ = T.expected_rx(rx)
    assert exp == "DELIVER", "partner model: %s (%s)" % (exp, why)
    frames, events = eth_host_split(bench.words(0))
    assert frames == [rx[:-4]], "host frames %s" % [len(f) for f in frames]
    assert events == [EV_SENT], "events %s" % [hex(w) for w in events]
    evs = T.decode_tx(dev.tx_wave(bench.m.cycle))
    pk = [e for e in evs if e.kind == "packet"]
    assert len(pk) == 1, "packets on the line: %s" % [e.kind for e in evs]
    e = pk[0]
    assert not e.violations, "packet violations: %s" % e.violations
    sent = body + bytes(max(0, 60 - len(body)))
    assert e.frame[:-4] == sent, "frame on the line differs from the host frame"
    ff.assert_status_clean(bench.m, [0])
    return ("received %d octets delivered to the host (FCS checked by the helper); sent %d octets + FCS, preamble %d "
            "bits, rate %+.1f ppm, jitter %.0f ps, TP_IDL HI %.2f BT, no violation" % (
                len(rx) - 4, len(sent), e.preamble_bits, e.measures["rate_ppm"], e.measures["jitter_ps"],
                e.measures["tpidl_hi_bt"]))


BT = T.BT_PS


def eth_collision_proc(make_bench, limit=600_000):
    """Two link test pulses from the partner (the helper's pulse classifier; in LINK TEST PASS they change no
    state), then test_eth_dev.test_eth_dev_collision with the per-cycle partner: the partner starts a burst (56
    preamble bits, then 32 jam bits) 76 cycles after the chip starts a frame. The chip must stop with the 32-bit
    jam, back off, wait for the end of the partner burst plus 96 BT and send the frame again, complete and valid."""
    prog, mem, info = eth_setup()
    dev = CD.TenBaseTPort(clk_ps=CLK_PS)
    nlp = T.nlp_wave()
    dev.send(nlp, 3000)
    dev.send(nlp, 15000)
    bench = make_bench(image_of(prog, mem), ETH_PINS, ("eth",), dev, "10BASE-T collision")
    yield run_for(bench, 20_000, 2000)
    body = MAC_PARTNER + MAC_DUT + ETHERTYPE.to_bytes(2, "big") + bytes(range(46))
    n_tx = len(dev.tx_edges)
    bench.send(0, *eth_words(body))
    yield bench, (lambda: len(dev.tx_edges) > n_tx), limit, 200
    t_dut0 = dev.tx_edges[n_tx][0]
    burst = T.manchester_wave([(i + 1) % 2 for i in range(56)] + [1, 0] * 16)
    c_p0 = bench.m.cycle + 76
    dev.send(burst, c_p0)
    t_p0 = c_p0 * CLK_PS
    t_p1 = t_p0 + burst.marks["idle_start"]
    yield bench, (lambda: {EV_SENT, EV_NOTSENT} & set(eth_host_split(bench.words(0))[1])), limit, 2000
    yield run_for(bench, 6000)
    frames, events = eth_host_split(bench.words(0))
    # as test_eth_dev_collision: sent (3), not refused (4); the partner's burst itself may be reported as a received
    # frame that was discarded (5)
    assert EV_SENT in events and EV_NOTSENT not in events, "events %s" % [hex(w) for w in events]
    evs = T.decode_tx(dev.tx_wave(bench.m.cycle))
    bursts = [e for e in evs if e.kind != "pulse"]
    assert len(bursts) == 2, "chip bursts: %s" % [(e.kind, round((e.t1 - e.t0) / BT, 1)) for e in evs]
    first, second = bursts
    first_bt = (first.t1 - first.t0) / BT
    assert first_bt < 200, "first burst %.1f BT: not stopped by the collision" % first_bt
    jam_end_bt = (first.t1 - t_p0) / BT
    assert jam_end_bt >= 32, "first burst ended %.1f BT after the partner start (< 32 jam bits)" % jam_end_bt
    gap_bt = (second.t0 - t_p1) / BT
    assert gap_bt >= 96, "retry %.1f BT after the end of the partner burst (< 96 BT)" % gap_bt
    assert not second.violations and second.frame[:-4] == body, second.violations
    ff.assert_status_clean(bench.m, [0])
    return ("2 link pulses received; collision: first chip burst %.1f BT (jam ends %.1f BT after the partner start), "
            "retry %.1f BT after the partner burst, valid %d octets + FCS; events %s" % (
                first_bt, jam_end_bt, gap_bt, len(second.frame) - 4, [hex(w) for w in events]))


# ----- model runs (the simulator's procedures) -----

def usb_enum_run():
    return drive_model(usb_enum_proc(model_bench))


def eth_rxtx_run():
    return drive_model(eth_rxtx_proc(model_bench))


@cocotb.test(skip=GL)
async def test_helpers_model_usb_enum(dut):
    """fw/usb_dev.s in the model with the helper model, against the per-cycle USB host model: enumeration with the
    checks of test_usb_dev.test_usb_dev_enumerate."""
    dut._log.info("model: " + drive_model(usb_enum_proc(model_bench)))


@cocotb.test(skip=GL)
async def test_helpers_model_usb_errors(dut):
    """fw/usb_dev.s in the model: damaged packets get no answer (checks of test_usb_dev.test_usb_dev_errors)."""
    dut._log.info("model: " + drive_model(usb_errors_proc(model_bench)))


@cocotb.test(skip=GL)
async def test_helpers_model_eth(dut):
    """fw/eth_10bt.s in the model with the helper model, against the per-cycle 10BASE-T partner: a 64-octet frame
    received and delivered, a 60-octet frame from the host sent (checks of test_eth_dev rx and tx)."""
    dut._log.info("model: " + drive_model(eth_rxtx_proc(model_bench)))


@cocotb.test(skip=GL)
async def test_helpers_model_eth_collision(dut):
    """fw/eth_10bt.s in the model: link pulses, then collision, jam, backoff and retry (checks of
    test_eth_dev.test_eth_dev_collision)."""
    dut._log.info("model: " + drive_model(eth_collision_proc(model_bench)))

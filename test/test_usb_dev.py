"""USB 2.0 low-speed device firmware (fw/usb_dev.s, data fw/usb_dev_data.py) in the chip, against the host
model of test/linemodel (written from USB 2.0 chapters 7 to 9, not from the RTL or the firmware).

The bus model joins the host model and the chip pins as a low-speed transceiver would: the chip drives VPO
(uio0), VMO (uio1) and OE# (uio2); it receives RCV (ui_in[0], the differential receiver: 1 for K, held in
SE0), VP (ui_in[1]) and VM (ui_in[2]). The bus level is the host level while the host drives, the device
level while OE# is low, else J (the device pull-up on D-). The device hears its own packets, as through a
real transceiver. The model has no transceiver or cable delay: a logic simulation at 60 MHz.

Every device packet is checked by the host model against the USB 2.0 limits (line coding, stuffing, CRC,
bit rate, jitter, EOP width, reply time 2 to 7.5 bit times); every check is in session.log. Each test fails
on any violation, on a collision on the bus, or on a wrong result.

Simulation-speed shortcuts (stated in each test): bus reset SE0 20 us instead of 10 ms (the helper detects a
reset after 19 x 16 cycles = 5.07 us), reset recovery 100 us instead of 10 ms, resume K 100 us instead of 20 ms.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Event, First, ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time

try:
    from cocotb.triggers import ValueChange
except ImportError:                      # older cocotb
    from cocotb.triggers import Edge as ValueChange

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "asm"))
sys.path.insert(0, os.path.join(ROOT, "fw"))
import host_spi as hs  # noqa: E402
import pe_asm  # noqa: E402
import usb_dev_data  # noqa: E402
from linemodel import usb_ch9 as C  # noqa: E402
from linemodel import usb_host as H  # noqa: E402
from linemodel import usb_ls as L  # noqa: E402
from linemodel.wave import Wave  # noqa: E402

FW = os.path.join(ROOT, "fw", "usb_dev.s")
CHIP_CLK_PS = 16666                       # 60 MHz
PIN_HDRV = 1 << 4
RESET_PS, RECOVERY_PS = 20e6, 100e6       # simulation speed, see the module text
LEVEL_PINS = {"J": (0, 1), "K": (1, 0), "SE0": (0, 0), "SE1": (1, 1)}   # (VP, VM)
EV_RESET, EV_SUSPEND, EV_RESUME, EV_CONFIG, EV_LED, EV_EP1, EV_ADDR = 1, 2, 3, 4, 5, 6, 7


def helper_pin(signal):
    """Pin configuration word: USB helper drive, signal 0 TXA (VPO), 1 TXB (VMO), 5 not OE."""
    return PIN_HDRV | (signal << 6)


def setup8(bm, req, wvalue, windex, wlength):
    return C.setup_packet(bm, req, wvalue, windex, wlength)


class Bus:
    """The low-speed bus between the host model and the transceiver pins of the chip."""

    def __init__(self, dut, host):
        self.dut, self.host = dut, host
        self.h = "IDLE"                   # host level (IDLE = host driver off)
        self.d = "IDLE"                   # device level (IDLE = OE# high)
        self.rcv = 0
        self.dev = []                     # device level changes (absolute ps, level)
        self.host_eops = []
        self.collisions = []
        self.changed = Event()
        self._apply()
        cocotb.start_soon(self._watch())

    def level(self):
        if self.h != "IDLE":
            return self.h
        return "J" if self.d == "IDLE" else self.d

    def _apply(self):
        if self.h != "IDLE" and self.d != "IDLE":
            self.collisions.append((get_sim_time("ps"), self.h, self.d))
        lv = self.level()
        vp, vm = LEVEL_PINS[lv]
        if lv in ("J", "K"):
            self.rcv = vp
        self.host.inputs = (self.host.inputs & ~0x7) | self.rcv | (vp << 1) | (vm << 2)
        self.host._drive()

    def drive(self, lvl):
        if lvl != self.h:
            if self.h == "SE0" and lvl in ("J", "IDLE"):
                self.host_eops.append(get_sim_time("ps"))   # host EOP end (SE0 -> J)
            self.h = lvl
            self._apply()

    def first_reply_marks(self, n=1):
        """(host EOP end, device SOP) in absolute ps for the first n device packets, for the waveform check."""
        out = []
        prev = "IDLE"
        for t, lv in self.dev:
            if lv == "K" and prev == "IDLE":
                eops = [e for e in self.host_eops if e < t]
                if eops:
                    out.append((eops[-1], t))
                if len(out) >= n:
                    break
            prev = lv
        return out

    def _dev_level(self):
        v, oe = self.dut.uio_out.value, self.dut.uio_oe.value
        if not (v.is_resolvable and oe.is_resolvable):
            return "IDLE"
        v, oe = v.to_unsigned(), oe.to_unsigned()
        if (oe & 0x7) != 0x7 or (v >> 2) & 1:
            return "IDLE"
        return {(1, 0): "K", (0, 1): "J", (0, 0): "SE0", (1, 1): "SE1"}[(v & 1, (v >> 1) & 1)]

    async def _watch(self):
        while True:
            await ValueChange(self.dut.uio_out)
            d = self._dev_level()
            if d != self.d:
                self.d = d
                self.dev.append((get_sim_time("ps"), d))
                self._apply()
                self.changed.set()


class Glue:
    """Runs the actions of a host-session generator on the bus (test/linemodel/usb_host.py)."""

    def __init__(self, bus):
        self.bus = bus
        self.t0 = get_sim_time("ps")

    def now(self):
        return get_sim_time("ps") - self.t0

    async def until(self, t):
        dt = t - self.now()
        if dt >= 1:
            await Timer(round(dt), unit="ps")

    async def run(self, gen):
        result = None
        while True:
            try:
                act = gen.send(result)
            except StopIteration as stop:
                return stop.value
            result = await self.act(act)

    async def act(self, a):
        if isinstance(a, H.Drive):
            start = self.now()
            self.bus.drive(a.wave.initial)
            for t, lvl in a.wave.edges:
                await self.until(start + t)
                self.bus.drive(lvl)
            await self.until(start + a.wave.end)
            return H.Result(t=start)
        if isinstance(a, H.Wait):
            start = self.now()
            self.bus.drive("IDLE")
            await self.until(start + a.duration_ps)
            return H.Result(t=start)
        if isinstance(a, H.Listen):
            return await self.listen(a)
        raise TypeError("unknown host action %r" % (a,))

    async def listen(self, a):
        self.bus.drive("IDLE")
        start = self.now()
        k0 = len(self.bus.dev)
        init = self.bus.d
        while True:
            ed = [(t - self.t0, lv) for t, lv in self.bus.dev[k0:]]
            sop = next((t for t, lv in ed if lv == "K"), None)
            if sop is None:
                limit = start + a.timeout_ps
            else:
                eop, prev = None, None
                for t, lv in ed:
                    if t > sop and prev == "SE0" and lv in ("J", "IDLE"):
                        eop = t
                        break
                    prev = lv
                limit = start + a.cap_ps
                if eop is not None:
                    limit = min(limit, eop + a.post_eop_ps)
            if self.now() >= limit:
                break
            self.bus.changed.clear()
            await First(self.bus.changed.wait(), Timer(max(1, round(limit - self.now())), unit="ps"))
        wave = Wave(init, [(t - start, lv) for t, lv in ed if t <= limit], end=limit - start)
        return H.Result(t=start, wave=wave)


async def usb_chip(dut, clk_ps=CHIP_CLK_PS):
    """Chip reset, firmware and data image through the host port, helper pins, START."""
    prog = pe_asm.assemble_file(FW)
    v_first = prog.symbols["V_BASE"][0]
    assert prog.size <= v_first, "firmware %d words overlaps the variables at %d" % (prog.size, v_first)
    mem, info = usb_dev_data.build(prog.symbols)
    cocotb.start_soon(Clock(dut.clk, clk_ps, unit="ps").start())
    dut.ena.value = 1
    dut.ui_in.value = 1 << hs.CS_N_BIT
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    host = hs.SpiTimed(dut)
    bus = Bus(dut, host)
    bus.prog = prog
    await host.write_mem(0, prog.image()[:prog.size])
    for addr, words in mem:
        await host.write_mem(addr, words)
    head, _ = hs.config_words((0, 0, 0, 0), (True, False, False, False), [None] * 15, [False] * 15)
    pins = [0] * 15
    pins[0], pins[1], pins[2] = helper_pin(0), helper_pin(1), helper_pin(5)
    await host.write_cfg(0, head)
    await host.write_cfg(16, pins)
    await host.start()
    await ClockCycles(dut.clk, 400)
    dut._log.info("fw/usb_dev.s: %d program words (variables from %d), data image %d words" %
                  (prog.size, v_first, sum(len(w) for _, w in mem)))
    return host, bus, prog, info


async def events(host):
    """All words in the thread-to-host FIFO of thread 0."""
    out = []
    while True:
        words, n = await host.pull(0)
        out += words
        if n <= len(words):
            return out


def summary(s):
    """Measured values of the device packets in a session log."""
    def col(k):
        return [e.measures[k] for e in s.log if k in e.measures]
    ipd, eop, rate = col("ipd_bits"), col("eop_se0_ps"), col("bit_rate_bps")
    nxt, tld = col("next_src_ps"), col("tldeop_ps")
    if not ipd:
        return "no device packets"
    return ("%d device packets: reply %.3f to %.3f bit times after the host EOP; EOP SE0 %.0f to %.0f ns; "
            "rate %.0f to %.0f bit/s; consecutive jitter max %.0f ps; diff-to-SE0 %.0f to %.0f ns" %
            (len(ipd), min(ipd), max(ipd), min(eop) / 1e3, max(eop) / 1e3, min(rate), max(rate),
             max(nxt), min(tld) / 1e3, max(tld) / 1e3))


def check_clean(s, bus):
    assert not s.violations, "USB 2.0 violations: %s" % s.violations[:6]
    assert not bus.collisions, "host and device drove the bus at the same time: %s" % bus.collisions[:4]
    for eop, sop in bus.first_reply_marks(2):
        cocotb.log.info("waveform marks: host EOP end %d ps, device SOP %d ps (reply %.3f bit times)" % (
            eop, sop, (sop - eop) / L.BIT_PS))


def ev(code, value=0):
    return code | (value << 8)


async def dump(dut, s, bus, glue, prog):
    """Diagnosis after a failure: the last host checks, the last device edges, firmware variables."""
    for e in s.log[-6:]:
        dut._log.info("host log: %s %s %s" % (e.what, e.violations, {k: round(v, 3) if isinstance(v, float)
                                                                       else v for k, v in e.measures.items()}))
    t_end = glue.now()
    dut._log.info("device edges (us before now): %s" % ", ".join(
        "%.2f %s" % ((t_end - (t - glue.t0)) / 1e6, lv) for t, lv in bus.dev[-24:]))
    if prog is None:
        return
    core, usb = dut.user_project.u_proc.u_core, dut.user_project.u_proc.u_usb
    pcs = []
    for _ in range(64):                       # thread 0 state is st0 in the cycles with now mod 4 = 0
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(core.now.value) & 3 == 0:
            st = core.st0.value.to_unsigned()
            pcs.append((st >> 80) & 0x1FF)
    regs = [(st >> (16 * i)) & 0xFFFF for i in range(4)]
    line = prog.source.get(pcs[-1], (0, ""))
    dut._log.info("thread 0: PCs %s; last PC %d (line %d: %s); r0-r3 %s" % (
        sorted(set(pcs)), pcs[-1], line[0], line[1].strip(), ["%04x" % r for r in regs]))
    dut._log.info("USB helper: tx_st %d rx_st %d sticky %04x tx_cnt %d rx_cnt %d tx_time %d now %d" % (
        int(usb.tx_st.value), int(usb.rx_st.value), int(usb.sticky.value), int(usb.tx_cnt.value),
        int(usb.rx_cnt.value), int(usb.tx_time.value), int(core.now.value)))


async def enumerate_once(dut, host, bus, glue, ppm, seed, label):
    s = H.UsbHostSession(ppm=ppm, stress_seed=seed)
    try:
        rep = await glue.run(s.enumerate(addr=5, reset_ps=RESET_PS, recovery_ps=RECOVERY_PS))
    except Exception:
        await dump(dut, s, bus, glue, bus.prog)
        raise
    check_clean(s, bus)
    assert rep["strings"] == {1: usb_dev_data.MANUFACTURER, 2: usb_dev_data.PRODUCT}, rep["strings"]
    assert rep["device"]["idVendor"] == usb_dev_data.VID and rep["config"]["endpoints"] == [(0x81, 3, 8, 10)]
    got = await events(host)
    assert got == [ev(EV_RESET), ev(EV_ADDR, 5), ev(EV_CONFIG, 1)], "events %s" % [hex(w) for w in got]
    dut._log.info("%s: enumerated (address 5, configuration 1, strings %s); %d checks, no violation; %s" %
                  (label, rep["strings"], len(s.log), summary(s)))


@cocotb.test()
async def test_usb_dev_enumerate(dut):
    """Host enumeration (USB 2.0 9.1.2): bus reset, GET_DESCRIPTOR device at address 0, SET_ADDRESS 5, the
    old address must stay silent after the 2 ms recovery, device, configuration (9 bytes, then all), string 0,
    1, 2, device_qualifier (must STALL: 9.6.2), SET_CONFIGURATION 1. Host at the nominal rate. Bus reset 20 us
    and recovery 100 us (simulation speed)."""
    host, bus, _, _ = await usb_chip(dut)
    glue = Glue(bus)
    await enumerate_once(dut, host, bus, glue, 0.0, None, "nominal host")
    dut._log.info("simulated %.3f ms" % (glue.now() / 1e9))


@cocotb.test()
async def test_usb_dev_drift(dut):
    """Enumeration with the host at +500 ppm and then at -500 ppm (TLDRATHS, USB 2.0 Table 7-10), each with
    host_rx_stress jitter at the device receiver limits (TDJR1 75 ns, TDJR2 45 ns). The second run starts with
    a bus reset of the same chip, so it also checks that a reset returns the device to the default state."""
    host, bus, _, _ = await usb_chip(dut)
    glue = Glue(bus)
    await enumerate_once(dut, host, bus, glue, 500.0, 11, "host +500 ppm, receiver jitter limits")
    await enumerate_once(dut, host, bus, glue, -500.0, 12, "host -500 ppm, receiver jitter limits")


@cocotb.test()
async def test_usb_dev_errors(dut):
    """Damaged packets get no answer (USB 2.0 8.3.1, 8.4.6.4, 8.7.3): SETUP token with a bad CRC5, with a bad
    PID check field, to another address; setup data with a bad CRC16, with a stuffing error (seven ones);
    IN token with a bad CRC5. Then a correct control read must still work."""
    host, bus, _, _ = await usb_chip(dut)
    glue = Glue(bus)
    s = H.UsbHostSession()
    gd = C.get_descriptor(C.DT_DEVICE, 0, 18)
    out = {}

    def seq():
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
    await glue.run(seq())
    check_clean(s, bus)
    silent = [e.what for e in s.log if e.what.endswith("no reply")]
    assert len(silent) == 6, "silent checks: %s" % silent
    dut._log.info("waveform marks: host EOP ends of the bus reset and the first damaged packets %s ps; first "
                  "device packet start %s ps" % (bus.host_eops[:5],
                                                 next((t for t, lv in bus.dev if lv == "K"), None)))
    assert out["dev"] == usb_dev_data.DEVICE and not out["stall"]
    dut._log.info("errors: 6 damaged or foreign packets, no answer to any; then GET_DESCRIPTOR device ok; %s"
                  % summary(s))


@cocotb.test()
async def test_usb_dev_hid(dut):
    """HID boot keyboard functions after SET_ADDRESS 3 and SET_CONFIGURATION 1:
    endpoint 1 IN: NAK with no report; a report from the host port (4 words) is sent as DATA0, the next as
    DATA1 (data toggle, 8.6), then NAK; SET_REPORT output (LEDs) as a control write with a data stage;
    SET_IDLE / GET_IDLE, SET_PROTOCOL / GET_PROTOCOL (HID 7.2); SET_FEATURE(ENDPOINT_HALT) gives STALL on
    endpoint 1 and GET_STATUS(endpoint) = 1, CLEAR_FEATURE gives NAK again (9.4.1, 9.4.5, 9.4.9);
    an unknown request STALLs (9.2.7); the HID report descriptor (63 bytes, 8 packets) and string 2 (32 bytes:
    a zero-length packet ends the data stage, 5.5.3) are read in full."""
    host, bus, _, _ = await usb_chip(dut)
    glue = Glue(bus)
    s = H.UsbHostSession()
    r = {}

    def part1():
        yield from s.reset(RESET_PS, RECOVERY_PS)
        r["addr"] = yield from s.control_write_nodata(0, C.set_address(3))
        yield from s.wait(20e6)
        r["conf"] = yield from s.control_write_nodata(3, C.set_configuration(1))
        yield from s.gap()
        r["ep1_empty"] = yield from s.in_(3, 1)
    await glue.run(part1())
    assert r["addr"] == "ACK" and r["conf"] == "ACK" and r["ep1_empty"][0] == "NAK", r
    e1 = await events(host)
    assert e1 == [ev(EV_RESET), ev(EV_ADDR, 3), ev(EV_CONFIG, 1)], [hex(w) for w in e1]

    rep1 = bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0])          # left shift + 'a'
    rep2 = bytes([0, 0, 0x05, 0x06, 0, 0, 0, 0])          # 'b' 'c'
    await host.push(0, usb_dev_data.words_of(rep1))

    def part2():
        yield from s.gap()
        r["ep1_a"] = yield from s.in_(3, 1)
        yield from s.gap()
        r["ep1_a_nak"] = yield from s.in_(3, 1)
    await glue.run(part2())
    await host.push(0, usb_dev_data.words_of(rep2))

    def part3():
        yield from s.gap()
        r["ep1_b"] = yield from s.in_(3, 1)
        yield from s.gap()
        r["led_setup"] = yield from s.setup(3, 0, setup8(0x21, 9, 0x0200, 0, 1))
        yield from s.gap()
        r["led_out"] = yield from s.out(3, 0, "DATA1", bytes([0x05]))
        yield from s.gap()
        r["led_status"] = yield from s.in_(3, 0)
        yield from s.gap()
        r["set_idle"] = yield from s.control_write_nodata(3, setup8(0x21, 10, 0x7D00, 0, 0))
        r["get_idle"] = yield from s.control_read(3, setup8(0xA1, 2, 0, 0, 1))
        r["proto0"] = yield from s.control_read(3, setup8(0xA1, 3, 0, 0, 1))
        r["set_proto"] = yield from s.control_write_nodata(3, setup8(0x21, 11, 0, 0, 0))
        r["proto1"] = yield from s.control_read(3, setup8(0xA1, 3, 0, 0, 1))
        r["halt"] = yield from s.control_write_nodata(3, setup8(0x02, 3, 0, 0x81, 0))
        yield from s.gap()
        r["ep1_halted"] = yield from s.in_(3, 1)
        yield from s.gap()
        r["status_ep"] = yield from s.control_read(3, setup8(0x82, 0, 0, 0x81, 2))
        r["clear"] = yield from s.control_write_nodata(3, setup8(0x02, 1, 0, 0x81, 0))
        yield from s.gap()
        r["ep1_cleared"] = yield from s.in_(3, 1)
        yield from s.gap()
        r["unknown"] = yield from s.control_read(3, setup8(0xC0, 0x33, 0, 0, 4))
        r["report_desc"] = yield from s.control_read(3, setup8(0x81, 6, 0x2200, 0, 0x7F))
        r["string2"] = yield from s.control_read(3, C.get_descriptor(C.DT_STRING, 2, 255, 0x0409))
        r["config9"] = yield from s.control_read(3, C.get_descriptor(C.DT_CONFIG, 0, 9))
    await glue.run(part3())
    e2 = await events(host)
    check_clean(s, bus)
    assert r["ep1_a"] == ("DATA0", rep1) and r["ep1_a_nak"][0] == "NAK", r
    assert r["ep1_b"] == ("DATA1", rep2), r["ep1_b"]
    assert (r["led_setup"], r["led_out"], r["led_status"]) == ("ACK", "ACK", ("DATA1", b"")), r
    assert r["set_idle"] == "ACK" and r["get_idle"] == (bytes([0x7D]), False), r
    assert r["proto0"] == (bytes([1]), False) and r["set_proto"] == "ACK" and r["proto1"] == (bytes([0]), False)
    assert r["halt"] == "ACK" and r["ep1_halted"][0] == "STALL" and r["status_ep"] == (bytes([1, 0]), False)
    assert r["clear"] == "ACK" and r["ep1_cleared"][0] == "NAK"
    assert r["unknown"] == (None, True), r["unknown"]
    assert r["report_desc"] == (usb_dev_data.REPORT, False)
    assert r["string2"] == (usb_dev_data.string_desc(usb_dev_data.PRODUCT), False)
    assert r["config9"] == (usb_dev_data.CONFIG[:9], False)
    assert e2 == [ev(EV_EP1), ev(EV_EP1), ev(EV_LED, 5)], [hex(w) for w in e2]
    dut._log.info("HID: reports DATA0/DATA1 then NAK; LED report 0x05 -> event %04x; idle, protocol, halt, "
                  "stall, 63-byte report descriptor and 32-byte string with ZLP ok; %s" % (e2[-1], summary(s)))


@cocotb.test()
async def test_usb_dev_suspend(dut):
    """Suspend and resume (USB 2.0 7.1.7.6, 7.1.7.7): after SET_ADDRESS 2 the host stops the keep-alive; the
    device must report suspend after more than 3 ms of idle bus (the helper: J for 15,000 x 16 cycles = 4.0 ms)
    and drive nothing; the host resumes with K (100 us here instead of 20 ms, simulation speed) and a
    low-speed EOP; the device reports resume and answers GET_DESCRIPTOR. Keep-alive EOPs every 1 ms before
    the idle period must not suspend it."""
    host, bus, _, _ = await usb_chip(dut)
    glue = Glue(bus)
    s = H.UsbHostSession()

    def part1():
        yield from s.reset(RESET_PS, RECOVERY_PS)
        st = yield from s.control_write_nodata(0, C.set_address(2))
        assert st == "ACK"
        yield from s.wait(5.2e9)                 # 5.2 ms with keep-alive: no suspend
    await glue.run(part1())
    e1 = await events(host)
    assert e1 == [ev(EV_RESET), ev(EV_ADDR, 2)], [hex(w) for w in e1]
    n_dev = len(bus.dev)
    s.keepalive = False
    t_idle0 = get_sim_time("ps")
    await glue.run(s.wait(4.3e9))                 # 4.3 ms idle, no keep-alive
    t_idle1 = get_sim_time("ps")
    dut._log.info("waveform marks: idle bus from %d ps to %d ps (resume K starts at the end)" % (t_idle0, t_idle1))
    e2 = await events(host)
    assert e2 == [ev(EV_SUSPEND)], [hex(w) for w in e2]
    assert len(bus.dev) == n_dev, "device drove the bus while idle or suspended"
    bit = L.BIT_PS
    resume = Wave.from_segments([("K", 100e6), ("SE0", 2 * bit), ("J", bit), ("IDLE", 0.0)], initial="IDLE")
    s.keepalive = True

    def part2():
        yield from s._act(H.Drive(resume))
        yield from s.wait(50e6)
        d, st = yield from s.control_read(2, C.get_descriptor(C.DT_DEVICE, 0, 18))
        return d, st
    d, st = await glue.run(part2())
    e3 = await events(host)
    check_clean(s, bus)
    assert e3 == [ev(EV_RESUME)], [hex(w) for w in e3]
    assert d == usb_dev_data.DEVICE and not st
    dut._log.info("suspend: no suspend with keep-alive for 5.2 ms; suspend event after 4.3 ms idle, no device "
                  "drive; resume event after K; GET_DESCRIPTOR ok; %s" % summary(s))

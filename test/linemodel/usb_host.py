"""USB LS host model: transactions, control transfers and enumeration.

The host is a Python generator that yields line actions and receives the
results. It does not depend on cocotb. A test bench runs it like this:

    gen = session.enumerate(addr=5)
    result = None
    while True:
        try:
            act = gen.send(result)
        except StopIteration as stop:
            report = stop.value
            break
        result = await glue(act)      # the test bench drives and captures the pins

Actions (times in ps, relative to the moment the action starts):
  Drive(wave)          drive the wave (levels J/K/SE0/IDLE); return when
                       wave.end is reached. Result.t = absolute start time.
  Wait(duration_ps)    keep the host driver off (bus idle).
  Listen(timeout_ps, post_eop_ps)
                       driver off; capture the device. If no SOP (J->K)
                       arrives before timeout_ps, return at timeout_ps.
                       If a packet starts, return post_eop_ps after its
                       SE0->J transition (or at cap_ps, a hard limit).
                       Result.wave = captured levels, times relative to the
                       start of the Listen.
The session checks every device packet against the limits in usb_ls.py and
records each check in session.log.

References: USB 2.0, section and printed page.
"""
from dataclasses import dataclass, field

from . import usb_ls as L
from . import usb_ch9 as C
from .wave import Wave


@dataclass
class Drive:
    wave: Wave


@dataclass
class Wait:
    duration_ps: float


@dataclass
class Listen:
    timeout_ps: float
    post_eop_ps: float
    cap_ps: float


@dataclass
class Result:
    t: float
    wave: Wave = None


@dataclass
class LogEntry:
    what: str
    violations: list
    measures: dict = field(default_factory=dict)


class EnumerationFailed(Exception):
    pass


class UsbHostSession:
    """ppm: host rate offset (TLDRATHS allows +/-500 ppm, Table 7-10 p.182).
    stress_seed: if set, host packets get host_rx_stress() jitter
    (TDJR1/TDJR2 limits, Table 7-10 p.182).
    host_reply_bits: host handshake delay after a device EOP (2..7.5 bit
    times, 7.1.18.1 p.168).
    ipg_bits: gap between two host packets (>= 2 bit times, TIPD).
    """

    def __init__(self, ppm=0.0, stress_seed=None, host_reply_bits=3.0,
                 ipg_bits=4.0, retry_gap_bits=20.0, keepalive=True):
        assert L.LIM["TIPD_BITS"] <= host_reply_bits <= L.LIM["HOST_RSP_MAX_BITS"]
        assert ipg_bits >= L.LIM["TIPD_BITS"]
        self.ppm = ppm
        self.bit = L.BIT_PS / (1 + ppm * 1e-6)
        self.stress_seed = stress_seed
        self._stress_n = 0
        self.host_reply_bits = host_reply_bits
        self.ipg_bits = ipg_bits
        self.retry_gap_bits = retry_gap_bits
        self.keepalive = keepalive
        self.t = 0.0
        self.t_frame0 = None
        self.log = []

    # ---------------- bookkeeping -------------------------------------
    def record(self, what, violations, measures=None):
        self.log.append(LogEntry(what, list(violations), dict(measures or {})))

    @property
    def violations(self):
        return [(e.what,) + v for e in self.log for v in e.violations]

    def _act(self, act):
        r = yield act
        self.t = r.t
        if isinstance(act, Drive):
            self.t += act.wave.end
        elif isinstance(act, Wait):
            self.t += act.duration_ps
        elif isinstance(act, Listen):
            self.t += r.wave.end
        return r

    # ---------------- line primitives ---------------------------------
    def _wave(self, bits, inj=None):
        w = L.packet_wave(bits, inj, ppm=self.ppm)
        if self.stress_seed is not None:
            self._stress_n += 1
            w = L.host_rx_stress(w, seed=self.stress_seed * 100003 + self._stress_n)
        return w

    def send(self, bits, inj=None):
        """Drive one packet. Returns the EOP end relative to the wave end
        (negative), the reference for the device reply delay."""
        w = self._wave(bits, inj)
        yield from self._act(Drive(w))
        ref = w.marks.get("eop_end")
        return None if ref is None else ref - w.end

    def gap(self, bits=None):
        """Idle between two host packets: TIPD >= 2 bit times from SE0->J,
        7.1.18.1 p.168. The packet wave already holds 1 bit of J."""
        b = self.ipg_bits if bits is None else bits
        yield from self._act(Wait(max(0.0, (b - 1.0) * self.bit)))

    def listen(self):
        """Listen window: host waits at least 18 bit times for a response,
        7.1.19.1 p.168. Cap = window + longest LS packet (8 SYNC + 8 PID +
        64 data + 16 CRC + 16 stuffed bits + 3 EOP < 120 bits)."""
        to = L.LIM["TIMEOUT_MAX_BITS"] * L.BIT_PS
        r = yield from self._act(Listen(timeout_ps=to,
                                        post_eop_ps=self.host_reply_bits * self.bit,
                                        cap_ps=to + 140 * L.BIT_PS))
        return r.wave

    def wait(self, duration_ps):
        """Idle with a keep-alive at the start of each 1 ms frame (a LS
        device suspends after 3 ms idle, 7.1.7.6 p.154; keep-alive 11.8.4.1
        p.332)."""
        if not self.keepalive:
            yield from self._act(Wait(duration_ps))
            return
        if self.t_frame0 is None:
            self.t_frame0 = self.t
        end = self.t + duration_ps
        F = L.LIM["FRAME_PS"]
        while self.t < end:
            k = int((self.t - self.t_frame0) // F) + 1
            nxt = self.t_frame0 + k * F
            if nxt >= end:
                yield from self._act(Wait(end - self.t))
                break
            yield from self._act(Wait(nxt - self.t))
            ka = L.keepalive_wave(self.ppm)
            yield from self._act(Drive(ka))

    def reset(self, reset_ps=None, recovery_ps=None):
        """Bus reset: SE0 >= 10 ms (TDRST), then 10 ms recovery (TRSTRCY),
        7.1.7.5 p.153, Table 7-13 p.186, Table 7-14 p.188. Shorter values
        are allowed for simulation speed only if the DUT's reset detection
        time is known to be shorter (TDETRST may be up to 10 ms)."""
        reset_ps = L.LIM["TDRST_PS"] if reset_ps is None else reset_ps
        recovery_ps = L.LIM["TRSTRCY_PS"] if recovery_ps is None else recovery_ps
        if reset_ps < L.LIM["TDRST_PS"]:
            self.record("reset", [], {"note": "reset shorter than TDRST 10 ms (simulation speed)"})
        yield from self._act(Drive(L.se0_wave(reset_ps)))
        self.t_frame0 = self.t
        yield from self.wait(recovery_ps)

    # ---------------- device reply checks -----------------------------
    def _check_reply(self, what, captured, ref, expect):
        rc = L.check_device_packet(captured, host_eop_end_ps=ref, expect_pid=expect)
        self.record(what, rc.violations, rc.measures)
        return rc

    def _check_silent(self, what, captured):
        v = L.check_no_response(captured, 0, captured.end + 1)
        self.record(what, v)
        return not v

    # ---------------- transactions (8.5, 8.4.6) -----------------------
    def setup(self, addr, ep, data8, tok_flags=None, tok_inj=None,
              data_flags=None, data_inj=None, expect_ack=True):
        """SETUP transaction: SETUP token, DATA0 (8 bytes), device ACK.
        8.5.3 p.225-226; 8.4.6.4 p.209. With an injected error the device
        must not answer (8.3.1 p.196; 8.4.6.4 p.209)."""
        tf, df = tok_flags or {}, data_flags or {}
        yield from self.send(L.token_bits("SETUP", addr, ep, **tf), tok_inj)
        yield from self.gap()
        ref = yield from self.send(L.data_bits("DATA0", data8, **df), data_inj)
        cap = yield from self.listen()
        if expect_ack:
            rc = self._check_reply("SETUP ack", cap, ref, "ACK")
            return rc.decoded.pid_name if rc.decoded.found else None
        self._check_silent("SETUP no reply", cap)
        return None

    def in_(self, addr, ep, tok_flags=None, tok_inj=None, drop_ack=False,
            expect="any"):
        """IN transaction: IN token; device sends DATAx, NAK or STALL
        (Table 8-4 p.208); host ACKs valid data (Table 8-5 p.208).
        Returns (pid_name or None, payload)."""
        ref = yield from self.send(L.token_bits("IN", addr, ep, **(tok_flags or {})), tok_inj)
        cap = yield from self.listen()
        if expect is None:
            self._check_silent("IN no reply", cap)
            return None, b""
        exp = (L.DATAS - {"DATA2", "MDATA"}) | {"NAK", "STALL"}
        rc = self._check_reply("IN reply", cap, ref, exp)
        d = rc.decoded
        if d.found and d.pid_name in ("DATA0", "DATA1") and not d.errors:
            if not drop_ack:
                yield from self.send(L.handshake_bits("ACK"))
            return d.pid_name, d.payload
        return (d.pid_name if d.found else None), b""

    def out(self, addr, ep, pid, payload, tok_flags=None, data_flags=None,
            data_inj=None, expect="any"):
        """OUT transaction: OUT token, DATAx, device handshake
        (Table 8-6 p.209)."""
        yield from self.send(L.token_bits("OUT", addr, ep, **(tok_flags or {})))
        yield from self.gap()
        ref = yield from self.send(L.data_bits(pid, payload, **(data_flags or {})), data_inj)
        cap = yield from self.listen()
        if expect is None:
            self._check_silent("OUT no reply", cap)
            return None
        rc = self._check_reply("OUT handshake", cap, ref, {"ACK", "NAK", "STALL"})
        return rc.decoded.pid_name if rc.decoded.found else None

    # ---------------- control transfers (8.5.3 p.225-227) -------------
    def control_read(self, addr, setup8, mps=8, data_deadline_ps=None,
                     status_deadline_ps=None, max_retries=2000):
        """Setup stage, IN data stage starting with DATA1 and toggling,
        then OUT DATA1 zero-length status (Figure 8-37 p.226).
        Ends on a short packet or when wLength bytes arrived (9.4.3 p.253).
        Deadlines: first and later data packets 500 ms, status 50 ms
        (9.2.6.4 p.246). Returns (data or None if STALLed, stalled flag)."""
        wlen = int.from_bytes(setup8[6:8], "little")
        data_deadline_ps = L.LIM["TDRETDATA1_PS"] if data_deadline_ps is None else data_deadline_ps
        status_deadline_ps = L.LIM["TDRQCMPLTND_PS"] if status_deadline_ps is None else status_deadline_ps
        ack = yield from self.setup(addr, 0, setup8)
        if ack != "ACK":
            raise EnumerationFailed("no ACK to SETUP %s" % setup8.hex())
        toggle, data, t_mark = 1, b"", self.t
        stalled = False
        while True:
            yield from self.gap()
            pid, payload = yield from self.in_(addr, 0)
            if pid == "NAK":
                if self.t - t_mark > data_deadline_ps:
                    self.record("control read", [("DEADLINE", "no data within 500 ms", "9.2.6.4 p.246")])
                    raise EnumerationFailed("data deadline")
                yield from self.wait(self.retry_gap_bits * self.bit)
                continue
            if pid == "STALL":
                stalled = True
                break
            if pid not in ("DATA0", "DATA1"):
                raise EnumerationFailed("no valid data packet (%s)" % pid)
            want = "DATA1" if toggle else "DATA0"
            if pid != want:
                self.record("control read", [("TOGGLE", "got %s, expected %s" % (pid, want),
                                              "8.6 p.232; Figure 8-37 p.226")])
            if len(payload) > mps:
                self.record("control read", [("MPS", "%d bytes > wMaxPacketSize %d" % (len(payload), mps),
                                              "5.5.3 p.39")])
            data += payload
            toggle ^= 1
            t_mark = self.t
            if len(payload) < mps or len(data) >= wlen:
                break
        if len(data) > wlen:
            self.record("control read", [("TOO_LONG", "%d bytes > wLength %d" % (len(data), wlen),
                                          "9.4.3 p.253")])
        if stalled:
            return None, True
        # status stage: OUT DATA1 zero length, device ACK
        t_mark = self.t
        while True:
            yield from self.gap()
            hs = yield from self.out(addr, 0, "DATA1", b"")
            if hs == "ACK":
                break
            if hs == "STALL":
                return data, True
            if self.t - t_mark > status_deadline_ps:
                self.record("control read status", [("DEADLINE", "status not done in 50 ms", "9.2.6.4 p.246")])
                raise EnumerationFailed("status deadline")
            yield from self.wait(self.retry_gap_bits * self.bit)
        return data, False

    def control_write_nodata(self, addr, setup8, deadline_ps=None):
        """Setup stage, then status stage IN that must return DATA1 with
        zero length (Figure 8-37 p.226; Table 8-7 p.227). Deadline 50 ms
        (9.2.6.3 p.246, 9.2.6.4 p.246). Returns 'ACK' (done) or 'STALL'."""
        deadline_ps = L.LIM["TDRQCMPLTND_PS"] if deadline_ps is None else deadline_ps
        ack = yield from self.setup(addr, 0, setup8)
        if ack != "ACK":
            raise EnumerationFailed("no ACK to SETUP %s" % setup8.hex())
        t_mark = self.t
        while True:
            yield from self.gap()
            pid, payload = yield from self.in_(addr, 0)
            if pid == "STALL":
                return "STALL"
            if pid in ("DATA0", "DATA1"):
                v = []
                if pid != "DATA1":
                    v.append(("TOGGLE", "status stage used %s" % pid, "8.5.3 p.226"))
                if payload:
                    v.append(("STATUS_LEN", "status data not zero length", "Table 8-7 p.227"))
                self.record("control write status", v)
                return "ACK"
            if self.t - t_mark > deadline_ps:
                self.record("control write status", [("DEADLINE", "status not done in 50 ms", "9.2.6.4 p.246")])
                raise EnumerationFailed("status deadline")
            yield from self.wait(self.retry_gap_bits * self.bit)

    # ---------------- enumeration (9.1.2, 9.4) ------------------------
    def enumerate(self, addr=5, reset_ps=None, recovery_ps=None,
                  first_len=64, check_old_address=True, check_qualifier=True):
        """Host enumeration sequence:
        reset; GET_DESCRIPTOR(device) at address 0; SET_ADDRESS; 2 ms
        SetAddress recovery (9.2.6.3 p.246); GET_DESCRIPTOR(device);
        GET_DESCRIPTOR(configuration, 9 bytes, then wTotalLength);
        GET_DESCRIPTOR(string 0 and each string index used);
        GET_DESCRIPTOR(device_qualifier) must STALL if bcdUSB >= 2.00
        (9.6.1 p.261, 9.6.2 p.264, 9.2.7 p.247); SET_CONFIGURATION.
        Returns a report dict; checks are in self.log."""
        rep = {}
        yield from self.reset(reset_ps, recovery_ps)
        d0, st = yield from self.control_read(0, C.get_descriptor(C.DT_DEVICE, 0, first_len))
        if st or not d0 or len(d0) < 8:
            raise EnumerationFailed("first device descriptor read failed")
        mps0 = d0[7]
        self.record("first device descriptor", [] if mps0 == 8 else
                    [("DEV_MPS0_LS", "LS bMaxPacketSize0 %d != 8" % mps0, "5.5.3 p.39")])
        st = yield from self.control_write_nodata(0, C.set_address(addr))
        if st != "ACK":
            raise EnumerationFailed("SET_ADDRESS stalled")
        yield from self.wait(L.LIM["TRSETADDR_RECOVERY_PS"])
        if check_old_address:
            # after recovery the device must not answer the old address
            # (9.2.6.3 p.246)
            yield from self.gap()
            yield from self.setup(0, 0, C.get_descriptor(C.DT_DEVICE, 0, 18), expect_ack=False)
        yield from self.gap()
        dd, st = yield from self.control_read(addr, C.get_descriptor(C.DT_DEVICE, 0, 18))
        if st or dd is None:
            raise EnumerationFailed("device descriptor stalled")
        self.record("device descriptor", C.check_device_descriptor(dd))
        dev = C.parse_device(dd) if len(dd) >= 18 else {}
        rep["device"] = dev
        c9, st = yield from self.control_read(addr, C.get_descriptor(C.DT_CONFIG, 0, 9))
        if st or c9 is None or len(c9) < 9:
            raise EnumerationFailed("configuration descriptor (9) failed")
        total = int.from_bytes(c9[2:4], "little")
        cfg, st = yield from self.control_read(addr, C.get_descriptor(C.DT_CONFIG, 0, total))
        if st or cfg is None:
            raise EnumerationFailed("configuration descriptor failed")
        v, info = C.check_config_bundle(cfg)
        self.record("configuration descriptor", v)
        rep["config"] = info
        idx = [dev.get(k, 0) for k in ("iManufacturer", "iProduct", "iSerialNumber")] + info["strings"]
        idx = sorted(set(i for i in idx if i))
        rep["strings"] = {}
        if idx:
            s0, st = yield from self.control_read(addr, C.get_descriptor(C.DT_STRING, 0, 255))
            if st or s0 is None:
                self.record("string 0", [("STR0_MISSING", "string indexes used but string 0 failed",
                                          "9.6.7 p.273")])
            else:
                self.record("string 0", C.check_string0(s0))
                langs = C.langids(s0)
                lang = langs[0] if langs else 0
                for i in idx:
                    s, st = yield from self.control_read(addr, C.get_descriptor(C.DT_STRING, i, 255, lang))
                    if st or s is None:
                        self.record("string %d" % i, [("STR_MISSING", "string %d not returned" % i,
                                                       "9.6.7 p.273; 9.4.3 p.254")])
                        continue
                    self.record("string %d" % i, C.check_string(s))
                    if len(s) % 2 == 0:
                        rep["strings"][i] = s[2:].decode("utf-16-le", "replace")
        if check_qualifier and dev.get("bcdUSB", 0) >= 0x0200:
            q, st = yield from self.control_read(addr, C.get_descriptor(C.DT_DEVICE_QUALIFIER, 0, 10))
            self.record("device_qualifier", [] if st else
                        [("QUALIFIER", "LS device returned a device_qualifier, Request Error (STALL) required",
                          "9.6.1 p.261; 9.6.2 p.264; 9.2.7 p.247")])
        st = yield from self.control_write_nodata(addr, C.set_configuration(info["bConfigurationValue"]))
        self.record("SET_CONFIGURATION", [] if st == "ACK" else
                    [("SETCONFIG", "SET_CONFIGURATION stalled", "9.4.7 p.257")])
        rep["violations"] = self.violations
        return rep

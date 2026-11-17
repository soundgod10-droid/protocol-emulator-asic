"""Reference low-speed USB device and a pure-Python line harness.

Used only by the model self-tests: it plays the DUT so that the host model,
the checker and the enumeration can be tested without a simulator. It can
inject device faults so that the self-tests prove the checker sees them.
It is NOT a model of the chip.
"""
import struct

from . import usb_ls as L
from . import usb_ch9 as C
from .usb_host import Drive, Wait, Listen, Result
from .wave import Wave


def default_descriptors():
    dev = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0, 0, 0, 8,
                      0x1209, 0x0001, 0x0100, 1, 2, 0, 1)
    cfg = struct.pack("<BBHBBBBB", 9, 2, 9 + 9 + 7, 1, 1, 0, 0x80, 50)
    itf = bytes([9, 4, 0, 0, 1, 0x03, 0x00, 0x00, 0])
    ep = struct.pack("<BBBBHB", 7, 5, 0x81, 0x03, 8, 10)
    s0 = bytes([4, 3, 0x09, 0x04])
    s1 = "Model test".encode("utf-16-le")
    s2 = "LS reference device".encode("utf-16-le")
    return {
        (1, 0): dev,
        (2, 0): cfg + itf + ep,
        (3, 0): s0,
        (3, 1): bytes([2 + len(s1), 3]) + s1,
        (3, 2): bytes([2 + len(s2), 3]) + s2,
    }


class RefDevice:
    """faults: set of names
      bad_crc16     flip a CRC16 bit in every data packet sent
      no_toggle     always send DATA1 in the data stage
      answer_old    keep answering address 0 after SET_ADDRESS
      bad_eop       SE0 of EOP 1.0 us
      ignore_setup_crc  ACK a SETUP even when DATA0 has a bad CRC
    reply_bits: device reply delay after the host EOP (bit times of the
      device). ppm: device rate offset. jitter_ps: per-edge jitter on the
      device output. nak_first: NAK this many INs before answering.
    """

    def __init__(self, descriptors=None, faults=(), reply_bits=3.0, ppm=0.0,
                 jitter_ps=0.0, seed=1, nak_first=0):
        self.desc = dict(descriptors or default_descriptors())
        self.faults = set(faults)
        self.reply_bits = reply_bits
        self.ppm = ppm
        self.jitter_ps = jitter_ps
        self.seed = seed
        self.nak_first = nak_first
        self.reset()
        self.decoded_log = []

    def reset(self):
        self.addr = 0
        self.pending_addr = None
        self.config = 0
        self.token = None
        self.buf = b""
        self.ptr = 0
        self.toggle = 1
        self.stage = None
        self.stall = False
        self.reply = None
        self.naks_left = self.nak_first
        self.last_sent = None

    # host side -------------------------------------------------------
    def on_host_wave(self, w):
        """Return the device reply bits (or None) and the time reference."""
        self.reply = None
        for a, b, lvl in w.runs():
            if lvl == "SE0" and b - a > L.LIM["TDETRST_MIN_PS"]:
                self.reset()
                return
        d = L.decode_packet(w)        # receiver view: TLST glitch filter
        if not d.found:
            return
        self.decoded_log.append(d)
        if d.errors:
            if not ("ignore_setup_crc" in self.faults and d.codes() == {"CRC16"}
                    and self.token and self.token[0] == "SETUP"):
                return                  # corrupted packet: no response
        eop = d.eop_end
        name = d.pid_name
        if name in L.TOKENS and name != "SOF":
            a, e = d.fields["addr"], d.fields["endp"]
            mine = a == self.addr or ("answer_old" in self.faults and a == 0)
            self.token = (name, e) if mine and e == 0 else None
            if self.token and name == "IN":
                self.reply = (self._in_reply(), eop)
            return
        if name in ("DATA0", "DATA1") and self.token:
            kind = self.token[0]
            if kind == "SETUP":
                self._setup(d.payload)
                self.reply = (L.handshake_bits("ACK"), eop)
            elif kind == "OUT":
                # status stage of a control read (Figure 8-37 p.226)
                self.reply = (L.handshake_bits("STALL" if self.stall else "ACK"), eop)
                self.stage = None
            self.token = None
            return
        if name == "ACK" and self.last_sent is not None:
            # host accepted our data: advance (8.6.2 p.233)
            n = self.last_sent
            self.last_sent = None
            if self.stage == "data_in":
                self.ptr += n
                self.toggle ^= 1
            elif self.stage == "status_in":
                self.stage = None
                if self.pending_addr is not None:
                    self.addr, self.pending_addr = self.pending_addr, None

    def _setup(self, s):
        self.stall = False
        self.toggle = 1
        self.ptr = 0
        self.naks_left = self.nak_first
        if len(s) != 8:
            self.stall = True
            return
        bm, req, wv, wi, wl = struct.unpack("<BBHHH", s)
        if bm == 0x80 and req == C.GET_DESCRIPTOR:
            key = (wv >> 8, wv & 0xFF)
            if key in self.desc:
                self.buf = self.desc[key][:wl]
                self.stage = "data_in"
            else:
                self.stall = True       # Request Error, 9.2.7 p.247
                self.stage = "data_in"
        elif bm == 0x00 and req == C.SET_ADDRESS:
            self.pending_addr = wv & 0x7F
            self.stage = "status_in"
        elif bm == 0x00 and req == C.SET_CONFIGURATION:
            self.config = wv & 0xFF
            self.stage = "status_in"
        else:
            self.stall = True
            self.stage = "data_in"

    def _in_reply(self):
        if self.stall:
            return L.handshake_bits("STALL")
        if self.naks_left > 0:
            self.naks_left -= 1
            return L.handshake_bits("NAK")
        if self.stage == "data_in":
            chunk = self.buf[self.ptr:self.ptr + 8]   # b"" = zero-length packet
            pid = "DATA1" if (self.toggle or "no_toggle" in self.faults) else "DATA0"
            self.last_sent = len(chunk)
            return L.data_bits(pid, chunk, bad_crc="bad_crc16" in self.faults)
        if self.stage == "status_in":
            self.last_sent = 0
            return L.data_bits("DATA1", b"", bad_crc="bad_crc16" in self.faults)
        return L.handshake_bits("NAK")

    def reply_wave(self, listen_start_ps):
        """Device packet as captured, times relative to the Listen start."""
        if self.reply is None:
            return None
        bits, eop = self.reply
        self.reply = None
        bit = L.BIT_PS / (1 + self.ppm * 1e-6)
        sop = eop + self.reply_bits * bit - listen_start_ps
        inj = L.Inject(eop_se0_ps=1.0e6) if "bad_eop" in self.faults else None
        w = L.packet_wave(bits, inj, ppm=self.ppm, lead_idle_ps=max(0.0, sop))
        if self.jitter_ps:
            self.seed += 1
            w = w.with_jitter(self.jitter_ps, self.seed, mode="extreme",
                              select=lambda i, t, o, n: {L._norm(o), n} == {"J", "K"})
        return w


def run(gen, dev):
    """Drive a host-session generator against a RefDevice in pure Python.
    Same contract as the cocotb glue (see usb_host.py)."""
    t = 0.0
    result = None
    last_drive = None
    while True:
        try:
            act = gen.send(result)
        except StopIteration as stop:
            return stop.value
        if isinstance(act, Drive):
            w = act.wave
            # status-stage OUT data: device side bookkeeping
            dev.on_host_wave(w)
            last_drive = (t, w)
            result = Result(t, None)
            t += w.end
        elif isinstance(act, Wait):
            result = Result(t, None)
            t += act.duration_ps
        elif isinstance(act, Listen):
            # device eop reference was computed on the host wave time base;
            # the Listen starts at the end of that wave
            ws, w = last_drive
            rw = None
            if dev.reply is not None:
                rw = dev.reply_wave(listen_start_ps=w.end)
            if rw is None or "sop" not in rw.marks or rw.marks["sop"] >= act.timeout_ps:
                cap = Wave("J", [], end=act.timeout_ps)
            else:
                end = rw.marks.get("eop_end", act.cap_ps) + act.post_eop_ps
                end = min(end, act.cap_ps)
                cap = Wave(rw.initial, [(tt, l) for tt, l in rw.edges if tt < end], end=end,
                           marks=rw.marks)
            result = Result(t, cap)
            t += cap.end
        else:
            raise TypeError(act)

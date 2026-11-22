# SPDX-License-Identifier: Apache-2.0
"""10BASE-T station host side: frame pump for fw/eth_10bt.s and an ARP (RFC 826) and ICMP echo (RFC 792)
responder for IPv4 (RFC 791). MicroPython and CPython.

Host FIFO format of fw/eth_10bt.s (file header), thread 0:
  host to thread: a frame = its length in octets (DA to the end of the data, no FCS, at most 408), then the
  octets in words, low byte first;
  thread to host: a received frame = 0x8000 + its length in octets (no FCS), then the words; an event word:
  low byte code, high byte value (1 link pass, 2 link fail, 3 frame sent, 4 frame not sent, 5 frame
  discarded with the reason in the high byte).
The chip delivers every frame with a good FCS; the address filter is here.
"""

from pe_host.core import ChipError, Fifo

EV_LINK_PASS, EV_LINK_FAIL, EV_SENT, EV_NOT_SENT, EV_DISCARD = 1, 2, 3, 4, 5
MAX_TX_OCTETS = 408
ETH_ARP, ETH_IPV4 = 0x0806, 0x0800
BROADCAST = b"\xff\xff\xff\xff\xff\xff"


def csum16(b):
    """Internet checksum (RFC 1071): one's complement of the one's-complement sum of 16-bit words."""
    s = 0
    n = len(b)
    for i in range(0, n - 1, 2):
        s += (b[i] << 8) | b[i + 1]
    if n & 1:
        s += b[n - 1] << 8
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def _u16(b, i):
    return (b[i] << 8) | b[i + 1]


def frame_words(frame):
    words = [len(frame)]
    for i in range(0, len(frame), 2):
        lo = frame[i]
        hi = frame[i + 1] if i + 1 < len(frame) else 0
        words.append(lo | (hi << 8))
    return words


class Station:
    def __init__(self, chip, mac, ip, thread=0):
        self.chip = chip
        self.mac = bytes(mac)
        self.ip = bytes(ip)
        self.fifo = Fifo(chip, thread)
        self.frames = []          # received frames (bytes, no FCS)
        self.events = []          # event words
        self.log = []             # (what, detail) for each answered or ignored frame
        self.sent = 0

    # ----- pump -----

    def poll(self):
        """One PULL; splits the words into frames and events. Returns True if anything arrived."""
        n = self.fifo.poll()
        rx = self.fifo.rx
        while rx:
            w = rx[0]
            if w & 0x8000:
                octets = w & 0x7FFF
                nw = (octets + 1) // 2
                if len(rx) < 1 + nw:
                    break
                b = bytearray()
                for x in rx[1:1 + nw]:
                    b.append(x & 0xFF)
                    b.append(x >> 8)
                self.frames.append(bytes(b[:octets]))
                del rx[:1 + nw]
            else:
                self.events.append(w)
                del rx[0]
        return n > 0

    def send(self, frame):
        if len(frame) > MAX_TX_OCTETS:
            raise ChipError("frame of %d octets > %d" % (len(frame), MAX_TX_OCTETS))
        self.fifo.send(frame_words(frame))
        self.sent += 1

    def wait_event(self, codes, timeout_us):
        t0 = self.chip.t.ticks_us()
        while True:
            for i in range(len(self.events)):
                if (self.events[i] & 0xFF) in codes:
                    return self.events.pop(i)
            if self.chip.t.ticks_us() - t0 > timeout_us:
                raise ChipError("no event %s in %d us (events %s)" % (codes, timeout_us, self.events))
            if not self.poll():
                self.chip.t.sleep_us(5)

    # ----- ARP and ICMP -----

    def answer(self, f):
        """The reply frame for f, or None. Records what it did in self.log."""
        if len(f) < 14 or not (f[0:6] == self.mac or f[0:6] == BROADCAST):
            self.log.append(("ignored", "not for this station"))
            return None
        et = _u16(f, 12)
        if et == ETH_ARP:
            return self._arp(f)
        if et == ETH_IPV4:
            return self._icmp(f)
        self.log.append(("ignored", "EtherType %04x" % et))
        return None

    def _arp(self, f):
        a = f[14:42]
        # RFC 826: hardware 1 (Ethernet), protocol 0x0800, lengths 6 and 4, op 1 request
        if len(a) < 28 or _u16(a, 0) != 1 or _u16(a, 2) != ETH_IPV4 or a[4] != 6 or a[5] != 4:
            self.log.append(("ignored", "ARP format"))
            return None
        if _u16(a, 6) != 1 or a[24:28] != self.ip:
            self.log.append(("ignored", "ARP op %d for %s" % (_u16(a, 6), ".".join(str(x) for x in a[24:28]))))
            return None
        sha, spa = a[8:14], a[14:18]
        reply = (bytes(a[0:6]) + b"\x00\x02" + self.mac + self.ip + bytes(sha) + bytes(spa))
        self.log.append(("arp", ".".join(str(x) for x in spa)))
        return bytes(sha) + self.mac + b"\x08\x06" + reply

    def _icmp(self, f):
        ip = f[14:]
        if len(ip) < 20 or ip[0] >> 4 != 4:
            self.log.append(("ignored", "not IPv4"))
            return None
        ihl = (ip[0] & 0xF) * 4
        total = _u16(ip, 2)
        if ihl < 20 or total > len(ip) or total < ihl + 8 or csum16(ip[:ihl]) != 0:
            self.log.append(("ignored", "IPv4 header"))
            return None
        if ip[9] != 1 or ip[16:20] != self.ip or (_u16(ip, 6) & 0x3FFF):
            self.log.append(("ignored", "IPv4 protocol %d, fragment or other address" % ip[9]))
            return None
        icmp = ip[ihl:total]
        if icmp[0] != 8 or icmp[1] != 0 or csum16(icmp) != 0:
            self.log.append(("ignored", "ICMP type %d code %d" % (icmp[0], icmp[1])))
            return None
        # RFC 792 echo reply: type 0, same identifier, sequence number and data; addresses swapped
        rep = bytearray(icmp)
        rep[0] = 0
        rep[2] = rep[3] = 0
        c = csum16(rep)
        rep[2], rep[3] = c >> 8, c & 0xFF
        hdr = bytearray(ip[:20])
        hdr[0] = 0x45                       # options are not copied (RFC 1122 3.2.2.6 allows it)
        hdr[2], hdr[3] = (20 + len(rep)) >> 8, (20 + len(rep)) & 0xFF
        hdr[6] = hdr[7] = 0                 # no fragmentation flags
        hdr[8] = 64                         # TTL
        hdr[10] = hdr[11] = 0
        hdr[12:16], hdr[16:20] = self.ip, ip[12:16]
        c = csum16(hdr)
        hdr[10], hdr[11] = c >> 8, c & 0xFF
        self.log.append(("icmp", "id %d seq %d, %d data octets" % (_u16(icmp, 4), _u16(icmp, 6), len(icmp) - 8)))
        return bytes(f[6:12]) + self.mac + b"\x08\x00" + bytes(hdr) + bytes(rep)

    def serve(self, until, timeout_us):
        """Answers ARP and echo requests until until(self) is true. Each reply waits for its 'frame sent'
        event before the next frame is read (the chip sends one frame at a time)."""
        t0 = self.chip.t.ticks_us()
        while not until(self):
            if self.chip.t.ticks_us() - t0 > timeout_us:
                raise ChipError("serve: time-out after %d us (log %s, events %s)" % (
                    timeout_us, self.log[-4:], self.events))
            if not self.frames and not self.poll():
                self.chip.t.sleep_us(5)
                continue
            while self.frames:
                rep = self.answer(self.frames.pop(0))
                if rep is not None:
                    self.send(rep)
                    ev = self.wait_event((EV_SENT, EV_NOT_SENT), 20000)
                    if ev & 0xFF != EV_SENT:
                        raise ChipError("reply not sent (event %04x)" % ev)

# SPDX-License-Identifier: Apache-2.0
"""CAN 2.0 and CAN FD node model at bit level, written from the Bosch CAN Specification 2.0 (1991, Part A) and the
Bosch CAN FD Protocol Specification (April 2012): frame fields (CAN FD document pages 10 to 14), bit stuffing and
the fixed stuff bits of the CRC field (page 22), the three CRC polynomials (page 13), the bit rate switch at the
sample points of BRS and the CRC delimiter (page 27), bit timing and synchronization (pages 26 to 32). Not written
from any RTL. The node runs on its own clock (a bit time in cycles as a float: the other node's oscillator error),
sends its frames when the bus is idle, repeats a frame that was not acknowledged or lost arbitration, receives every
frame, acknowledges a correct one and records the errors it detects; it sends no error flags.

This is the model of the CAN helper study (branch rt1-can, test/test_can_fd.py), kept as a module so that a CAN node
built from any other block of the chip is measured against the same model.
"""

from collections import deque

# CAN FD page 13: CRC_15 x^15+x^14+x^10+x^8+x^7+x^4+x^3+1, CRC_17 x^17+x^16+x^14+x^13+x^11+x^6+x^4+x^3+x+1,
# CRC_21 x^21+x^20+x^13+x^11+x^7+x^4+x^3+1 (the polynomial without its top term, as the register XOR value)
CRC15, CRC17, CRC21 = (0x4599, 15), (0x1685B, 17), (0x02899, 21)
FD_LEN = {9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}          # CAN FD page 12
LEN_FD = {v: k for k, v in FD_LEN.items()}


def crc(bits, poly, n):
    """CAN FD page 14: CRC_RG = 0; CRCNXT = NXTBIT xor CRC_RG(n-1); shift left; xor the polynomial if CRCNXT."""
    reg, top, mask = 0, 1 << (n - 1), (1 << n) - 1
    for b in bits:
        nxt = b ^ (1 if reg & top else 0)
        reg = (reg << 1) & mask
        if nxt:
            reg ^= poly
    return reg


def to_bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def of_bits(bits):
    return int("".join(map(str, bits)), 2) if bits else 0


def frame_bits(f):
    """Destuffed bits from SOF to the last data bit (CAN 2.0 Part A pages 11 to 13; CAN FD pages 10 to 12).
    f: ident, data, fd, brs, rtr, ide, dlc. Returns (bits, meta)."""
    ident, fd, brs, rtr, ide = f["ident"], f.get("fd", False), f.get("brs", False), f.get("rtr", False), f.get("ide", False)
    data = list(f.get("data", ()))
    dlc = f.get("dlc", len(data) if len(data) <= 8 else LEN_FD[len(data)])
    bits = [0]
    if not ide:
        bits += to_bits(ident, 11) + [0 if fd else int(rtr), 0]            # RTR (r1 in CAN FD frames), IDE
    else:
        bits += to_bits(ident >> 18, 11) + [1, 1] + to_bits(ident & 0x3FFFF, 18) + [0 if fd else int(rtr)]
    if fd:
        bits += [1, 0, int(brs), 0]                                         # EDL, r0, BRS, ESI
    else:
        bits += [0, 0] if ide else [0]                                      # r1, r0 (extended) / r0 (base)
    bits += to_bits(dlc, 4)
    nbytes = 0 if (rtr and not fd) else (dlc if dlc <= 8 else (FD_LEN[dlc] if fd else 8))
    data = (data + [0] * nbytes)[:nbytes]
    for b in data:
        bits += to_bits(b, 8)
    edl_pos = 33 if ide else 14
    return bits, dict(fd=fd, brs=brs and fd, nbytes=nbytes, brs_idx=edl_pos + 2, arb_end=32 if ide else 12,
                      dlc=dlc, data=data, ident=ident, ide=ide, rtr=rtr and not fd)


def stuff(bits):
    """Part A page 22: after five equal bits a complementary bit. Returns (stream, stuff flags)."""
    out, flags, run, last = [], [], 0, None
    for b in bits:
        out.append(b)
        flags.append(False)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            flags.append(True)
            last, run = 1 - b, 1
    return out, flags


def fixed_stuff(crcbits, prev):
    """CAN FD page 22: a fixed stuff bit before the first CRC bit and after each fourth, the inverse of the bit
    before it."""
    out, flags = [], []
    for i, b in enumerate(crcbits):
        if i % 4 == 0:
            out.append(1 - prev)
            flags.append(True)
            prev = 1 - prev
        out.append(b)
        flags.append(False)
        prev = b
    return out, flags


def crc_poly(meta):
    return CRC15 if not meta["fd"] else (CRC17 if meta["nbytes"] <= 16 else CRC21)


def stream_of(f, bad_crc=False):
    """Line bits of a frame from SOF to the end of frame: stuffed fields, CRC field (CAN: stuffed with the fields;
    CAN FD: fixed stuff bits), CRC delimiter, ACK slot (recessive), ACK delimiter, 7 EOF bits. meta gets the
    stream index of the BRS bit and of the CRC delimiter."""
    bits, meta = frame_bits(f)
    poly, n = crc_poly(meta)
    st, fl = stuff(bits)
    if meta["fd"]:
        c = crc(st, poly, n) ^ int(bad_crc)                                   # CAN FD page 13: stuff bits included
        cs, cfl = fixed_stuff(to_bits(c, n), st[-1])
        stream, flags = st + cs, fl + cfl
    else:
        c = crc(bits, poly, n) ^ int(bad_crc)
        stream, flags = stuff(bits + to_bits(c, n))
    brs_si = None
    k = -1
    for si, is_s in enumerate(flags):
        if not is_s:
            k += 1
            if k == meta["brs_idx"]:
                brs_si = si
                break
    crcdel_si = len(stream)
    stream += [1, 1, 1] + [1] * 7
    return stream, dict(meta, brs_si=brs_si if meta["fd"] else None, crcdel_si=crcdel_si, bits=bits, crc=c)


def words_of(bits):
    """Thread words of the frame bits: bit 15 first, the last word padded with 0."""
    b = bits + [0] * (-len(bits) % 16)
    return [of_bits(b[i:i + 16]) for i in range(0, len(b), 16)]


def bits_of_words(words, nbits):
    out = []
    for w in words:
        out += to_bits(w, 16)
    return out[:nbits]


class Node:
    """A CAN / CAN FD node at bit level, from the documents (see the module text).

    bt_n, sp_n, sjw_n: nominal bit time, sample point and SJW in cycles (floats); bt_d, sp_d, sjw_d: data phase.
    frames: dicts for frame_bits; the node sends them when the bus is idle (c >= start), repeats a frame that
    was not acknowledged or lost arbitration, receives every frame, acknowledges a correct one (ack=True), and
    records received frames, its send results and the errors it detects. It sends no error flags.
    """

    def __init__(self, bt_n, sp_n, sjw_n=None, bt_d=None, sp_d=None, sjw_d=None, frames=(), start=0, ack=True,
                 name="node", bad_crc=False, stuff_err=False):
        self.bt_n, self.sp_n = bt_n, sp_n
        self.sjw_n = sjw_n if sjw_n is not None else max(1, round(bt_n / 10))
        self.bt_d = bt_d if bt_d is not None else bt_n
        self.sp_d = sp_d if sp_d is not None else sp_n
        self.sjw_d = sjw_d if sjw_d is not None else max(1, round(self.bt_d / 5))
        self.pending = deque(frames)
        self.start, self.ack, self.name = start, ack, name
        self.bad_crc, self.stuff_err = bad_crc, stuff_err
        self.tx, self.prev_bus = 1, 1
        self.state = "idle"
        self.received, self.results, self.errors = [], [], []
        self.idle_bits = 0
        self.last_fall = 0

    def level(self, c):
        return self.tx

    # -- bit timing --
    def _set_phase(self, data):
        self.data = data
        self.bt, self.sp, self.sjw = (self.bt_d, self.sp_d, self.sjw_d) if data else (self.bt_n, self.sp_n, self.sjw_n)

    def _begin(self, start, send):
        self._set_phase(False)
        self.bitstart, self.blen, self.shift, self.sampled, self.synced = start, self.bt, 0, False, False
        self.state = "frame"
        self.dbits, self.sbits, self.run, self.last, self.stuff_next = [], [], 0, None, False
        self.total, self.tail, self.crc_ok, self.last_sample = None, None, None, 1
        self.ide = self.fd = False
        self.crcbits, self.ncrc, self.fixcnt, self.stage = [], None, 0, "fields"
        self.sending = send
        if send:
            f = self.pending[0]
            self.stream, self.smeta = stream_of(f, bad_crc=self.bad_crc)
            if self.stuff_err:                                 # six equal bits: the stuff bit is left out
                for i in range(1, len(self.stream) - 7):
                    if self.stream[i - 5:i] == [self.stream[i - 1]] * 5 and self.stream[i] != self.stream[i - 1]:
                        del self.stream[i]
                        break
            self.idx = 0
            self.cur = self.stream[0]
        else:
            self.cur = 1

    def see(self, c, bus):
        fall = self.prev_bus == 1 and bus == 0
        self.prev_bus = bus
        if fall:
            self.last_fall = c
        if self.state == "idle":
            if fall:                                           # hard synchronization on a SOF (page 31 rule 3)
                self._begin(c, bool(self.pending and c >= self.start))
            elif self.pending and c >= self.start:
                self._begin(c + 1, True)
                self.tx = self.cur
                return
            else:
                return
        # CAN FD page 31 rule 3: hard synchronization at the EDL to r0 edge (receivers)
        if (fall and not self.sending and self.stage == "fields" and self.fd and not self.sampled
                and len(self.dbits) == (34 if self.ide else 15) and c > self.bitstart):
            self.bitstart, self.shift, self.synced = c, 0, True
        elif fall and not self.synced and self.last_sample == 1 and c > self.bitstart:
            if not (self.sending and self.data):               # page 31 rule 5: no resynchronization in the own data phase
                e = c - self.bitstart
                if e < self.sp:
                    if not (self.sending and self.cur == 0):  # rule 4: not on the own dominant edge
                        self.bitstart += min(e, self.sjw)
                        self.synced = True
                else:
                    self.shift = min(self.bitstart + self.blen - c, self.sjw)
                    self.synced = True
        if not self.sampled and c >= self.bitstart + self.sp:
            self.sampled = True
            self.last_sample = bus
            self._bit(bus)
        if self.state != "idle" and c + 1 >= self.bitstart + self.blen - self.shift:
            self.bitstart += self.blen - self.shift
            self.blen = self.bt
            self.shift, self.sampled, self.synced = 0, False, False
            self._next()
            if self.state == "idle" and bus == 0:
                self._begin(self.bitstart, bool(self.pending and c >= self.start))
        self.tx = self.cur if self.state != "idle" else 1

    def _switch(self, data):
        """Bit rate switch after this sample point (CAN FD page 27): the rest of this bit is PHASE_SEG2 of the
        other set, so the bit is intermediate; the next bits use the other set."""
        old_sp = self.sp
        self._set_phase(data)
        self.blen = old_sp + (self.bt - self.sp)

    def _error(self, text):
        self.errors.append(f"{text} at bit {len(self.dbits)} (tail {self.tail}); destuffed bits so far "
                           f"{''.join(map(str, self.dbits))}")
        self.sending = False
        self.state, self.cur = "wait", 1                       # wait for 11 recessive bits (bus idle)
        self.idle_bits = 0

    def _bit(self, v):
        if self.state == "wait":
            self.idle_bits = self.idle_bits + 1 if v == 1 else 0
            if self.idle_bits >= 11:
                self.state = "idle"
            return
        if self.stage == "ifs":                                # intermission: a dominant bit is a SOF (page 31)
            if v == 0:
                self._begin(self.last_fall, bool(self.pending))
            return
        if self.sending and v != self.cur:
            if (self.stage == "fields" and self.cur == 1 and v == 0 and not self.stuff_next
                    and len(self.dbits) <= self.smeta["arb_end"]):
                self.sending = False                           # lost arbitration (page 23, bit error exception)
                self.results.append("lost")
            elif self.tail == 1:
                pass                                           # the ACK slot
            else:
                self.results.append("bit error")
                self._error("bit error (sender)")
                return
        if self.stage == "fields":
            if self.stuff_next:                                # dynamic stuff bit
                if v == self.last:
                    self._error("stuff error")
                    return
                self.sbits.append(v)
                self.stuff_next, self.last, self.run = False, v, 1
                return
            self.dbits.append(v)
            self.sbits.append(v)
            self.run = self.run + 1 if v == self.last else 1
            self.last = v
            if self.run == 5:
                self.stuff_next = True
            n = len(self.dbits)
            if n == 14:
                self.ide = bool(self.dbits[13])
            edl_pos = 33 if self.ide else 14
            if n == edl_pos + 1:
                self.fd = bool(v)
                self.dlc_start = edl_pos + (4 if self.fd else (2 if self.ide else 1))
            if self.fd and n == edl_pos + 3 and v == 1:        # BRS recessive: switch now
                self._switch(True)
            if hasattr(self, "dlc_start") and n == self.dlc_start + 4:
                dlc = of_bits(self.dbits[self.dlc_start:self.dlc_start + 4])
                rtr = (not self.fd) and self.dbits[32 if self.ide else 12] == 1
                nb = 0 if rtr else (dlc if dlc <= 8 else (FD_LEN[dlc] if self.fd else 8))
                self.total = self.dlc_start + 4 + 8 * nb
                self.ncrc = 15 if not self.fd else (17 if nb <= 16 else 21)
                self.nbytes = nb
            if self.total is not None and n == self.total:
                self.stage, self.crcbits, self.fixcnt = "crc", [], 0
                if self.fd:
                    self.stuff_next = False                    # CAN FD: fixed stuff bits only from here
            return
        if self.stage == "crc":
            if self.fd:
                if self.fixcnt % 5 == 0:                       # fixed stuff bit (page 22)
                    if v == self.last:
                        self._error("fixed stuff error")
                        return
                    self.last = v
                    self.fixcnt += 1
                    return
                self.fixcnt += 1
            else:
                if self.stuff_next:
                    if v == self.last:
                        self._error("stuff error")
                        return
                    self.stuff_next, self.last, self.run = False, v, 1
                    return
                self.run = self.run + 1 if v == self.last else 1
                if self.run == 5:
                    self.stuff_next = True
            self.last = v
            self.crcbits.append(v)
            if len(self.crcbits) == self.ncrc:
                poly, n = (CRC15 if not self.fd else (CRC17 if self.nbytes <= 16 else CRC21))
                want = crc(self.sbits if self.fd else self.dbits, poly, n)
                self.crc_ok = of_bits(self.crcbits) == want
                self.stage, self.tail = "tail", -1
            return
        # tail: 0 CRC delimiter, 1 ACK slot, 2 ACK delimiter, 3 to 9 EOF (Part A page 14)
        t = self.tail
        if t == 0:
            if self.data:
                self._switch(False)
            if v != 1:
                self._error("form error (CRC delimiter)")
                return
        elif t == 1:
            if self.sending:
                self.results.append("ack" if v == 0 else "no ack")
        elif t == 2:
            if v != 1:
                self._error("form error (ACK delimiter)")
                return
            self.received.append(dict(ident=of_bits(self.dbits[1:12]) if not self.ide else
                                      (of_bits(self.dbits[1:12]) << 18) | of_bits(self.dbits[14:32]),
                                      fd=self.fd, ide=self.ide, nbytes=self.nbytes,
                                      data=[of_bits(self.dbits[self.dlc_start + 4 + 8 * k:self.dlc_start + 12 + 8 * k])
                                            for k in range(self.nbytes)],
                                      crc_ok=self.crc_ok, bits=list(self.dbits)))
            if self.sending and self.results and self.results[-1] == "ack":
                self.pending.popleft()
        elif 3 <= t <= 9:
            if v != 1 and not (t == 9 and not self.sending):
                self._error("form error (EOF)")
                return

    def _next(self):
        if self.state == "wait":
            return
        if self.sending:
            self.idx += 1
        if self.stage == "tail":
            self.tail += 1
            if self.tail == 10:                                # intermission: 3 bits, then idle
                self.state, self.stage, self.tail = "ifs", "ifs", 0
        elif self.stage == "ifs":
            self.tail += 1
            if self.tail == 3:
                self.state, self.cur = "idle", 1
                return
        if self.sending and self.idx < len(self.stream):
            self.cur = self.stream[self.idx]
        elif self.sending:
            self.cur = 1
        else:
            self.cur = 0 if (self.stage == "tail" and self.tail == 1 and self.crc_ok and self.ack) else 1

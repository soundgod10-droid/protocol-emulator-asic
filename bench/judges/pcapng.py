# SPDX-License-Identifier: Apache-2.0
"""Minimal pcapng reader and writer (Section Header, Interface Description and Enhanced Packet blocks), so that the
network judges read Wireshark/tshark captures and `pktmon etl2pcap` output with no extra package.

Format: IETF draft-ietf-opsawg-pcapng (block types 0x0A0D0D0A SHB, 0x00000001 IDB, 0x00000006 EPB; little or big
endian by the byte-order magic 0x1A2B3C4D). Link type 1 = Ethernet. Timestamps in microseconds (if_tsresol not read:
the default 10^-6 is assumed, which is what pktmon and Wireshark write by default).
"""

import struct

SHB, IDB, EPB, SPB = 0x0A0D0D0A, 0x00000001, 0x00000006, 0x00000003


def read(path):
    """[(t_us, frame bytes)] of every Ethernet packet in the file."""
    data = open(path, "rb").read()
    out = []
    i, end = 0, "<"
    while i + 12 <= len(data):
        btype = struct.unpack_from("<I", data, i)[0]
        if btype == SHB:
            magic = struct.unpack_from("<I", data, i + 8)[0]
            end = "<" if magic == 0x1A2B3C4D else ">"
        btype, blen = struct.unpack_from(end + "II", data, i)
        if blen < 12 or i + blen > len(data):
            raise ValueError("pcapng block at %d: length %d" % (i, blen))
        if btype == EPB:
            _, ts_hi, ts_lo, cap, _ = struct.unpack_from(end + "IIIII", data, i + 8)
            out.append((((ts_hi << 32) | ts_lo), data[i + 28:i + 28 + cap]))
        elif btype == SPB:
            orig = struct.unpack_from(end + "I", data, i + 8)[0]
            out.append((None, data[i + 12:i + 12 + min(orig, blen - 16)]))
        i += blen
    return out


def _pad(b):
    return b + b"\0" * ((4 - len(b) % 4) % 4)


def write(path, packets):
    """packets: [(t_us, frame bytes)]; one Ethernet interface."""
    out = bytearray()
    shb_body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    out += struct.pack("<II", SHB, 12 + len(shb_body)) + shb_body + struct.pack("<I", 12 + len(shb_body))
    idb_body = struct.pack("<HHI", 1, 0, 0)
    out += struct.pack("<II", IDB, 12 + len(idb_body)) + idb_body + struct.pack("<I", 12 + len(idb_body))
    for t, f in packets:
        t = int(t)
        body = struct.pack("<IIIII", 0, t >> 32, t & 0xFFFFFFFF, len(f), len(f)) + _pad(bytes(f))
        out += struct.pack("<II", EPB, 12 + len(body)) + body + struct.pack("<I", 12 + len(body))
    open(path, "wb").write(bytes(out))

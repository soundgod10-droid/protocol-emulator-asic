"""Reference functions for the serial line unit tests, written from the standards.

USB 2.0 (usb_20.pdf): NRZI, 7.1.8 (a 0 is a change of state, a 1 no change); bit stuffing, 7.1.9
(a 0 after six consecutive 1s, also before EOP); SYNC KJKJKJKK, 7.1.10 (bits 00000001); CRC5
x^5 + x^2 + 1 and CRC16 x^16 + x^15 + x^2 + 1, preset to all ones, sent inverted with the highest
order coefficient first, 8.3.5; residues 01100 and 1000000000001101.
IEEE 802.3-2022: Manchester, 7.3.1.1 (first half of a bit cell is the complement of the bit, second
half the bit); preamble 7 x 10101010 and SFD 10101011 in transmission order, 3.2.1-3.2.2; FCS = CRC-32,
3.2.9 (zlib/binascii crc32 gives the same value; the FCS is sent least significant byte first, each
byte least significant bit first).
"""

import binascii


def bits_of_bytes(data):
    """Bytes to bits in transmission order (least significant bit first)."""
    return [(b >> i) & 1 for b in data for i in range(8)]


def bytes_of_bits(bits):
    n = len(bits) // 8
    return bytes(sum(bits[8 * k + i] << i for i in range(8)) for k in range(n))


def crc_reflected(bits, poly_rev, width, preset_ones=True):
    """Serial CRC, least significant register bit = highest order coefficient (reflected form)."""
    mask = (1 << width) - 1
    c = mask if preset_ones else 0
    for b in bits:
        c = ((c >> 1) ^ (poly_rev if ((c ^ b) & 1) else 0)) & mask
    return c


def usb_crc16_bits(data_bits):
    c = crc_reflected(data_bits, 0xA001, 16)
    return [((~c) >> i) & 1 for i in range(16)]


def usb_crc5_bits(data_bits):
    c = crc_reflected(data_bits, 0x14, 5)
    return [((~c) >> i) & 1 for i in range(5)]


def stuff(bits):
    out = []
    run = 0
    for b in bits:
        out.append(b)
        run = run + 1 if b else 0
        if run == 6:
            out.append(0)
            run = 0
    return out


def destuff(bits):
    out = []
    run = 0
    skip = False
    for b in bits:
        if skip:
            skip = False
            run = 0
            continue
        out.append(b)
        run = run + 1 if b else 0
        if run == 6:
            skip = True
    return out


def nrzi(bits, level=1):
    """Line levels (1 = J) from bits: a 0 changes the level."""
    out = []
    for b in bits:
        if b == 0:
            level ^= 1
        out.append(level)
    return out


def un_nrzi(levels, level=1):
    out = []
    for lv in levels:
        out.append(1 if lv == level else 0)
        level = lv
    return out


SYNC_BITS = [0, 0, 0, 0, 0, 0, 0, 1]


def usb_packet_bits(pid, data, crc="16"):
    """SYNC, PID and data in transmission order, with the CRC appended (crc '16', '5' or None);
    returned before stuffing."""
    bits = SYNC_BITS + bits_of_bytes(bytes([pid]))
    d = bits_of_bytes(data)
    if crc == "16":
        d = d + usb_crc16_bits(d)
    elif crc == "5":
        d = d[:11] + usb_crc5_bits(d[:11])
    return bits + d


def eth_frame_bits(payload):
    """Preamble, SFD, payload and FCS in transmission order."""
    pre = [1, 0] * 28 + [1, 0, 1, 0, 1, 0, 1, 1]
    fcs = binascii.crc32(payload) & 0xFFFFFFFF
    return pre + bits_of_bytes(payload + fcs.to_bytes(4, "little"))


def manchester_halves(bits):
    out = []
    for b in bits:
        out += [1 - b, b]
    return out

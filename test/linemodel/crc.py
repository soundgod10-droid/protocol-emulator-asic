"""Bit-serial CRCs written from the text of the standards.

USB CRC5 and CRC16: USB 2.0 section 8.3.5, printed p.198
  (register seeded with ones; per bit: XOR of the register high bit and the
  data bit; shift left; if the XOR is one, XOR the polynomial; at the end
  invert the register and send it MSb first; receiver residual check).
  CRC5  G = x^5 + x^2 + 1, pattern 00101b, residual 01100b   (8.3.5.1 p.198)
  CRC16 G = x^16 + x^15 + x^2 + 1, pattern 0x8005, residual 0x800D (8.3.5.2 p.198)

Ethernet CRC-32: IEEE 802.3-2022 clause 3.2.9, printed p.243
  (first 32 bits complemented, M(x)*x^32 mod G(x), result complemented,
  sent x31 first). G = 0x04C11DB7. Protected-field octets are sent LSB first
  (3.3 p.244), and that is the bit order fed to the CRC.

Bits are lists of ints 0/1 in line (transmission) order.
"""

USB_CRC5_POLY = 0b00101          # USB 2.0 8.3.5.1 p.198
USB_CRC5_RESIDUAL = 0b01100      # USB 2.0 8.3.5.1 p.198
USB_CRC16_POLY = 0x8005          # USB 2.0 8.3.5.2 p.198
USB_CRC16_RESIDUAL = 0x800D      # USB 2.0 8.3.5.2 p.198
ETH_CRC32_POLY = 0x04C11DB7      # IEEE 802.3-2022 3.2.9 p.243


def _serial(bits, width, poly, init):
    mask = (1 << width) - 1
    top = 1 << (width - 1)
    reg = init & mask
    for b in bits:
        fb = ((reg & top) != 0) ^ (b & 1)
        reg = (reg << 1) & mask
        if fb:
            reg ^= poly
    return reg


def _reg_to_bits_msb_first(reg, width):
    return [(reg >> (width - 1 - i)) & 1 for i in range(width)]


def usb_crc5_bits(bits):
    """CRC5 field bits, in line order (MSb of the inverted register first)."""
    reg = _serial(bits, 5, USB_CRC5_POLY, 0x1F)
    return _reg_to_bits_msb_first(reg ^ 0x1F, 5)


def usb_crc16_bits(bits):
    reg = _serial(bits, 16, USB_CRC16_POLY, 0xFFFF)
    return _reg_to_bits_msb_first(reg ^ 0xFFFF, 16)


def usb_crc5_check(bits_with_crc):
    """True if the register after field + CRC equals the residual."""
    return _serial(bits_with_crc, 5, USB_CRC5_POLY, 0x1F) == USB_CRC5_RESIDUAL


def usb_crc16_check(bits_with_crc):
    return _serial(bits_with_crc, 16, USB_CRC16_POLY, 0xFFFF) == USB_CRC16_RESIDUAL


def eth_fcs_bits(frame_bits):
    """FCS bits in line order (x31 first) for the protected-field bits.

    Step a) of 3.2.9 (complement the first 32 bits) is the same as seeding
    the register with ones; steps c)-e) are the serial division and the final
    complement.
    """
    reg = _serial(frame_bits, 32, ETH_CRC32_POLY, 0xFFFFFFFF)
    return _reg_to_bits_msb_first(reg ^ 0xFFFFFFFF, 32)


def bytes_to_bits_lsb_first(data):
    out = []
    for byte in data:
        for i in range(8):
            out.append((byte >> i) & 1)
    return out


def bits_to_bytes_lsb_first(bits):
    if len(bits) % 8:
        raise ValueError("not a whole number of octets")
    out = bytearray()
    for i in range(0, len(bits), 8):
        v = 0
        for k in range(8):
            v |= (bits[i + k] & 1) << k
        out.append(v)
    return bytes(out)


def eth_fcs_octets(protected_octets):
    """FCS as the four octets that follow the frame on the wire.
    Line bit order is x31 first and each octet goes LSB first, so octet 0
    holds x31 in its LSB."""
    return bits_to_bytes_lsb_first(eth_fcs_bits(bytes_to_bits_lsb_first(protected_octets)))


def eth_fcs_ok(frame_with_fcs):
    """FCS validation by recomputation (IEEE 802.3-2022 4.2.4.1.2 p.260)."""
    if len(frame_with_fcs) < 4:
        return False
    return eth_fcs_octets(frame_with_fcs[:-4]) == bytes(frame_with_fcs[-4:])

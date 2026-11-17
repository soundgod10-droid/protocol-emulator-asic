"""Known vectors from independent sources.

[CAT]  Greg Cook, "Catalogue of parametrised CRC algorithms",
       https://reveng.sourceforge.io/crc-catalogue/all.htm (read 2026-11-15):
       CRC-32/ISO-HDLC check=0xcbf43926 residue=0xdebb20e3;
       CRC-16/USB check=0xb4c8 residue=0xb001; CRC-5/USB check=0x19
       residue=0x06. "check" = CRC of the ASCII string "123456789".
       Catalogue values are in reflected form: bit i = i-th bit on the line.
[USBCRC] USB-IF, "Cyclic Redundancy Checks in USB" (draft), crcdes.pdf
       from https://www.usb.org/sites/default/files/crcdes.pdf, pp.5-6:
       token, SOF and data packet examples as NRZ bit strings.
The Ethernet FCS is also compared with Python's binascii.crc32 (an independent
implementation of the same CRC-32).
"""
import binascii
import random
import struct

from linemodel import crc
from linemodel import usb_ls as L

CHECK = b"123456789"


def as_line_int(bits):
    return sum(b << i for i, b in enumerate(bits))


def test_crc32_check_value_cat():
    assert crc.eth_fcs_octets(CHECK) == struct.pack("<I", 0xCBF43926)


def test_crc32_matches_binascii_random():
    rng = random.Random(20261115)
    for n in [0, 1, 2, 3, 4, 59, 60, 63, 64, 100, 1514, 1517]:
        for _ in range(3):
            data = bytes(rng.randrange(256) for _ in range(n))
            assert crc.eth_fcs_octets(data) == struct.pack("<I", binascii.crc32(data) & 0xFFFFFFFF)


def test_crc32_residue_cat():
    # serial register after frame + FCS, compared with the catalogue residue
    data = b"line model residue test frame"
    bits = crc.bytes_to_bits_lsb_first(data + crc.eth_fcs_octets(data))
    reg = crc._serial(bits, 32, crc.ETH_CRC32_POLY, 0xFFFFFFFF)
    refl = int("{:032b}".format(reg)[::-1], 2)
    assert refl == 0xDEBB20E3


def test_usb_crc16_check_value_cat():
    assert as_line_int(crc.usb_crc16_bits(crc.bytes_to_bits_lsb_first(CHECK))) == 0xB4C8


def test_usb_crc5_check_value_cat():
    assert as_line_int(crc.usb_crc5_bits(crc.bytes_to_bits_lsb_first(CHECK))) == 0x19


def test_usb_residuals_spec_vs_cat():
    # spec residuals (8.3.5.1/8.3.5.2 p.198) reflected = catalogue residues
    assert int("{:05b}".format(crc.USB_CRC5_RESIDUAL)[::-1], 2) == 0x06
    assert int("{:016b}".format(crc.USB_CRC16_RESIDUAL)[::-1], 2) == 0xB001


# [USBCRC] p.5-6: (description, packet bits builder, NRZ string without "XX1")
USBCRC_PACKETS = [
    ("sof 710", lambda: L.sof_bits(0x710), "00000001101001010000100011110100"),
    ("setup addr 15 endp e", lambda: L.token_bits("SETUP", 0x15, 0xE), "00000001101101001010100011110111"),
    ("out addr 3a endp a", lambda: L.token_bits("OUT", 0x3A, 0xA), "00000001100001110101110010111100"),
    ("in addr 70 endp 4", lambda: L.token_bits("IN", 0x70, 0x4), "00000001100101100000111001001110"),
    ("sof 001", lambda: L.sof_bits(0x001), "00000001101001011000000000010111"),
    ("data0 00 01 02 03", lambda: L.data_bits("DATA0", bytes([0, 1, 2, 3])),
     "0000000111000011000000001000000001000000110000001111011101011110"),
    ("data1 23 45 67 89", lambda: L.data_bits("DATA1", bytes([0x23, 0x45, 0x67, 0x89])),
     "0000000111010010110001001010001011100110100100010111000000111000"),
]
USBCRC_CRC5 = [("10101000111", "10111"), ("01011100101", "11100"), ("00001110010", "01110"),
               ("10000000000", "10111"), ("00001000111", "10100")]
USBCRC_CRC16 = [("00000000100000000100000011000000", "1111011101011110"),
                ("11000100101000101110011010010001", "0111000000111000")]


def _b(s):
    return [int(c) for c in s]


def test_usbcrc_paper_crc5():
    for data, want in USBCRC_CRC5:
        assert crc.usb_crc5_bits(_b(data)) == _b(want)
        assert crc.usb_crc5_check(_b(data) + _b(want))


def test_usbcrc_paper_crc16():
    for data, want in USBCRC_CRC16:
        assert crc.usb_crc16_bits(_b(data)) == _b(want)
        assert crc.usb_crc16_check(_b(data) + _b(want))


def test_usbcrc_paper_packets():
    for name, build, nrz in USBCRC_PACKETS:
        assert L.SYNC_BITS + build() == _b(nrz), name


def test_usbcrc_paper_packets_decode_from_line():
    # the same packets through stuffing, NRZI and a waveform, then decoded
    for name, build, nrz in USBCRC_PACKETS:
        d = L.decode_packet(L.packet_wave(build()))
        assert d.ok, (name, d.errors)
        assert d.pid_raw == as_line_int(_b(nrz[8:16])), name
        if d.pid_name in L.DATAS:
            assert crc.bytes_to_bits_lsb_first(d.payload) == _b(nrz[16:-16]), name
        else:
            assert d.fields, name


def test_ethernet_frames_fcs_binascii():
    rng = random.Random(8157)
    for n in (64, 64, 64, 1518):
        body = bytes(rng.randrange(256) for _ in range(n - 4))
        fr = body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF)
        assert crc.eth_fcs_ok(fr)
        bad = bytearray(fr)
        bad[20] ^= 0x01
        assert not crc.eth_fcs_ok(bytes(bad))

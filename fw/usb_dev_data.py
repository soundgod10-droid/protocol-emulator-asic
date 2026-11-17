# SPDX-License-Identifier: Apache-2.0
"""Data memory image of fw/usb_dev.s: request table, descriptors, CRC5 token table, reply stages and the
start values of the variables.

The host writes these words (WRITE_MEM) before START. Addresses of the firmware tables and handlers come
from the assembled firmware (its symbols), so the two files cannot disagree.

Descriptors are stored as packet chunks of 7 words: TXCTRL and the word count (written by the firmware for
the bytes it sends), SYNC + PID, then 8 data bytes in 4 words, low byte first (the order in which the USB
helper sends a word). The PID alternates DATA1, DATA0, ... from the first chunk, as a control read data
stage does (USB 2.0 8.5.3). After a descriptor whose length is a multiple of 8 there is one more chunk
header, for the zero-length packet that ends a shorter-than-requested data stage (5.5.3).

Request table entry (5 words): K1 = bmRequestType + 256 bRequest, handler address (+ 0x8000: any wValue),
wValue, pointer (a chunk, or a program-memory variable for the variable handlers), length in bytes. The
firmware writes K1 of each request into the sentinel entry after the table, so a request that is not in
the table finds the sentinel: Request Error, STALL (9.2.7). wIndex is not decoded (one interface, one
interrupt endpoint).
References: USB 2.0 chapter 9; Device Class Definition for HID 1.11, section 7 and appendix B.1.
"""

# pid.codes test VID/PID: for development only, not for a product.
VID, PID = 0x1209, 0x0001
MANUFACTURER = "protocol emulator"
PRODUCT = "USB LS keyboard"
ANY = 0x8000
BLOBS = 1100                         # first data word of the descriptor chunks
SYNC_PID = {"DATA0": 0xC380, "DATA1": 0x4B80, "ACK": 0xD280, "NAK": 0x5A80, "STALL": 0x1E80}

# Device descriptor (9.6.1): USB 2.00, class in the interface, bMaxPacketSize0 8 (low speed, 5.5.3).
DEVICE = bytes([18, 1, 0x00, 0x02, 0, 0, 0, 8, VID & 0xFF, VID >> 8, PID & 0xFF, PID >> 8,
                0x00, 0x01, 1, 2, 0, 1])

# HID boot keyboard report descriptor (HID 1.11 appendix B.1): 8-byte input report (modifiers, reserved,
# 6 key codes), 1-byte output report (5 LEDs and 3 bits of padding).
REPORT = bytes.fromhex(
    "05010906a101050719e029e7150025017501950881029501750881019505"
    "7501050819012905910295017503910195067508150025650507190029658100c0")

# HID descriptor (HID 6.2.1) and the configuration bundle (9.6.3, 9.6.5, 9.6.6): one interface, HID boot
# keyboard (class 3, subclass 1, protocol 1), endpoint 1 IN interrupt, 8 bytes, 10 ms. Self-powered: the
# chip takes its supply from the board.
HID = bytes([9, 0x21, 0x11, 0x01, 0, 1, 0x22, len(REPORT), 0])
CONFIG = bytes([9, 2, 34, 0, 1, 1, 0, 0xC0, 0x32,
                9, 4, 0, 0, 1, 3, 1, 1, 0]) + HID + bytes([7, 5, 0x81, 3, 8, 0, 10])


def string_desc(text):
    """String descriptor (9.6.7): UNICODE UTF-16LE."""
    b = text.encode("utf-16-le")
    return bytes([2 + len(b), 3]) + b


STRING0 = bytes([4, 3, 0x09, 0x04])  # LANGID 0x0409


def words_of(b):
    b = bytes(b) + (b"\0" if len(b) % 2 else b"")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def chunks(b):
    """Packet chunks of a control read data stage (see the module text)."""
    out = []
    n = (len(b) + 7) // 8
    for i in range(n):
        part = b[8 * i:8 * i + 8]
        w = words_of(part)
        pid = "DATA1" if i % 2 == 0 else "DATA0"
        out += [0, 0, SYNC_PID[pid]] + w + [0] * (4 - len(w))
    if len(b) % 8 == 0:
        out += [0, 0, SYNC_PID["DATA1" if n % 2 == 0 else "DATA0"]]
    return out


def crc5(field11):
    """USB CRC5 of an 11-bit token field (8.3.5.1), LSB first; result bit 0 is sent first."""
    c = 0x1F
    for i in range(11):
        if (c ^ (field11 >> i)) & 1:
            c = (c >> 1) ^ 0x14
        else:
            c >>= 1
    return c ^ 0x1F


def token_word1(addr, endp):
    """Second received word of a token (the last 8 bits): ENDP bits 3..1, CRC5."""
    return (endp >> 1) | (crc5(addr | (endp << 7)) << 3)


def k1(bm, req):
    return bm | (req << 8)


def build(symbols):
    """symbols: name -> value of the assembled fw/usb_dev.s (pe_asm Program.symbols gives (value, kind)
    pairs; both forms are accepted). Returns (mem, info): mem = [(address, words)] for WRITE_MEM,
    info = addresses and the table for the tests."""
    s = {k: (v[0] if isinstance(v, tuple) else v) for k, v in symbols.items()}
    assert len(REPORT) == 63 and len(CONFIG) == CONFIG[2]
    blobs = [("DEVD", DEVICE), ("CFGD", CONFIG), ("HIDD", HID), ("REPD", REPORT), ("STR0", STRING0),
             ("STR1", string_desc(MANUFACTURER)), ("STR2", string_desc(PRODUCT)),
             ("DEVST", bytes([1, 0])),             # GET_STATUS device: self-powered, no remote wakeup
             ("ZERO", bytes(2)), ("ZRPT", bytes(8))]
    addr = BLOBS
    at = {}
    blob_words = []
    for name, b in blobs:
        at[name] = addr
        w = chunks(b)
        blob_words += w
        addr += len(w)
    assert addr <= 1536, "descriptor chunks end at %d, after the data memory" % addr

    H = {n: s[n] for n in ("astall", "aread", "aget", "aaddr", "aconf", "aclrh", "ahalt", "aset", "aseth",
                           "awr", "aok")}
    GD = k1(0x80, 6)
    table = [
        # standard requests (9.4)
        (GD, H["aread"], 0x0100, at["DEVD"], len(DEVICE)),
        (GD, H["aread"], 0x0200, at["CFGD"], len(CONFIG)),
        (GD, H["aread"], 0x0300, at["STR0"], len(STRING0)),
        (GD, H["aread"], 0x0301, at["STR1"], len(string_desc(MANUFACTURER))),
        (GD, H["aread"], 0x0302, at["STR2"], len(string_desc(PRODUCT))),
        (k1(0x00, 5), H["aaddr"] | ANY, 0, 0, 0),                # SET_ADDRESS
        (k1(0x00, 9), H["aconf"] | ANY, 0, 0, 0),                # SET_CONFIGURATION
        (k1(0x80, 8), H["aget"], 0, s["V_CONF"], 1),             # GET_CONFIGURATION
        (k1(0x80, 0), H["aread"], 0, at["DEVST"], 2),            # GET_STATUS device
        (k1(0x81, 0), H["aread"], 0, at["ZERO"], 2),             # GET_STATUS interface
        (k1(0x82, 0), H["aget"], 0, s["V_HALT1"], 2),            # GET_STATUS endpoint
        (k1(0x02, 1), H["aclrh"], 0, 0, 0),                      # CLEAR_FEATURE(ENDPOINT_HALT)
        (k1(0x02, 3), H["ahalt"], 0, 0, 0),                      # SET_FEATURE(ENDPOINT_HALT)
        (k1(0x81, 10), H["aread"], 0, at["ZERO"], 1),            # GET_INTERFACE: alternate 0
        (k1(0x01, 11), H["aok"], 0, 0, 0),                       # SET_INTERFACE alternate 0 only
        # HID class descriptors (HID 7.1) and requests (HID 7.2)
        (k1(0x81, 6), H["aread"], 0x2200, at["REPD"], len(REPORT)),
        (k1(0x81, 6), H["aread"], 0x2100, at["HIDD"], len(HID)),
        (k1(0xA1, 1), H["aread"], 0x0100, at["ZRPT"], 8),        # GET_REPORT input: no key down
        (k1(0x21, 9), H["awr"], 0x0200, s["V_LED"], 1),          # SET_REPORT output: LEDs
        (k1(0xA1, 2), H["aget"] | ANY, 0, s["V_IDLE"], 1),       # GET_IDLE
        (k1(0x21, 10), H["aseth"] | ANY, 0, s["V_IDLE"], 0),     # SET_IDLE: duration in wValue high
        (k1(0xA1, 3), H["aget"], 0, s["V_PROTO"], 1),            # GET_PROTOCOL
        (k1(0x21, 11), H["aset"] | ANY, 0, s["V_PROTO"], 0),     # SET_PROTOCOL
    ]
    assert len(table) == s["NREQ"] and s["TABLE"] + 5 * len(table) == s["SENT"], \
        "table has %d entries; the firmware expects %d" % (len(table), s["NREQ"])
    tw = []
    for key, handler, wv, ptr, ln in table:
        tw += [key, handler, wv, ptr, ln]
    tw += [0, H["astall"] | ANY, 0, 0, 0]                        # sentinel: K1 written by the firmware

    crct = [token_word1(a, 0) for a in range(128)] + [token_word1(a, 1) for a in range(128)]

    init = {"V_ADDR": 0, "V_PEND": 0xFFFF, "V_CONF": 0, "V_STATE": 0, "V_PTR": 0, "V_REM": 0, "V_TOK": 0,
            "V_HSW": 0, "V_AW": 0, "V_HALT1": 0, "V_TOG1": 0, "V_K2": 0, "V_WLEN": 0, "V_LASTEP": 0xFF,
            "V_WVAR": s["V_LED"], "V_IDLE": 0, "V_PROTO": 1,       # report protocol after reset (HID 7.2.6)
            "V_LED": 0, "V_HW": 0, "V_FPTR": s["V_NONE"], "V_FP1": 0, "V_FC": 0,
            "V_ACKW": SYNC_PID["ACK"], "V_PID0": SYNC_PID["DATA0"], "V_PID1": SYNC_PID["DATA1"],
            "V_NONE": 0xFFFF, "V_IN0B": 0xFFFF, "V_IN1B": 0xFFFF,  # no token matches before a bus reset
            "V_ST0P": s["STNAK"], "V_ST1P": s["STNAK"]}
    base = s["V_BASE"]
    nvar = 512 - base
    assert len(init) == nvar, "%d start values for %d variables" % (len(init), nvar)
    # handshake for an OUT data packet in each endpoint 0 state: ACK in the status stage of a control
    # read (1) and in the data stage of a control write (3), else STALL (8.5.3.4)
    ouths = [SYNC_PID[h] for h in ("STALL", "ACK", "STALL", "ACK", "STALL")]
    by_addr = {s[name]: v for name, v in init.items()}
    assert sorted(by_addr) == list(range(base, 512)), "start values do not cover V_BASE..511"
    assert s["V_IN1B"] == s["V_IN0B"] + 1 and s["V_ST0P"] == s["V_IN0B"] + 2 and s["V_ST1P"] == s["V_IN0B"] + 3

    mem = [
        (s["CRCT"], crct),
        (s["TABLE"], tw),
        (s["ST1"], [0x20, 5, SYNC_PID["DATA0"], 0, 0, 0, 0]),
        (s["STNAK"], [0, 1, SYNC_PID["NAK"]]),
        (s["STSTALL"], [0, 1, SYNC_PID["STALL"]]),
        (s["STZLP1"], [0x20, 1, SYNC_PID["DATA1"]]),
        (s["VARBUF"], [0, 0, SYNC_PID["DATA1"], 0]),
        (s["INIT"], [by_addr[a] for a in range(base, 512)]),
        (BLOBS, blob_words),
    ]
    spans = sorted((a, a + len(w)) for a, w in mem)
    for (a0, a1), (b0, _) in zip(spans, spans[1:]):
        assert a1 <= b0, "data image regions overlap at %d" % b0
    assert s["SENT"] + 5 <= s["RXBUF"] and s["RXBUF"] + 8 <= s["ST1"]
    info = dict(at, table=table)
    return mem, info

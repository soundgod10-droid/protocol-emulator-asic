# SPDX-License-Identifier: Apache-2.0
"""Data memory image of fw/usb_dev.s: descriptors, the request table and the reply stages.

The host writes these words (WRITE_MEM) before START. Byte k of a descriptor is in word k // 2, low
byte first: the order in which the USB helper sends a word. The table and stage addresses come from
the assembled firmware (its .equ symbols), so the two files cannot disagree.

Request table entry (5 words): K1 = bmRequestType + 256 bRequest, flags (action in bits 3:0, bit 15 =
any wValue), wValue, pointer (data word, or a program-memory variable for the variable actions),
length in bytes. The firmware writes K1 of each request into the sentinel entry after the table, so a
request that is not in the table finds the sentinel: Request Error, STALL (USB 2.0 9.2.7).
References: USB 2.0 chapter 9; Device Class Definition for HID 1.11, section 7 and appendix B.1.
"""

# pid.codes test VID/PID: for development only, not for a product.
VID, PID = 0x1209, 0x0001
MANUFACTURER = "protocol emulator"
PRODUCT = "USB LS keyboard"

A_STALL, A_READ, A_ADDR, A_CONF, A_SET, A_GET, A_WRITE, A_FEAT, A_SETH, A_OK = range(10)
ANY = 0x8000

# Device descriptor (9.6.1): USB 2.00, class in the interface, bMaxPacketSize0 8 (low speed, 5.5.3).
DEVICE = bytes([18, 1, 0x00, 0x02, 0, 0, 0, 8, VID & 0xFF, VID >> 8, PID & 0xFF, PID >> 8,
                0x00, 0x01, 1, 2, 0, 1])

# HID boot keyboard report descriptor (HID 1.11 appendix B.1): 8-byte input report (modifiers,
# reserved, 6 key codes), 1-byte output report (5 LEDs and 3 bits of padding).
REPORT = bytes.fromhex(
    "05010906a101050719e029e7150025017501950881029501750881019505"
    "7501050819012905910295017503910195067508150025650507190029658100c0")

# Configuration bundle (9.6.3, 9.6.5, HID 6.2.1, 9.6.6): one interface, HID boot keyboard
# (class 3, subclass 1, protocol 1), endpoint 1 IN interrupt, 8 bytes, 10 ms. Self-powered: the chip
# takes its supply from the board.
CONFIG = bytes([9, 2, 34, 0, 1, 1, 0, 0xC0, 0x32,
                9, 4, 0, 0, 1, 3, 1, 1, 0,
                9, 0x21, 0x11, 0x01, 0, 1, 0x22, len(REPORT), 0,
                7, 5, 0x81, 3, 8, 0, 10])
HID_OFFSET = 18                       # byte offset of the HID descriptor in CONFIG (even: a word start)


def string_desc(text):
    """String descriptor (9.6.7): UNICODE UTF-16LE."""
    b = text.encode("utf-16-le")
    return bytes([2 + len(b), 3]) + b


STRING0 = bytes([4, 3, 0x09, 0x04])   # LANGID 0x0409


def words_of(b):
    b = bytes(b) + (b"\0" if len(b) % 2 else b"")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def k1(bm, req):
    return bm | (req << 8)


def build(symbols):
    """symbols: name -> value of the assembled fw/usb_dev.s (pe_asm Program.symbols gives
    (value, kind) pairs; both forms are accepted). Returns (mem, info): mem = [(address, words)] for
    WRITE_MEM, info = addresses and the table for the tests."""
    s = {k: (v[0] if isinstance(v, tuple) else v) for k, v in symbols.items()}
    assert len(REPORT) == 63 and len(CONFIG) == CONFIG[2]
    blobs = [("DEVD", DEVICE), ("CFGD", CONFIG), ("REPD", REPORT), ("STR0", STRING0),
             ("STR1", string_desc(MANUFACTURER)), ("STR2", string_desc(PRODUCT)),
             ("DEVST", bytes([1, 0])),              # GET_STATUS device: self-powered, no remote wakeup
             ("ZERO", bytes(2)), ("ZRPT", bytes(8))]
    addr = 512
    at = {}
    desc_words = []
    for name, b in blobs:
        at[name] = addr
        w = words_of(b)
        desc_words += w
        addr += len(w)
    assert addr <= s["TABLE"], "descriptors overlap the request table"
    hid = at["CFGD"] + HID_OFFSET // 2

    GD = k1(0x80, 6)
    table = [
        # standard requests (9.4)
        (GD, A_READ, 0x0100, at["DEVD"], len(DEVICE)),
        (GD, A_READ, 0x0200, at["CFGD"], len(CONFIG)),
        (GD, A_READ, 0x0300, at["STR0"], len(STRING0)),
        (GD, A_READ, 0x0301, at["STR1"], len(string_desc(MANUFACTURER))),
        (GD, A_READ, 0x0302, at["STR2"], len(string_desc(PRODUCT))),
        (k1(0x00, 5), A_ADDR | ANY, 0, 0, 0),                     # SET_ADDRESS
        (k1(0x00, 9), A_CONF | ANY, 0, 0, 0),                     # SET_CONFIGURATION
        (k1(0x80, 8), A_GET, 0, s["V_CONF"], 1),                  # GET_CONFIGURATION
        (k1(0x80, 0), A_READ, 0, at["DEVST"], 2),                 # GET_STATUS device
        (k1(0x81, 0), A_READ, 0, at["ZERO"], 2),                  # GET_STATUS interface
        (k1(0x82, 0), A_GET, 0, s["V_HALT1"], 2),                 # GET_STATUS endpoint
        (k1(0x02, 1), A_FEAT, 0, 0, 0),                           # CLEAR_FEATURE(ENDPOINT_HALT)
        (k1(0x02, 3), A_FEAT, 0, 0, 0),                           # SET_FEATURE(ENDPOINT_HALT)
        (k1(0x81, 10), A_READ, 0, at["ZERO"], 1),                 # GET_INTERFACE: alternate 0
        (k1(0x01, 11), A_OK, 0, 0, 0),                            # SET_INTERFACE alternate 0 only
        # HID class descriptors (HID 7.1) and requests (HID 7.2)
        (k1(0x81, 6), A_READ, 0x2200, at["REPD"], len(REPORT)),
        (k1(0x81, 6), A_READ, 0x2100, hid, 9),
        (k1(0xA1, 1), A_READ, 0x0100, at["ZRPT"], 8),             # GET_REPORT input: no key down
        (k1(0x21, 9), A_WRITE, 0x0200, s["V_LED"], 1),            # SET_REPORT output: LEDs
        (k1(0xA1, 2), A_GET | ANY, 0, s["V_IDLE"], 1),            # GET_IDLE
        (k1(0x21, 10), A_SETH | ANY, 0, s["V_IDLE"], 0),          # SET_IDLE: duration in wValue high
        (k1(0xA1, 3), A_GET, 0, s["V_PROTO"], 1),                 # GET_PROTOCOL
        (k1(0x21, 11), A_SET | ANY, 0, s["V_PROTO"], 0),          # SET_PROTOCOL
    ]
    assert s["TABLE"] + 5 * len(table) == s["SENT"], \
        "table has %d entries; the firmware sentinel is at %d" % (len(table), s["SENT"])
    tw = []
    for key, flags, wv, ptr, ln in table:
        tw += [key, flags, wv, ptr, ln]
    tw += [0, ANY | A_STALL, 0, 0, 0]                             # sentinel: K1 written by the firmware

    def stage(pid, ctrl, nbytes, data=()):
        return [1 + len(data), 0x80 | (pid << 8), ctrl, nbytes & 0xFFFF] + list(data)

    mem = [
        (512, desc_words),
        (s["TABLE"], tw),
        (s["ST0"], stage(0x4B, 0x20, 0)),
        (s["ST1"], stage(0xC3, 0x20, 8, [0, 0, 0, 0])),
        (s["STACK"], stage(0xD2, 0, -1)),
        (s["STNAK"], stage(0x5A, 0, -1)),
        (s["STSTALL"], stage(0x1E, 0, -1)),
        (s["STZLP1"], stage(0x4B, 0x20, 0)),
        (s["VARBUF"], [0]),
    ]
    info = dict(at, HID=hid, table=table)
    return mem, info

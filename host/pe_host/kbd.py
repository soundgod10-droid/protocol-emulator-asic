# SPDX-License-Identifier: Apache-2.0
"""Key-report pump for the USB low-speed keyboard firmware (fw/usb_dev.s). MicroPython and CPython.

Host FIFO of thread 0 (fw/usb_dev.s header): host to thread, 4 words = one 8-byte boot keyboard report for
endpoint 1, low byte first; thread to host, event words (low byte code, high byte value): 1 bus reset,
2 suspend, 3 resume, 4 configuration, 5 LED report, 6 endpoint 1 report taken by the host, 7 address.
The firmware holds one report: the pump gives the next one only after event 6 for the last one.
Report format: HID 1.11 appendix B.1 (modifiers, reserved, 6 key codes); key codes: HID Usage Tables,
Keyboard/Keypad page (0x07).
"""

from pe_host.core import ChipError, Fifo

EV_RESET, EV_SUSPEND, EV_RESUME, EV_CONFIG, EV_LED, EV_EP1, EV_ADDR = 1, 2, 3, 4, 5, 6, 7
LEFT_SHIFT = 0x02

_PLAIN = {" ": 0x2C, "\n": 0x28, "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33,
          "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38, "0": 0x27}
_SHIFTED = {"!": 0x1E, "@": 0x1F, "#": 0x20, "$": 0x21, "%": 0x22, "^": 0x23, "&": 0x24, "*": 0x25,
            "(": 0x26, ")": 0x27, "_": 0x2D, "+": 0x2E, "{": 0x2F, "}": 0x30, "|": 0x31, ":": 0x33,
            '"': 0x34, "~": 0x35, "<": 0x36, ">": 0x37, "?": 0x38}


def key_of(ch):
    """(modifier, key code) of one ASCII character (US layout)."""
    o = ord(ch)
    if 0x61 <= o <= 0x7A:
        return 0, 0x04 + o - 0x61
    if 0x41 <= o <= 0x5A:
        return LEFT_SHIFT, 0x04 + o - 0x41
    if 0x31 <= o <= 0x39:
        return 0, 0x1E + o - 0x31
    if ch in _PLAIN:
        return 0, _PLAIN[ch]
    if ch in _SHIFTED:
        return LEFT_SHIFT, _SHIFTED[ch]
    raise ValueError("no key for %r" % ch)


def report(mod, key):
    return bytes([mod, 0, key, 0, 0, 0, 0, 0])


def report_words(r):
    return [r[i] | (r[i + 1] << 8) for i in range(0, 8, 2)]


class Keyboard:
    def __init__(self, chip, thread=0):
        self.chip = chip
        self.fifo = Fifo(chip, thread)
        self.events = []
        self.reports = 0

    def poll(self):
        n = self.fifo.poll()
        self.events += self.fifo.rx
        self.fifo.rx = []
        return n > 0

    def wait_event(self, code, timeout_us):
        t0 = self.chip.t.ticks_us()
        while True:
            for i in range(len(self.events)):
                if self.events[i] & 0xFF == code:
                    return self.events.pop(i)
            if self.chip.t.ticks_us() - t0 > timeout_us:
                raise ChipError("no event %d in %d us (events %s)" % (code, timeout_us,
                                                                      [hex(e) for e in self.events]))
            if not self.poll():
                self.chip.t.sleep_us(20)

    def wait_configured(self, timeout_us):
        return self.wait_event(EV_CONFIG, timeout_us)

    def send_report(self, r, timeout_us):
        self.fifo.send(report_words(r))
        self.wait_event(EV_EP1, timeout_us)
        self.reports += 1

    def type_text(self, text, timeout_us=100000):
        """Each character is one keystroke: its key-down report, then the all-keys-up report."""
        up = report(0, 0)
        for ch in text:
            self.send_report(report(*key_of(ch)), timeout_us)
            self.send_report(up, timeout_us)
        return len(text)

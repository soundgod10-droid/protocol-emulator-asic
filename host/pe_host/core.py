# SPDX-License-Identifier: Apache-2.0
"""Host side of the chip's SPI port (docs/host_port.md), for the RP2350 and for CPython.

The same file runs under MicroPython on the board and under CPython in the test bench. It uses only the
language core: no struct, no typing, no f-string features beyond MicroPython's.

A transport gives three things:
  xfer(out_bytes) -> in_bytes   one transaction: CS_N low, the bytes MSB first in SPI mode 0, CS_N high;
  sleep_us(n)                   wait n microseconds of chip time;
  ticks_us()                    a microsecond counter (for time-outs).
On the board the transport is pio_spi.PioSpi; in the test bench it drives the pins of the simulated chip.

Written from docs/host_port.md, not from test/host_spi.py (the test model) or the RTL, so that a misreading
of the document does not pass both.
"""

READ_ID = 0x1E
READ_STATUS = 0x2D
STOP = 0x3C
START = 0x4B
WRITE_MEM = 0x5A
READ_MEM = 0x69
WRITE_CFG = 0x78
PUSH = 0x87
PULL = 0x96
CLEAR = 0xA5

CHIP_ID = (0x50, 0x45, 0x02, 0x03)   # host_port.md 4: "PE" and two version bytes
FIFO_WORDS = 4                        # host_port.md 5, 9: each FIFO holds 4 words
CFG_START, CFG_RUN, CFG_PIN = 0, 4, 16


class ChipError(Exception):
    pass


def _hi_lo(words):
    out = bytearray()
    for w in words:
        out.append((w >> 8) & 0xFF)
        out.append(w & 0xFF)
    return out


def _words(b, start):
    return [(b[i] << 8) | b[i + 1] for i in range(start, len(b) - 1, 2)]


class Status:
    """The 7 status bytes (host_port.md 5)."""

    def __init__(self, b):
        self.raw = bytes(b)
        self.running = b[0] & 1
        self.cmd_err = (b[0] >> 1) & 1
        self.push_drop = (b[0] >> 2) & 1
        self.run = b[1] & 0xF
        self.late = b[2] & 0xF
        self.err = (b[2] >> 4) & 0xF
        self.in_cnt = [b[3 + n] & 0x7 for n in range(4)]
        self.out_cnt = [(b[3 + n] >> 4) & 0x7 for n in range(4)]

    def __repr__(self):
        return ("Status(running=%d cmd_err=%d push_drop=%d run=%x late=%x err=%x in=%s out=%s)" %
                (self.running, self.cmd_err, self.push_drop, self.run, self.late, self.err, self.in_cnt,
                 self.out_cnt))


class Chip:
    """Commands of host_port.md 4 with the host rules of sections 8 and 9."""

    def __init__(self, transport, reset_pin=None):
        self.t = transport
        self.reset_pin = reset_pin          # callable(level) for rst_n, or None (board SDK resets)
        self.image = None

    # ----- single commands -----

    def read_id(self):
        return tuple(self.t.xfer(bytes([READ_ID, 0, 0, 0, 0]))[1:5])

    def status(self, n=7):
        b = self.t.xfer(bytes([READ_STATUS]) + bytes(n))[1:]
        return Status(bytes(b) + bytes(7 - n))

    def stop(self):
        self.t.xfer(bytes([STOP]))

    def start(self):
        self.t.xfer(bytes([START]))

    def clear(self):
        self.t.xfer(bytes([CLEAR]))

    def write_mem(self, addr, words, chunk=64):
        for i in range(0, len(words), chunk):
            a = addr + i
            self.t.xfer(bytes([WRITE_MEM, (a >> 8) & 0x07, a & 0xFF]) + _hi_lo(words[i:i + chunk]))

    def read_mem(self, addr, n, chunk=64):
        out = []
        for i in range(0, n, chunk):
            a, k = addr + i, min(chunk, n - i)
            b = self.t.xfer(bytes([READ_MEM, (a >> 8) & 0x07, a & 0xFF, 0]) + bytes(2 * k))
            out += _words(b, 4)
        return out

    def write_cfg(self, addr, words):
        self.t.xfer(bytes([WRITE_CFG, addr & 0x7F]) + _hi_lo(words))

    def push(self, thread, words):
        """PUSH of at most FIFO_WORDS words; the caller keeps the count (host_port.md 9)."""
        self.t.xfer(bytes([PUSH, thread & 3]) + _hi_lo(words))

    def pull(self, thread):
        """PULL that clocks all 4 word slots (host_port.md 9: words after the count K are 0 and not taken).
        Returns the K valid words."""
        b = self.t.xfer(bytes([PULL, thread & 3, 0]) + bytes(2 * FIFO_WORDS))
        k = b[2]
        if k > FIFO_WORDS:
            raise ChipError("PULL count byte %d > %d" % (k, FIFO_WORDS))
        return _words(b, 3)[:k]

    # ----- composite operations -----

    def check_id(self):
        got = self.read_id()
        if got != CHIP_ID:
            raise ChipError("READ_ID %s, expected %s" % (" ".join("%02X" % x for x in got),
                                                         " ".join("%02X" % x for x in CHIP_ID)))
        return got

    def reset(self, cycles_us=2):
        """rst_n low for at least 10 clock cycles (host_port.md 8 rule 6); 2 us is 50 cycles at 25 MHz."""
        if self.reset_pin is None:
            return
        self.reset_pin(0)
        self.t.sleep_us(cycles_us)
        self.reset_pin(1)
        self.t.sleep_us(1)

    def load(self, image, chip_clock_hz, verify=True):
        """The reload sequence of host_port.md 8 rule 6: reset, WRITE_MEM of every word the image uses,
        WRITE_CFG, READ_MEM check, then START. The image must be built for the chip clock (section 11)."""
        if image["clock_hz"] != chip_clock_hz:
            raise ChipError("image %s is built for %d Hz, the chip runs at %d Hz" % (
                image["name"], image["clock_hz"], chip_clock_hz))
        self.reset()
        self.check_id()
        st = self.status(1)
        if st.running:
            self.stop()
        for addr, words in image["mem"]:
            self.write_mem(addr, words)
        for addr, words in image["cfg"]:
            self.write_cfg(addr, words)
        if verify:
            for addr, words in image["mem"]:
                got = self.read_mem(addr, len(words))
                if got != list(words):
                    bad = [i for i in range(len(words)) if got[i] != words[i]]
                    raise ChipError("READ_MEM check of %s: %d words differ, first at %d (%04X, wrote %04X)" % (
                        image["name"], len(bad), addr + bad[0], got[bad[0]], words[bad[0]]))
        st = self.status(3)
        if st.cmd_err:
            raise ChipError("CMD_ERR after the load: %r" % st)
        self.start()
        st = self.status(3)
        if not st.running:
            raise ChipError("START: threads not running: %r" % st)
        self.image = image
        return st


class Fifo:
    """Host side of one thread's FIFOs: PUSH with its own free count (host_port.md 9, 10), PULL into a list."""

    def __init__(self, chip, thread):
        self.chip, self.thread = chip, thread
        self.free = 0
        self.rx = []

    def send(self, words, timeout_us=200000):
        i = 0
        t0 = self.chip.t.ticks_us()
        while i < len(words):
            if self.free == 0:
                self.free = FIFO_WORDS - self.chip.status(4 + self.thread).in_cnt[self.thread]
                if self.free == 0:
                    if self.chip.t.ticks_us() - t0 > timeout_us:
                        raise ChipError("thread %d takes no words" % self.thread)
                    self.chip.t.sleep_us(2)
                    continue
            n = min(self.free, len(words) - i)
            self.chip.push(self.thread, words[i:i + n])
            self.free -= n
            i += n

    def poll(self):
        """One PULL; returns the number of new words."""
        w = self.chip.pull(self.thread)
        self.rx += w
        return len(w)

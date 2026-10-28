# SPDX-License-Identifier: Apache-2.0
"""Independent host model of the host SPI port (docs/host_port.md, M2c).

What it does:
  It is the host side of the protocol in docs/host_port.md, written from
  that document and not from the RTL (src/pe_host_port.v):
  - command codes, byte formats, the status bytes and the chip ID;
  - SpiTimed: a host with real-time pin edges. SPI mode 0, MSB first. The
    rising SCK edges are 1 ns after a rising clock edge (the worst phase
    for the 2-stage synchronizers: the edge is taken 19 ns later). The host
    samples MISO allow_ns before each rising SCK edge. allow_ns stands for
    the input pad, the output pad and the set-up time of the host, which
    the RTL simulation does not have (docs/host_port.md, maximum SCK);
  - cycle_schedule(): the pins of a list of transactions for each clock
    cycle, for the lockstep tests (test/test_core.py). The test bench
    changes the pins at the falling clock edge, so the cycle in which the
    port acts on a byte is known: 2 cycles (synchronizer) after the cycle
    of the rising SCK edge of the last bit of the byte.

Why:
  The tests must decide pass or fail from the chip pins and from a model
  that does not share code or mistakes with the RTL.
"""

from cocotb.triggers import RisingEdge, Timer

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
ALL_COMMANDS = (READ_ID, READ_STATUS, STOP, START, WRITE_MEM, READ_MEM, WRITE_CFG, PUSH, PULL, CLEAR)

CHIP_ID = [0x50, 0x45, 0x02, 0x03]  # "PE", milestone 2, run c (docs/host_port.md)
STATUS_BYTES = 7

SCK_BIT, CS_N_BIT, MOSI_BIT, MISO_BIT = 7, 6, 5, 7  # ui_in[7:5], uo_out[7] (A1 section 2.2)


def code_ok(byte):
    """docs/host_port.md: each command code is {c, not c}."""
    return (byte & 0x0F) == ((~byte >> 4) & 0x0F)


def words_to_bytes(words):
    out = []
    for w in words:
        out += [(w >> 8) & 0xFF, w & 0xFF]
    return out


def bytes_to_words(data):
    return [(data[2 * i] << 8) | data[2 * i + 1] for i in range(len(data) // 2)]


def parse_status(b):
    """The 7 status bytes (docs/host_port.md) as a dict."""
    return {
        "running": b[0] & 1,
        "cmd_err": (b[0] >> 1) & 1,
        "push_drop": (b[0] >> 2) & 1,
        "byte0_rest": b[0] >> 3,
        "run": [(b[1] >> n) & 1 for n in range(4)],
        "late": [(b[2] >> n) & 1 for n in range(4)],
        "err": [(b[2] >> (4 + n)) & 1 for n in range(4)],
        "in_cnt": [b[3 + n] & 0x7 for n in range(4)],
        "out_cnt": [(b[3 + n] >> 4) & 0x7 for n in range(4)],
    }


# Transactions as lists of host bytes (MOSI). Response bytes come back in the
# same positions (MISO).

def t_write_mem(addr, words):
    return [WRITE_MEM, addr & 0x7F] + words_to_bytes(words)


def t_read_mem(addr, n):
    return [READ_MEM, addr & 0x7F] + [0] * (2 * n)


def t_write_cfg(addr, words):
    return [WRITE_CFG, addr & 0x7F] + words_to_bytes(words)


def t_push(thread, words):
    return [PUSH, thread & 3] + words_to_bytes(words)


def t_pull(thread, n):
    """PULL: thread byte, count byte (response), then n words (response)."""
    return [PULL, thread & 3, 0] + [0] * (2 * n)


def config_words(start_pc, run, owner, od):
    """WRITE_CFG data: addresses 0 to 4 (start addresses, RUN mask) and 16 to 30 (pins)."""
    head = [pc & 0x7F for pc in start_pc] + [sum(1 << n for n in range(4) if run[n])]
    pins = []
    for p in range(15):
        own = owner[p]
        pins.append(((own or 0) & 3) | ((own is not None) << 2) | (bool(od[p]) << 3))
    return head, pins


class SpiTimed:
    """Host with real-time pin edges (see the module text)."""

    def __init__(self, dut, period_ns=100, allow_ns=27.4, lag_ns=1, clk=None):
        self.dut = dut
        self.clk = clk if clk is not None else dut.clk
        self.period_ns = period_ns
        self.half_ns = period_ns / 2
        self.allow_ns = allow_ns
        self.lag_ns = lag_ns
        self.inputs = 0  # ui_in[4:0]: processor inputs
        self.sck, self.cs_n, self.mosi = 0, 1, 0
        self._drive()

    def _drive(self):
        self.dut.ui_in.value = ((self.sck << SCK_BIT) | (self.cs_n << CS_N_BIT) | (self.mosi << MOSI_BIT)
                                | (self.inputs & 0x1F))

    @staticmethod
    async def _wait(ns):
        # Whole picoseconds: the simulator precision is 1 ps (12.6 ns as a float is not exact).
        await Timer(round(ns * 1000), unit="ps")

    def miso(self):
        v = self.dut.uo_out.value
        assert v.is_resolvable, f"uo_out has X or Z: {v}"
        return (v.to_unsigned() >> MISO_BIT) & 1

    async def transaction(self, out_bytes, reply=None):
        """Sends out_bytes in one CS_N low period and returns the MISO bytes.

        reply(k, in_bytes) may give a new list of out bytes after byte k (for
        example a PULL that reads only the words the count byte gives).
        """
        await RisingEdge(self.clk)
        await self._wait(self.lag_ns)
        # CS_N low; the first rising SCK edge is 3 clock cycles later.
        self.cs_n, self.sck, self.mosi = 0, 0, (out_bytes[0] >> 7) & 1 if out_bytes else 0
        self._drive()
        await self._wait(60 - self.half_ns if self.half_ns < 60 else 10)
        in_bytes = []
        k = 0
        out = list(out_bytes)
        while k < len(out):
            value = 0
            for i in range(8):
                self.mosi = (out[k] >> (7 - i)) & 1
                self._drive()
                await self._wait(self.half_ns - self.allow_ns)
                value = (value << 1) | self.miso()  # MISO allow_ns before the rising edge
                await self._wait(self.allow_ns)
                self.sck = 1
                self._drive()
                await self._wait(self.half_ns)
                self.sck = 0
                self._drive()
            in_bytes.append(value)
            if reply is not None:
                new = reply(k, in_bytes)
                if new is not None:
                    out = out[:k + 1] + list(new)
            k += 1
        # CS_N high 60 ns after the last rising edge (low phase + the rest of 60 ns).
        await self._wait(max(10, 60 - self.half_ns))
        self.cs_n, self.mosi = 1, 0
        self._drive()
        await self._wait(60)
        return in_bytes

    # ----- commands (docs/host_port.md) -----

    async def read_id(self):
        return (await self.transaction([READ_ID, 0, 0, 0, 0]))[1:]

    async def read_status(self, n=STATUS_BYTES):
        return parse_status((await self.transaction([READ_STATUS] + [0] * n))[1:] + [0] * (STATUS_BYTES - n))

    async def stop(self):
        await self.transaction([STOP])

    async def start(self):
        await self.transaction([START])

    async def clear(self):
        await self.transaction([CLEAR])

    async def write_mem(self, addr, words):
        await self.transaction(t_write_mem(addr, words))

    async def read_mem(self, addr, n):
        return bytes_to_words((await self.transaction(t_read_mem(addr, n)))[2:])

    async def write_cfg(self, addr, words):
        await self.transaction(t_write_cfg(addr, words))

    async def push(self, thread, words):
        await self.transaction(t_push(thread, words))

    async def pull(self, thread, max_words=4):
        """Reads the count byte, then only the words that it gives (at most max_words)."""
        def reply(k, got):
            if k == 2:
                n = min(got[2], max_words)
                return [0] * (2 * n)
            return None
        got = await self.transaction([PULL, thread & 3, 0], reply=reply)
        return bytes_to_words(got[3:]), got[2]


# ----- per-cycle schedule for the lockstep tests -----

class Schedule:
    """Pins for each test-bench cycle, and the cycles in which the port acts.

    pins[t] = (sck, cs_n, mosi) for test-bench cycle t (set at the falling
    clock edge of cycle t). acts[t] = list of (txn, byte index, out byte):
    the port acts on that byte at the clock edge at the end of cycle t.
    samples[t] = (txn, byte index, bit index): the host takes MISO in
    cycle t (the cycle of the rising SCK edge).
    """

    SYNC = 2  # synchronizer stages: an edge driven in cycle t is seen in cycle t + 2

    def __init__(self, period=4):
        assert period >= 4 and period % 2 == 0, "each SCK level must last 2 clock cycles or more"
        self.period = period
        self.pins = []
        self.acts = {}
        self.samples = {}
        self.txns = []  # (label, out bytes)

    def idle(self, cycles):
        self.pins += [(0, 1, 0)] * cycles

    def add(self, out_bytes, label=""):
        n = len(self.txns)
        self.txns.append((label, list(out_bytes)))
        self.pins += [(0, 0, 0)] * 2  # CS_N low 2 cycles before the first SCK edge
        half = self.period // 2
        for k, b in enumerate(out_bytes):
            for i in range(8):
                bit = (b >> (7 - i)) & 1
                self.pins += [(0, 0, bit)] * half
                t_rise = len(self.pins)
                self.samples[t_rise] = (n, k, i)
                if i == 7:
                    self.acts.setdefault(t_rise + self.SYNC, []).append((n, k, b))
                self.pins += [(1, 0, bit)] * half
        self.pins += [(0, 0, 0)] * 2  # CS_N stays low 2 cycles after the last high phase
        self.pins += [(0, 1, 0)] * 2  # CS_N high 2 cycles
        return n

    def act_cycle(self, txn, byte):
        for t, lst in self.acts.items():
            for n, k, _ in lst:
                if n == txn and k == byte:
                    return t
        raise KeyError((txn, byte))

"""Chip-level tests of the two line helpers for the transmit, receive and timer paths that no other test drives.

Each test runs the whole chip: a thread drives the helper through its I/O ports (USB helper 16-31, 10BASE-Te helper
32-47; docs/helpers.md), the pins come from the pin configuration, and the test checks the documented behaviour
(STATUS flags, TX state, the words received, the levels on the pins). They are the tests for the reachable coverage
holes of the chip copies of src/pe_slu.v (coverage/scope.py lists the holes; the points proved unreachable are in
coverage/exclusions.csv).

Two timers are too long to run through in a simulation: the 10BASE-Te jabber (2^22 cycles of transmission, 2^25
idle cycles) and the 16-bit USB line timer of condition B (65,535 ticks of 16 cycles). Their tests write the timer
register near its end (stated in each test) and check what the helper does from there. RTL only: the gate-level
netlist has no such register names.
"""

import os
import random

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotb.utils import get_sim_time

import test_slu as ts

GL = os.environ.get("GATES") == "yes"
ref, hs, pe_asm = ts.ref, ts.hs, ts.pe_asm
T_PS = ts.CHIP_CLK_PS
DIN, AUX0, AUX1 = 0, 1, 2                      # ui_in bits used as DIN / AUX0 / AUX1 below
TXDONE, TXLATE, TXUNDER, RXEOP, RXOVER, LINEB, LINKPULSE, JABBER = (1 << 0, 1 << 1, 1 << 2, 1 << 5, 1 << 9, 1 << 11,
                                                                   1 << 13, 1 << 15)


async def pull_words(dut, host, n, max_cycles=400000):
    """Words that thread 0 pushes, until there are n of them."""
    got, waited = [], 0
    while True:
        words, _ = await host.pull(0)
        got += words
        if len(got) >= n:
            return got
        assert waited < max_cycles, "thread 0 pushed %d words, %d expected: %s" % (len(got), n, got)
        await ClockCycles(dut.clk, 1000)
        waited += 1000


def capture(dut, store, n, meta=None):
    """uio_out after each falling clock edge, n cycles (background task); meta["t0"] = time of the first sample (ns)."""
    async def run():
        for i in range(n):
            await FallingEdge(dut.clk)
            if i == 0 and meta is not None:
                meta["t0"] = get_sim_time("ns")
            store.append(int(dut.uio_out.value))
    return cocotb.start_soon(run())


def window_times(meta, win):
    """Simulation times (ns) of the falling edges that sample the first and the last high cycle of each window."""
    return ["%.3f-%.3f ns" % (meta["t0"] + s * T_PS / 1000.0, meta["t0"] + (s + n - 1) * T_PS / 1000.0)
            for s, n in win]


def oe_windows(caps, bit):
    """(start, length) of each run of cycles with uio[bit] high, after the first one: a helper pin shows its reset level
    (1) while the threads are stopped, so the capture, which starts before START, begins with a high run."""
    out, start = [], 0 if (caps and (caps[0] >> bit) & 1) else None
    skip = start == 0
    for i, v in enumerate(caps + [0]):
        on = (v >> bit) & 1
        if on and start is None:
            start = i
        elif not on and start is not None:
            if not skip:
                out.append((start, i - start))
            skip = False
            start = None
    return out


def word_bits(words_and_counts):
    return [(w >> i) & 1 for w, n in words_and_counts for i in range(n)]


def tx_rules_fw(e):
    """Transmit start and buffer rules (docs/helpers.md section 2), in order: (a) a late TXTIME with an empty buffer;
    (b) a late TXTIME with TXCTRL bit 6; (c) a TXTIME inside the lead window, then an abort while armed; (d) three
    words into the two-word buffer; (e) a start with no last word (the buffer runs empty inside the frame); (f) a frame
    of 16 + 8 bits without CRC. The thread pushes the STATUS word or the TX state after each step."""
    return """
        .equ    E, %d
        LDI     r3, 0
        ADDI    r3, -1          ; r3 = FFFF
        IOW     r3, E+2         ; clear the sticky flags
        LDI     r0, 0
        IOW     r0, E+4         ; TXCTRL 0
        TNOW    0
        TGET    r0
        IOW     r0, E+5         ; (a) TXTIME = T, written 8 cycles after T: late
wa:     IOR     r1, E+2
        ANDI    r1, 4
        BEQ     wa              ; TXUNDER: the start found no word
        IOR     r1, E+2
        PUSH    r1
        IOW     r3, E+2
        LDI     r0, 64
        IOW     r0, E+4         ; TXCTRL bit 6: no start when late
        TNOW    0
        TGET    r0
        IOW     r0, E+5         ; (b) late again
        LDI     r2, 6
wb:     ADDI    r2, -1
        BEQ     wb2
        JMP     wb
wb2:    IOR     r1, E+2
        PUSH    r1
        IOR     r1, E+4
        PUSH    r1              ; TX state
        IOW     r3, E+2
        LDI     r0, 0
        IOW     r0, E+4
        LDI     r0, 0x155
        IOW     r0, E+0         ; one word in the buffer
        TNOW    0
        TGET    r0
        ADDI    r0, 100
        ADDI    r0, 100
        IOW     r0, E+5         ; (c) TXTIME = T + 200: armed
        IOR     r1, E+4
        PUSH    r1              ; TX state: armed
        LDI     r0, 256
        IOW     r0, E+4         ; TXCTRL bit 8: abort
        IOR     r1, E+4
        PUSH    r1              ; TX state: idle
        IOR     r1, E+2
        PUSH    r1
        LDI     r0, 1
        SHL     r0, 10
        IOW     r0, E+4         ; TXCTRL bit 10: empty the buffer
        LDI     r0, 0x111
        IOW     r0, E+0
        LDI     r0, 0x222
        IOW     r0, E+0
        LDI     r0, 0x333
        IOW     r0, E+0         ; (d) third word: the buffer is full
        IOR     r1, E+0
        PUSH    r1              ; free words
        IOW     r3, E+2
        LDI     r0, 512
        IOW     r0, E+4         ; (e) start now, no CRC, no last word in the buffer
we:     IOR     r1, E+2
        ANDI    r1, 1
        BEQ     we
        IOR     r1, E+2
        PUSH    r1
        IOW     r3, E+2
        LDI     r0, 0x055
        IOW     r0, E+0
        LDI     r0, 0x0AB
        IOW     r0, E+14        ; TXLAST
        LDI     r0, 520
        IOW     r0, E+4         ; (f) start now, 8 bits in the last word, no CRC
wf:     IOR     r1, E+2
        ANDI    r1, 1
        BEQ     wf
        IOR     r1, E+2
        PUSH    r1
        HALT
""" % e


async def tx_rules(dut, eth):
    port = ts.ETH_PORT if eth else ts.USB_PORT
    prog = pe_asm.assemble(tx_rules_fw(port), "tx_rules")
    pins = [0] * 15
    pins[0], pins[1] = ts.helper_pin(eth, 0), ts.helper_pin(eth, 1)
    pins[2] = ts.helper_pin(eth, 4)                                  # uio2: OE
    host = await ts.chip_start(dut, prog, [], pins)
    caps, meta = [], {}
    task = capture(dut, caps, 12000 if eth else 20000, meta)
    await host.start()
    st_a, st_b, state_b, state_c1, state_c2, st_c, free_d, st_e, st_f = await pull_words(dut, host, 9)
    await task
    name = "10BASE-Te" if eth else "USB"
    dut._log.info("%s helper in the chip: (a) late, empty: status %04x; (b) late, TXCTRL bit 6: status %04x, state %d; "
                  "(c) armed state %d, after abort %d, status %04x; (d) free words after 3 writes %d; (e) underflow: "
                  "status %04x; (f) 16 + 8 bits, no CRC: status %04x" % (name, st_a, st_b, state_b, state_c1, state_c2,
                                                                         st_c, free_d, st_e, st_f))
    assert st_a & 7 == TXLATE | TXUNDER, "(a) late TXTIME with an empty buffer: TXLATE and TXUNDER, no TXDONE"
    assert st_b & 7 == TXLATE and state_b & 0xF == 0, "(b) late with TXCTRL bit 6: TXLATE, no start"
    assert state_c1 & 0xF == 1 and state_c2 & 0xF == 0 and st_c & 7 == 0, "(c) armed (state 1), abort to idle (0)"
    assert free_d & 3 == 0, "(d) the buffer holds two words: a third write leaves 0 free words"
    assert st_e & 7 == TXDONE | TXUNDER, "(e) a frame that runs out of words: TXUNDER, tail, TXDONE"
    assert st_f & 7 == TXDONE, "(f) a frame of 16 + 8 bits: TXDONE"
    if not eth:
        return
    win = oe_windows(caps, 2)
    assert len(win) == 2, "two frames on the pins, OE windows %s" % win
    # (e): 0111 and 0222 (the third word was dropped), then the 16-cycle tail; (f): 0055, then 8 bits of 00AB
    for (first, n), words in zip(win, ([(0x111, 16), (0x222, 16)], [(0x055, 16), (0x0AB, 8)])):
        exp = ref.manchester_halves(word_bits(words))
        td = [(caps[first + 3 * i + 1] & 1) for i in range(len(exp))]
        assert td == exp, "uio0 differs from the Manchester halves of %s" % words
        assert n == 3 * len(exp) + 16, "OE for %d cycles, expected %d bits of 6 cycles + the 16-cycle tail" % (
            n, len(exp) // 2)
    dut._log.info("10BASE-Te pins: frame (e) = 0111, 0222 (the third write was dropped), %d OE cycles; frame (f) = 0055 "
                  "and 8 bits of 00AB with no CRC, %d OE cycles; OE windows (falling-edge samples) %s" % (
                      win[0][1], win[1][1], window_times(meta, win)))


@cocotb.test(skip=GL)
async def test_slu_cov_eth_tx_rules(dut):
    """10BASE-Te helper in the chip, transmit rules: a late TXTIME sets TXLATE and starts at write + 4 (TXUNDER with an
    empty buffer), or does not start with TXCTRL bit 6; a TXTIME inside the lead window arms the transmitter until the
    abort (TXCTRL bit 8); a third word into the full two-word buffer is dropped; a frame that runs out of words ends with
    TXUNDER and the tail; a frame of 16 + 8 bits without CRC is sent as such (Manchester halves on uio0)."""
    await tx_rules(dut, True)


@cocotb.test(skip=GL)
async def test_slu_cov_usb_tx_rules(dut):
    """USB low-speed helper in the chip, the same transmit rules as test_slu_cov_eth_tx_rules (STATUS and TX state)."""
    await tx_rules(dut, False)


ETH_RX_HOLD_FW = """
        .equ    E, 32
        LDI     r0, 8
        IOW     r0, E+12        ; CFGADR = 8 (SEL)
        LDI     r0, 0x398       ; DIN = input 8 (ui_in[0]), AUX0 = input 9 (ui_in[1])
        IOW     r0, E+13
        LDI     r3, 0
        ADDI    r3, -1
        IOW     r3, E+2
        PUSH    r3              ; ready
wr:     IOR     r1, E+2
        ANDI    r1, 0x20
        BEQ     wr              ; RXEOP; no word is read
        IOR     r1, E+2
        PUSH    r1
        IOR     r1, E+6
        PUSH    r1              ; RXINFO
        IOW     r3, E+2
        HALT
"""


async def drive_eth_frame(dut, payload, ppm, rng, jit=0.0):
    """A frame on ui_in[0] (slicer output) with the squelch comparator on ui_in[1], as test_slu_chip_eth_rx."""
    bits = ref.eth_frame_bits(payload)
    lv0, ev = ts.manchester_edges(bits, 100.0, ppm, jit, rng, t0=2000.0)
    ui = 1 << hs.CS_N_BIT
    base = get_sim_time("ns")
    events = [(1700.0, 0, lv0), (1700.0, 1, 1)] + [(t, 0, lv) for t, lv in ev]
    tend = sorted(ev)[-1][0] + 100.0
    events += [(tend, 0, 0), (tend + 300.0, 1, 0)]
    level = {0: 0, 1: 0}
    for t, pin, lv in sorted(events):
        dt = base + t - get_sim_time("ns")
        if dt > 0:
            await Timer(round(dt * 1000), unit="ps")
        level[pin] = lv
        dut.ui_in.value = ui | level[0] | (level[1] << 1)


@cocotb.test(skip=GL)
async def test_slu_cov_eth_rx_overflow(dut):
    """10BASE-Te helper in the chip: a 64-byte frame that the thread does not read keeps two words in the receive buffer
    and sets RXOVER (STATUS bit 9, words lost); RXINFO[6:5] = 2. Then a high on TD+ with the squelch comparator active
    for 100 cycles is too long for a link test pulse (2 to 12 cycles): no LINKPULSE, and the pulse-width counter
    stops at its limit of 63."""
    rng = random.Random(61)
    prog = pe_asm.assemble(ETH_RX_HOLD_FW, "eth_rx_hold")
    host = await ts.chip_start(dut, prog, [], [0] * 15)
    await host.start()
    await pull_words(dut, host, 1)
    await drive_eth_frame(dut, bytes(rng.randrange(256) for _ in range(64)), 50.0, rng, 10.0)
    await Timer(5000 * 1000, unit="ps")
    st, info = await pull_words(dut, host, 2)
    dut._log.info("frame not read: status %04x, RXINFO %04x" % (st, info))
    assert st & RXEOP and st & RXOVER, "RXEOP and RXOVER, status %04x" % st
    assert (info >> 5) & 3 == 2, "two words kept, RXINFO %04x" % info
    eth = dut.user_project.u_proc.u_eth
    await RisingEdge(dut.clk)
    await Timer(2, unit="ns")
    dut.ui_in.value = (1 << hs.CS_N_BIT) | 3                       # TD+ high, squelch active
    widest = 0
    for _ in range(100):
        await RisingEdge(dut.clk)
        widest = max(widest, int(eth.lp_w.value))
    await Timer(2, unit="ns")
    dut.ui_in.value = (1 << hs.CS_N_BIT) | 2
    await ClockCycles(dut.clk, 20)
    dut.ui_in.value = 1 << hs.CS_N_BIT
    await ClockCycles(dut.clk, 20)
    sticky = int(eth.sticky.value)
    dut._log.info("100-cycle high on TD+: pulse-width counter up to %d; sticky flags %04x" % (widest, sticky))
    assert widest == 63, "pulse-width counter limit 63, reached %d" % widest
    assert not sticky & LINKPULSE, "a 100-cycle high is no link test pulse"


ETH_JABBER_FW = """
        .equ    E, 32
        LDI     r3, 0
        ADDI    r3, -1
        IOW     r3, E+2
        LDI     r0, 0x155
        IOW     r0, E+0
        IOW     r0, E+0
        LDI     r0, 512
        IOW     r0, E+4         ; start now, no CRC, no last word: the thread feeds words
feed:   IOR     r1, E+2
        SHR     r1, 15
        CMPI    r1, 0
        BEQ     more
        JMP     jab
more:   IOR     r1, E+0
        CMPI    r1, 0
        BEQ     feed
        LDI     r0, 0x155
        IOW     r0, E+0
        JMP     feed
jab:    IOR     r1, E+2
        PUSH    r1              ; JABBER
        LDI     r0, 1
        SHL     r0, 10
        IOW     r0, E+4         ; TXCTRL bit 10: empty the buffer
        LDI     r0, 0x155
        IOW     r0, E+0
        TNOW    0
        TGET    r0
        ADDI    r0, 100
        IOW     r0, E+5         ; TXTIME inside the lead window, while JABBER is set
        IOR     r1, E+4
        PUSH    r1              ; TX state
wj:     IOR     r1, E+2
        SHR     r1, 15
        CMPI    r1, 0
        BEQ     unj
        JMP     wj
unj:    IOR     r1, E+2
        PUSH    r1              ; JABBER cleared
        HALT
"""


@cocotb.test(skip=GL)
async def test_slu_cov_eth_jabber(dut):
    """10BASE-Te helper in the chip, jabber (IEEE 802.3 14.2.1.6): an unbroken transmission stops at 8,192 ticks of 512
    cycles (2^22 cycles) with JABBER set; a frame armed while JABBER is set does not start; JABBER clears 65,536 ticks
    later. Both counts are too long to simulate (69.9 ms and 0.56 s at 60 MHz): the test writes the tick counter to
    8,190 while the thread feeds words (two ticks before the limit) and, once JABBER is set and a frame is armed, to
    65,535 (one tick before the end). The full 2^22-cycle count runs in test_slu.test_slu_jabber on the same RTL."""
    prog = pe_asm.assemble(ETH_JABBER_FW, "eth_jabber")
    pins = [0] * 15
    pins[0], pins[2] = ts.helper_pin(1, 0), ts.helper_pin(1, 4)
    host = await ts.chip_start(dut, prog, [], pins)
    eth = dut.user_project.u_proc.u_eth
    await host.start()
    for _ in range(4000):
        await RisingEdge(dut.clk)
        if int(eth.tx_st.value) == 2:
            break
    assert int(eth.tx_st.value) == 2, "test fault: the frame did not start"
    await ClockCycles(dut.clk, 50)
    eth.jab_cnt.value = 8190                                        # register write: two ticks before the limit
    t0 = get_sim_time("ns")
    st1, state = await pull_words(dut, host, 2)
    t1 = get_sim_time("ns")
    caps = []
    await capture(dut, caps, 3000)
    oe_while_jabber = sum((v >> 2) & 1 for v in caps)
    eth.jab_cnt.value = 0xFFFF                                      # register write: one tick before the unjab
    (st2,) = await pull_words(dut, host, 1)
    dut._log.info("JABBER after the counter write: status %04x within %.0f ns (pull included); TX state after the "
                  "TXTIME write %d; OE on uio2 for %d of 3,000 cycles while JABBER; after the unjab write: status %04x" % (
                      st1, t1 - t0, state, oe_while_jabber, st2))
    assert st1 & JABBER, "JABBER after 8,192 ticks of transmission, status %04x" % st1
    assert state & 0xF in (0, 1), "TX state after the TXTIME write %d" % state
    assert oe_while_jabber == 0, "no frame on the pins while JABBER is set"
    assert not st2 & JABBER, "JABBER clears at the 65,536th tick, status %04x" % st2


USB_MAN_FW = """
        .equ    E, 16
        LDI     r3, 0
        ADDI    r3, -1
        IOW     r3, E+2
        LDI     r0, 0
        IOW     r0, E+12        ; CFGADR 0
        LDI     r0, 2
        IOW     r0, E+13        ; MODE: Manchester code, RX off, not the USB line
        LDI     r0, 3
        IOW     r0, E+13        ; TXPER 3: half a bit = 3 cycles
        LDI     r0, 0x055
        IOW     r0, E+0
        LDI     r0, 0x0AB
        IOW     r0, E+14
        LDI     r0, 520
        IOW     r0, E+4         ; start now, 8 bits in the last word
w1:     IOR     r1, E+2
        ANDI    r1, 1
        BEQ     w1
        IOR     r1, E+2
        PUSH    r1
        IOW     r3, E+2
        LDI     r0, 640
        IOW     r0, E+4         ; TXCTRL: link test pulse (bit 7), start now (bit 9)
w2:     IOR     r1, E+2
        ANDI    r1, 1
        BEQ     w2
        IOR     r1, E+2
        PUSH    r1
        HALT
"""


@cocotb.test(skip=GL)
async def test_slu_cov_usb_manchester(dut):
    """USB helper in the chip with the Manchester code (MODE bits 1:0 are writable in the USB form, docs/helpers.md
    section 6): a frame of 16 + 8 bits at 3 cycles a half bit on TXA (uio0) equal to the Manchester halves, then the
    tail (TAIL1 = 84 cycles in the USB form), TXDONE; then a link test pulse (TXCTRL bit 7) ends with TXDONE."""
    prog = pe_asm.assemble(USB_MAN_FW, "usb_man")
    pins = [0] * 15
    pins[0], pins[2] = ts.helper_pin(0, 0), ts.helper_pin(0, 4)
    host = await ts.chip_start(dut, prog, [], pins)
    caps, meta = [], {}
    task = capture(dut, caps, 2000, meta)
    await host.start()
    st1, st2 = await pull_words(dut, host, 2)
    await task
    win = oe_windows(caps, 2)
    exp = ref.manchester_halves(word_bits([(0x055, 16), (0x0AB, 8)]))
    first, n = win[0]
    td = [(caps[first + 3 * i + 1] & 1) for i in range(len(exp))]
    dut._log.info("USB helper, Manchester: OE windows %s at %s; frame status %04x, link pulse status %04x" % (
        win, window_times(meta, win), st1, st2))
    assert td == exp, "uio0 differs from the Manchester halves of 0055, AB"
    assert n == 3 * len(exp) + 84, "OE for %d cycles: 24 bits of 6 cycles + the 84-cycle tail expected" % n
    assert st1 & 7 == TXDONE and st2 & 7 == TXDONE, "TXDONE after the frame and after the link test pulse"
    assert len(win) == 2, "the link test pulse is on the pins after the frame: %s" % win


USB_RXSTATE_FW = """
        .equ    E, 16
        LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 3
        IOW     r0, E+13        ; MODE: clocked code, RX off
        IOR     r1, E+11
        PUSH    r1              ; LINE: RX state in bits 7:4
        LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 11
        IOW     r0, E+13        ; MODE: clocked code, RX on
        IOR     r1, E+11
        PUSH    r1
        LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 3
        IOW     r0, E+13        ; RX off again
        IOR     r1, E+11
        PUSH    r1
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0xFF
        SHL     r0, 4
        IOW     r0, E+13        ; SEL 0x0FF0: DIN = input 0 (uio_in[0]; the host port writes ui_in)
        LDI     r0, 18
        IOW     r0, E+12
        LDI     r0, 0x140
        SHL     r0, 4
        IOW     r0, E+13        ; LTC 0x1400: [12] falling-edge DIN copy, [10] frames start at a DIN falling edge
        LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 11
        IOW     r0, E+13        ; MODE: clocked code, RX on
        LDI     r0, 1
        IOW     r0, E+13        ; TXPER 1
        LDI     r0, 8
        IOW     r0, E+4         ; TXCTRL: 8 bits a frame
        LDI     r3, 0
        ADDI    r3, -1
        IOW     r3, E+2
        PUSH    r3              ; ready for the edge
w:      IOR     r1, E+2
        ANDI    r1, 0x20
        BEQ     w
        IOR     r1, E+6
        PUSH    r1              ; RXINFO
        IOR     r1, E+1
        PUSH    r1              ; the received word
        HALT
"""


@cocotb.test(skip=GL)
async def test_slu_cov_usb_clocked_states(dut):
    """USB helper in the chip, clocked mode receive states (docs/helpers.md section 6): RX off moves the receive state to
    0 (off); RX on in the clocked code moves it from 0 straight to 2 (data, no start pattern); RX off in state 2 moves it
    back to 0. Then an edge-started frame (LTC[10]) with TXPER 1 and the falling-edge DIN copy (LTC[12]): a DIN falling
    edge on uio_in[0] starts a frame of 8 bits, which ends with RXEOP and one word of 8 bits."""
    prog = pe_asm.assemble(USB_RXSTATE_FW, "usb_rxstate")
    host = await ts.chip_start(dut, prog, [], [0] * 15)
    dut.uio_in.value = 1                                           # DIN idle high (uio0 is an input)
    await host.start()
    l_off, l_on, l_off2, _ready = await pull_words(dut, host, 4)
    await ClockCycles(dut.clk, 20)
    await FallingEdge(dut.clk)
    dut.uio_in.value = 0                                           # the start edge, then DIN low for the frame
    await ClockCycles(dut.clk, 40)
    dut.uio_in.value = 1
    await ClockCycles(dut.clk, 200)
    info, word = await pull_words(dut, host, 2)
    dut._log.info("RX state after RX off %d, after clocked RX on %d, after RX off in data %d; edge-started frame: "
                  "RXINFO %04x, word %04x" % ((l_off >> 4) & 15, (l_on >> 4) & 15, (l_off2 >> 4) & 15, info, word))
    assert ((l_off >> 4) & 15, (l_on >> 4) & 15, (l_off2 >> 4) & 15) == (0, 2, 0), "receive states 0, 2, 0"
    assert info & 0x1F == 8, "a frame of 8 bits (RXINFO[4:0] = %d)" % (info & 0x1F)


USB_NRZ_RX_FW = """
        .equ    E, 16
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0xFF
        SHL     r0, 4
        ORI     r0, 8
        IOW     r0, E+13        ; SEL 0x0FF8: DIN = input 8 (ui_in[0])
        LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 8
        IOW     r0, E+13        ; MODE: NRZ code, RX on, no stuffing, not the USB line
        PUSH    r0              ; ready
        LDI     r3, 2
rx:     IOR     r1, E+6
        ANDI    r1, 0x60
        BEQ     rx
        IOR     r0, E+1
        PUSH    r0
        ADDI    r3, -1
        BEQ     fin
        JMP     rx
fin:    HALT
"""


@cocotb.test(skip=GL)
async def test_slu_cov_usb_nrz_rx(dut):
    """USB helper in the chip as an NRZ receiver (MODE code 0 with RX on; the receive loop keeps its USB setting of 40
    samples a bit, 1.5 Mbit/s at 60 MHz): an NRZ line with the start pattern of the USB form (bits 0, 0, 1 after at
    least two edges), then two data words, LSB first; the thread reads both words."""
    prog = pe_asm.assemble(USB_NRZ_RX_FW, "usb_nrz_rx")
    host = await ts.chip_start(dut, prog, [], [0] * 15)
    dut.ui_in.value = (1 << hs.CS_N_BIT) | 0
    await host.start()
    await pull_words(dut, host, 1)
    data = [0x5A3C, 0xC3A5]
    bits = [1, 0] * 8 + [0, 1] + word_bits([(w, 16) for w in data]) + [0, 1, 0, 1]
    await FallingEdge(dut.clk)
    for b in bits:
        dut.ui_in.value = (1 << hs.CS_N_BIT) | b
        await Timer(40 * T_PS, unit="ps")
    got = await pull_words(dut, host, 2)
    dut._log.info("NRZ receive at 40 cycles a bit: words %s, sent %s" % (["%04x" % w for w in got],
                                                                       ["%04x" % w for w in data]))
    assert got == data, "received words differ"


USB_EOPW_FW = """
        .equ    E, 16
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0xA9
        SHL     r0, 4
        ORI     r0, 8
        IOW     r0, E+13        ; SEL 0xA98: DIN = input 8 (RCV), AUX0 = input 9 (VP), AUX1 = input 10 (VM)
        PUSH    r0              ; ready
w3:     IOR     r1, E+11
        SHR     r1, 4
        ANDI    r1, 15
        CMPI    r1, 3
        BEQ     eop
        JMP     w3              ; until the receive state is 3 (EOP wait)
eop:    LDI     r0, 0
        IOW     r0, E+12
        LDI     r0, 0x211
        IOW     r0, E+13        ; MODE: the USB setting with RX off
w0:     IOR     r1, E+11
        SHR     r1, 4
        ANDI    r1, 15
        CMPI    r1, 0
        BEQ     off
        JMP     w0
off:    IOR     r1, E+11
        PUSH    r1
        IOR     r1, E+2
        PUSH    r1
        HALT
"""


@cocotb.test(skip=GL)
async def test_slu_cov_usb_rx_off_in_eop(dut):
    """USB helper in the chip: receive switched off during the EOP of a packet (the thread clears MODE bit 3 while the
    receive state is 3, EOP wait); at the end of the SE0 the receive state goes through 4 (one cycle) to 0 (off), not
    back to 1 (hunt); RXEOP is set as for a normal packet end. The host packet is DATA0 with 2 bytes at the nominal
    rate, its SE0 held 30 us so that the thread sees the EOP state."""
    prog = pe_asm.assemble(USB_EOPW_FW, "usb_eopw")
    host = await ts.chip_start(dut, prog, [], [0] * 15)
    await host.start()
    await pull_words(dut, host, 1)
    line = ref.nrzi(ref.stuff(ref.usb_packet_bits(0xC3, bytes([0x12, 0x34]), "16")))
    sch, _ = ts.usb_host_schedule(line, 1000.0 / 1.5, 2000.0)
    state, end_state, se0_at = {}, {}, None
    for t, pins in sch:
        state.update(pins)
        end_state = dict(state)
        if se0_at is None and t > 2000.0 and state.get(AUX0, 1) == 0 and state.get(AUX1, 1) == 0:
            se0_at = t
    assert se0_at is not None, "test fault: no SE0 in the host schedule"
    long_eop = [(t, p) for t, p in sch if t <= se0_at] + [(se0_at + 30000.0, end_state)]
    await ts.drive_ui(dut, long_eop)
    line_w, st = await pull_words(dut, host, 2)
    dut._log.info("RX off during the EOP: LINE %04x (RX state %d), status %04x" % (line_w, (line_w >> 4) & 15, st))
    assert (line_w >> 4) & 15 == 0 and st & RXEOP, "receive state 0 after the EOP, RXEOP set"


USB_LINEB_FW = """
        .equ    E, 16
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0x210
        IOW     r0, E+13        ; SEL 0x210: DIN input 0, AUX0 = VP input 1, AUX1 = VM input 2 (uio_in; the host
                                ; port writes ui_in)
        LDI     r3, 0
        ADDI    r3, -1
        IOW     r3, E+2
        PUSH    r3              ; ready
w:      IOR     r1, E+2
        SHR     r1, 11
        ANDI    r1, 1
        BEQ     w
        IOR     r1, E+2
        PUSH    r1
        HALT
"""


@cocotb.test(skip=GL)
async def test_slu_cov_usb_lineb_saturates(dut):
    """USB helper in the chip, line condition B (J for 15,000 ticks of 16 cycles: suspend, LINEB): its 16-bit timer
    stops at 65,535 and does not wrap. 65,535 ticks are 1,048,560 cycles (17.5 ms at 60 MHz): the test drives J and
    writes the timer register to 65,520 (16 ticks before the limit); LINEB is set at once (65,520 >= 15,000), the timer
    reaches 65,535 and stays there."""
    prog = pe_asm.assemble(USB_LINEB_FW, "usb_lineb")
    host = await ts.chip_start(dut, prog, [], [0] * 15)
    dut.uio_in.value = 1 << AUX1                                    # J: VP (uio1) 0, VM (uio2) 1
    await host.start()
    await pull_words(dut, host, 1)
    await ClockCycles(dut.clk, 40)
    usb = dut.user_project.u_proc.u_usb
    usb.lt_b.value = 0xFFF0                                         # register write: 16 ticks before the limit
    await ClockCycles(dut.clk, 20 * 16)
    v1 = int(usb.lt_b.value)
    await ClockCycles(dut.clk, 10 * 16)
    v2 = int(usb.lt_b.value)
    (st,) = await pull_words(dut, host, 1)
    dut._log.info("line timer B after the write and 20 ticks: %d, 10 ticks later: %d; status %04x" % (v1, v2, st))
    assert st & LINEB, "LINEB (suspend) set"
    assert v1 == 0xFFFF and v2 == 0xFFFF, "line timer B stops at 65,535"


async def eth_integrator(dut, ppm):
    rng = random.Random(int(abs(ppm)))
    prog = pe_asm.assemble(ts.ETH_RX_FW, "eth_rx")
    host = await ts.chip_start(dut, prog, [], [0] * 15)
    await host.start()
    await ClockCycles(dut.clk, 200)
    loop = dut.user_project.u_proc.u_eth.u_loop
    seen = {"min": 0, "max": 0}

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            v = loop.ii.value.to_signed()
            seen["min"] = min(seen["min"], v)
            seen["max"] = max(seen["max"], v)
    task = cocotb.start_soon(watch())
    await drive_eth_frame(dut, bytes(rng.randrange(256) for _ in range(200)), ppm, rng)
    task.cancel()
    return seen


async def integrator_limit(dut, ppm):
    """10BASE-Te receive loop in the chip, frequency integrator limit (src/pe_slu_loop.v: I is held within +/-2047): a
    frame of 200 bytes far outside IEEE 802.3's +/-100 ppm, beyond the integrator's range of about 2,600 ppm, drives I to
    one limit, and I never goes beyond either limit."""
    seen = await eth_integrator(dut, ppm)
    dut._log.info("integrator I at %+.0f ppm: %d..%d" % (ppm, seen["min"], seen["max"]))
    assert -2047 <= seen["min"] and seen["max"] <= 2047, "integrator outside +/-2047"
    # a longer bit time (+ppm) makes every edge late, so I goes negative (it slows the bit clock), and the reverse
    if ppm > 0:
        assert seen["min"] == -2047, "the integrator reaches -2047"
    else:
        assert seen["max"] == 2047, "the integrator reaches +2047"
    return seen


@cocotb.test(skip=GL)
async def test_slu_cov_eth_loop_integrator_fast(dut):
    """Frame at +5,000 ppm (longer bits): the integrator of the 10BASE-Te receive loop stops at -2047 (integrator_limit)."""
    await integrator_limit(dut, 5000.0)


@cocotb.test(skip=GL)
async def test_slu_cov_eth_loop_integrator_slow(dut):
    """Frame at -5,000 ppm (shorter bits): the integrator stops at +2047 (integrator_limit)."""
    await integrator_limit(dut, -5000.0)

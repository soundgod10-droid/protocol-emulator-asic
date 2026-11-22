# SPDX-License-Identifier: Apache-2.0
"""PIO SPI transport for the RP2350 (docs/host_port.md 2, 3, 12). MicroPython on the board; the same program
text runs in CPython through host/pio_sim.py (an rp2 stand-in with a PIO interpreter) in the test bench.

Why PIO: on the demo board HOST_SCK is GPIO24, which has no SPI clock function, so no SPI block can drive it
(host_port.md 12). A PIO program also sets the time at which MISO is sampled after the rising SCK edge
(host_port.md 12 rule 1).

Program, at the state-machine clock f_sm (clkdiv from the chip clock, below):
  - side-set 2 pins: bit 0 = CS_N, bit 1 = SCK; OUT pin = MOSI; IN pin = MISO;
  - a transaction is one word with (bits - 1), then one word per byte with the byte in bits 31:24;
  - per bit: SCK low 6 cycles (MOSI changes in the first low cycle after the fall), SCK high 6 cycles;
    MISO is taken by the IN instruction d + 1 cycles after the rising edge (d = 0 to 4); the RP2350 input
    synchronizer (2 cycles, not bypassed) makes the pin sample about d - 1 cycles after the edge;
  - CS_N low 6 cycles before the first rising edge, CS_N high 11 cycles after the last rising edge and at
    least 6 cycles between transactions.
At f_sm = 120 MHz (chip 60 MHz) this is SCK 10 MHz, each level 50 ns >= 2 T (33.3 ns), the rated rate of
host_port.md 6 and 12 rule 2. At the 25 MHz bench clock f_sm = 50 MHz (150 MHz / 3 on a Pico 2): SCK
4.17 MHz, each level 120 ns = 3 T >= 2 T (80 ns).
"""

try:
    import rp2
    from machine import Pin
except ImportError:          # CPython: the test bench gives rp2 and Pin (host/pio_sim.py)
    rp2 = None
    Pin = None

SAMPLE_D_DEFAULT = 2


def make_program(asm_pio, PIO, d):
    if not 0 <= d <= 4:
        raise ValueError("sample delay d must be 0 to 4")

    @asm_pio(sideset_init=(PIO.OUT_HIGH, PIO.OUT_LOW), out_init=PIO.OUT_LOW,
             out_shiftdir=PIO.SHIFT_LEFT, in_shiftdir=PIO.SHIFT_LEFT,
             autopull=True, pull_thresh=8, autopush=True, push_thresh=8)
    def spi_host():
        wrap_target()
        pull(block)             .side(0b01) [3]     # CS_N high, SCK low; word = bits - 1
        mov(x, osr)             .side(0b01)
        out(null, 32)           .side(0b01)         # empty the OSR: the next OUT pulls the first byte
        nop()                   .side(0b00)         # CS_N low
        label("bit")
        out(pins, 1)            .side(0b00) [4]     # MOSI
        nop()                   .side(0b10) [d]     # SCK rises
        in_(pins, 1)            .side(0b10) [4 - d] # MISO
        jmp(x_dec, "bit")       .side(0b00)         # SCK falls
        nop()                   .side(0b00) [3]     # CS_N lag
        wrap()

    return spi_host


def sm_freq(chip_clock_hz):
    """State-machine clock: 2 cycles per chip clock (120 MHz at 60 MHz), so that the PIO and chip clocks
    are locked on the demo board (project clock = system clock / 2) and each SCK level is 6 cycles = 3 T."""
    return 2 * chip_clock_hz


class PioSpi:
    """Transport for pe_host.core.Chip on the RP2350."""

    def __init__(self, chip_clock_hz, sck, cs_n, mosi, miso, sm_id=0, gpio_base=None, d=SAMPLE_D_DEFAULT,
                 rp2_mod=None, pin_cls=None, time_mod=None):
        r = rp2_mod or rp2
        P = pin_cls or Pin
        if time_mod is None:
            import time as time_mod
        self.time = time_mod
        if gpio_base is not None:
            r.PIO(sm_id // 4).gpio_base(P(gpio_base))
        if sck != cs_n + 1:
            raise ValueError("SCK must be the pin after CS_N (one 2-pin side-set group)")
        self.sm = r.StateMachine(sm_id, make_program(r.asm_pio, r.PIO, d), freq=sm_freq(chip_clock_hz),
                                 sideset_base=P(cs_n), out_base=P(mosi), in_base=P(miso))
        self.sm.active(1)

    def xfer(self, out):
        n = len(out)
        got = bytearray(n)
        self.sm.put(8 * n - 1)
        i = j = 0
        while j < n:
            if i < n and self.sm.tx_fifo() < 4:
                self.sm.put(out[i] << 24)
                i += 1
            if self.sm.rx_fifo():
                got[j] = self.sm.get() & 0xFF
                j += 1
        return bytes(got)

    def sleep_us(self, us):
        self.time.sleep_us(us)

    def ticks_us(self):
        return self.time.ticks_us()

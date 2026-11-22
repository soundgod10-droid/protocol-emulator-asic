# SPDX-License-Identifier: Apache-2.0
"""Board entry of the host program (MicroPython on the RP2350). Not run in CI: the library under it is.

Copy host/pe_host/ and the image modules made by host/build_image.py (for example
  python host/build_image.py eth_10bt 60000000 img_eth.py) to the board, set BOARD, then:
  import main; main.ping()     ARP and ICMP echo responder through fw/eth_10bt.s (demo board, 60 MHz only)
  import main; main.keys("hello")   types text through fw/usb_dev.s (demo board, 60 MHz only)
  import main; main.link()     READ_ID and a memory pattern check (both boards)

Pin maps (docs/host_program.md):
  demo  TT demo board v3 (RP2350B): HOST_SCK GPIO24, HOST_CS_N 23, HOST_MOSI 22, HOST_MISO 40, PIO GPIO base
        16; project clock 60 MHz from the board SDK (system clock 120 MHz / 2), reset through the SDK.
  bench Pico 2 (RP2350A) wired to the ULX3S (docs/bench.md 2): ui[0..4] GP0-4, HOST_MOSI GP5, HOST_CS_N GP6,
        HOST_SCK GP7, uo[0..6] GP8-14, HOST_MISO GP15, uio[0..6] GP16-22, uio[7] GP26, rst_n GP27; the chip
        clock is the ULX3S 25 MHz oscillator.
"""

from pe_host.core import Chip
from pe_host.pio_spi import PioSpi

BOARD = "demo"
DESIGN = "tt_um_soundgod10_droid_protocol_emulator"
MAC = b"\x02\x00\x00\x00\x00\x01"       # locally administered
IP = bytes([192, 168, 7, 2])


def open_chip():
    if BOARD == "demo":
        from ttboard.demoboard import DemoBoard
        from ttboard.mode import RPMode
        tt = DemoBoard.get()
        getattr(tt.shuttle, DESIGN).enable()
        tt.mode = RPMode.ASIC_RP_CONTROL
        clock = 60_000_000
        tt.clock_project_PWM(clock)

        def rst(level):
            tt.reset_project(not level)
        t = PioSpi(clock, sck=24, cs_n=23, mosi=22, miso=40, gpio_base=16)
    else:
        from machine import Pin
        clock = 25_000_000
        rst_pin = Pin(27, Pin.OUT, value=1)

        def rst(level):
            rst_pin.value(level)
        t = PioSpi(clock, sck=7, cs_n=6, mosi=5, miso=15)
    return Chip(t, reset_pin=rst), clock


def link(words=1536):
    chip, clock = open_chip()
    chip.reset()
    print("READ_ID", chip.check_id())
    chip.stop()
    for pat in (0x0000, 0xFFFF, 0xA5A5, 0x5A5A):
        chip.write_mem(0, [pat ^ (a & 0xFF) for a in range(words)])
        got = chip.read_mem(0, words)
        bad = sum(1 for a in range(words) if got[a] != pat ^ (a & 0xFF))
        print("pattern %04X: %d errors in %d words" % (pat, bad, words))


def ping():
    from img_eth import IMAGE
    from pe_host.eth import Station
    chip, clock = open_chip()
    chip.load(IMAGE, clock)
    st = Station(chip, MAC, IP)
    st.serve(lambda s: False, 1 << 62)


def keys(text):
    from img_usb import IMAGE
    from pe_host.kbd import Keyboard
    chip, clock = open_chip()
    chip.load(IMAGE, clock)
    kb = Keyboard(chip)
    kb.wait_configured(10_000_000)
    kb.type_text(text)

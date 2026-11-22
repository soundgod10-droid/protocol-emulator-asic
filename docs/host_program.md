# Host program for the RP2350

| Item | Value |
|---|---|
| Document | docs/host_program.md |
| Code | host/pe_host/ (library), host/main.py (board entry), host/build_image.py (load images, on a PC), host/pio_sim.py (PIO stand-in for the test bench) |
| Tests | test/test_host_prog.py, CI job `host` of .github/workflows/test.yaml |
| Port rules | docs/host_port.md |
| Language | ASD-STE100. Signal names, file names and code names are technical names. |

## 1 What it is

The host program loads the chip and exchanges words with its threads. It runs on the RP2350 of the TTIHP27a demo
board, and on a Raspberry Pi Pico 2 (RP2350A) on the FPGA bench (docs/bench.md). The same Python files run under
MicroPython on the board and under CPython in the test bench.

| Module | Function |
|---|---|
| pe_host/core.py | commands of docs/host_port.md section 4; load with reset, WRITE_MEM, WRITE_CFG, READ_MEM check and START (section 8 rule 6); refusal of an image built for another clock (section 11); FIFO pumps with the host's own free count (sections 9, 10) |
| pe_host/pio_spi.py | PIO SPI transport: SPI mode 0, MISO taken a set time after the rising SCK edge (section 12 rule 1) |
| pe_host/eth.py | frame pump for fw/eth_10bt.s; ARP responder (RFC 826) and ICMP echo responder (RFC 792) for IPv4 (RFC 791) |
| pe_host/kbd.py | key-report pump for fw/usb_dev.s (HID boot keyboard): one key-down report and one all-keys-up report per character |
| build_image.py | load images: program and data words, configuration words, the chip clock and the `.equ` values for that clock. The USB device and 10BASE-T station images exist only for 60 MHz (their line rates are fixed for that clock, docs/helpers.md section 1) |

## 2 What runs where

| Part | On the RP2350 | In CI (CPython against the RTL, logic simulation) |
|---|---|---|
| core, eth, kbd | yes (MicroPython) | yes: the same files; each blocking call is carried out by the simulator |
| PIO program text (pio_spi.make_program) | yes: assembled by MicroPython's `rp2.asm_pio` | yes: assembled by host/pio_sim.py and run in its PIO interpreter against the chip pins; its 9 words equal the RP2350 data sheet encodings (test_host_pio_link) |
| PioSpi.xfer (FIFO feed and drain by the CPU) | yes | the test bench does the same FIFO steps instantly |
| main.py (board SDK calls, pin maps) | yes | no |
| rp2.DMA, MicroPython speed per transaction | not used yet; not measured | no |

Nothing in this document has run on an RP2350 yet. The first board test (docs/bench.md, bench day 1) measures the
SCK period, the sample time and the transactions per second.

## 3 Pins

Demo board (RP2350B, tt-micropython-firmware src/ttboard/pins/gpio_map_dbv3.py, class GPIOMapTTDBv3):

| Chip pin | RP2350 GPIO | Use by the host program |
|---|---|---|
| clk | 16 | project clock, 60 MHz by PWM from the board SDK (system clock 120 MHz / 2) |
| rst_n | 14 | reset through the board SDK |
| ui_in[0..4] | 17-21 | protocol inputs (RP2350 drives them only for the no-wiring demos) |
| ui_in[5] HOST_MOSI | 22 | PIO OUT pin |
| ui_in[6] HOST_CS_N | 23 | PIO side-set bit 0 |
| ui_in[7] HOST_SCK | 24 | PIO side-set bit 1 (no SPI function on GPIO24, docs/host_port.md section 12) |
| uo_out[0..6] | 33-39 | protocol outputs |
| uo_out[7] HOST_MISO | 40 | PIO IN pin; PIO GPIO base 16 |
| uio[0..7] | 25-32 | protocol pins (USB transceiver or 10BASE-Te module on a Pmod) |

FPGA bench (Pico 2, RP2350A, GPIO 0-29) wired to the ULX3S pins of docs/bench.md section 2:

| Chip pin | Pico 2 GPIO |
|---|---|
| ui_in[0..4] | GP0-GP4 |
| HOST_MOSI, HOST_CS_N, HOST_SCK | GP5, GP6, GP7 |
| uo_out[0..6] | GP8-GP14 |
| HOST_MISO | GP15 |
| uio[0..6], uio[7] | GP16-GP22, GP26 |
| rst_n | GP27 |
| clk | not connected: the ULX3S 25 MHz oscillator clocks the FPGA |

## 4 Clock and host link

The PIO state machine runs at 2 x the chip clock, so each SCK level is 6 cycles = 3 T of the chip (each level must be
2 T or more, docs/host_port.md section 3):

| Chip clock | PIO clock | SCK | Each SCK level | Where |
|---|---|---|---|---|
| 60 MHz | 120 MHz (the demo board system clock) | 10 MHz | 50 ns | demo board |
| 25 MHz | 50 MHz (150 MHz / 3 on a Pico 2) | 4.17 MHz | 120 ns | FPGA bench |

MISO is taken by the IN instruction d + 1 PIO cycles after SCK rises (d = 0 to 4, default 2). With the RP2350 input
synchronizer (2 cycles) the pin is sampled about d - 1 cycles after the edge. Window in RTL simulation at 60 MHz, both
PIO clock phases, READ_ID and a 16-word memory pattern correct (test_host_pio_window). The delay is the board path each
way (RP2350 pad, board, shuttle multiplexer, chip pad), the same in both directions:

| d | Passes up to (each way) | First failure (each way) |
|---|---|---|
| 0, 1 | 20 ns | 25 ns |
| 2, 3 | 30 ns | 35 ns |
| 4 | 40 ns | 45 ns |

The shuttle's published estimate is about 10 ns round trip through the pads and multiplexer (not measured). With d = 2
the link passes up to 60 ns round trip. A failure was found only at long delays, not at 0 ns, in this model. The bring-up
test sweeps d on the board (docs/host_port.md section 12 rule 2).

## 5 Tests (CI job `host`)

| Test | Result (logic simulation, RTL) |
|---|---|
| test_host_eth_ping | the partner model sends an ARP request and 10 ICMP echo requests (32 data octets) through fw/eth_10bt.s; the host program answers; the ARP reply and the 10 echo replies are correct field by field (RFC 826, RFC 792), and every chip packet passes the partner model's 802.3 checks |
| test_host_usb_keys | the USB host model enumerates the device; the host program types 100 characters; the host model reads the 200 reports in order, DATA0/DATA1 alternating, every device packet within the USB 2.0 limits |
| test_host_pio_link | the PIO program in the interpreter: READ_ID and a 64-word memory pattern at 60 MHz, 5 ns board delay each way, both PIO clock phases |
| test_host_pio_link_25mhz | the same at the 25 MHz bench clock, 0 and 5 ns each way |
| test_host_pio_window | the table of section 4 |

Conditions: RTL at 60 MHz (25 MHz in one test) with ideal pins; no transceiver, magnetics or cable; board delays only
where a test sets them. These results do not show the RP2350 itself, the pads, the multiplexer or the line parts.

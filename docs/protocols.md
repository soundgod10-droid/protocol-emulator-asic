# Protocols

The chip has no UART, SPI or I2C block. Each protocol is a firmware program in `fw/`, written with the instruction set of [isa.md](isa.md) and loaded by the host through the SPI port ([host_port.md](host_port.md)). This page lists every program, how it is tested, and the waveforms of four of them on the chip pins.

## How each protocol is tested

Each protocol has three tests:

1. **Model test.** The firmware runs in the cycle-exact reference model (`model/pe_model.py`) against a protocol model written from the standard or data sheet, not from the firmware. These tests sweep the rates and the timing faults (`test/test_fw_full.py`, `test/test_flex.py`, `test/test_holdout.py`).
2. **RTL test.** The same firmware runs on the RTL at 60 MHz (`test/test_rtl_fw.py`). A host model loads it through the SPI port and serves the FIFOs from the status bytes, as a real host does. The protocol model of the model test is on the other pins, in closed loop. The reference model runs in lockstep: every pin in every clock cycle and every word on `HOST_MISO` must be equal to the model.
3. **Gate-level test.** The RTL test runs again on the netlist of the layout (`gl_protocols` jobs of the gds workflow, functional cell models, no delays).

The newest results of the RTL and gate-level tests, with their runs, are on the [Results](results.md) page.

## Programs

The RTL and gate-level column gives what `test/test_rtl_fw.py` runs at 60 MHz. The last column gives the highest rate that the model test measured at 50 MHz, the clock declared when the model tests were written.

| Protocol | Firmware, words | Standard or document | On the RTL and the netlist at 60 MHz | Highest rate in the model (50 MHz) |
|---|---|---|---|---|
| UART transmitter | `uart_tx_full.s`, 15 | - | 1 Mbit/s 8N1, 115,200 bit/s 8N1, 9,600 bit/s 8E1, frames back to back | 1 Mbit/s |
| UART receiver | `uart_rx_full.s` 13, `uart_rx_slow.s` 24 | - | 1 Mbit/s 8N1 and 115,200 bit/s 8E1 with the sender at -3.3 % and +3.3 %, 1,200 bit/s, a framing error | 1 Mbit/s |
| SPI controller | `spi_ctl.s`, 26 | - | modes 0 to 3, SCK 1.2 MHz (D 0) and 938 kHz (D 7) | 1.0 MHz |
| SPI target | `spi_tgt.s`, 33 | - | modes 0 to 3 at 1 MHz | 1 MHz |
| I2C controller | `i2c_ctl.s`, 49 | NXP UM10204 Rev. 7.0 | Fast-mode and Standard-mode, with and without clock stretching, repeated START, NACK, arbitration loss, spikes through the input filter; every bus time against Table 11 | Fast-mode, 384.6 kHz |
| CAN 2.0A node | `can.s`, 123, 2 threads | Bosch CAN Specification 2.0, Part A | 250 kbit/s, sample point 55 %, the other node at +0.5 % and -0.5 %, 10-cycle loop delay, arbitration loss | 250 kbit/s |
| JTAG controller | `jtag.s` 31, `jtag_tap.s` 67 | TI SSYA002C (IEEE 1149.1 primer) | TCK 1.5 MHz, two TAPs in a chain: IDCODE, IR and DR scans, Pause-DR | TCK 1.25 MHz (`jtag.s`) |
| SWD host | `swd.s`, 62 | Arm IHI0031H (ADIv5.2) | SWCLK 1.875 MHz: line reset, DPIDR, AP write with WAIT, posted read, parity error | SWCLK 1.5625 MHz |
| PS/2 host and device | `ps2_host.s` 62, `ps2_dev.s` 60 | IBM PS/2 Technical Reference (1988) | 12.5 kHz device clock, request to send, acknowledge, line contention | 10 to 16.7 kHz |
| Timed trigger | `trigger.s`, 30 | - | a pulse a set delay after a matching UART byte at 115,200 bit/s: the same delay at 4 start phases (0 cycles of jitter) | delay from 44 cycles |
| 1-Wire controller (hold-out) | `onewire.s`, 40 | Analog Devices AN126; DS18B20 data sheet | Read ROM, Write and Read Scratchpad against the slowest device times; no device | five device-timing corners |
| AM2302 sensor reader (hold-out) | `am2302.s`, 56 | AM2302 data sheet | sensor times x 1.2, a negative temperature, no sensor, a sensor that stops | sensor times 0.8 to 1.2 x |
| AVR XMEGA PDI programmer (hold-out) | `pdi.s`, 52 | Atmel XMEGA AU manual 8331F, section 32 | PDI_CLK 1 MHz: KEY, control and data access, REPEAT, a parity error, BREAK | PDI_CLK 1.25 MHz (at 60 MHz) |
| Four protocols at once | 121 words in one image (15 + 26 + 49 + 31) | as above | UART TX 460,800 bit/s, SPI controller, I2C Fast-mode with clock stretching and JTAG on the four threads at the same time, each against its own model | - |

Which programs fit together in the 128 words, and the host words of each program: [host_port.md](host_port.md) section 14 (for example UART TX + UART RX + JTAG + SWD: 121 words).

## See it run

The figures below are made on every push by the docs workflow ([figures/waves.py](../figures/waves.py)). The workflow runs the RTL test with a dump of the chip pins (VCD), cuts out one part of the test, measures the protocol times over the whole part and marks the measured values (green) and the limits (red). All four are logic simulations of the RTL at 60 MHz with ideal pins: no pad, wire or bus rise time.

![UART transmitter at 115,200 bit/s on the RTL](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_uart.svg)

*UART transmitter, `test_rtl_uart_tx`: each bit edge is a timed `OUT` event, so every edge is on the bit grid of 521 cycles whatever the instructions in between take. `HOST_CS_N` shows the host polling the status and refilling the FIFO.*

![SPI controller in mode 0 on the RTL](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_spi.svg)

*SPI controller, `test_rtl_spi_ctl`, mode 0: the chip drives CS, SCK and MOSI; MISO comes from the independent device model.*

![I2C controller in Fast-mode on the RTL](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_i2c.svg)

*I2C controller, `test_rtl_i2c`, Fast-mode: open-drain bus with the target model; every bus time of the part is checked against NXP UM10204 Table 11 by the test's own function.*

![CAN node at 250 kbit/s on the RTL](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_can.svg)

*CAN 2.0A node, `test_rtl_can`, two threads: thread 0 sends on TXA, the other node acknowledges in the ACK slot.*

## Flexibility: protocols the instruction set was not designed for

The instruction set ([isa.md](isa.md) section 13) was designed from UART, SPI and I2C firmware. The programs below were written later, each from its public specification, with the same instruction set, assembler and host port, and no hardware change. All four are named on the competition page, so they were known when the instruction set was made.

| Protocol | What the instruction set lacked (workaround) |
|---|---|
| CAN 2.0A node | instruction rate (about 31 instructions of receive work per bit), so 500 kbit/s and 1 Mbit/s do not fit; one owner per pin (two open-drain pins joined on the board); the host makes the bit stuffing and the CRC for sending; one thread would need 171 words |
| JTAG controller | `SAMPLE` takes 2 thread slots, so the loop is 40 cycles, not 32 |
| SWD host | no parity instruction (the host makes the parity); a write fills the 4-word host FIFO |
| PS/2 host and device | a thread cannot wait for an edge and a host word at the same time (the host polls in 4 us windows) |

### Hold-out trials

A hold-out trial is a protocol that nobody had in view when the instruction set was made, written afterwards from its public documents on the unchanged chip. 1-Wire was the first. The next two were chosen by a rule that a reader can repeat: the first two entries of the sigrok [libsigrokdecode](https://github.com/sigrokproject/libsigrokdecode/tree/master/decoders) decoder list (alphabetical) that decode a logic-level link between two devices, are not in `fw/` and not named on the competition page, can be driven from 3.3 V pins with external parts, allow a bit time of 1 us or more, and have a public specification. They are `am230x` and `avr_pdi` (`ac97` and `aud` need clocks of 12.288 and about 20 MHz; the other entries before them decode SPI, UART or I2C traffic).

| Protocol | Result | What the instruction set lacked (workaround) |
|---|---|---|
| 1-Wire controller | correct at five device-timing corners; found that the 60 µs write-0 time of AN126 has no margin against a device that samples at 60 µs, so the firmware uses 65 µs | `TADD` adds at most 4,095 cycles (two `TADD` for the 70 µs presence wait at 60 MHz) |
| AM2302 / DHT22 sensor reader | correct with all sensor times 0.8 to 1.2 times the data sheet; timeout status for no sensor and for a sensor that stops | `TADD` limit (two `TADD` for each 136 µs time limit) |
| AVR XMEGA PDI programmer | KEY, control registers, data read and write, REPEAT with LD, parity error and BREAK recovery correct at 6 device corners | no parity instruction (the host makes the parity bit) |

The next step is a census on the frozen chip: every protocol of a whole outside catalogue, in an order set by a public random beacon, with the rules fixed before the RTL change ([Flexibility census](../flex/PREREG.md)).

## External parts

The pins are 3.3 V digital pins with no 5 V tolerance. The parts that each protocol needs on the board (pull-ups, transceivers, level shifters) are in [info.md](info.md), section External hardware.

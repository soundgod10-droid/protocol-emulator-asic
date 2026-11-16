<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This design is a programmable protocol emulator. It has no fixed protocol block: a small processor makes the pin waveforms, and the host loads its firmware after power-up. A new protocol needs new firmware only.

The design has these blocks:

1. A barrel processor with 4 threads (docs/isa.md). Thread n executes one 16-bit instruction in each clock cycle c with c mod 4 = n. A stopped or waiting thread keeps its slot. The programs of all threads are in a program memory of 512 words of 16 bits; frames and tables are in a data memory of 1,024 words (two SRAM macros, docs/isa.md 2.2).
2. A timed I/O unit. A thread does not set a pin at once: it writes a pin event with a 16-bit cycle time (OUT pin at T + offset) into the event slot of that pin. When the free-running cycle counter is equal to the event time, the pin flip-flop changes. Thus each pin edge is exact to one clock cycle, also with 4 threads. An event must be written 4 to 32,767 cycles ahead; a late event happens 4 cycles after the instruction and sets the LATE flag of the thread, which the host reads. SAMPLE and WAIT read the inputs at a given cycle in the same way. Each output pin has one owner thread, set by the host; pins can be push-pull or open-drain.
3. Input synchronizers (2 flip-flops on each input). The inputs `uio[6]` and `uio[7]` (I2C) also have a spike filter that removes pulses of 1 to 4 clock cycles (I2C Fast-mode, docs/isa.md 6.1).
4. Host FIFOs: 4 words in each direction for each thread (PULL and PUSH instructions).
5. A host SPI port (docs/host_port.md). The host stops and starts the threads, writes and reads the memory, writes the configuration (start address of each thread, owner and mode of each output pin, helper pins), reads the chip ID and the status, and gives and takes FIFO words.
6. Two line helpers (docs/helpers.md): a USB low-speed helper (I/O ports 16 to 31) and a 10BASE-Te helper (ports 32 to 47). They do the bit-level work at line rate: NRZI or Manchester, bit stuffing, CRC generation and check, receive clock recovery, end of packet, link pulses. A thread works on words and frames with IOR and IOW; the host gives the helper its pins.

Firmware in `fw/` (assembler `asm/pe_asm.py`):

| File | Function | Words | Tested on the RTL and the netlist at 60 MHz |
|---|---|---|---|
| `uart_tx.s` | UART transmitter, 115,200 bit/s 8N1 (the example below) | 12 | 115,200 bit/s, at 50 MHz (`test/test.py`) |
| `uart_tx_full.s`, `uart_rx_full.s` | UART transmitter and receiver, 8N1, 8E1, 8N2 | 15, 13 | 1 Mbit/s and 115,200 bit/s; transmitter also 9,600 bit/s 8E1 |
| `uart_rx_slow.s` | UART receiver for bit times of more than 32,767 cycles (below 1,831 bit/s at 60 MHz) | 24 | 1,200 bit/s |
| `spi_ctl.s` | SPI controller, modes 0 to 3 | 26 | all four modes, SCK up to 1.2 MHz |
| `spi_tgt.s` | SPI target, modes 0 to 3 | 33 | all four modes at 1 MHz |
| `i2c_ctl.s` | I2C controller, Standard-mode and Fast-mode, clock stretching, repeated START, arbitration loss | 49 | Standard-mode and Fast-mode |
| `can.s` | CAN 2.0A node (2 threads) | 123 | 250 kbit/s |
| `jtag.s`, `jtag_tap.s` | JTAG controller | 31, 67 | TCK 1.5 MHz |
| `swd.s` | SWD host | 62 | SWCLK 1.875 MHz |
| `ps2_host.s`, `ps2_dev.s` | PS/2 host and device | 62, 60 | 12.5 kHz device clock |
| `trigger.s` | timed trigger: a pulse a set time after a matching UART byte | 30 | 115,200 bit/s, 0 cycles of jitter |
| `onewire.s` | 1-Wire controller | 40 | DS18B20 device times |
| `am2302.s` | AM2302 / DHT22 sensor reader | 56 | sensor times x 1.2 |
| `pdi.s` | AVR XMEGA PDI programmer | 52 | PDI_CLK 1 MHz |

Each program runs on the RTL and on the gate-level netlist against an independent protocol model, with the cycle-exact reference model (`model/pe_model.py`) in lockstep on every pin (`test/test_rtl_fw.py`; docs/protocols.md gives the conditions and the waveforms). `fw/uart_rx.s`, `fw/spi_mode0.s` and `fw/i2c_write.s` are the short outline programs of the first design, tested in the model. Clock-dependent values are host words or assembler constants (docs/host_port.md section 11).

Reset: when `rst_n` is low, and while the threads are stopped, each processor output has the value 1 and no output enable: `uo_out[6:0]` = 1 (a UART TX line is idle high), all `uio` pins are inputs. The reset is asynchronous, so the pins go to this state at once, also when the clock is stopped. The memory does not change at reset.

## How to test

Pins:

| Pin | Name | Direction | Function |
|---|---|---|---|
| `ui_in[7]` | HOST_SCK | input | host SPI clock (mode 0) |
| `ui_in[6]` | HOST_CS_N | input | host SPI chip select, active low |
| `ui_in[5]` | HOST_MOSI | input | host SPI data in |
| `uo_out[7]` | HOST_MISO | output | host SPI data out |
| `ui_in[4:0]` | IN8 to IN12 | input | processor inputs (`ui_in[3]`: default UART RX) |
| `uo_out[6:0]` | OUT8 to OUT14 | output | processor outputs (`uo_out[4]`: default UART TX) |
| `uio[7:0]` | PIN0 to PIN7 | in/out | processor pins (SPI on `uio[3:0]`, I2C on `uio[7:6]` in the example firmware); `uio[7:6]` inputs have a spike filter that removes pulses of 1 to 4 clock cycles (I2C Fast-mode, docs/isa.md 6.1) |

Procedure (UART transmitter). The steps are written for a 50 MHz clock and `fw/uart_tx.s` as it is (`BIT` 434 cycles). At the declared 60 MHz, assemble it with `.equ BIT, 521` (60,000,000 / 115,200 = 520.8; 115,163 bit/s, -0.03 %); each frame is then 86.8 us as well.

1. Set HOST_CS_N high and HOST_SCK low. Set `rst_n` to 0 for at least 10 clock cycles, then set it to 1. `uo_out[4]` must stay high.
2. Use SPI mode 0, MSB first, SCK 10 MHz or less (docs/host_port.md sections 3 and 6).
3. READ_ID: send 0x1E and 4 more bytes. MISO gives 0x50 0x45 0x02 0x03.
4. WRITE_MEM: send 0x5A, 0x00, 0x00 (address, 2 bytes), then the words of fw/uart_tx.s (assembled with asm/pe_asm.py), high byte first. READ_MEM (0x69, 0x00, 0x00, 1 dummy byte, then 2 dummy bytes for each word) must give the same words.
5. WRITE_CFG: send 0x78, 0x00, then 5 words: 0, 0, 0, 0 (start addresses), 0x0001 (thread 0 runs). Then send 0x78, 0x1C (address 28 = pin 12), 0x00, 0x04 (owner thread 0, enabled).
6. START: send 0x4B. READ_STATUS (0x2D and 7 dummy bytes): byte 0 bit 0 = 1, byte 1 = 0x01.
7. Connect a UART receiver (115,200 bit/s, 8N1, 3.3 V) to `uo_out[4]`. For each byte: read status byte 3 (bits 2:0 = words in the FIFO of thread 0); if it is less than 4, send PUSH: 0x87, 0x00, 0x00, the byte. The receiver must show the same bytes. With 4 words in the FIFO the frames are back to back (86.8 us each).
8. STOP (0x3C) stops the threads: `uo_out[4]` goes high at once.

The other firmware files load in the same way; their headers give the pins, the host words and the assembler constants. Before STOP, let the last frame or transfer end, and hold each SPI chip-select line high with a pull-up while the chip is stopped (STOP releases all `uio` pins; docs/host_port.md section 8).

## External hardware

The chip has only 3.3 V digital pins; it makes no 5 V tolerance claim. Every protocol needs the host: the RP2350 of the demo board (or an other SPI host) on `ui_in[7:5]` and `uo_out[7]`, which reloads the memory and the configuration after every select (docs/host_port.md section 8, rule 6). Parts on a Pmod or the board for each protocol:

| Protocol (firmware) | Pins | External parts |
|---|---|---|
| UART TX/RX (`uart_tx_full.s`, `uart_rx_full.s`) | `uo_out[4]`, `ui_in[3]` | 3.3 V logic-level partner (USB-to-UART adapter or the RP2350); an RS-232 line needs an RS-232 transceiver |
| SPI controller (`spi_ctl.s`) | `uio[3:0]` | 3.3 V device; a pull-up on each chip-select line while the chip is stopped (the RP2350 pad pull-up, docs/host_port.md section 8 rules 2 and 3) |
| SPI target (`spi_tgt.s`) | `uio[3:0]` | 3.3 V controller; with other targets on MISO, observe the MISO release time (docs/host_port.md section 13) |
| I2C controller (`i2c_ctl.s`) | `uio[6]` SCL, `uio[7]` SDA, open drain | pull-up resistors to 3.3 V on SCL and SDA, sized for the bus capacitance so that the rise time meets NXP UM10204 Table 10 (Fast-mode tr 300 ns or less); Standard-mode and Fast-mode only |
| 1-Wire controller (`onewire.s`) | `uio[4]`, open drain | pull-up resistor to 3.3 V (Analog Devices AN126, system requirement 1); 3.3 V devices |
| CAN 2.0A node (`can.s`) | `ui_in[4]` RX; `uio[4]`, `uio[5]` TX, open drain | a CAN transceiver with 3.3 V logic I/O; the two TX pins joined with a pull-up to the transceiver TXD (one owner thread per pin, docs/host_port.md section 14) |
| JTAG controller (`jtag.s`, `jtag_tap.s`) | `uo_out[2:0]`, `ui_in[0]` | a pull-up on TDO; a level shifter if the target is not 3.3 V |
| SWD host (`swd.s`) | `uo_out[3]` SWCLK, `uio[4]` SWDIO | the pull-up on SWDIO at the target (Arm IHI0031H B4-118); a level shifter if the target is not 3.3 V |
| PS/2 host and device (`ps2_host.s`, `ps2_dev.s`) | `uio[4]` CLK, `uio[5]` DATA, open drain | PS/2 devices use a 5 V bus (IBM PS/2 Technical Reference): a bidirectional open-drain level shifter with pull-ups on both sides |
| Timed trigger (`trigger.s`) | `ui_in[3]` in, `uo_out[5]` out | none |
| AM2302 / DHT22 sensor reader (`am2302.s`) | `uio[5]`, open drain | pull-up resistor to 3.3 V on the data line; the sensor runs from 3.3 V (AM2302 data sheet, supply 3.3 to 6 V) |
| USB low speed (USB helper, docs/helpers.md) | 3 output pins for VPO, VMO and OE# (any of the 15, pin configuration); 3 input pins for RCV, VP, VM | a USB transceiver with 3.3 V logic and a VP/VM interface (for example TI TUSB1106), and the low-speed pull-up on D- (USB 2.0 7.1.5.1) |
| 10BASE-Te (10BASE-Te helper, docs/helpers.md) | output pins for TD+, TD- (and the delayed tap); input pins for the receive slicer and the squelch comparator | a 10BASE-T line interface on the board: transmit driver with series resistors and a pulse transformer, receive transformer, a slicer comparator and a squelch comparator (IEEE 802.3 14.3.1.3) |
| AVR XMEGA PDI programmer (`pdi.s`) | `uio[2]` PDI_CLK (device RESET), `uio[3]` PDI_DATA | none for a device that runs from 3.3 V; VCC and GND connected between the board and the device (XMEGA AU manual 32.3) |

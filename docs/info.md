<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This design is a programmable protocol emulator (milestone M2). It has no fixed protocol block: a small processor makes the pin waveforms, and the host loads its program.

The design has these blocks:

1. A barrel processor with 4 threads (docs/isa.md). Thread n executes one 16-bit instruction in each clock cycle c with c mod 4 = n. The program and the data are in one memory of 128 words of 16 bits (latches).
2. A timed I/O unit. A thread does not set a pin at once: it writes a pin event with a 16-bit cycle time (OUT pin at T + offset). When the free-running cycle counter is equal to the event time, the pin flip-flop changes. Thus each pin edge is exact to one clock cycle, also with 4 threads. SAMPLE and WAIT read the inputs at a given cycle in the same way.
3. Host FIFOs: 4 words in each direction for each thread (PULL and PUSH instructions).
4. A host SPI port (docs/host_port.md). The host (the RP2350 of the demo board) stops and starts the threads, writes and reads the memory, writes the configuration (start address of each thread, owner of each output pin), reads the chip ID and the status, and gives and takes FIFO words.

The chip changes a protocol pin only when a program tells it to. Example: fw/uart_tx.s (12 words) makes thread 0 a UART transmitter on `uo_out[4]`: 8 data bits, LSB first, 1 start bit, 1 stop bit, bit time 434 cycles (115,207 bit/s at 50 MHz, +0.0064 %), one byte from the host FIFO for each frame.

Reset: when `rst_n` is low, and while the threads stop, each processor output has the value 1 and no output enable: `uo_out[6:0]` = 1 (a UART TX line is idle high), all `uio` pins are inputs. The reset is asynchronous, so the pins go to this state at once, also when the clock is stopped. The memory does not change at reset.

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
| `uio[7:0]` | PIN0 to PIN7 | in/out | processor pins (SPI on `uio[3:0]`, I2C on `uio[7:6]` in the example firmware) |

Procedure (UART transmitter, clock 50 MHz):

1. Set HOST_CS_N high and HOST_SCK low. Set `rst_n` to 0 for at least 10 clock cycles, then set it to 1. `uo_out[4]` must stay high.
2. Use SPI mode 0, MSB first, SCK 10 MHz or less (docs/host_port.md sections 3 and 6).
3. READ_ID: send 0x1E and 4 more bytes. MISO gives 0x50 0x45 0x02 0x03.
4. WRITE_MEM: send 0x5A, 0x00, then the words of fw/uart_tx.s (assembled with asm/pe_asm.py), high byte first. READ_MEM (0x69, 0x00, then 2 dummy bytes for each word) must give the same words.
5. WRITE_CFG: send 0x78, 0x00, then 5 words: 0, 0, 0, 0 (start addresses), 0x0001 (thread 0 runs). Then send 0x78, 0x1C (address 28 = pin 12), 0x00, 0x04 (owner thread 0, enabled).
6. START: send 0x4B. READ_STATUS (0x2D and 7 dummy bytes): byte 0 bit 0 = 1, byte 1 = 0x01.
7. Connect a UART receiver (115,200 bit/s, 8N1, 3.3 V) to `uo_out[4]`. For each byte: read status byte 3 (bits 2:0 = words in the FIFO of thread 0); if it is less than 4, send PUSH: 0x87, 0x00, 0x00, the byte. The receiver must show the same bytes. With 4 words in the FIFO the frames are back to back (86.8 us each).
8. STOP (0x3C) stops the threads: `uo_out[4]` goes high at once.

## External hardware

The RP2350 of the demo board (or an other SPI host) on `ui_in[7:5]` and `uo_out[7]`. A UART receiver for 3.3 V logic levels on `uo_out[4]`, for example a USB-to-UART adapter or the RP2350.

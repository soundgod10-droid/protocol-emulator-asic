<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This design is milestone M1 of a programmable protocol emulator. M1 is a UART transmitter.

The chip sends UART frames on `uo_out[4]` (UART_TX). Each frame has 1 start bit (0), 8 data bits (LSB first) and 1 stop bit (1). There is no parity bit. When the line is idle, it is high.

The bit time is 434 clock cycles. With the design clock of 50 MHz:

| Item | Value |
|---|---|
| Bit rate | 50,000,000 / 434 = 115,207.4 bit/s |
| Error against 115,200 bit/s | +0.0064 % |
| Bit time | 8.680 us |
| Frame (10 bits) | 86.800 us |

With a different clock frequency f, the bit rate is f / 434.

The design has these blocks:

1. A free-running cycle counter. All pin changes use this one time base.
2. A timed output channel for UART_TX. It holds one pin event: a 16-bit cycle time and a pin value. When the counter is equal to the event time, the channel changes the pin flip-flop. The pin is the output of a flip-flop, so it has no glitches.
3. A sequencer (temporary, M1 only). For each bit of the frame, it writes one event into the channel, with the times t0, t0 + 434, t0 + 868 and so on. Thus, the bit time comes from the event times only.

In later milestones, program threads write the pin events, and the sequencer and the M1 handshake are removed. The cycle counter and the timed output channel stay.

Reset: when `rst_n` is low, UART_TX is high. The reset is asynchronous, so UART_TX goes high at once, also when the clock is stopped. After reset, UART_TX stays high until the host gives a byte.

## How to test

Pins in M1:

| Pin | Name | Direction | Function |
|---|---|---|---|
| `uo_out[4]` | UART_TX | output | UART data |
| `uo_out[5]` | TX_ACK | output | Acknowledge level (temporary, M1) |
| `ui_in[0]` | TX_REQ | input | Request level (temporary, M1) |
| `uio[7:0]` | TX_DATA | input | Byte to send (temporary, M1). All uio pins are inputs. |

All other outputs are 0.

Procedure:

1. Set the clock to 50 MHz.
2. Set `ui_in[0]` (TX_REQ) to 0. Set `rst_n` to 0 for at least 10 clock cycles, then set it to 1.
3. Connect a UART receiver (115,200 bit/s, 8 data bits, no parity, 1 stop bit, 3.3 V) to `uo_out[4]`.
4. To send a byte:
   1. Put the byte on `uio[7:0]`.
   2. Change the level of `ui_in[0]` (0 to 1, or 1 to 0).
   3. Wait until `uo_out[5]` is equal to `ui_in[0]`. Keep the byte on `uio[7:0]` stable until then.
5. To send bytes with no gap, give the next byte immediately after step 4.3. If the next byte comes before the stop bit ends, the next frame starts exactly at the end of the stop bit.

The UART receiver must show the same bytes. Each frame is 86.8 us long at 50 MHz.

## External hardware

A UART receiver for 3.3 V logic levels, for example a USB-to-UART adapter or the RP2350 on the demo board.

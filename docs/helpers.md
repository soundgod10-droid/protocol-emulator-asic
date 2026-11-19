# Line helpers: USB low speed and 10BASE-Te

| Item | Value |
|---|---|
| Document | docs/helpers.md |
| Status | Implemented in the RTL (src/pe_slu.v, src/pe_slu_loop.v; two instances in src/pe_proc.v). Tested as units (test/test_slu.py test_slu_*) and in the chip with firmware (test_slu_chip_*). |
| Language | ASD-STE100. Signal names, port names and file names are technical names. |

## 1 Purpose

A thread of the processor executes one instruction every 4 cycles (docs/isa.md 3.1). That is enough for UART, SPI, I2C, CAN and the other firmware protocols, but not for the bit-level work of USB low speed (1.5 Mbit/s, NRZI, bit stuffing, CRC) or 10BASE-T (10 Mbit/s Manchester, CRC-32, receive clock recovery on both clock edges). Each helper does that work at line rate; the thread gives and takes 16-bit words and starts frames at a cycle that it chooses, as it starts pin events.

| Helper | I/O ports | Event input | Line functions |
|---|---|---|---|
| USB low speed | 16 to 31 | 13 | NRZI, bit stuffing after 6 ones, SYNC end detection, EOP (SE0 qualified for 20 samples), CRC16 and CRC5 checked at the same time, CRC16 appended, EOP SE0 84 cycles (1.40 us) and J 40 cycles, line timers for bus reset (SE0) and suspend (J) |
| 10BASE-Te | 32 to 47 | 14 | Manchester at 3 cycles per half bit, receive phase loop on both clock edges, SFD detection after 16 accepted edges, CRC-32 checked and appended, TP_IDL hold 16 cycles, link test pulse 6 cycles (100 ns), squelch qualifier (4 edge spacings of 3 to 16 half cycles, each measured to the falling-edge sample of the cycle of the new edge: 3 to 16 for an edge first seen at the falling-edge sample, 4 to 17 at the rising-edge sample), link-pulse classifier (2 to 12 cycles), collision (receive carrier while sending) with a 32-bit jam, jabber (transmit longer than 2^22 cycles = 69.9 ms stops it for 2^25 cycles = 0.559 s) |

Both helpers are the same RTL (pe_slu) with a fixed configuration (parameter SPEC). The pin choice SEL is writable in both. The USB helper also accepts MODE bits 0, 1, 3, 4, 9, TXPER bits 7:0 and LTC bits 12:8, so that it also runs the clocked and NRZ modes of section 6 (SPI controller, JTAG, SWD clock, UART). The fixed values are in src/pe_slu.v (F_*) and are the values that the tests use; after reset both helpers have them.

## 2 Ports

Port p of a helper is I/O port 16 + p (USB) or 32 + p (10BASE-Te).

| p | Name | IOR | IOW |
|---|---|---|---|
| 0 | TXDATA | free words in the transmit buffer (0 to 2) | add a word to the frame (bit 0 first on the line) |
| 1 | RXDATA | the oldest received word (0 if none); the read takes it out of the buffer | - |
| 2 | STATUS | flags (below) | 1 clears a sticky flag |
| 3 | EVMASK | flags that drive the event input | write |
| 4 | TXCTRL | bits 3:0 transmit state | bits 4:0 bits in the last word (0 = 16), 5 append CRC, 6 no start when late, 7 send a link test pulse instead of a frame, 8 abort, 9 start now, 10 empty the transmit buffer |
| 5 | TXTIME | the armed start time | start time (compared with now[15:0]); lead 4 to 32,767 cycles, else TXLATE: start at write + 4, or no start with TXCTRL bit 6 |
| 6 | RXINFO | bits 4:0 bits in the last word of the frame, 6:5 words in the receive buffer, 15:7 words of the frame | - |
| 7, 8 | CRCA_LO, CRCA_HI | CRC A register (CRC16 / CRC-32) | - |
| 9 | CRCB | CRC B register (CRC5, USB) | - |
| 10 | TSTAMP | now[15:0] at the event: USB end of packet, 10BASE-Te end of frame | - |
| 11 | LINE | bit 0 AUX0, 1 AUX1, 2 DIN, 3 carrier, 7:4 receive state | - |
| 12 | CFGADR | - | configuration index (+1 after each CFGDAT write); writable: 8 = SEL in both helpers, and 0 = MODE, 1 = TXPER, 18 = LTC in the USB helper (section 6) |
| 13 | CFGDAT | - | configuration word: SEL bits 3:0 DIN input, 7:4 AUX0 input, 11:8 AUX1 input (input numbers of docs/isa.md 5.1; 13 to 15 = none), 15:12 clocked sample delay (section 6) |
| 14 | TXLAST | - | add the last word of the frame (its bit count from TXCTRL bits 4:0) |

STATUS flags: 0 TXDONE, 1 TXLATE, 2 TXUNDER (buffer empty in a frame), 3 RXWORD (a word is waiting), 4 RXSOP, 5 RXEOP, 6 CRCA_OK (last frame), 7 CRCB_OK, 8 STUFFERR, 9 RXOVER (word lost), 10 LINEA (USB bus reset), 11 LINEB (USB suspend), 12 CARRIER, 13 LINKPULSE, 14 COLLISION, 15 JABBER. Flags 3, 6, 7, 12 and 15 show a level; the others stay set until cleared.

Transmit timing: a frame armed with TXTIME = t has its first symbol on the helper output in cycle t and, after the pin flip-flop, on the pad from cycle t + 1, as an OUT event at time t (docs/isa.md 4.4). A link test pulse (TXCTRL bit 7) armed with TXTIME = t starts one cycle earlier: on the helper output in cycle t - 1, on the pad from cycle t. The output stage holds only the first cycle of a frame, the cycle in which the first bit is loaded; a pulse has no bit to load. The pulse width (6 cycles) does not change, and 16.7 ns is far inside the 16 ms +/- 8 ms link-pulse spacing of IEEE 802.3 14.2.1.1 (test_slu_link_pulse_start). The buffer holds 2 words; the thread adds the next word while a word is sent (one 16-bit word lasts 1,600 ns at 10 Mbit/s and 10,667 ns at 1.5 Mbit/s).

## 3 Pins

The host gives an output pin to a helper in the pin configuration (docs/host_port.md, WRITE_CFG pin words): helper drive, helper select (0 USB, 1 10BASE-Te) and the signal:

| Signal | USB | 10BASE-Te |
|---|---|---|
| 0 TXA | VPO (K = 1) | TD+ |
| 1 TXB | VMO (J = 1) | TD- |
| 2 TXC | VPO one cycle later | TD+ one cycle later |
| 3 TXD | VMO one cycle later | TD- one cycle later |
| 4 OE | 1 while the helper sends | 1 while the helper sends |
| 5 not OE | OE# (for the transceiver's active-low enable) | not OE |

The inputs are chosen with SEL: USB DIN = RCV (transceiver receive output), AUX0 = VP, AUX1 = VM; 10BASE-Te DIN = receive slicer output, AUX0 = squelch comparator. DIN is sampled on both clock edges through a 2-stage synchronizer pair (rising and falling edge, formal/cdc.sby); AUX0 and AUX1 use the chip input synchronizers.

The analog parts are on the board: a USB low-speed transceiver (for example TUSB1106) and, for 10BASE-T, the transmit resistor network, the receive comparators and the magnetics.

### 3.1 Receive loop

The receive loop (src/pe_slu_loop.v) recovers the bit clock from the line edges: a phase accumulator counts the position in the bit in 1/4,096 sample steps, each accepted edge corrects it, and the bit is the line level at the decision point. USB: 40 samples per bit (one per cycle), every edge in a window of 20 samples sets the phase (hard resynchronization). 10BASE-Te: 12 samples per bit (two per cycle, on both clock edges), a second-order loop with gain steps after 8 and 24 edges.

In a cycle with two samples, the loop uses at most one edge: an edge at the second sample right after an edge at the first is a pulse of half a cycle (8.3 ns at 60 MHz), which is not a 10BASE-T symbol (the shortest half bit with 13.5 ns of jitter is 50 - 27 = 23 ns, IEEE 802.3 14.3.1.3.1). With this rule the logic of the second sample does not wait for the edge logic of the first, which brought the slow-corner path through the loop from -1.449 ns to inside the 60 MHz period (docs/decisions.md entry 4). test_slu_loop_equiv compares the loop with its bit-exact model in every cycle.

## 4 Measured results

| Test (test/test_slu.py) | Result |
|---|---|
| test_slu_loop_equiv | receive loop RTL = bit-exact model in every cycle: 50 10BASE-T frames (jitter up to 15 ns, +/-200 ppm), 50 USB packets (jitter up to 75 ns, +/-1.5 %) |
| test_slu_usb_tx, test_slu_chip_usb_tx | line levels = NRZI(stuff(SYNC, PID, data, CRC16)) of USB 2.0 7.1.8-7.1.10 and 8.3.5; EOP SE0 84 cycles (TLEOPT 1.25-1.50 us), J 40 cycles |
| test_slu_usb_rx, test_slu_chip_usb_rx | DATA0 with 8 bytes at 0, +/-500 ppm and +/-1.5 % (USB 2.0 7.1.11 low-speed function); CRC5 token; corrupted CRC16 rejected; 7 ones flagged |
| test_slu_eth_tx, test_slu_chip_eth_tx | 928 half bits = IEEE 802.3 Manchester of preamble, SFD, 46 bytes and FCS; TP_IDL hold 13.5-27 cycles (Figure 14-11); link test pulse 6 cycles (Figure 14-13) |
| test_slu_eth_rx, test_slu_chip_eth_rx | 20 frames of 64-200 bytes, zero-crossing jitter up to +/-13.5 ns (14.3.1.3.1), +/-100 ppm: data and CRC-32 correct; link pulses classified |
| test_slu_tx_late_underflow | late TXTIME: TXLATE and start at write + 4, or no start with TXCTRL bit 6; empty buffer at the start or inside a frame: TXUNDER, then the tail and TXDONE |
| test_slu_eth_collision | carrier while sending: COLLISION, 32 jam bits, TP_IDL tail, TXDONE (the test found a 31-bit jam, fixed) |
| test_slu_jabber | JABBER after 4,194,097 cycles of continuous sending (2^22 = 4,194,304, 512-cycle ticks), the transmitter stops, no frame starts while JABBER is set; JABBER clears at the 65,536th idle tick |
| test_slu_usb_line_conditions | SE0 of 17 ticks (272 cycles): no LINEA; 21 ticks: LINEA (bus reset); J for 14,000 ticks: no LINEB; 15,000 ticks (240,000 cycles, 4.0 ms): LINEB (suspend) |
| test_slu_chip_eth_events, test_slu_chip_usb_events | a thread waits for TXDONE with WAIT on event input 14 (10BASE-Te) and 13 (USB) and wakes after the frame; the delayed taps TXC/TXD on pins: TXC = TXA one cycle later, TXD = not TXC |
| USB low-speed receive over many packets (A1 study, the loop model, 2,000 packets per case: SYNC, PID, 8 bytes and 16 bits, stuffed, NRZI; ideal sampler at 60 MHz) | 2,000 of 2,000 at +/-600 ppm with uniform transition jitter up to +/-150 ns, and at +/-1.5 % up to +/-75 ns; at +/-1.5 % with +/-150 ns: 1,990 (-1.5 %) and 1,960 (+1.5 %) of 2,000 |
| 10BASE-T receive margin (A1 study, 1,000 frames of 1,518 bytes per case, the loop model on sampled streams of 60 MHz both edges with comparator skew +/-1.5 ns) | 1,000 of 1,000 frames at the 13.5 ns jitter limit at +/-200 ppm and duty 0.50 / 0.58; first failing jitter amplitude at least +1.34 ns above 13.5 ns when up to 3.0 ns of the jitter depends on the bit pattern (the rest uniform), +2.00 ns with uniform jitter; with 4.0 ns pattern share the margin at -200 ppm is +0.45 ns, with 5.0 ns one frame of 1,000 fails at -200 ppm, duty 0.58 |

These are logic simulations at 60 MHz with ideal pads; the pad delays, the transceiver and the comparators are not in them (the layout timing is in docs/signoff.md).

Synthesis area (typical corner, Yosys): USB helper 39,808 um2, 10BASE-Te helper 52,161 um2. The clocked and NRZ modes of section 6 add 4,355 um2 and 37 flip-flops to the USB helper (39,808.84 to 44,163.78 um2 in one synthesis job with both forms, same method); the 10BASE-Te helper keeps its 448 flip-flops.

## 5 Notes for firmware

Found while writing the USB device and 10BASE-T station programs (`fw/usb_dev.s`, `fw/eth_10bt.s`, [protocols.md](protocols.md)):

| Item | Behaviour | What the firmware does |
|---|---|---|
| Before SEL is written | the helper has no input pins and reads SE0; after 304 cycles the USB helper sets LINEA (bus reset) | write SEL first, then clear every STATUS flag |
| RXINFO bits 15:7 | the word count of the frame has 9 bits: it wraps above 511 words (1,022 octets) | count the stored words; use RXINFO only for the bits in the last word |
| USB reply time | the start time is TXTIME = TSTAMP + n; a buffer that is empty at TXTIME - 3 gives TXUNDER and no reply | put the reply words in the buffer before the end of packet (an IN token's reply at its first word, a data packet's handshake at its first word), then arm TXTIME at the end of packet; abort when the packet turns out wrong. With n = 224 the reply is 5.65 to 5.68 bit times after the host EOP |
| TXCTRL fields | bits in the last word, CRC append and link pulse are registers read when the frame starts; abort (bit 8) and empty (bit 10) act at once | a handshake can be put in the buffer with TXLAST while TXCTRL is 0 (16 bits, no CRC) |
| Own packets on USB | a transceiver feeds the device's own packets back into the receiver | after TXDONE, clear the receive flags and read the receive buffer empty |
| LINKPULSE (10BASE-Te) | the first preamble half bit of a frame can set it before the carrier is up | look at CARRIER first; clear LINKPULSE at the end of each frame |

## 6 Clocked and NRZ modes of the USB helper

The USB helper also sends and receives clocked serial lines (SPI controller, JTAG, the SWD clock) and NRZ lines (UART), so that these buses run at up to 15 MHz instead of the rates that a thread reaches with OUT and SAMPLE (docs/protocols.md). The thread writes four configuration words; after reset the helper is the USB helper again.

| Index | Word | Writable bits | Use |
|---|---|---|---|
| 0 | MODE | 1:0 code (0 NRZ, 3 clocked; 1 NRZI is USB), 3 receive on, 4 bit stuffing, 9 USB line | clocked: 0x000B (receive on) or 0x0003; NRZ (UART transmit): 0x0000; USB again: 0x0219 with TXPER 40 and LTC 0 |
| 1 | TXPER | 7:0 (bits 15:8 are 0) | NRZ: cycles a bit; clocked: cycles a clock half period (a bit = 2 x TXPER cycles); 1 to 255 |
| 8 | SEL | 15:0 | 3:0 DIN input, 15:12 clocked sample delay (0 to 15 cycles after the sampling clock edge) |
| 18 | LTC | 12:8 | 8 clock idle level (CPOL), 9 data out with the leading clock edge (CPHA), 10 receive frames that start at a DIN falling edge (UART receive), 12 sample the falling-edge copy of DIN (half a cycle earlier) |

Pins (pin configuration signal numbers of section 3): clocked mode: 0 TXA = data out, 1 TXB = clock; NRZ: 0 TXA = line. 2 and 3 are the same one cycle later, 4 and 5 OE and not OE. DIN comes from SEL[3:0] through the both-edge synchronizer of section 3.

Frames: the thread puts the bits in 16-bit words, the first bit on the line in bit 0, and gives the bit count of the last word in TXCTRL[4:0], as for USB. For MSB-first buses (SPI, most flash commands) the thread reverses the bit order of each byte. A frame starts at TXTIME. In clocked mode with receive on, the helper takes one DIN sample per bit at the set delay after the sampling edge and gives the received bits in words in the same order (first bit in bit 0); RXEOP is set when the last sample of the frame is in. The thread chip-select (or TMS, or SWDIO) is a thread pin driven with OUT events at times taken from TXTIME, so it is in step with the clock.

Edge-started receive (LTC[10], UART receive): with no transmit, a falling DIN edge starts a frame of TXCTRL[4:0] samples (for example 10 = start, 8 data bits, stop), the first sample TXPER + sample delay cycles after the edge (TXPER >= 2). Each frame is one word with the frame in the top bits (for 10 samples: start bit in bit 6, data in bits 7 to 14, stop bit in bit 15), RXEOP is set, and the helper waits for the next edge at once.

| Item | Behaviour | What the firmware does |
|---|---|---|
| After START | the helper pins are outputs from the first cycle after START. In that cycle they show 0 (the helper's reset output), then the USB idle J (TXA 0, TXB 1) until the thread writes MODE; OE stays 0, so a USB transceiver does not drive the bus. With MODE written by the thread's second instruction, TXA is low for 9 cycles (150 ns) and TXB for 1 cycle (test_slc_chip_start_levels). At 15 and 7.5 Mbaud a UART receiver can take the 150 ns low as one byte (0xFE, 0xFF); below about 3.3 Mbaud it is shorter than half a bit. A clock on TXB makes one 16.7 ns low pulse, then stays high until the clock configuration: with pull-ups on /CS, TMS and SWDIO (the thread pins are released until their first `OUT`), an SPI device ignores it and a JTAG or SWD target sees at most one clock with TMS or SWDIO high | write MODE in the first instructions; for a UART on the helper, discard the first received byte after START or start the protocol with a break; begin JTAG with Test-Logic-Reset (TMS high for 5 TCK cycles, the host sequence of fw/jtag.s) and SWD with the JTAG-to-SWD sequence and a line reset (the host words of fw/swd.s), as test_slc_jtag and test_slc_swd do |
| Time between frames | after each transmitted frame the helper keeps tail 1 for 84 cycles (1.40 us; the fixed USB SE0 time) before it accepts a new TXTIME: data stays at the last bit, the clock at its idle level, OE on | read TXCTRL until the transmit state is 0 before TXTIME; put long transfers in one frame (a frame has no length limit while the buffer is fed) |
| Buffers | 2 words each way, as for USB: 16 bits last 64 cycles at 15 MHz | one thread feeds TXDATA and a second thread drains RXDATA (test_slu_chip_spi_flash), or one thread alternates both |
| Sample delay | the right SEL[15:12] and LTC[12] depend on the clock rate, the pin path (pads, any board or multiplexer delay) and the device output delay | use a setting from the middle of the window the tests measured, or measure the window on the board with a known read (for example a flash ID) |

Measured results (test/test_slu_clk.py on the helper with the chip pin path as delay parameters: one pin flip-flop, output pad 1.13 to 3.32 ns, input pad 0.26 to 0.45 ns (docs/verification.md section 9), and a multiplexer of 0 to 10 ns each way; 4 corners from fast pads with no multiplexer to slow pads with 10 ns each way, and both skews). These are logic simulations with delay parameters, not electrical simulations.

| Test | Device model | Result |
|---|---|---|
| test_slc_spi_flash_15mhz | serial NOR flash at its pins, times of a 50 MHz part (clock high/low >= 45 %, set-up 1 ns, hold 2 ns, output valid <= 6 ns, output hold >= 1.5 ns, /CS times 3/3/10 ns), commands 9Fh and 03h | 15 MHz (TXPER 2), SPI modes 0 to 3: 5 sample settings read the ID at all 4 corners in each mode (delay 3, 4, 5 with the rising-edge copy, 4, 5 with the falling-edge copy); 256-byte 03h reads correct with 0 timing errors at the flash pins |
| test_slc_spi_flash_7m5_30mhz | same | 7.5 MHz, modes 0 to 3: 13 settings at all corners (delay 1 to 7 rising-edge copy, 2 to 7 falling-edge copy); 30 MHz mode 0: 1 setting at all corners, 256-byte reads correct; control (30 MHz, delay 0, 10 ns multiplexer) reads the ID wrong, as required |
| test_slc_jtag | two TAPs in a chain (IR 4 and 5 bits), output 7 ns after the falling edge, set-up 1 ns, hold 2 ns; TMS from a thread pin | 15 MHz shift: both IDCODEs, BYPASS, a 1,000-bit DR scan through Pause-DR, correct at all 4 corners; 30 MHz shift: 2 settings correct at all corners |
| test_slc_swd | SW-DP target, output 2 or 25 ns after the rising edge, set-up/hold 2 ns, SWCLK high/low >= 20 ns; SWDIO from a thread pin | 7.5 MHz SWCLK: line reset, DPIDR, SELECT and AP writes, posted AP read with RDBUFF correct at all 4 corners and both output delays |
| test_slc_uart_tx | independent receiver at each bit middle | 15 and 12 Mbaud: 64 back-to-back 8N1 bytes in one frame decoded equal; every edge on the bit grid |
| test_slc_uart_rx | sender at -2.5 % and +2.5 %, 16 start phases inside a clock period | 15 and 7.5 Mbaud: 1,024 bytes each and 256 back-to-back bytes correct, a 0 stop bit reported; control at +/-2.8 % and 15 Mbaud loses or corrupts bytes, as required |
| test_slu_chip_spi_flash | the flash model on the chip pins (uio0 clock, uio1 data, uio2 /CS, ui_in[0] data in), slow pads + 10 ns each way | the whole chip with two threads: 03h read of 256 bytes at 15 MHz in mode 0, equal to the flash, 0 timing errors at the flash pins |

With the USB configuration (no write to MODE, TXPER or LTC) the helper is the same as the USB-only helper at every output in every cycle: an equivalence proof of the two RTL forms (all 3,817 compared signals proven by induction), and a control with MODE writes allowed fails on exactly the writable MODE bits.

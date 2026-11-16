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
| 10BASE-Te | 32 to 47 | 14 | Manchester at 3 cycles per half bit, receive phase loop on both clock edges, SFD detection after 16 accepted edges, CRC-32 checked and appended, TP_IDL hold 16 cycles, link test pulse 6 cycles (100 ns), squelch qualifier (4 edge spacings of 3 to 16 half cycles), link-pulse classifier (2 to 12 cycles), collision (receive carrier while sending) with a 32-bit jam, jabber (transmit longer than 2^22 cycles = 69.9 ms stops it for 2^25 cycles = 0.559 s) |

Both helpers are the same RTL (pe_slu) with a fixed configuration (parameter SPEC); only the pin choice SEL is writable. The fixed values are in src/pe_slu.v (F_*) and are the values that the tests use.

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
| 12 | CFGADR | - | configuration index (only 8 = SEL is writable) |
| 13 | CFGDAT | - | configuration word: SEL bits 3:0 DIN input, 7:4 AUX0 input, 11:8 AUX1 input (input numbers of docs/isa.md 5.1; 13 to 15 = none) |
| 14 | TXLAST | - | add the last word of the frame (its bit count from TXCTRL bits 4:0) |

STATUS flags: 0 TXDONE, 1 TXLATE, 2 TXUNDER (buffer empty in a frame), 3 RXWORD (a word is waiting), 4 RXSOP, 5 RXEOP, 6 CRCA_OK (last frame), 7 CRCB_OK, 8 STUFFERR, 9 RXOVER (word lost), 10 LINEA (USB bus reset), 11 LINEB (USB suspend), 12 CARRIER, 13 LINKPULSE, 14 COLLISION, 15 JABBER. Flags 3, 6, 7, 12 and 15 show a level; the others stay set until cleared.

Transmit timing: a frame armed with TXTIME = t has its first symbol on the helper output in cycle t and, after the pin flip-flop, on the pad from cycle t + 1, as an OUT event at time t (docs/isa.md 4.4). The buffer holds 2 words; the thread adds the next word while a word is sent (one 16-bit word lasts 1,600 ns at 10 Mbit/s and 10,667 ns at 1.5 Mbit/s).

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

These are logic simulations at 60 MHz with ideal pads; the pad delays, the transceiver and the comparators are not in them (the layout timing is in docs/signoff.md).

Synthesis area (typical corner, Yosys): USB helper 39,808 um2, 10BASE-Te helper 52,161 um2.

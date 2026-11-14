# Host SPI port of the protocol emulator

| Item | Value |
|---|---|
| Document | docs/host_port.md |
| RTL | src/pe_host_port.v, synchronizers in src/project.v |
| Host model | test/host_spi.py (independent of the RTL; the tests use it) |
| Decisions | docs/decisions.md entries 8 (STOP) and 9 (host port bandwidth) |
| Language | ASD-STE100. Command names, signal names and file names are technical names. |

## 1 Function

The host (the RP2350 of the demo board) uses this port to:

- stop and start the threads,
- write and read the 128 x 16 program memory,
- write the configuration (start address of each thread, RUN mask, owner and open-drain mode of each output pin, docs/isa.md 2.2, 5.2, 5.3),
- read the chip ID and the status,
- write words into the host-to-thread FIFOs (PUSH) and read words from the thread-to-host FIFOs (PULL), docs/isa.md 8.5.

The chip changes a protocol pin only when a program tells it to. After reset no thread runs, and each processor output has V = 1, OE = 0 (docs/isa.md 2.4): uo_out[6:0] = 1 (UART TX idle level), all uio pins are inputs.

## 2 Pins and SPI mode

| Pin | Name | Direction | Function |
|---|---|---|---|
| ui_in[7] | HOST_SCK | in | SPI clock |
| ui_in[6] | HOST_CS_N | in | chip select, active low |
| ui_in[5] | HOST_MOSI | in | data, host to chip |
| uo_out[7] | HOST_MISO | out | data, chip to host (a flip-flop output; 0 while CS_N is high) |

SPI mode 0: SCK is low when idle. The host changes MOSI after the falling SCK edge. Both sides sample at the rising SCK edge. Each byte is sent MSB first. A transaction starts when CS_N goes low. Its first byte is the command. The transaction ends when CS_N goes high; a byte that is not complete has no effect.

The chip samples SCK, CS_N and MOSI with its clock (f_clk = 50 MHz, T = 20 ns) through 2-stage synchronizers with the same delay. Thus the port sees each SCK edge 1 to 2 clock cycles after it occurs at the pin, and it takes MOSI from the same cycle.

MISO: the chip sets the next bit at the end of the clock cycle in which it sees the rising SCK edge. Thus MISO changes after the rising SCK edge that sampled the bit before, not after the falling edge. The host samples MISO at the next rising edge, nearly a full SCK period later (section 6).

## 3 Timing rules for the host

| Rule | Value at 50 MHz | Reason |
|---|---|---|
| SCK high time and SCK low time | 2 T = 40 ns or more | the synchronizer must take each level in at least one clean sample |
| CS_N low to the first rising SCK edge | 2 T = 40 ns or more | CS_N and SCK have the same synchronizer delay; the port must see CS_N low first |
| Last rising SCK edge to CS_N high | 2 T = 40 ns or more | the port must see the last edge while CS_N is low |
| CS_N high time between transactions | 2 T = 40 ns or more | the port must see the end of the transaction |
| MOSI set-up and hold at the rising SCK edge | T + pin skew, or more | the port takes MOSI from the cycle of the edge; mode 0 gives half an SCK period |
| SCK period | 100 ns or more (10 MHz or less) | section 6 |
| MISO hold time that the host needs after its sampling edge | less than 2 T = 40 ns | MISO changes 2 T or more after the rising SCK edge at the pin (section 6) |

## 4 Commands

Each command code is {c, not c}: the high nibble c, the low nibble its complement. Two codes differ in 2 bits or more, so one wrong bit gives no command, and a stuck MOSI line (0x00 or 0xFF) gives no command. A byte that is not a valid code sets CMD_ERR and the rest of the transaction has no effect.

| Code | Name | Allowed when | MOSI bytes after the code | MISO bytes |
|---|---|---|---|---|
| 0x1E | READ_ID | always | any (4 dummy bytes) | 4 ID bytes: 0x50, 0x45, 0x02, 0x03 ("PE" and two version bytes), then 0x00 |
| 0x2D | READ_STATUS | always | any (up to 7 dummy bytes) | 7 status bytes (section 5), then 0x00 |
| 0x3C | STOP | always | none | none |
| 0x4B | START | threads stopped | none | none |
| 0x5A | WRITE_MEM | threads stopped | address A (bits 6:0), then words (2 bytes each, high byte first) | 0x00 |
| 0x69 | READ_MEM | threads stopped | address A, then 2 dummy bytes for each word | after the address byte: the words at A, A + 1, ... (high byte first) |
| 0x78 | WRITE_CFG | threads stopped | configuration address A, then words | 0x00 |
| 0x87 | PUSH | threads running | thread n (bits 1:0), then words | 0x00 |
| 0x96 | PULL | threads running | thread n, then 1 dummy byte, then 2 dummy bytes for each word | after the thread byte: the count byte K (words in the out-FIFO of thread n, 0 to 4), then the words; words after the first K are 0x0000 and are not taken from the FIFO |
| 0xA5 | CLEAR | always | none | none; clears CMD_ERR and PUSH_DROP |

Addresses: WRITE_MEM, READ_MEM and WRITE_CFG add 1 to the address after each word; the address wraps from 127 to 0. Configuration addresses (src/pe_config.v): 0 to 3 start address of thread 0 to 3 (bits 6:0); 4 RUN mask (bit n = thread n runs at START); 16 + p for output pin p = 0 to 14: bits 1:0 owner thread, bit 2 owner enable, bit 3 open drain. Other addresses have no register.

A command that is not allowed in the present state does nothing, sets CMD_ERR, and the rest of its transaction has no effect (MISO 0x00).

## 5 Status bytes (READ_STATUS)

| Byte | Bits |
|---|---|
| 0 | bit 0 RUNNING (1 = threads started), bit 1 CMD_ERR (sticky), bit 2 PUSH_DROP (sticky), bits 7:3 0 |
| 1 | bits 3:0 RUN of thread 3 to 0 (bit n = thread n), bits 7:4 0 |
| 2 | bits 3:0 LATE of thread 3 to 0, bits 7:4 ERR of thread 3 to 0 (docs/isa.md 2.1) |
| 3 + n (n = 0 to 3) | bits 2:0 words in the host-to-thread FIFO of thread n (0 to 4), bits 6:4 words in the thread-to-host FIFO of thread n, bits 7 and 3 0 |

The host can stop a READ_STATUS transaction after the bytes that it needs (for example after byte 3 for thread 0).

## 6 Maximum SCK

Delay from a rising SCK edge at the HOST_SCK pin to the next MISO bit at the HOST_MISO pin:

| Part | Value | Source |
|---|---|---|
| Wait for the first synchronizer clock edge | 0 to T (20 ns) | asynchronous input |
| Second synchronizer stage | T (20 ns) | src/project.v, pe_sync2 |
| Edge detect and MISO flip-flop | T (20 ns) | src/pe_host_port.v |
| Core part, worst case | 3 T = 60 ns | RTL simulation: test_host_sck_limit (section 7) |
| Allowance: output pad and mux, input pad and mux, flip-flop clock-to-output, host input set-up | 27.4 ns | estimate: the competition FAQ gives about 10 ns round trip through the pads and mux (a Tiny Tapeout estimate, no CMOS5L silicon yet); the rest is not verified in this repository and is to be measured on the board |
| Total | 87.4 ns | |

Thus the SCK period must be 87.4 ns or more. The rated maximum is 10 MHz (100 ns, 12.6 ns margin). The SCK level rule (section 3) alone allows 12.5 MHz.

The smallest delay from the rising SCK edge to a MISO change is 2 T plus the clock-to-output and pad delays (40 ns or more), so the host has 40 ns of MISO hold time after its sampling edge.

Measured in RTL simulation with the independent host model (test_host_sck_limit): the host samples MISO 27.4 ns before each rising SCK edge, and the rising edges are 1 ns after a rising clock edge (worst synchronizer phase). SCK period 100 ns and 90 ns: READ_ID and READ_MEM correct; 80 ns: not correct.

Alternative (not used): MISO changes after the falling SCK edge (classic mode 0 target). Then half an SCK period must hold the 87.4 ns: period 174.8 ns or more, 5.7 MHz or less.

## 7 Timing diagram (WRITE_MEM, one word)

```
           byte 0: 0x5A (WRITE_MEM)        byte 1: address       bytes 2, 3: word
CS_N  ‾‾‾\________________________________________________________ ... ______/‾‾‾‾‾
           |<-- 40 ns or more -->|
SCK   _____________/‾‾\__/‾‾\__/‾‾\__/‾‾\ ... /‾‾\__/‾‾\ ... /‾‾\_____ ... _______
MOSI  -----<  0  ><  1  ><  0  ><  1  > ... ( 8 bits ) ... (16 bits) ------------
MISO  ____________________________________________________________ (0x00) _______
                 ^ sample            ^ sample
chip clock: the port sees each rising SCK edge 1 to 2 clock cycles later and takes
MOSI from the same cycle. The word is written in the clock cycle in which the port
sees the last rising SCK edge of the low byte (2 clock cycles after that edge with
the test bench phase of test/test_core.py).
```

READ_MEM and PULL (MISO):

```
            ... last bit of the address byte          first bit of the response byte
SCK   ______/‾‾‾‾\________________/‾‾‾‾\_____________/‾‾‾‾\______
MOSI  -----< a0  >---------------< x  >-------------< x  >------
MISO  --------------XXXX< d15                 >< d14       >------
            |<- 60 ns max ->| (3 clock cycles: synchronizer, edge detect, MISO flip-flop)
                            the host samples d15 at the next rising edge
```

## 8 STOP, START and reset

- After chip reset: threads stopped, configuration 0 (start address 0, RUN mask 0, no pin owner, push-pull), FIFOs empty, CMD_ERR = PUSH_DROP = 0. The memory does not change at reset (docs/isa.md 2.4).
- STOP holds the processor in reset: the threads, the FIFOs, the output event slots and the pin registers (all pins V = 1, OE = 0 at once). The memory, the configuration and the input synchronizers do not change. A STOP in a UART frame thus sets UART_TX high at once.
- START (only when stopped) releases the processor at the end of the clock cycle in which the port sees the last bit of the START byte (cycle k0). In cycle k0 + 1 the core loads the start address and RUN of each thread, and the cycle counter goes to 0 at the end of that cycle. Thus cycle 0 of docs/isa.md 3.1 (thread 0 has the slot) is cycle k0 + 2.
- STOP and then START give the same state as chip reset and then START, with the same memory and configuration: no event of the last run occurs and no word of the last run is in a FIFO.

Host rules for STOP (STOP cuts a frame or transfer in progress at once). Rules 1 and 2 follow from docs/decisions.md entry 8 (processor held in reset while stopped); the RTL test is test_r2_stop_pins (below):

1. End the transfer before STOP. Wait until the in-FIFO of each thread that drives a protocol is empty (status byte 3 + n, bits 2:0 = 0), and then wait for the last frame or transfer to end: one frame time for UART (the stop bit), the last byte and CS high for SPI (host word bit 8 in fw/spi_ctl.s), the STOP condition for I2C. Reason: a STOP inside a UART frame sets UART_TX high at once, so a receiver can get a wrong byte with a valid stop bit and no error flag; a STOP inside an SPI byte or an I2C byte leaves the device in the middle of the byte (an I2C target can then hold SDA low until it gets clock pulses).
2. While the chip is stopped, hold each SPI CS line high with a pull-up. On the demo board the host sets the RP2350 pad of the CS pin to pull-up (pull-up enable 1, pull-down enable 0) before STOP, and keeps it until START. For the CS pin of fw/spi_ctl.s, uio[0], this is RP2350 GPIO25 (UIO0 in the demo board firmware, tt-micropython-firmware src/ttboard/pins/gpio_map_dbv3.py). On an other board, use a pull-up resistor on each CS line. Reason: STOP releases all uio pins (OE = 0). The RP2350 pads reset with the pull-down on (RP2350 data sheet RP-008373-DS-3, Table 885, PDE reset 1) and the demo board firmware sets Pin.PULL_DOWN on these pins, so a released CS line goes low and the device stays selected while the chip is stopped. A flash device such as the W25Q32JV accepts a new instruction only after a high-to-low edge of /CS (W25Q32JV data sheet Rev. G, section 4.1): with CS low across STOP, the next transfer after START has no CS edge.

3. After the host sets the CS pull-up (rule 2), it waits about 3 us before STOP. Reason: the RP2350 pad pull-up is 32 to 86 kohm at IOVDD = 3.3 V (RP2350 data sheet RP-008373-DS-3, pad electrical characteristics, PDF page 1338). With a line load of 15 pF (the upper working load, design note R3 P6), the largest RC is 86 kohm x 15 pF = 1.29 us, so a released CS line is at 90 % of IOVDD 2.3 RC = 3.0 us after the pull-up is on (calculation). Alternatives: a 1.6 us wait (1.2 RC, 70 % of IOVDD) leaves no margin for a slower line; a 10 kohm resistor on the board (RC 0.15 us) needs a board part. The wait costs the host 3 us for each STOP. Decisions D7, D14.
4. I2C bus clear. If a STOP or a chip reset cut an I2C transfer (rule 1 not kept, for example after a host fault), a target can hold SDA low. After the next START, send these two words to the thread of fw/i2c_ctl.s before the next transfer: 0xFFE0 (START, nine released bits, STOP), then 0x0000 (check word: no arbitration check). The firmware gives nine SCL pulses and a STOP, and returns one reply word (bits 8 to 0: the SDA levels). The firmware needs no change and no memory word. Ground: NXP UM10204 Rev. 7.0, 3.1.16 "Bus clear" (printed page 19): if SDA is stuck low, the controller sends nine clock pulses. Measured in the model (test/test_fw_full.py test_r3_i2c_bus_clear, words I2C_CLEAR): a cut at 20 points (read bits 1 to 8, write bits 1 and 8, SCL high and low) leaves SDA low at 16 points, and the next transfer fails at those 16 without the clear; with the clear it is correct at 20 of 20, with nine pulses, the STOP and all Fast-mode times. Alternatives: no clear (16 of 20 fail); a bus-clear routine in the firmware (more memory words for the same pulses). Decision D13.
5. Host timeout for a stuck SCL. fw/i2c_ctl.s waits for each SCL rise with WAIT and no timeout, because the I2C-bus protocol sets no limit on how long a target stretches the clock (UM10204 Rev. 7.0, 4.2.2, printed page 31). Thus a target that holds SCL low stops the thread with no report: the host gets no reply word (model, test_m3_i2c_stuck_scl: no word for 20 ms of simulation, thread status 0, waiting in WAIT SCL RISE, SCL released by the chip, SDA still driven by the chip). The host therefore starts a timer after each byte word pair. If the reply word does not come within T_TO:
   - send STOP (all uio pins released, rule 1 measurement below);
   - read the SCL and SDA lines with the RP2350 inputs on uio[6] and uio[7];
   - if SCL is low, a target holds it: reset the target with its reset input, or cycle its power (UM10204 3.1.16); the chip cannot clear it;
   - if SCL is high and SDA is low, START and send the bus clear of rule 4.

   T_TO = 2 x (9 SCL periods) + the longest clock stretch in the data sheet of the target + the polling period of the host. A Fast-mode byte with no stretching takes 1,224 clock cycles (24.5 us at 50 MHz, test_m3_i2c_stuck_scl). If the data sheet gives no stretch limit, use 35 ms, the SMBus limit (UM10204 4.2.2). Alternatives: a WAIT with a timeout in the firmware (memory words at each of the 3 WAIT instructions, and a timeout value in every byte); a hardware watchdog (area). Decision D13.

RTL measurement of the pins (test_r2_stop_pins, fw/spi_ctl.s on thread 0, STOP 150 cycles after CS low): before STOP uio_oe = 00001011 (CS, MOSI, SCK driven), uio_out = 11111100 (CS 0); after STOP uio_oe = 00000000, uio_out = 11111111, uo_out[6:0] = 1, and no pin change in the 2,000 cycles after.

## 9 FIFO rule (host side)

- PUSH: a word for an in-FIFO that has 4 words in the cycle of the word is dropped and PUSH_DROP becomes 1. The thread side can only take words out, so a host that reads the count of the in-FIFO (status byte 3 + n) and then pushes 4 - count words or fewer never loses a word, also when the thread pulls in the same cycle.
- PULL: the count byte K is the number of words in the out-FIFO when the port sees the thread byte. Only the thread adds words, so the first K words are valid. The port takes a word from the FIFO when it loads it into MISO; words after the first K are 0x0000 and nothing is taken.
- PUSH and PULL use the FIFO state of the clock cycle in which the port acts, at the same clock edge as the thread slot of that cycle (docs/isa.md 8.5, M2b_core.md 9 item 1).

## 10 Host throughput

The tests named in this section are on the research branch r2-timing; the numbers are copied from their logs.

Use the fast methods:

- PUSH without a status read when the host knows the free space: the host reads the in-FIFO count once (status byte 3 + n), then keeps its own count (it adds the words it pushes; only the thread takes words out, so the real free space is never less than the host count). Push 4 - count words in one PUSH transaction.
- PULL with the count byte: one PULL transaction gives the count K and the K words (no READ_STATUS before it).

Measured port rates at 50 MHz (RTL, test_r2_host_throughput, host words per second, 4 words per transaction):

| Host method | SCK 100 ns (10 MHz) | SCK 90 ns |
|---|---|---|
| READ_STATUS, then PUSH of the free words (status first) | 327,869 | 363,616 |
| PUSH of 4 words, no status read | 493,827 | 547,945 |
| PULL of 4 words with the count byte | 449,438 | 498,753 |
| PUSH 4 and PULL 4 in turn | 470,588 | 522,193 |

Limit: any three protocols at full rate, not all four. The host words that the example firmware needs at its highest rates (model, test_r2_host_need: UART 1 Mbit/s 8N1 both ways, SPI H 25, I2C Fast-mode with no clock stretching) are 297,349 words/s to the chip and 253,210 words/s from the chip, 550,559 words/s in total. With the fast methods at SCK 10 MHz this is 297,349 / 493,827 + 253,210 / 449,438 = 116.5 % of the port: all four at full rate do not fit. Each set of three fits: UART RX + SPI + I2C 96.3 % (the largest, almost no margin), UART TX + SPI + I2C 94.3 %, UART TX + UART RX + SPI 89.7 %, UART TX + UART RX + I2C 69.3 %. To run all four, lower the rate of one protocol (for example SPI) until the total is under 100 %.

## 11 System clock and host constants

The declared and timed clock is 50 MHz (info.yaml clock_hz, src/config.json CLOCK_PERIOD 20). Every clock-dependent value of the firmware is a host constant: a host word at start (UART bit time B and sample point H, SPI half period H, the chunk time C and count K of fw/uart_tx_full.s) or an .equ that the host sets when it assembles the file (I2C times, SPI mode, the UART receiver ND1). The host keeps one table of these constants for each clock it uses. The values in sections 3 and 6 are for 50 MHz.

40 MHz mode (for 10BASE-T only): at 40 MHz a 10BASE-T half bit (50 ns) is exactly 2 clock cycles; at 50 MHz the edge grid error is 10 ns, more than the +/-8 ns of IEEE 802.3-2022 14.3.1.2.3. The demo board can make 40 MHz exactly (RP2350 80 MHz / 2) from its +/-10 ppm crystal, inside the +/-0.01 % of IEEE 802.3-2022 7.3.2. Limits at 40 MHz (measured; tests test_r2_clock_margins in the model and test_r2_host_sck_clocks in RTL):

| Item | 40 MHz | 50 MHz |
|---|---|---|
| Host SCK period, all host phases (smallest correct) | 102 ns or more (9.8 MHz); rated 8.7 MHz to keep the 12.6 ns margin of section 6 | 88 ns; rated 10 MHz |
| UART receiver at 1 Mbit/s, stop bit sampled (ND1 = 9) | best H 12: -3.1 / +3.4 %, does not meet +/-3.3 % | H 15: -3.7 / +3.4 % |
| UART receiver at 1 Mbit/s, stop bit not sampled (ND1 = 8) | H 18: -5.9 / +5.7 % | H 23: -5.8 / +5.8 % |
| UART receiver at 921,600 bit/s, stop bit sampled | B 43 (+0.937 % divider error): -3.3 / +3.4 % against B, under 3.3 % on one side against the nominal rate | B 54: -3.7 / +3.8 % against B |
| UART receiver at 921,600 bit/s, stop bit not sampled | -5.8 / +5.9 % | -5.8 / +5.9 % |
| SPI controller (fw/spi_ctl.s), smallest H | 25 cycles = 800 kHz (D 0) | 25 cycles = 1.0 MHz |
| I2C Fast-mode (UM10204 Table 11 minimum times) | pass, 360.4 kHz | pass, 396.8 kHz |

Thus at 40 MHz the UART receiver at 921,600 and 1,000,000 bit/s must not sample the stop bit (assemble fw/uart_rx_full.s with ND1 = 8; there is then no framing-error report), and the host SCK must be 102 ns or longer. Do not use 60 MHz: the slow-corner flip-flop setup slack at 60 MHz was -1.773 ns and -1.219 ns in two gds runs, and +0.344 ns only with timing-driven placement and 10 max-slew violations; at 50 MHz it is +1.560 ns (gds 40955889046, nom_slow_1p08V_125C).

## 12 Host program and firmware format (decision D12)

Host program: on the demo board the host port is on RP2350 GPIO 24 (HOST_SCK), 23 (HOST_CS_N), 22 (HOST_MOSI) and 40 (HOST_MISO) (tt-micropython-firmware, src/ttboard/pins/gpio_map_dbv3.py, read in design note R3). GPIO24 has no SPI clock function (RP2350 data sheet RP-008373-DS-3, Table 731 GPIO24_CTRL FUNCSEL, printed page 584: spi1_rx, uart1_tx, i2c0_sda, pwm, sio, pio0 to pio2, clocks_gpout_2, usb), so no RP2350 SPI block can drive HOST_SCK. The host is a PIO program, and a PIO program sets the time at which it samples MISO.

Rules for the PIO host:

1. Sample MISO a set time after each rising SCK edge, not before the next edge. The next bit comes to MISO 2 T to 3 T after the rising edge at the pin (section 6), so the window is about one SCK period less one clock cycle wide. RTL measurement, no pad delay, sample times that are correct at all 10 host phases (test_r3_host_late_sample; R3 on branch r3-host-load, and the same result on the M3a RTL):

   | SCK period | Sample time after the rising SCK edge that passes |
   |---|---|
   | 100, 90, 80 ns | 0.5 to 39 ns (every time tried) |
   | 70 ns | 0.5 to 30 ns |
   | 60 ns | 0.5 to 25 ns |
   | 50 ns | 10 to 20 ns |
   | 44 ns | 20 ns |

2. Rating: SCK 100 ns (10 MHz) until bring-up step T05. At T05, sweep the sample time at SCK 100 ns and 80 ns and use the middle of the passing window; use 80 ns (12.5 MHz) only after T05 shows a window there. The SCK level rule of section 3 (2 T or more for each level) gives 80 ns as the shortest period. Shorter periods in the table are RTL results only.
3. A host that samples before its next rising edge (an SPI block, or the model SpiTimed of the tests) keeps the limits of section 6 (87.4 ns, rated 100 ns).

Firmware format: the default is the present format of the firmware files (one host word for each UART frame and each SPI byte, two words for each I2C byte; file headers). fw/uart_tx_full.s 15 + fw/uart_rx_full.s 13 + fw/spi_ctl.s 26 + fw/i2c_ctl.s 49 = 103 of 128 words, 25 words free (test_fw_full_word_count). The packed files with two bytes in one host word (fw/spi_pack.s, fw/uart_tx_pack.s, fw/uart_rx_pack.s, on the research branch a2-fw-pack) are optional loads that a host can choose after fabrication; SPI first, because it gives the largest load reduction for its words (+4 words, all-four load 116.0 % to 88.4 % at SCK 100 ns, design note R3 3.2). They are not needed: no requirement asks for all four protocols at full rate at the same time (section 10).

## 13 SPI target (fw/spi_tgt.s)

Requirement: A1 section 8.2 (SPI target, correct MISO in all four modes) and A1 section 10 step T13 (correct at 1 MHz, record the maximum). The pins are those of the SPI controller firmware with the other direction: CS = uio[0], MOSI = uio[1], SCK = uio[3] are inputs; MISO = uio[2] is the output of the thread (owner: the thread, push-pull). fw/spi_ctl.s and fw/spi_tgt.s use the same pins, so load one of them.

Host settings when it assembles the file: SE = 1 for modes 0 and 3, SE = 0 for modes 1 and 2 (the edge at which the controller samples MISO); GAP (cycles) for the SCK rate, see the rules below.

Host words: one word for each MISO byte, the byte in bits 9 to 2. For a transfer of N bytes, give N + 1 words; the last word is not sent. With no word in the FIFO, the target sends 0xFF. The thread gives one word for each byte: bits 7 to 0 are the MOSI byte.

Controller limits, measured in the model against an SPI controller model that does not use the firmware or the processor model (test_m3_spi_tgt, all four modes, 4 start phases, transfers of 5 and 2 bytes). Values in clock cycles of the chip; ns at 50 MHz:

| Item | Limit | At 50 MHz |
|---|---|---|
| SCK half period | 25 or more (24 fails) | 500 ns or more: SCK up to 1 MHz (800 kHz at 40 MHz) |
| MOSI held after the sample edge | 21 or more | 420 ns; at 1 MHz MOSI changes at the shift edge, 500 ns after the sample edge: 80 ns margin |
| CS low to the first SCK edge | 21 or more (modes 0, 2); 1 or more (modes 1, 3) | 420 ns |
| MISO setup before the sample edge | 11 or more in a transfer; first bit: CS lead - 19 | 220 ns; first bit at CS lead 25: 120 ns |
| MISO hold after the sample edge | 19 or more | 380 ns |
| Pause between the bytes of a transfer | 2 x half period + pause <= GAP + 16 (GAP 200: pause <= 166 at half period 25) | |
| Next CS low after the last sample edge | GAP + 57 or more (GAP 200: 207 cycles CS high in modes 0, 2 and 232 in modes 1, 3 at half period 25) | 5.1 us at GAP 200 |
| MISO released after the last sample edge | GAP + 39 or less | 4.8 us at GAP 200 |
| Slowest SCK | GAP <= 4,095 (TADD), so half period <= 2,055 | about 12 kHz; 100 kHz (GAP 600) and 13.2 kHz (GAP 3,900) measured correct |

Rules for the controller and the host:

1. SCK up to 1 MHz at a 50 MHz chip clock (T13). The limit comes from the instruction rate, not from the pins: one instruction every 4 cycles with a lead of 4 cycles (docs/isa.md 3.1, 4.2) gives 49 cycles between the sample edges at the start and at the end of a byte. The A1 estimate of about 5 MHz (G1 A-OWN-11) does not hold.
2. The target ends a transfer when no sample edge comes GAP cycles after the end of a byte: set GAP so that 2 x half period + the longest pause between bytes is GAP + 16 or less, and keep CS high GAP + 57 cycles or more after the last sample edge. MISO stays driven up to GAP + 39 cycles after the last sample edge: with other targets on the same MISO line, wait this time before you select another target.
3. CS low to the first SCK edge: 21 cycles or more in modes 0 and 2 (420 ns at 50 MHz; an SPI controller that waits one half period at 1 MHz meets it).
4. Memory: fw/spi_tgt.s has 31 words. With fw/uart_tx_full.s, fw/uart_rx_full.s and fw/i2c_ctl.s: 15 + 13 + 31 + 49 = 108 of 128 words.

Alternatives measured: a thread that also waits for the shift edge (no SE setting) needs a half period of 49 cycles (510 kHz at 50 MHz). Two earlier orders of the same instructions met the half period of 25 but needed a CS lead of 29 cycles (1 MHz with a CS lead of one half period failed in modes 0 and 2), or a MOSI hold of 25 cycles (no margin at 1 MHz).

## 14 Firmware for other protocols (CAN, JTAG, SWD, PS/2, timed trigger)

These programs were written after the instruction set was fixed, each from its protocol specification. Each is tested in the reference model against a protocol model written from the same specification (test/test_flex.py). The file header of each program gives the full host word format, its pins, its sources and its limits. Values at 50 MHz.

| Program | Words, threads | Pins | Host words to the thread | Words from the thread | Measured limit (test) |
|---|---|---|---|---|---|
| fw/can.s (CAN 2.0A node) | 123, 2 (thread 0 at label tx, thread 1 at label rx) | CAN_RX = ui_in[4]; TXA = uio[4] (thread 0), TXB = uio[5] (thread 1), both open drain, joined on the board with a pull-up to the transceiver TXD | thread 0, each frame: M, then M + 1 words of the stuffed bit stream (SOF to the CRC, then CRC delimiter, ACK slot, ACK delimiter as 1, filled with 1), MSB first; the host makes the stuffing and the CRC-15 | thread 0: the bus levels of the last stream word (ACK slot bit: 0 = acknowledged), or 0xFFFF = arbitration lost (send the frame again); thread 1, each frame on the bus: the destuffed bits, then a CRC word (0 = correct) | 250 kbit/s with the sample point at 50 % or 55 %, 125 kbit/s up to 70 %, with the other node +/-0.5 %; 500 kbit/s and 1 Mbit/s do not fit (test_flex_can) |
| fw/jtag.s (JTAG controller) | 31, 1 | TCK = uo_out[0], TMS = uo_out[1], TDI = uo_out[2], TDO = ui_in[0] (pull-up on the board) | n (TCK cycles), then for each group of 16 cycles the TMS word and the TDI word (first cycle in bit 0) | for each group: the TDO bits (last cycle in bit 15) | TCK 1.25 MHz (40-cycle period), 1.157 MHz mean over a 64-bit scan; any TAP path, Pause states, chains (test_flex_jtag) |
| fw/jtag_tap.s (JTAG, TAP sequences in the firmware) | 67, 1 | as fw/jtag.s | command (bit 15: 1 = IR scan, 0 = DR scan; bits 9:0 = bits, 0 = reset), then the TDI words | for each TDI word: the TDO bits | TCK 1.389 MHz (36-cycle period); scans from and to Run-Test/Idle only (test_flex_jtag) |
| fw/swd.s (SWD host) | 62, 1 | SWCLK = uo_out[3], SWDIO = uio[4] (push-pull, released for the target) | raw cycles: 0x8000 + n, then n words of SWDIO bits; transaction: the request in bits 7:0, RnW again in bit 10, for a write in the same PUSH WDATA[15:0], WDATA[31:16] and the parity word | ACK word (ACK[0:2] in bits 13 to 15); for a read with OK: RDATA[15:0], RDATA[31:16], the parity bit in bit 14 | SWCLK 1.5625 MHz (32-cycle period), a DPIDR read in 1,596 cycles; the host makes the parity and the retries after WAIT (test_flex_swd) |
| fw/ps2_host.s (PS/2 system side) | 62, 1 | CLK = uio[4], DATA = uio[5], open drain, pull-ups on the board | a command: byte in bits 7:0, odd parity in bit 8, bit 9 = 1 | each frame: the 11 bits in bits 5 to 15; a word below 32: a frame that stopped; each command: bit 15 = acknowledge (0 = yes), 0xFFFF = no device clock in 16 ms | device clock 10 to 16.7 kHz, decodes up to 556 kHz (test_flex_ps2) |
| fw/ps2_dev.s (PS/2 device side) | 60, 1 | CLK = uio[4], DATA = uio[5], open drain | a frame: the 11 bits, start bit in bit 0 | 0x0000 = sent, 0xFFFF = stopped by the host (send it again); each received command: the 11 bits in bits 5 to 15 | clock 10, 12.5, 16.7 kHz set by .equ TSU, TLO, THI; line contention and acknowledge (test_flex_ps2) |
| fw/trigger.s (timed trigger) | 30, 1 | UART RX = ui_in[3] (input, can be shared with fw/uart_rx_full.s), TRG = uo_out[5] | B (bit time), the match byte, D (44 to 4,095 cycles) | the matched byte, one word for each pulse | pulse 3,892 cycles after the start edge at B 434, D 200 at all 16 start phases: 0 cycles of jitter in the firmware (test_flex_trigger) |

Memory: a program and the four example programs do not fit together (103 + 30 words or more, and they use all 4 threads). test_flex_word_count assembles every set of 2 to 4 programs that fits in 128 words and 4 threads; examples with no shared pin: UART TX + UART RX + JTAG + SWD 121 words; UART RX + SPI + I2C + JTAG 119; UART RX + SPI + I2C + timed trigger 118. CAN (123 words, 2 threads) fits only alone. PS/2, CAN, I2C and SWD need uio pins (only uio pins have an output enable), so next to SPI (4 uio pins) and I2C (2) only one of them fits.

Host port load: the JTAG firmware at its mean rate needs 144,600 words/s to the chip and 72,300 from the chip (45 % of the port at SCK 10 MHz, section 10); fw/jtag_tap.s 32 %; an SWD read is 1 word in and 4 out (34 % at 31.3 k reads/s). Calculation from the measured rates and section 10.

### 14.1 I2C target timing characterization

The host can measure a timing limit of an I2C target with fw/i2c_ctl.s and no firmware change: it assembles the file again with one time constant changed, writes a short transfer, and reads the ACK bits. For the SDA set-up time: a binary search on TLOW (the SDA change comes HD cycles after the SCL fall, so the SDA set-up time at the pins is TLOW - 6 cycles with HD = 4). Measured in the model against target models with set SDA set-up limits of 15, 20, 25 and 30 cycles: the smallest TLOW that works gives exactly the set limit, in 7 tries each, at 1-cycle (20 ns) resolution (test_flex_i2c_char). Limits: the search finds the set-up time at the chip pins; the pull-up rise time and the board add to it, so calibrate once with a known target. tHIGH cannot be swept below 1.08 us (TSMP 16 or more, THIGH2 + HD 36 or more, file header).

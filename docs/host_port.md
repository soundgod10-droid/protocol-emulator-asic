# Host SPI port of the protocol emulator (M2c)

| Item | Value |
|---|---|
| Document | docs/host_port.md |
| Milestone | M2 "Then make it programmable" (requirement R-15), run M2c |
| RTL | src/pe_host_port.v (A1 block D7), synchronizers in src/project.v |
| Host model | test/host_spi.py (independent of the RTL; the tests use it) |
| Design note | design/js_emulator/M2c_loader.md (grounds and measurements of each choice) |
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
| 0x1E | READ_ID | always | any (4 dummy bytes) | 4 ID bytes: 0x50, 0x45, 0x02, 0x03 ("PE", milestone 2, run c), then 0x00 |
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
| Allowance: output pad and mux, input pad and mux, flip-flop clock-to-output, host input set-up | 27.4 ns | M2c_loader.md section 1 (part of it is not grounded yet; bring-up T05, T09 measure it) |
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

## 9 FIFO rule (host side)

- PUSH: a word for an in-FIFO that has 4 words in the cycle of the word is dropped and PUSH_DROP becomes 1. The thread side can only take words out, so a host that reads the count of the in-FIFO (status byte 3 + n) and then pushes 4 - count words or fewer never loses a word, also when the thread pulls in the same cycle.
- PULL: the count byte K is the number of words in the out-FIFO when the port sees the thread byte. Only the thread adds words, so the first K words are valid. The port takes a word from the FIFO when it loads it into MISO; words after the first K are 0x0000 and nothing is taken.
- PUSH and PULL use the FIFO state of the clock cycle in which the port acts, at the same clock edge as the thread slot of that cycle (docs/isa.md 8.5, M2b_core.md 9 item 1).

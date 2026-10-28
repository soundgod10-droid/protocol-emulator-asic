# Instruction set of the protocol emulator processor (M2a)

| Item | Value |
|---|---|
| Document | docs/isa.md |
| Milestone | M2 "Then make it programmable" (requirement R-15), run M2a |
| Status | Proposal. The engineer must confirm the items with the mark [ENGINEER] (section 14). |
| Reference model | model/pe_model.py. If this document and the model do not agree, that is a fault. Report it. |
| Assembler | asm/pe_asm.py |
| Language | ASD-STE100. Instruction names, signal names and file names are technical names. |

Sources:

- [A1] design/js_emulator/A1_architecture.md (engineering workspace): section 2.2 pins, section 4.4 candidate D, section 7 area, section 8 verification.
- [D2] design/js_emulator/DECISIONS.md, decision D2: 128 x 16 latch memory for program and data, shared by the four threads.
- [R1] design/js_emulator/R1_uart_tx.md: M1 results. Section 9.4 item 3: the cell library has no flip-flop with enable, so each loadable register bit costs dfrbpq_1 + mux2_1 = 67.13 um2.
- [M1] src/pe_timed_out.v (event time and pin flip-flop), src/pe_sync.v (2-stage synchronizer), src/project.v (UART_TX on uo_out[4]).

## 1 Summary

- Four threads share one barrel processor. Thread n executes one instruction in each clock cycle c with c mod 4 = n. Thus, each thread executes one instruction every 4 cycles.
- The instruction word has 16 bits. The program and the data are in one shared memory of 128 words of 16 bits [D2]. An address has 7 bits.
- Each thread has 4 data registers r0 to r3 (16 bits), one time register T (16 bits), a program counter (7 bits) and 4 flags (Z, C, N, E).
- Pin events: OUT changes an output pin at time T + offset. SAMPLE reads an input pin at time T + offset. WAIT waits for an edge on an input pin, with a timeout at time T, and gives the time of the edge. The time comes from T, not from the time of the instruction [A1 section 4.4].
- ALU: MOV, ADD, SUB, AND, OR, XOR, NOT, CMP, TST, shifts and rotate, immediate forms. Compare and branch: 8 conditions on the flags. Loop counter: DJNZ on a data register. Subroutines: JAL and JR.
- Host: PULL reads a word from the host-to-thread FIFO. PUSH writes a word into the thread-to-host FIFO.
- Helper hooks: IOR and IOW read and write I/O ports. Ports 16 to 63 are for the helpers (CRC unit, line-code helpers, process monitor). Opcode 15 is kept for helper instructions.

## 2 Machine state

### 2.1 State of each thread

| Name | Bits | Function |
|---|---|---|
| r0, r1, r2, r3 | 4 x 16 | Data registers. |
| T | 16 | Time register. It gives the time of the pin events (section 4). |
| PC | 7 | Address of the next instruction. PC + 1 wraps from 127 to 0. |
| Z, C, N, E | 4 | Flags (section 9). Z zero, C carry, N negative (bit 15), E event (result of SAMPLE, WAIT and of the non-blocking FIFO operations). |
| LATE | 1 | Sticky status. It becomes 1 when the thread gave a pin event too late (section 4.3). Only IOW to port STATUS clears it. |
| ERR | 1 | Sticky status. It becomes 1 when the thread wrote a pin that it does not own (section 5.2) or executed an illegal instruction (section 12). Only IOW to port STATUS clears it. |
| RUN | 1 | 1 = the thread executes instructions. The host sets RUN. HALT and an illegal instruction clear RUN. |

### 2.2 Shared state

| Name | Size | Function |
|---|---|---|
| Memory | 128 x 16 | Program and data of all threads [D2]. |
| Cycle counter | 32 bits | Free-running, +1 in each clock cycle [M1 pe_cycle_counter]. "now" is its low 16 bits. |
| Output event slots | 15 | One slot for each output pin: event time (16 bits), value, output enable, valid [A1 section 7, block D2]. |
| Pin registers | 15 x 2 | Value V and output enable OE of each output pin. The pin is the output of these flip-flops [M1 pe_timed_out]. |
| Input synchronizer | 13 x 2 | 2 flip-flop stages for each input pin [M1 pe_sync2]. |
| Sample and wait units | 4 | One for each thread: sample time, pin, captured bit, edge capture time [A1 block D3]. |
| Host FIFOs | 4 x 2 | For each thread: host-to-thread FIFO and thread-to-host FIFO, 4 words of 16 bits each [A1 block D6]. |
| Pin configuration | 15 | For each output pin: owner thread (2 bits), owner enable, open-drain mode. The host writes it (section 5). |
| Start address | 4 x 7 | For each thread: the PC value at start. The host writes it. |

The host loads the memory and the configuration while the threads stop. Then the host sets RUN. The host SPI port (milestone M2c) is specified in docs/host_port.md: commands STOP, START, WRITE_MEM, READ_MEM, WRITE_CFG, READ_ID, READ_STATUS, PUSH, PULL, CLEAR. STOP puts the threads, the FIFOs, the event slots and the output pins into the state after reset; the memory and the configuration do not change.

### 2.3 Register count and area

Each register of a thread is written only in the slot of that thread. Thus, each bit is a loadable flip-flop: dfrbpq_1 + mux2_1 = 67.13 um2 [R1 section 9.4 item 3].

| Item | Bits for each thread | Bits for 4 threads | Area at 67.13 um2 for each bit |
|---|---|---|---|
| r0 to r3 | 64 | 256 | 17,185.3 um2 |
| T | 16 | 64 | 4,296.3 um2 |
| PC | 7 | 28 | 1,879.6 um2 |
| Z, C, N, E | 4 | 16 | 1,074.1 um2 |
| LATE, ERR, RUN | 3 | 12 | 805.6 um2 |
| Total | 94 | 376 | 25,240.9 um2 |

Comparison with A1 section 7, block D1 (8 registers of 16 bits, PC 10, flags 4, loop counter 16, base time 16 = 174 bits for each thread, 696 bits in total):

- A1 area of these 696 bits at 48.99 um2: 34,097 um2. At 67.13 um2: 46,722.5 um2.
- This instruction set: 376 bits, 25,240.9 um2. This is 21,481.6 um2 less than the A1 bits at 67.13 um2.
- With 8 data registers (A1) instead of 4, the data registers alone are 512 bits = 34,370.6 um2 (+17,185.3 um2).

The register read multiplexers are not loadable bits, but they also grow with the register count. Two read ports of 16 bits from 16 registers (4 threads x 4) need 16:1 multiplexers: 5 mux4_1 for each bit, 32 x 5 x 38.10 = 6,096 um2. With 8 registers (32:1, 10 mux4_1 + 1 mux2_1 for each bit): 32 x 399.14 = 12,772.5 um2. These are estimates [OWN]. M2b synthesis gives the real values.

We propose flip-flops, not latches, for the registers. Section 13 gives the reason.

### 2.4 Reset state

The reset is the chip reset rst_n (asynchronous assert, release at a clock edge [M1 pe_sync.v]).

| Item | State after reset |
|---|---|
| RUN of all threads | 0 (all threads stop) |
| PC | start address of the thread (host configuration). The reset value of the start address is 0. |
| r0 to r3, T | 0 |
| Z, C, N, E, LATE, ERR | 0 |
| Cycle counter | 0. It counts from the release of the reset. |
| Output event slots | empty |
| Output pins | V = 1, OE = 0 ("released") |
| Input synchronizer | 0 |
| Sample and wait units | empty |
| FIFOs | empty |
| Pin configuration | no owner, push-pull |
| Memory | not changed. Latches have no reset. The host must load the memory before it sets RUN. |

Effect of V = 1, OE = 0 on the chip pins: uio_oe = 0 (all uio pins are inputs), uio_out = 0xFF, uo_out[6:0] = 1. Thus, UART_TX (uo_out[4]) is 1 (idle level) during and after reset, as in M1. uo_out[7] is the host MISO pin [A1 section 2.2]. It is not an output pin of the threads.

## 3 Barrel timing

### 3.1 Slots

Cycle c is the value of the cycle counter in that clock cycle. The slot of thread n is each cycle c with c mod 4 = n. In its slot, the thread executes one instruction. An instruction "executes at cycle c" when it executes in the slot at cycle c. "now" is c mod 65,536.

A thread with RUN = 0 does nothing in its slot. The slot is not given to an other thread.

### 3.2 Blocking instructions

Some instructions must wait for an event. Such an instruction does not complete in its slot. The thread then executes the same instruction again in its next slot (4 cycles later). The PC does not change until the instruction completes. A slot that does not complete the instruction is lost for the thread.

| Instruction | Waits for |
|---|---|
| OUT | an empty event slot of the pin (section 4.4) |
| SAMPLE | the sample time (section 6.2) |
| WAIT | an edge or the timeout (section 6.3) |
| TWAIT | time T (section 7) |
| PULL (blocking form) | a word in the host-to-thread FIFO |
| PUSH (blocking form) | space in the thread-to-host FIFO |
| LD, LDX | always one more slot (section 3.3) |

A blocking instruction that waits for a cycle t completes in the first slot of the thread after cycle t (first slot c with c > t).

### 3.3 Memory access

The memory has one read port. In each cycle, the read port reads the instruction of the thread that has the slot. LD and LDX use the read port in the next slot of the thread. Thus, LD and LDX take 2 slots (8 cycles). The data is read in the second slot.

ST and STX write the memory at the cycle of their slot. A store at cycle cs to address a has this effect on a read of address a (instruction fetch, LD or LDX) at cycle x:

- x >= cs + 4: the read gives the new word.
- cs < x < cs + 4: not defined. The firmware must not do this. The model stops with an error.

A thread can thus read a word that it wrote in its last slot. Code that changes the instructions of an other running thread must obey the same rule.

## 4 Time and output pin events

### 4.1 Event time

OUT and SAMPLE have a 3-bit offset field (0 to 7). The event time is t = (T + offset) mod 65,536. The instruction does not change T. The firmware moves T with TADD, TNOW, TSYNC, TSET or WAIT (section 7).

### 4.2 When a thread may issue an event

The instruction executes at cycle c. The lead is d = (t - c) mod 65,536.

- If 4 <= d <= 32,767, the event is on time. The event occurs at cycle c + d. This agrees with A1 section 4.4: "A thread must issue an event at least 4 cycles before the event time." LEAD = 4.
- If d < 4 or d >= 32,768, the event is late (section 4.3).

Thus, the firmware must issue each event 4 to 32,767 cycles before its time. 32,767 cycles are 655 us at 50 MHz.

### 4.3 Late events

A late event occurs at cycle c + 4 (the earliest time that is always possible), not at time t. The LATE status of the thread becomes 1. The thread continues.

Reason: the pin change still occurs and the frame does not stop. The host can read LATE and know that the timing was not correct. Section 13 gives the rejected alternatives.

### 4.4 Output event slots

Each output pin has one event slot [A1 section 7, M1 pe_timed_out]. OUT writes the event into the slot of its pin. If the slot is full (it holds an event that did not occur), OUT waits (section 3.2). The lead and the late rule apply at the cycle when OUT completes.

At the clock edge at the end of cycle t (the event cycle), the pin registers V and OE get the values of the event, and the slot becomes empty. Thus, the chip pin shows the new value from cycle t + 1. This is the M1 behavior [M1 pe_timed_out.v]. An OUT at cycle t + 1 or later can write the next event into the slot.

Two events of one thread at the same time on the same pin: the second OUT waits until the first event occurs. The second event is then late (section 4.3). Use one OUT for each pin and each time.

### 4.5 Values of an output event

| OUT source | Push-pull pin: V, OE | Open-drain pin: V, OE |
|---|---|---|
| 0 | 0, 1 | 0, 1 |
| 1 | 1, 1 | 1, 0 (released) |
| Z | 1, 0 (released) | 1, 0 (released) |
| bit of a register = 0 | 0, 1 | 0, 1 |
| bit of a register = 1 | 1, 1 | 1, 0 (released) |

A uio pin shows V on uio_out and OE on uio_oe. A uo_out pin has no output enable. It shows V. Thus, a released uo_out pin is 1.

## 5 Pins

### 5.1 Pin numbers

Output pins (OUT):

| Number | Chip pin | Assembler names |
|---|---|---|
| 0 to 7 | uio[0] to uio[7] | UIO0 to UIO7 |
| 8 to 14 | uo_out[0] to uo_out[6] | UO0 to UO6 |
| 15 | none | none |

Input pins (SAMPLE, WAIT, port PINS):

| Number | Chip pin | Assembler names |
|---|---|---|
| 0 to 7 | uio[0] to uio[7] (pad value) | UIO0 to UIO7 |
| 8 to 12 | ui_in[0] to ui_in[4] | UI0 to UI4 |
| 13 to 15 | none, read as 0 | none |

ui_in[7:5] and uo_out[7] are the host SPI port [A1 section 2.2]. They are not thread pins. The default UART pins [A1 section 2.2] are UART_TX = uo_out[4] = output 12 (UO4) and UART_RX = ui_in[3] = input 11 (UI3).

A uio pin with OE = 1 reads back its own value V. Thus, an open-drain pin that is released reads the bus level, and a pin that drives 0 reads 0.

### 5.2 Owner of a pin, and two threads on one pin

Each output pin has one owner thread in the pin configuration. The host writes the configuration before it sets RUN. Only the owner can write events for the pin.

If a thread executes OUT for a pin that it does not own, or for pin 15:

- The pin and its event slot do not change.
- The ERR status of the thread becomes 1.
- The rest of the instruction executes (the register shift of the source, section 8.3). The instruction does not wait.

Thus, two threads can never drive the same pin [A1 section 8.2: "Two threads never drive the same pin."]. The first thread that the host gives the pin to is the only one that changes it. Each thread can read each input pin.

### 5.3 Open-drain mode

The pin configuration has an open-drain bit for each output pin (section 4.5). In open-drain mode the pin drives 0 or it is released. An external pull-up makes the line high. Use it for I2C [A1 section 3].

## 6 Input pins

### 6.1 Synchronizer

Each input pin goes through a 2-stage synchronizer [M1 pe_sync2]. The synchronized value s(c) in cycle c is the pin level in cycle c - 2. The pin level of cycle c is the value at the clock edge at the end of cycle c. This instruction set has no glitch filter. [ENGINEER] (section 14).

### 6.2 SAMPLE

SAMPLE pin, reg>> (or reg<<), offset: the sample time t is calculated as in section 4.1, with the lead and late rule of section 4.2 and 4.3. The sample unit of the thread keeps s(t), the synchronized value at cycle t. Thus, the bit is the pin level of cycle t - 2. The instruction completes in the first slot after cycle t. Then:

- reg>>: reg = (reg >> 1) or (bit << 15). Use it for LSB-first protocols (UART).
- reg<<: reg = ((reg << 1) or bit) mod 65,536. Use it for MSB-first protocols (SPI, I2C).
- E = bit.

Firmware that must read the pin level of cycle p uses t = p + 2.

### 6.3 WAIT

WAIT pin, edge, rd, [TO], [CT]: the thread waits for an edge on the synchronized input.

- Edge FALL: s(c - 1) = 1 and s(c) = 0. Edge RISE: s(c - 1) = 0 and s(c) = 1. Edge ANY: s(c - 1) is not equal to s(c). The edge cycle is c.
- The instruction executes first at cycle c0. Only edges at cycles c > c0 count.
- Without TO there is no timeout.
- With TO, the timeout cycle is c0 + d with d = (T - c0) mod 65,536, if 1 <= d <= 32,767. If d = 0 or d >= 32,768, the timeout cycle is c0 (the time T is already past, and no edge counts).
- An edge at a cycle up to and including the timeout cycle counts.
- On an edge at cycle e: the instruction completes in the first slot after e. rd = e mod 65,536. If CT is given, also T = e mod 65,536. E = 1.
- On a timeout at cycle u with no edge: the instruction completes in the first slot after u. rd and T do not change. E = 0.

The edge cycle is 2 cycles after the pin edge (synchronizer). Example: the pin is 1 in cycle p - 1 and 0 in cycle p. Then the edge cycle is p + 2. A SAMPLE at time p + 2 + k reads the pin level of cycle p + k. Thus, firmware that takes T from the edge cycle needs no correction for the synchronizer.

## 7 Time instructions

- TADD imm12: T = (T + imm12) mod 65,536, imm12 = 0 to 4,095.
- TADD rs: T = (T + rs) mod 65,536.
- TSET rs: T = rs. TGET rd: rd = T. NOW rd: rd = now.
- TNOW imm7: T = (now + imm7) mod 65,536.
- TSYNC imm7: if (T - now) as a signed 16-bit number is less than imm7, then T = (now + imm7) mod 65,536. Thus, TSYNC moves T to a time not earlier than now + imm7, and it never moves T back.
- TWAIT: if d = (T - now) mod 65,536 >= 32,768 (T is past), TWAIT completes at once. If not, it waits and completes in the first slot after cycle now + d.

Rule for the firmware: T must stay within 32,767 cycles of now. If T is more than 32,767 cycles in the past, the 16-bit difference shows it in the future (wrap). After a wait with no time limit (for example PULL), do not use the old T. The firmware in fw/ uses this loop for an idle line: TSYNC, then PULL.NB, then a branch back if the FIFO was empty. TSYNC keeps T near now while the thread waits. [ENGINEER] (section 14).

The minimum value of imm7 in TSYNC and TNOW: the next pin event must have a lead of at least 4 cycles when it executes. If the event instruction is k instructions after TSYNC, use imm7 >= 4 x k + 4.

## 8 Instructions

Notation: rd = destination register, rs = source register, ra = address register. imm = immediate value. "Flags" gives the flags that change. All arithmetic is mod 65,536 unless the text says otherwise.

### 8.1 System and control

| Instruction | Operation | Flags |
|---|---|---|
| HALT | RUN = 0. The thread stops. | none |
| NOP | nothing | none |
| JR rs | PC = rs mod 128 | none |
| JAL rd, addr | rd = (PC + 1) mod 128, PC = addr | none |
| JMP addr | PC = addr | none |
| Bcc addr | if the condition is true, PC = addr (section 9.2) | none |
| DJNZ rd, addr | rd = rd - 1. If rd is not 0, PC = addr. | none |

### 8.2 Data

| Instruction | Operation | Flags |
|---|---|---|
| LDI rd, imm10 | rd = imm10 (0 to 1,023, zero-extended) | none |
| ADDI rd, simm10 | rd = rd + simm10 (-512 to 511, sign-extended to 16 bits) | Z, N, C |
| MOV rd, rs | rd = rs | Z, N |
| ADD rd, rs | rd = rd + rs | Z, N, C |
| SUB rd, rs | rd = rd - rs | Z, N, C |
| AND rd, rs / OR / XOR | rd = rd and rs / or / xor | Z, N |
| NOT rd, rs | rd = not rs | Z, N |
| CMP rd, rs | rd - rs, the result is not kept | Z, N, C |
| TST rd, rs | rd and rs, the result is not kept | Z, N |
| ANDI / ORI / XORI rd, imm8 | rd = rd and / or / xor imm8 (0 to 255, zero-extended) | Z, N |
| CMPI rd, imm8 | rd - imm8, the result is not kept | Z, N, C |
| SHL rd, n | rd = rd << n, n = 0 to 15 | Z, N, C |
| SHR rd, n | rd = rd >> n (logical, 0 in at the top) | Z, N, C |
| ASR rd, n | rd = rd >> n (arithmetic, bit 15 in at the top) | Z, N, C |
| ROR rd, n | rotate right by n | Z, N, C |
| LD rd, addr | rd = memory[addr]. 2 slots. | none |
| ST rs, addr | memory[addr] = rs | none |
| LDX rd, [ra] | rd = memory[ra mod 128]. 2 slots. | none |
| STX rs, [ra] | memory[ra mod 128] = rs | none |

### 8.3 Pin events

| Instruction | Operation | Flags |
|---|---|---|
| OUT pin, 0 / 1 / Z, off | Event at time T + off: the pin drives 0, drives 1 or is released (section 4.5) | none |
| OUT pin, rs, off | Event: the pin value is bit 0 of rs. rs does not change. | none |
| OUT pin, rs>>, off | Event: the pin value is bit 0 of rs. Then rs = rs >> 1 (logical). | none |
| OUT pin, rs<<, off | Event: the pin value is bit 15 of rs. Then rs = (rs << 1) mod 65,536. | none |
| SAMPLE pin, rd>>, off | Sample at time T + off, bit into bit 15, rd shifts right (section 6.2) | E |
| SAMPLE pin, rd<<, off | Sample at time T + off, bit into bit 0, rd shifts left (section 6.2) | E |
| WAIT pin, edge, rd [, TO] [, CT] | Wait for an edge (section 6.3) | E |

The offset is optional in the assembler (default 0).

### 8.4 Time

See section 7. No time instruction changes the flags.

### 8.5 Host FIFOs and I/O ports

| Instruction | Operation | Flags |
|---|---|---|
| PULL rd | Wait for a word in the host-to-thread FIFO. rd = the word. | Z, N from the word. E = 1. |
| PULL.NB rd | If the FIFO has a word: rd = the word, E = 1. If not: rd does not change, E = 0. No wait. | Z, N (only if E = 1), E |
| PUSH rs | Wait for space in the thread-to-host FIFO. Write rs into it. | E = 1 |
| PUSH.NB rs | If the FIFO has space: write rs, E = 1. If not: E = 0. No wait. | E |
| IOR rd, port | rd = value of the I/O port (section 10) | none |
| IOW rs, port | write rs to the I/O port (section 10) | only port FLAGS |

## 9 Flags and conditions

### 9.1 Flags

- Z = 1 if the 16-bit result is 0.
- N = bit 15 of the result.
- C, add (ADD, ADDI): the carry out of bit 15. ADDI adds the sign-extended immediate as a 16-bit number.
- C, subtract (SUB, CMP, CMPI): 1 if there is no borrow (rd >= the other operand, unsigned).
- C, shift by n >= 1: the last bit that goes out. SHL: bit 16 - n of the old value. SHR, ASR, ROR: bit n - 1 of the old value. With n = 0, the result is rd and C does not change.
- Logic instructions (MOV, AND, OR, XOR, NOT, TST, ANDI, ORI, XORI) do not change C.
- E: see SAMPLE, WAIT, PULL, PUSH.

### 9.2 Branch conditions

| cc field | Mnemonic | Condition |
|---|---|---|
| 000 | JMP | always |
| 001 | BEQ | Z = 1 |
| 010 | BNE | Z = 0 |
| 011 | BCS | C = 1 |
| 100 | BCC | C = 0 |
| 101 | BMI | N = 1 |
| 110 | BES | E = 1 |
| 111 | BEC | E = 0 |

## 10 I/O ports

| Port | Name | Read (IOR) | Write (IOW) |
|---|---|---|---|
| 0 | STATUS | bit 0 LATE, bit 1 ERR, other bits 0 | clears LATE if bit 0 = 1, clears ERR if bit 1 = 1 |
| 1 | FLAGS | bit 0 Z, bit 1 C, bit 2 N, bit 3 E, other bits 0 | Z, C, N, E = bits 0 to 3 |
| 2 | PINS | s(c) of the input pins 0 to 15 (section 6.1) at this cycle | ignored |
| 3 | THREAD | the thread number 0 to 3 | ignored |
| 4 to 15 | none | 0 | ignored |
| 16 to 63 | helper hooks | 0 in M2a. For the CRC unit, the line-code helpers, the process monitor and the event recorder [A1 section 7, blocks D4, D5, D9, D10]. | ignored in M2a |

## 11 Encoding

### 11.1 Fields

Bits 15 to 12 are the opcode. In the bit patterns, the bits are in the sequence 15 to 0, in groups of 4. The letters are:

| Letter | Field |
|---|---|
| d | rd (2 bits) |
| s | rs (2 bits) |
| a | ra (2 bits) in LDX and STX |
| r | register (2 bits) |
| i | immediate |
| f | function |
| c | condition |
| m | memory address (7 bits) |
| p | pin number (4 bits) |
| k | kind (3 bits) |
| o | offset (3 bits) |
| e | edge (2 bits) |
| t | TO bit |
| x | CT bit |
| n | NB bit, or shift amount in SHIFT |
| q | port number (6 bits) |
| - | not used. The assembler writes 0. The processor ignores the bit. |

### 11.2 Encoding table

| Opcode | Instruction | Bits 15..0 | Field values |
|---|---|---|---|
| 0000 | HALT | 0000 0000 ---- ---- | |
| 0000 | NOP | 0000 0001 ---- ---- | |
| 0000 | JR rs | 0000 0010 ss-- ---- | |
| 0000 | LDX rd, [ra] | 0000 0011 ddaa ---- | |
| 0000 | STX rs, [ra] | 0000 0100 ssaa ---- | |
| 0000 | (illegal) | 0000 0101 to 0000 1111 | bits 11..8 = 5 to 15: illegal |
| 0001 | LDI rd, imm10 | 0001 ddii iiii iiii | i = 0 to 1,023 |
| 0010 | ADDI rd, simm10 | 0010 ddii iiii iiii | i = -512 to 511, two's complement |
| 0011 | ALU rd, rs | 0011 ddss ffff ---- | f: 0000 MOV, 0001 ADD, 0010 SUB, 0011 AND, 0100 OR, 0101 XOR, 0110 CMP, 0111 TST, 1000 NOT, 1001 to 1111 illegal |
| 0100 | ALU rd, imm8 | 0100 ddff iiii iiii | f: 00 ANDI, 01 ORI, 10 XORI, 11 CMPI |
| 0101 | SHIFT rd, n | 0101 ddff nnnn ---- | f: 00 SHL, 01 SHR, 10 ASR, 11 ROR; n = 0 to 15 |
| 0110 | TADD imm12 | 0110 iiii iiii iiii | i = 0 to 4,095 |
| 0111 | TIME | 0111 fffr riii iiii | f: 000 TADD rs, 001 TSET rs, 010 TGET rd, 011 NOW rd, 100 TNOW imm7, 101 TSYNC imm7, 110 TWAIT, 111 illegal. r is used by f = 000 to 011, i by f = 100 and 101. Bits that the function does not use are ignored. |
| 1000 | Bcc addr | 1000 ccc- -mmm mmmm | c: section 9.2 |
| 1001 | DJNZ rd, addr | 1001 dd-- -mmm mmmm | |
| 1010 | JAL rd, addr | 1010 dd-- -mmm mmmm | |
| 1011 | LD rd, addr | 1011 0dd- -mmm mmmm | |
| 1011 | ST rs, addr | 1011 1ss- -mmm mmmm | |
| 1100 | OUT / SAMPLE | 1100 pppp rrkk kooo | k: 000 OUT 0, 001 OUT 1, 010 OUT Z, 011 OUT r, 100 OUT r>>, 101 OUT r<<, 110 SAMPLE r>>, 111 SAMPLE r<<. r is ignored for k = 000 to 010. o = offset 0 to 7. |
| 1101 | WAIT | 1101 pppp ddee tx-- | e: 00 FALL, 01 RISE, 10 ANY, 11 illegal. t = 1: TO. x = 1: CT. |
| 1110 | HOST / IO | 1110 ffrr n-qq qqqq | f: 00 PULL rd, 01 PUSH rs, 10 IOR rd, 11 IOW rs. n = 1: non-blocking (.NB), PULL and PUSH only. q = port, IOR and IOW only. |
| 1111 | (reserved) | 1111 xxxx xxxx xxxx | illegal in M2a. Kept for helper instructions. |

Examples (the assembler tests check these words, test/test_isa.py):

| Source | Word |
|---|---|
| HALT | 0x0000 |
| NOP | 0x0100 |
| LDI r1, 9 | 0x1409 |
| TADD 434 | 0x61B2 |
| OUT UO4, r0>> | 0xCC20 |
| OUT UO4, 1 | 0xCC08 |
| SAMPLE UI3, r0>>, 2 | 0xCB32 |
| WAIT UI3, FALL, r2, CT | 0xDB84 |
| DJNZ r1, 6 | 0x9406 |
| JMP 1 | 0x8001 |
| TSYNC 24 | 0x7A18 |
| PULL.NB r0 | 0xE080 |

## 12 Illegal instructions

An illegal instruction is an opcode or a function value that section 11.2 marks "illegal". When a thread executes an illegal instruction, RUN becomes 0 and ERR becomes 1. The PC keeps the address of the illegal instruction. The word 0x0000 is HALT, so a thread that runs into a cleared memory area stops.

Bits with "-" have no effect. Thus, two words that differ only in "-" bits have the same function. The RTL and the model must both ignore them.

## 13 Choices, reasons and rejected alternatives

| Choice | Reason | Rejected alternative | Why rejected |
|---|---|---|---|
| 4 data registers for each thread | The UART, SPI and I2C firmware in fw/ needs 4 or fewer. Area 17,185.3 um2 for 4 threads (section 2.3). | 8 registers (A1 section 4.4) | +17,185.3 um2 of loadable bits and +6,676 um2 of read multiplexers (section 2.3), and 1 more bit for each register field. |
| Flip-flops for the registers | Each bit is written in the thread slot and read in the same cycle by the next stage. Flip-flops give simple STA and formal checks, as in M1. | Latches (dlhq_1, 30.84 um2) | Smaller (256 x 30.84 = 7,895 um2 for r0 to r3), but each word needs a clock gate and a timing check of the latch window. The M1 flow did not check latch timing. [ENGINEER] |
| Separate time register T | OUT, SAMPLE and WAIT use T with no register field. The register field stays free for the data. | T as one of the data registers | One data register less, and OUT would need one more field. |
| Event time = T + 3-bit offset | Two pins can change some cycles apart with no TADD. The sample delay for the synchronizer and the pad round trip (A1 section 9.3) is in the instruction. | Event time = T only | One more TADD for each such event: more words in the 128-word memory. |
| Late event occurs at c + 4 and sets LATE | The pin change still occurs, the frame continues, and the host knows that the timing was not correct. The behavior is the same in each case, so the model and the RTL can agree cycle by cycle. | Wait for the next match of the 16-bit time (M1 pe_timed_out: 65,536 cycles later) | A 1.3 ms gap with no indication. |
| | | Drop the event | The pin stays at a wrong value with no change, and the protocol state is wrong. |
| | | Stop the thread | One late event stops a protocol that can continue. The host can still stop the thread when it reads LATE. |
| LEAD = 4 cycles | A1 section 4.4. It also gives the RTL some pipeline stages between execute and the event slot. | LEAD = 1 (M1 pe_timed_out needs only 1) | It fixes the M2b pipeline too early. |
| Horizon 32,767 cycles (signed 16-bit difference) | The 16-bit time of A1 section 4.4. The signed difference shows a past time as late. | 32-bit T and 32-bit event times | +64 loadable bits in T (4,296 um2) and +16 bits in each of 15 event slots. |
| One owner thread for each output pin, OUT from an other thread ignored and ERR set | Two threads can never drive the same pin (A1 section 8.2). The fault is visible. | Priority (the lower thread number wins) | A firmware fault is not visible. |
| | | Wired AND or OR of the threads | The pin value depends on two programs. This is not useful for a protocol. |
| Owner set by the host, not by an instruction | The pin map is fixed for a set of protocols. No instruction word is necessary. | CLAIM instruction | One more instruction, and a race between threads. |
| OUT waits when the slot is full | One slot for each pin (A1 section 7). The thread then writes the next event as soon as the previous one occurs, as the M1 sequencer did. | More slots (a FIFO) for each pin | More flip-flops for each pin (19 for each slot, A1 section 7). |
| SAMPLE blocks until the sample time | The next instruction usually needs the bit. One sample unit for each thread (A1 block D3). | Non-blocking SAMPLE and a separate read instruction | One more word in each receive loop. |
| Loop counter = DJNZ on a data register | No extra register. | A separate 16-bit loop counter (A1 section 7) | +64 loadable bits (4,296 um2). |
| JAL and JR with a data register | No link register. | CALL and RET with a stack or a link register | More flip-flops for each thread. |
| LD and LDX take 2 slots | The 128 x 16 latch memory needs only one read multiplexer (section 3.3). | LD in one slot | A second 16-bit 128:1 read multiplexer (about 25,900 um2, see the M2a design note). |
| Helper hooks as I/O ports 16 to 63 and opcode 15 | The helpers (A1 blocks D4, D5) are not specified yet. The ports and the opcode keep space for them. | Helper instructions now | Their function is not known yet. |
| 0x0000 = HALT, illegal instructions stop the thread | A thread that runs into a cleared area or into data stops and sets ERR. | 0x0000 = NOP | The thread runs through the memory with no indication. |
| Reset level of the output pins V = 1, OE = 0 | uio pins are inputs, UART_TX is at idle level 1, as in M1. | V = 0 | UART_TX would show a start bit at reset. |

## 14 Items that the engineer must confirm [ENGINEER]

1. 4 data registers for each thread, not 8 (section 2.3, 13).
2. Flip-flops for the registers, not latches (section 13).
3. Late rule: a late event occurs at c + 4 and sets LATE; the thread continues (section 4.3).
4. LEAD = 4 cycles and the horizon of 32,767 cycles (section 4.2).
5. Pin owner from the host configuration; OUT from an other thread is ignored and sets ERR (section 5.2).
6. Reset level of the output pins: V = 1, OE = 0. Thus, uo_out[6:0] = 1 after reset (section 2.4). M1 had uo_out = 0x10.
7. LD and LDX take 2 slots, so that the latch memory needs one read port (section 3.3).
8. No glitch filter on the inputs in this instruction set. The I2C Fast-mode spike filter (50 ns, A1 section 3) needs a filter later (section 6.1).
9. The firmware keeps T near now with TSYNC while it waits for the host (section 7). The alternative is an "expired" bit for each thread, which makes T safe after a long wait.
10. Pin numbers and the default pins of the SPI and I2C outline firmware (fw/spi_mode0.s, fw/i2c_write.s).
11. The word formats from the host for SPI and I2C (bits 8 and 9 as control bits, see the firmware headers).
12. The store-and-read rule of section 3.3 (not defined for 1 to 3 cycles).

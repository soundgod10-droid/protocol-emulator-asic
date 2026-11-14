; fw/swd.s - Serial Wire Debug (SWD) host: line sequences, read and write
; transactions with turnaround, ACK and data phase.
;
; What it does:
;   Pins: SWCLK = uo_out[3]; SWDIO = uio[4] (push-pull, released in the
;   turnaround and target cycles; the target has the pull-up). In each
;   SWCLK cycle the clock falls, the host sets SWDIO at the falling edge,
;   the clock rises H1 cycles later and falls again H2 cycles after the
;   rise. The target samples SWDIO and changes it at the rising edge (ARM
;   IHI0031H, ADIv5.2, B4.3.1, page B4-118); the firmware samples a
;   target bit 2 cycles before the next rising edge. SWCLK stays low
;   between commands and between the parts of a transaction (the low time
;   gets longer; the clock can be stopped, B4-118).
;   Transaction (B4.2, pages B4-108, B4-109): the 8-bit request, one
;   turnaround cycle, ACK[0:2]. Only on OK (ACK[0:2] = 1, 0, 0) the data
;   phase follows: for a read 32 data bits and the parity bit from the
;   target and a turnaround; for a write a turnaround, then 32 data bits,
;   the parity bit and 15 idle cycles (line low) from the host. On WAIT,
;   FAULT or no response (line high: 1, 1, 1) the firmware makes the
;   turnaround, with no data phase (B4-117, Table B4-10), and takes the
;   data words of a write from the FIFO. The next request can follow at
;   once (B4-104). The host does the parity and the retries.
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility).
;   The ACK decides the next bit, so the decision must be in the thread:
;   a host round trip is much longer than a bit.
;
; Host words to the thread:
;   Raw cycles (line reset, JTAG-to-SWD sequence, idle cycles): 0x8000 + n
;     (n = 1 to 255 words of 16 cycles), then the n words of SWDIO bits,
;     the first bit in bit 0. The host drives SWDIO in all cycles. Example:
;     JTAG-to-SWD and line reset = 4 words 0xFFFF, 0xE79E, 4 words 0xFFFF,
;     0x0000 (B5-125, B4-118).
;   Transaction: bits 7:0 the request (bit 0 Start = 1, bit 1 APnDP, bit 2
;     RnW, bits 3, 4 A[2:3], bit 5 parity, bit 6 Stop = 0, bit 7 Park = 1),
;     bit 10 = RnW again, bit 15 = 0; for a write in the same PUSH also
;     WDATA[15:0], WDATA[31:16] and the parity bit (bit 0, bits 15:1 = 0),
;     so that the 4-word FIFO holds the transaction.
; Words from the thread for a transaction: the ACK word (ACK[0:2] in bits
;   13 to 15); for a read with OK also RDATA[15:0], RDATA[31:16] and a
;   word with the parity bit in bit 14.
; Limits (model, test/test_flex.py test_flex_swd): smallest correct SWCLK
;   period 32 cycles (1.5625 MHz at 50 MHz) with H1 12 to 15 and H1 + H2 =
;   32; a shorter period or H1 below 12 sets LATE. A DPIDR read takes
;   1,596 clock cycles from the first to the last SWCLK edge (31.3 k reads
;   per second without the host time). Target delays of 3 and 8 cycles
;   after the rising edge pass. ADIv5 gives no times; use the data sheet.
;   The same code with the bit loops in line is 76 words and 4 cycles
;   faster per read.
; Not done: overrun detection (STICKYORUN data phase), turnaround of more
;   than 1 cycle (DLCR.TURNROUND), SWD protocol version 2 dormant state.

        .equ    SWCLK, UO3
        .equ    SWDIO, UIO4
        .equ    H1, 12          ; falling to rising SWCLK edge (cycles)
        .equ    H2, 20          ; rising to falling SWCLK edge (cycles)

swd:    TNOW    8
        OUT     SWCLK, 0
cmd:    TSYNC   12
        PULL.NB r1              ; N = bit 15 of the word
        BEC     cmd
        BMI     raw
        LDI     r3, 8
        JAL     r0, outb        ; request; then r1 = word >> 8, bit 2 = RnW
        LDI     r3, 4
        JAL     r0, inb         ; turnaround, ACK[0:2] to bits 13 to 15
        PUSH    r2
        ANDI    r1, 4
        SHR     r2, 13
        CMPI    r2, 1
        BNE     fail            ; not OK: no data phase
        MOV     r1, r1
        BNE     rd
        LDI     r3, 1
        JAL     r0, inb         ; turnaround
        LDI     r2, 3           ; WDATA[0:15], WDATA[16:31], parity and 15 idle cycles
        JMP     rg
rd:     LDI     r1, 2
rl:     LDI     r3, 16
        JAL     r0, inb         ; RDATA[0:15], RDATA[16:31]
        PUSH    r2
        DJNZ    r1, rl
        LDI     r3, 2
        JAL     r0, inb         ; parity (bit 14), turnaround
        PUSH    r2
        JMP     cmd
fail:   LDI     r3, 1
        JAL     r0, inb         ; turnaround
        MOV     r1, r1
        BNE     cmd             ; a read: no data words to take
        LDI     r2, 3
fw:     PULL    r1              ; a write: take its 3 data words
        DJNZ    r2, fw
        JMP     cmd
raw:    MOV     r2, r1
        ANDI    r2, 0xFF        ; number of 16-cycle words
rg:     PULL    r1
        LDI     r3, 16
        JAL     r0, outb
        DJNZ    r2, rg
        JMP     cmd
outb:   TSYNC   12              ; r3 cycles, the host drives SWDIO from r1 (bit 0 first)
ob:     OUT     SWDIO, r1>>
        TADD    H1
        OUT     SWCLK, 1
        TADD    H2
        OUT     SWCLK, 0
        DJNZ    r3, ob
        JR      r0
inb:    TSYNC   12              ; r3 cycles, SWDIO released, target bits into r2 (bit 15)
        OUT     SWDIO, Z
ib:     TADD    H1
        OUT     SWCLK, 1
        SAMPLE  SWDIO, r2>>     ; level 2 cycles before the rising edge
        TADD    H2
        OUT     SWCLK, 0
        DJNZ    r3, ib
        JR      r0

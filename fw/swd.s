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
;   target, a turnaround and one idle cycle (line low); for a write a
;   turnaround, then 32 data bits and the parity bit from the host. On
;   WAIT, FAULT or no response (line high: 1, 1, 1) the firmware makes the
;   turnaround and one idle cycle, with no data phase (B4-117, Table
;   B4-10). The host does the parity and the retries.
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility).
;   The ACK decides the next bit, so the decision must be in the thread:
;   a host round trip is much longer than a bit.
;
; Host words to the thread:
;   Raw cycles (line reset, JTAG-to-SWD sequence, idle cycles): 0x8000 + n
;     (n = 1 to 1,023 cycles), then the SWDIO bits, 16 in each word, the
;     first bit in bit 0. The host drives SWDIO in all n cycles.
;   Transaction: the request byte (bit 0 Start = 1, bit 1 APnDP, bit 2 RnW,
;     bits 3, 4 A[2:3], bit 5 parity, bit 6 Stop = 0, bit 7 Park = 1); for
;     a write in the same PUSH also WDATA[15:0], WDATA[31:16] and the
;     parity bit (bit 0), so that the 4-word FIFO holds the transaction.
; Words from the thread for a transaction: the ACK word (ACK[0:2] in bits
;   13 to 15); for a read with OK also RDATA[15:0], RDATA[31:16] and a
;   word with the parity bit in bit 14.
; Not done: overrun detection (STICKYORUN data phase), turnaround of more
;   than 1 cycle (DLCR.TURNROUND), SWD protocol version 2 dormant state.

        .equ    SWCLK, UO3
        .equ    SWDIO, UIO4
        .equ    H1, 12          ; falling to rising SWCLK edge (cycles)
        .equ    H2, 16          ; rising to falling SWCLK edge (cycles)

swd:    TNOW    8
        OUT     SWCLK, 0
        OUT     SWDIO, 0        ; idle: the host drives the line low
cmd:    TSYNC   12
        PULL.NB r1
        BEC     cmd
        ST      r1, HDR
        MOV     r1, r1
        BMI     raw
        LDI     r3, 8
        JAL     r0, outb        ; request
        LDI     r3, 4
        JAL     r0, inb         ; turnaround, ACK[0:2] to bits 13 to 15
        PUSH    r2
        SHR     r2, 13
        CMPI    r2, 1
        BNE     fail            ; not OK: no data phase
        LD      r1, HDR
        ANDI    r1, 4
        BNE     rd
        LDI     r3, 1
        JAL     r0, inb         ; turnaround
        PULL    r1
        LDI     r3, 16
        JAL     r0, outb        ; WDATA[0:15]
        PULL    r1
        LDI     r3, 16
        JAL     r0, outb        ; WDATA[16:31]
        PULL    r1
        LDI     r3, 1
        JAL     r0, outb        ; parity
        JMP     cmd
rd:     LDI     r3, 16
        JAL     r0, inb         ; RDATA[0:15]
        PUSH    r2
        LDI     r3, 16
        JAL     r0, inb         ; RDATA[16:31]
        PUSH    r2
        LDI     r3, 2
        JAL     r0, inb         ; parity (bit 14), turnaround
        PUSH    r2
        JMP     idle1
fail:   LDI     r3, 1
        JAL     r0, inb         ; turnaround
idle1:  LDI     r1, 0
        LDI     r3, 1
        JAL     r0, outb        ; one idle cycle: the host drives the line low
        JMP     cmd
raw:    SHL     r1, 6
        SHR     r1, 6
        ST      r1, HDR         ; cycles left
rg:     PULL    r1
        LD      r3, HDR
        ADDI    r3, -16
        ST      r3, HDR
        BMI     rp
        LDI     r3, 16
        JAL     r0, outb
        LD      r3, HDR
        MOV     r3, r3
        BEQ     cmd
        JMP     rg
rp:     ADDI    r3, 16          ; 1 to 15 cycles in the last word
        JAL     r0, outb
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
HDR:    .word   0

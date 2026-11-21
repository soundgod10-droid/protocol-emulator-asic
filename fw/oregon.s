; fw/oregon.s - Oregon Scientific version 2.1 sensor transmitter (OOK, Manchester, 1024 Hz).
;
; What it does:
;   Sends Oregon Scientific protocol version 2.1 messages ("Oregon Scientific
;   RF Protocol Description (Versions 1.0, 2.1, 3.0)", November 2012, pages 1
;   to 4, 6, 13) on RF = uio[4] (output pin 4, UIO4, push-pull; a pull-down on
;   the board holds it low, carrier off, while the chip is in reset or
;   stopped) to the data input of a 433.92 MHz OOK transmitter module
;   (1 = carrier on).
;   Bit rate 1024 Hz: half a clock period is HALF = 29,297 cycles = 488.28 us
;   (1,023.99 Hz). Manchester code with the "normal" polarity: a transmitted
;   bit b is b in the first half of its clock period and not b in the second
;   (a 0 is an off-to-on transition in the middle). Version 2.1 sends each
;   data bit d twice, inverted first: the transmitted pair is (not d, d).
;   Every carrier-on pulse ends TRUNC = 5,580 cycles = 93 us early (the
;   pulse is shortened at its end, not at its start; page 3).
;   A message is the preamble (16 data bits 1), the sync nibble 0xA, the
;   payload nibbles from the host, and the checksum (the 8-bit sum of the
;   payload nibbles, low nibble first); every nibble goes LSB first. The
;   message is sent twice, with a gap of 22 half periods (10.74 ms; page 2:
;   about 10.9 ms for the THGR122NX) between the copies.
;
; Why:
;   The sensor protocol is a Manchester stream at a slow rate with doubled,
;   inverted bits, shortened pulses and a nibble checksum: a UART or SPI
;   cannot make it, and the pulse ends sit off the clock grid.
;
; Host words to this thread, for each message: N (1 to 32 payload nibbles),
;   then N words with a nibble each (bits 3..0), in the order of transmission
;   (sensor ID, channel, rolling code, flags, data).
; Words to the host: none.
; Registers: r0 bits and work, r1 work, r2 carrier level of the last half,
;   r3 return address.
; Memory: BUF.. in data memory (sync, payload and checksum nibbles); LL, BC,
;   NC, PTR, CUR, REP, HC and the time constants after the code.

        .equ    RF, UIO4
        .equ    HALF, 29297     ; half clock period: 488.28 us (1,024 Hz)
        .equ    TRUNC, 5580     ; carrier-on pulses end 93 us early
        .equ    GAPH, 22        ; gap between the two copies: 22 half periods
        .equ    BUF, 512

        TSYNC   32
        OUT     RF, 0           ; carrier off
idle:   PULL    r0              ; N
        MOV     r1, r0
        ADDI    r1, 3
        ST      r1, LL          ; sync + N + 2 checksum nibbles
        LDI     r1, BUF
        LDI     r2, 10
        STX     r2, [r1]        ; sync nibble 0xA
        LDI     r2, 0           ; checksum
get:    ADDI    r1, 1
        PULL    r3
        ANDI    r3, 15
        STX     r3, [r1]
        ADD     r2, r3
        DJNZ    r0, get
        MOV     r3, r2
        ANDI    r3, 15
        ADDI    r1, 1
        STX     r3, [r1]        ; checksum, low nibble
        SHR     r2, 4
        ANDI    r2, 15
        ADDI    r1, 1
        STX     r2, [r1]        ; high nibble
        LDI     r0, 2
        ST      r0, REP
copy:   TSYNC   100
        LDI     r2, 0           ; carrier off
        LDI     r0, 16
        ST      r0, BC
pre:    LDI     r0, 1           ; preamble: data bits 1
        JAL     r3, dbit
        LD      r0, BC
        ADDI    r0, -1
        ST      r0, BC
        BNE     pre
        LDI     r0, BUF
        ST      r0, PTR
        LD      r0, LL
        ST      r0, NC
nib:    LD      r1, PTR
        LDX     r0, [r1]
        ADDI    r1, 1
        ST      r1, PTR
        ST      r0, CUR
        LDI     r0, 4
        ST      r0, BC
bit:    LD      r0, CUR         ; LSB first
        MOV     r1, r0
        SHR     r1, 1
        ST      r1, CUR
        JAL     r3, dbit
        LD      r0, BC
        ADDI    r0, -1
        ST      r0, BC
        BNE     bit
        LD      r0, NC
        ADDI    r0, -1
        ST      r0, NC
        BNE     nib
        MOV     r2, r2          ; carrier still on after the last half?
        BEQ     ended
        LD      r1, NTR
        TADD    r1
        OUT     RF, 0
        LD      r1, TRC
        TADD    r1
ended:  LD      r0, REP
        ADDI    r0, -1
        ST      r0, REP
        BEQ     idle
        LDI     r0, GAPH
gap:    TWAIT
        LD      r1, HLF
        TADD    r1
        DJNZ    r0, gap
        JMP     copy

; One data bit (bit 0 of r0) as four half periods from T: d = 1 is off, on,
; on, off; d = 0 is on, off, off, on (the pair (not d, d), Manchester). A rise
; is at the half's start, a fall TRUNC before it. T ends at the next half.
dbit:   ANDI    r0, 1
        BEQ     d0
        LDI     r0, 6           ; halves 0, 1, 1, 0 (first half in bit 0)
        JMP     dgo
d0:     LDI     r0, 9           ; halves 1, 0, 0, 1
dgo:    LDI     r1, 4
        ST      r1, HC
dh:     MOV     r1, r0
        ANDI    r1, 1
        SHR     r0, 1
        CMP     r1, r2
        BEQ     dsame
        MOV     r2, r1
        BEQ     doff
        OUT     RF, 1           ; carrier on at the half's start
        JMP     dsame
doff:   LD      r1, NTR         ; carrier off TRUNC before the half's start
        TADD    r1
        OUT     RF, 0
        LD      r1, TRC
        TADD    r1
dsame:  TWAIT                   ; keep T within one half period of now
        LD      r1, HLF
        TADD    r1
        LD      r1, HC
        ADDI    r1, -1
        ST      r1, HC
        BNE     dh
        JR      r3

HLF:    .word   HALF
TRC:    .word   TRUNC
NTR:    .word   0x10000-TRUNC
LL:     .word   0
BC:     .word   0
NC:     .word   0
PTR:    .word   0
CUR:    .word   0
REP:    .word   0
HC:     .word   0

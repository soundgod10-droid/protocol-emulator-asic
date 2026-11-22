; fw/joybus.s - Joybus host (console side of the Nintendo 64 controller line): commands out, replies in.
;
; What it does:
;   Drives the one-wire, half-duplex controller line on DATA = uio[0] (UIO0,
;   open drain, pull-up on the board: the line idles high). Bit cells of
;   CELL = 240 cycles (4 us, 250 kbit/s at 60 MHz), MSB first: a 1 is low
;   L1 = 1 us then high 3 us, a 0 is low L0 = 3 us then high 1 us. After the
;   command bytes the console stop bit: low 1 us, then the line is released
;   (high 2 us before the pad may answer). The reply bytes from the pad are
;   read by edge time: each falling edge starts a bit, and the level TS = 2 us
;   after it is the bit (1 us low = 1, 3 us low = 0), so the pad's own bit
;   rate may differ from 250 kbit/s by much more than a real pad does. The
;   reply goes to data memory first and to the host after the pad's stop bit
;   (low 2 us, not read), so a slow host cannot make the thread miss a bit.
;   No fall within TFIRST (100 us) after the release, or within TOB after the
;   sample point of a bit: the byte is flagged (bit 15) and its missing bits
;   are 0, and every later byte of the reply is 8000h.
;
; Why:
;   The controller line has no clock and no length field: the host must time
;   every bit cell itself and know the reply length of each command, and the
;   pad can answer a few microseconds after the stop bit.
;
; Host words to this thread, one command each:
;   first word  bits 5..0 = N command bytes (1 to 63), bits 13..8 = R reply
;               bytes (0 to 63)
;   then N words, bits 7..0 = the command bytes in line order.
;   All N bytes are gathered in data memory before the first bit is sent, so
;   a late host word delays the command but cannot cut it.
; Words to the host: R words per command, bits 7..0 = a reply byte, bit 15 =
;   a bit of this byte had no falling edge in time (no pad, or a short reply).
; Registers: r0 bit count / edge time / pointer, r1 byte, r2 pointer / bit
;   count, r3 byte count.
; Memory: NC, NR; data memory BUF.. (command bytes), BUFR + 1 .. BUFR + R
;   (reply bytes, last byte first).

        .equ    D, UIO0
        .equ    L1, 60          ; low part of a 1 (1 us)
        .equ    L0, 180         ; low part of a 0 (3 us)
        .equ    CELL, 240       ; bit cell (4 us)
        .equ    STOPL, 60       ; console stop bit low (1 us)
        .equ    TS, 120         ; fall to the sample point of a reply bit (2 us)
        .equ    TOB, 300        ; sample point to the time-out of the next fall (fall + 7 us)
        .equ    TFIRST, 6000    ; release to the time-out of the first reply fall (100 us)
        .equ    GAP, 360        ; last time-out point to the next command (fall + 13 us)
        .equ    BUF, 512        ; command bytes in data memory
        .equ    BUFR, 640       ; reply bytes in data memory

        TNOW    16
        OUT     D, 1            ; DATA released (idle high)
cmd:    TNOW    8               ; keep T near now while there is no command
        PULL.NB r0              ; command word: N and R
        BEC     cmd
        MOV     r1, r0
        ANDI    r1, 63
        ST      r1, NC
        SHR     r0, 8
        ANDI    r0, 63
        ST      r0, NR
        LDI     r2, BUF
        LD      r3, NC
gat:    TNOW    8
        PULL.NB r0              ; the N command bytes
        BEC     gat
        STX     r0, [r2]
        ADDI    r2, 1
        DJNZ    r3, gat
        TNOW    32
        LDI     r2, BUF
        LD      r3, NC
sbyte:  LDX     r1, [r2]
        SHL     r1, 8           ; MSB to bit 15
        LDI     r0, 8
sbit:   OUT     D, 0            ; every cell starts with a fall
        TADD    L1
        OUT     D, r1<<         ; a 1 releases after 1 us, a 0 stays low
        TADD    L0 - L1
        OUT     D, 1            ; a 0 releases after 3 us
        TADD    CELL - L0
        DJNZ    r0, sbit
        ADDI    r2, 1
        DJNZ    r3, sbyte
        OUT     D, 0            ; console stop bit
        TADD    STOPL
        OUT     D, 1            ; released: the pad answers from here
        LD      r3, NR
        MOV     r3, r3
        BEQ     gap
        TADD    TFIRST
rbyte:  LDI     r1, 0
        LDI     r2, 8
rbit:   WAIT    D, FALL, r0, TO, CT
        BEC     rto
        TADD    TS              ; T = the fall + 2 us
        SAMPLE  D, r1<<         ; high = 1, low = 0
        TADD    TOB
        DJNZ    r2, rbit
        LDI     r0, BUFR
        ADD     r0, r3
        STX     r1, [r0]
        DJNZ    r3, rbyte
        JMP     gap
rto:    SHL     r1, 1           ; no fall in time: the missing bits are 0
        DJNZ    r2, rto
        LDI     r0, 1
        SHL     r0, 15
        OR      r1, r0          ; and the byte is flagged
rtf:    LDI     r0, BUFR
        ADD     r0, r3
        STX     r1, [r0]
        LDI     r1, 1
        SHL     r1, 15          ; every later byte: 8000h
        DJNZ    r3, rtf
gap:    TADD    GAP             ; the pad's stop bit ends before T
        TWAIT
        LD      r3, NR
        MOV     r3, r3
        BEQ     cmd
push:   LDI     r0, BUFR
        ADD     r0, r3
        LDX     r1, [r0]
        PUSH    r1
        DJNZ    r3, push
        JMP     cmd

NC:     .word   0
NR:     .word   0

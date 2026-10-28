; fw/uart_rx_slow.s - UART receiver for bit times of more than 32,767 cycles.
;
; What it does:
;   The same receiver and the same host words as fw/uart_rx_full.s, for
;   the low rates of A1 section 3 (50 MHz: 300 bit/s to 1,525 bit/s). The
;   bit time is B = K x C and the first sample time H = K x HC. A SAMPLE
;   must be issued 32,767 cycles or less before its time (isa 4.2), so the
;   thread waits (TWAIT) after each chunk.
;
; Why:
;   G1 step 3a: A1 gives a UART range from 300 bit/s. At 50 MHz, 300 bit/s
;   is 166,667 cycles for each bit, more than the 32,767-cycle horizon.
;   This file is the measured word cost of that low end. The host loads
;   this file or fw/uart_rx_full.s, for the rate that it needs.
;
; Host words to this thread:
;   1. C: chunk time in cycles, 64 to 32,767. B = K x C.
;   2. HC: chunk of the first wait. H = K x HC (about 0.35 x B).
; .equ K: chunks in each bit (6 for 300 bit/s at 50 MHz).
; Word to the host: as fw/uart_rx_full.s.
; Registers: r0 frame, r1 chunk count and WAIT time, r2 C, r3 HC and bit count.

        .equ    RX, UI3
        .equ    ND1, 9          ; ND + 1: data and parity bits, and the stop bit
        .equ    K, 6

        PULL    r2              ; C
        PULL    r3              ; HC
        ST      r3, hc
        JMP     idle
bit:    LDI     r1, K
bl:     TADD    r2
        DJNZ    r1, bw
        SAMPLE  RX, r0>>
        DJNZ    r3, bit
        PUSH    r0
idle:   WAIT    RX, FALL, r1, CT ; T = start edge
        LD      r3, hc
        LDI     r1, K
hl:     TADD    r3
        DJNZ    r1, hw
        SAMPLE  RX, r0>>, 4     ; start bit
        BES     idle            ; 1: glitch, not a start bit
        LDI     r3, ND1
        JMP     bit
bw:     TWAIT
        JMP     bl
hw:     TWAIT
        JMP     hl
hc:     .word   0

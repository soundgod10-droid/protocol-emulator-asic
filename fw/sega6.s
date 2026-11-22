; fw/sega6.s - Sega Mega Drive / Genesis 6-button pad host: four select pulses per poll, twelve buttons.
;
; What it does:
;   Reads a 6-button pad on its DB-9 lines as Charles Rosenberg's notes \"Sega
;   Six Button Controller Hardware Info\" (version 1.0, 9 September 1996)
;   describe them: SELECT (DB-9 pin 7) = uo_out[4] (UO4, idle high; uo pins
;   are 1 from reset), and the pad's outputs DB-9 pins 1, 2, 3, 4, 6, 9 =
;   uio[0..5] (inputs, never driven; low = pressed).
;   One poll per host word: SELECT low for HP cycles (pins 6 and 9 = A and
;   Start; pins 3 and 4 low = a pad is there), high (pins 1, 2, 3, 4, 6, 9 =
;   up, down, left, right, B, C), two more pulses, and after the third rising
;   edge pins 1, 2, 3 = Z, Y, X; the fourth pulse returns the pad to its
;   3-button state. Each phase is read RD cycles after its edge (the pad's
;   chip answers more slowly than the 74HC157 of the 3-button pad, notes
;   section Six Button Interface). HP = 20 us (the notes measured 13.8 us in
;   a game and used up to 100 us).
;
; Why:
;   The pad has no clock or protocol chip on the host side: the extra buttons
;   appear only on a timed select pulse train, which a timed thread makes.
;
; Host words to this thread: any word starts one poll.
; Words to the host, one per poll: bits 5..0 = up, down, left, right, B, C;
;   bit 6 = A; bit 7 = Start; bits 10..8 = Z, Y, X (1 = pressed); bit 15 = a
;   pad answered (pins 3 and 4 low while SELECT is low).
; Registers: r0 high phase / result, r1 low phase, r2 third-pulse phase, r3 work.

        .equ    SEL, UO4
        .equ    HP, 1200        ; select half period (20 us)
        .equ    RD, 600         ; select edge to the read (10 us)

        TNOW    16
        OUT     SEL, 1
poll:   TNOW    8
        PULL.NB r0
        BEC     poll
        TNOW    64
        OUT     SEL, 0          ; pulse 1: low phase
        TADD    RD
        TWAIT
        IOR     r1, PINS
        TADD    HP - RD
        OUT     SEL, 1          ; high phase
        TADD    RD
        TWAIT
        IOR     r0, PINS
        TADD    HP - RD
        OUT     SEL, 0          ; pulse 2
        TADD    HP
        OUT     SEL, 1
        TADD    HP
        OUT     SEL, 0          ; pulse 3
        TADD    HP
        OUT     SEL, 1          ; third rising edge: X, Y, Z
        TADD    RD
        TWAIT
        IOR     r2, PINS
        TADD    HP - RD
        OUT     SEL, 0          ; pulse 4: back to the 3-button state
        TADD    HP
        OUT     SEL, 1
        NOT     r0, r0          ; pressed = 1
        ANDI    r0, 63          ; up, down, left, right, B, C
        NOT     r1, r1
        MOV     r3, r1
        SHR     r3, 4
        ANDI    r3, 3
        SHL     r3, 6
        OR      r0, r3          ; A, Start
        NOT     r2, r2
        ANDI    r2, 7
        SHL     r2, 8
        OR      r0, r2          ; Z, Y, X
        SHR     r1, 2
        ANDI    r1, 3
        CMPI    r1, 3
        BNE     nopad
        LDI     r1, 1
        SHL     r1, 15
        OR      r0, r1          ; pins 3 and 4 were low: a pad answered
nopad:  PUSH    r0
        TADD    HP              ; select high at least one half period before the next poll
        TWAIT
        JMP     poll

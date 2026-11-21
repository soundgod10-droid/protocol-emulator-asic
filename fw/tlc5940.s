; fw/tlc5940.s - TLC5940 LED driver controller: grayscale PWM clock, dot correction and grayscale data, latch.
;
; What it does:
;   Drives a TI TLC5940 (data sheet SLVS515D, December 2004, revised November
;   2015: 6.3 AC characteristics, 8.3.1 serial interface, 8.3.6 output
;   enable, 8.4.1 to 8.4.3 and 8.4.5): SIN = uo_out[0] (UO0), SCLK = uo_out[1]
;   (UO1), XLAT = uo_out[2] (UO2), BLANK = uo_out[3] (UO3), GSCLK = uo_out[4]
;   (UO4), VPRG = uo_out[5] (UO5; logic level: the 22 V EEPROM programming
;   level is not used, DCPRG is tied on the board).
;   One thread makes the grayscale PWM cycles without a pause in GSCLK: each
;   cycle has NGS = NGSH x 8 GSCLK pulses (4,096, the data sheet's full cycle)
;   with period 2 x H = 80 cycles (750 kHz), then BLANK is high for 400 cycles
;   (counter reset; the thread prepares the next cycle in this time); the next
;   cycle's first GSCLK rise comes 16 cycles after the BLANK fall.
;   During the first GSCLK periods of a cycle the thread shifts the frame that
;   was gathered in the cycle before, one bit with each GSCLK period: SIN
;   changes SL cycles after the GSCLK rise while SCLK is low, SCLK rises with
;   the GSCLK fall (192 bits for grayscale with VPRG low, 96 bits for dot
;   correction with VPRG high; VPRG is set at the cycle start, H cycles before
;   the first SCLK rise). During the other GSCLK periods it takes at most one
;   host word a period into data memory (the next frame). While BLANK is high
;   at the end of the cycle it pulses XLAT (8 cycles after the BLANK rise, 8
;   cycles high) when a frame was shifted in that cycle; after the first
;   grayscale latch that follows a dot correction latch it gives the
;   additional SCLK pulse of 8.4.3 (96 cycles after the BLANK rise). BLANK stays high (outputs off) until the
;   first grayscale data are latched (8.4.1: register values are unknown after
;   power-up).
;
; Why:
;   The TLC5940 needs a free-running grayscale clock and a latch only at the
;   end of a PWM cycle; data, latch and PWM must be kept in step, which one
;   thread does without a handshake.
;
; Host words to this thread, one frame of 17 words at a time: a header word
;   (0 = grayscale frame, 1 = dot correction frame), then 16 values for OUT15
;   down to OUT0, left aligned: a 12-bit grayscale value in bits 15..4, or a
;   6-bit dot correction value in bits 15..10. A frame goes to the chip in the
;   cycle after the one in which its last word came, and takes effect at the
;   end of that cycle (dot correction) or in the cycle after it (grayscale).
; Words to the host: none.
; Registers: data phase r0 GSCLK periods left, r1 value bits, r2 value
;   pointer, r3 bits left in the value; gather phase r0 periods left, r1 host
;   word, r2 gather pointer, r3 words gathered.
; Memory: BUF..BUF+16 in data memory (the frame); GP, GC, MODE, LAT, BPV,
;   VPL, VC, DCL, RUN after the code.

        .equ    SIN, UO0
        .equ    SCLK, UO1
        .equ    XLAT, UO2
        .equ    BLANK, UO3
        .equ    GSCLK, UO4
        .equ    VPRG, UO5
        .equ    H, 40           ; GSCLK half period
        .equ    SL, 32          ; GSCLK rise to the SIN change (SIN to SCLK rise: H - SL = 8 cycles)
        .equ    NGSH, 512       ; GSCLK pulses a cycle / 8
        .equ    BUF, 512

        TSYNC   32
        OUT     XLAT, 0         ; first: every uo pin is high from reset, SCLK and VPRG must not change with XLAT high
        TADD    2               ; th3: XLAT fall to VPRG
        OUT     SCLK, 0
        OUT     GSCLK, 0
        OUT     VPRG, 0
        LDI     r0, 0
        ST      r0, GC
        ST      r0, MODE
        ST      r0, DCL
        ST      r0, RUN
        LDI     r0, BUF
        ST      r0, GP
        TSYNC   100
        JMP     nonext
cycle:  MOV     r3, r3          ; T = the first GSCLK rise of the cycle; r0 = GSCLK periods, r3 = LAT
        BEQ     plain
        LDI     r1, 0
        ST      r1, MODE
        LD      r3, VPL
        OUT     VPRG, r3>>      ; VPRG for this frame, H cycles before the first SCLK rise
        LDI     r2, BUF+1
        LDI     r1, 16
        ST      r1, VC
dval:   LDX     r1, [r2]
        ADDI    r2, 1
        LD      r3, BPV
dbit:   OUT     GSCLK, 1        ; T
        OUT     SCLK, 0
        TADD    SL
        OUT     SIN, r1<<       ; T + SL: the next bit, MSB first
        TADD    H-SL
        OUT     GSCLK, 0        ; T + H
        OUT     SCLK, 1         ; the chip takes SIN
        TADD    H
        ADDI    r0, -1
        DJNZ    r3, dbit
        LD      r3, VC
        ADDI    r3, -1
        ST      r3, VC
        BNE     dval
        OUT     SCLK, 0, 4      ; SCLK low again (before XLAT: tsu1)
plain:  LD      r2, GP
        LD      r3, GC
pbit:   OUT     GSCLK, 1
        TADD    H
        OUT     GSCLK, 0
        TADD    H
        CMPI    r3, 17
        BCS     pnext           ; frame complete: no more words this cycle
        PULL.NB r1
        BEC     pnext
        STX     r1, [r2]
        ADDI    r2, 1
        ADDI    r3, 1
pnext:  DJNZ    r0, pbit
        ST      r2, GP          ; T = the end of the last GSCLK period
        ST      r3, GC
        OUT     BLANK, 1        ; counter reset, outputs off
        LD      r1, LAT
        MOV     r1, r1
        BEQ     nolat
        TADD    8
        OUT     XLAT, 1
        TADD    8
        OUT     XLAT, 0
        CMPI    r1, 2
        BCS     latdc           ; dot correction latched
        LDI     r1, 1
        ST      r1, RUN         ; grayscale latched: outputs on from the next cycle
        LD      r1, DCL
        MOV     r1, r1
        BEQ     nolat0
        TADD    80
        OUT     SCLK, 1         ; BLANK rise + 96: additional SCLK pulse after the first grayscale latch after dot correction
        TADD    8
        OUT     SCLK, 0
        LDI     r1, 0
        ST      r1, DCL
        JMP     nolat1
latdc:  LDI     r1, 1
        ST      r1, DCL
nolat0: TADD    88
        JMP     nolat1
nolat:  TADD    104
nolat1: CMPI    r3, 17          ; T = BLANK rise + 104
        BCC     nonext
        LDI     r3, BUF
        ST      r3, GP
        LDX     r1, [r3]        ; header: 0 grayscale, 1 dot correction
        LDI     r3, 0
        ST      r3, GC
        MOV     r1, r1
        BNE     ndc
        LDI     r3, 1
        ST      r3, MODE
        LDI     r3, 12
        ST      r3, BPV
        LDI     r3, 0
        ST      r3, VPL
        JMP     nonext
ndc:    LDI     r3, 2
        ST      r3, MODE
        LDI     r3, 6
        ST      r3, BPV
        LDI     r3, 1
        ST      r3, VPL
nonext: LD      r3, MODE        ; frame to shift in the next cycle: 0 none, 1 grayscale, 2 dot correction
        ST      r3, LAT
        LDI     r0, NGSH
        SHL     r0, 3
        LD      r1, RUN
        TADD    296             ; room for the latch, frame and setup instructions (longest path: the first
                                ; SCLK event of the next cycle written 336 cycles after the BLANK rise)
        MOV     r1, r1
        BEQ     dark
        OUT     BLANK, 0        ; BLANK high 400 cycles
dark:   TADD    16              ; the first GSCLK rise 16 cycles after the BLANK fall
        JMP     cycle

GP:     .word   0
GC:     .word   0
MODE:   .word   0
LAT:    .word   0
BPV:    .word   12
VPL:    .word   0
VC:     .word   0
DCL:    .word   0
RUN:    .word   0

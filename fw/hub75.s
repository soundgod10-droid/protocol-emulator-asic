; fw/hub75.s - HUB75 LED panel driver (32 x 16 panel, 1/8 scan, one bit per colour), frame buffer in data memory.
;
; What it does:
;   Refreshes a HUB75 RGB LED panel without stop. Pins (HUB75 connector names):
;   R1 G1 B1 R2 G2 B2 = uo_out[0..5] (UO0..UO5), OE = uo_out[6] (UO6, low =
;   LEDs on; uo pins are 1 from reset, so the panel is dark until the
;   firmware runs), CLK = uio[0], LAT = uio[1], A B C = uio[2..4] (UIO0..UIO4;
;   board pull-downs, released at reset). The panel holds one shift chain per
;   colour line (16-output constant-current drivers that work like 74HC595s), latched
;   by LAT, and lights row pair ROW (row ROW on R1 G1 B1, row ROW + 8 on R2 G2
;   B2) selected by A + 2B + 4C while OE is low.
;   For each row pair: the COLS columns are shifted while the previous row
;   pair is lit, last column first (the bit clocked first moves to the far
;   end of the chain): the six data lines change at T, CLK rises at T + 2
;   (set-up 33 ns; SN74HC595: 25 ns) and falls CHI cycles later (high 500 ns,
;   low 567 ns; 20 ns), one column every COLT cycles (0.94 MHz; 25 MHz).
;   Then OE goes high (dark), LAT rises 2 cycles later (more than 500 ns
;   after the last CLK rise; 19 ns) for 14 cycles, the address changes 3
;   cycles after OE, and OE goes low 20 cycles after it. Every row pair is
;   lit for the same time (HOSTT plus the shift time of the next one plus
;   the turn), so the brightness is equal.
;   In the HOSTT window after a row pair is lit the thread takes up to 4 host
;   words into the frame buffer (data memory 512 + ROW x COLS + column).
;
; Why:
;   A HUB75 panel has no controller: the driver must clock in every row pair
;   and switch rows, blanking and latching in a fixed order, at a rate high
;   enough that the scan does not flicker.
;
; Host words to this thread: two columns each, bits 5..0 column c and bits
;   13..8 column c + 1, each as R1 G1 B1 R2 G2 B2 in bits 0..5; bit 15 starts
;   a frame (the word goes to row pair 0, column 0). The words fill the frame
;   buffer row pair by row pair, column 0 first.
; Words to the host: none.
; Registers: r0 host word / work, r1 pixel bits, r2 pointer, r3 count.
; Memory: ROW, WP (host write pointer).

        .equ    R1, UO0
        .equ    G1, UO1
        .equ    B1, UO2
        .equ    R2, UO3
        .equ    G2, UO4
        .equ    B2, UO5
        .equ    OE, UO6
        .equ    CLK, UIO0
        .equ    LAT, UIO1
        .equ    A, UIO2
        .equ    B, UIO3
        .equ    C, UIO4
        .equ    COLS, 32        ; columns of the panel
        .equ    CSH, 5          ; log2(COLS)
        .equ    COLT, 64        ; cycles per column (1.07 us)
        .equ    CHI, 30         ; CLK high time (500 ns); low COLT - CHI
        .equ    HOSTT, 400      ; host window after a row pair is lit (4 words fit)
        .equ    FB, 512         ; frame buffer in data memory

        LDI     r0, FB
        ST      r0, WP
        LDI     r0, 0
        ST      r0, ROW
        TSYNC   64
        OUT     CLK, 0          ; drive the clock, latch and address lines
        OUT     LAT, 0
        OUT     A, 0
        OUT     B, 0
        OUT     C, 0
row:    LD      r2, ROW         ; pointer to the last column of this row pair
        SHL     r2, CSH
        LDI     r1, FB + COLS - 1
        ADD     r2, r1
        LDI     r3, COLS
        TSYNC   48
col:    LDX     r1, [r2]
        OUT     R1, r1>>        ; the six data lines at T
        OUT     G1, r1>>
        OUT     B1, r1>>
        OUT     R2, r1>>
        OUT     G2, r1>>
        OUT     B2, r1>>
        OUT     CLK, 1, SU      ; CLK rises at T + SU
        TADD    CHI
        OUT     CLK, 0          ; and falls CHI cycles later
        TADD    COLT - CHI
        ADDI    r2, -1
        DJNZ    r3, col
        OUT     OE, 1           ; turn: dark at T
        OUT     LAT, 1, 2       ; latch at T + 2
        LD      r0, ROW
        OUT     A, r0>>, 3      ; address at T + 3
        OUT     B, r0>>, 3
        OUT     C, r0>>, 3
        TADD    16
        OUT     LAT, 0          ; T + 16
        OUT     OE, 0, 4        ; lit at T + 20
        TADD    20 + HOSTT      ; the next row pair starts HOSTT cycles after
        LDI     r3, 4           ; up to 4 host words while this row pair is lit
hw:     PULL.NB r0
        BEC     next
        LD      r1, WP
        TST     r0, r0
        BMI     fstart
        JMP     store
fstart: LDI     r1, FB          ; bit 15: a new frame
store:  MOV     r2, r0
        ANDI    r2, 63
        STX     r2, [r1]
        ADDI    r1, 1
        SHR     r0, 8
        ANDI    r0, 63
        STX     r0, [r1]
        ADDI    r1, 1
        ST      r1, WP
        DJNZ    r3, hw
next:   LD      r0, ROW         ; next row pair
        ADDI    r0, 1
        ANDI    r0, 7
        ST      r0, ROW
        JMP     row

ROW:    .word   0
WP:     .word   0

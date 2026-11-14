; fw/trigger.s - timed trigger: a pulse on uo_out[5] a set number of clock
; cycles after a UART byte that matches.
;
; What it does:
;   The thread receives 8N1 UART frames on ui_in[3] (the UART RX pin of
;   fw/uart_rx_full.s; a second thread can run the receiver on the same
;   pin). It takes T from the start edge (WAIT CT: the edge cycle is the
;   pin edge + 2, docs/isa.md 6.3) and samples the 8 data bits at their
;   middles. If the byte equals the match byte, the pulse starts D cycles
;   after the middle of data bit 7 and lasts WIDTH cycles. All times come
;   from T, so the pulse has a fixed delay from the start edge at the pin,
;   whatever the phase of the edge against the thread slots.
;   FIRE = fire_t (default) starts the pulse at T + D; FIRE = fire_n starts
;   it 8 cycles after the instruction that sees the match (for the
;   comparison of the two methods in test_flex_trigger).
;
; Why:
;   Option O5 (DECISIONS.md D17): an exact, programmable delay from a
;   protocol event to an output edge, for test benches and logic analyzer
;   triggers.
;
; Host words to the thread: B (bit time in cycles, 2 x B / 2 + 8 x B up to
;   32,767), the match byte (bits 7:0), D (44 to 4,095 cycles).
; Word from the thread for each pulse: the matched byte.
; Measured (model, test/test_flex.py test_flex_trigger, B 434, D 200, 16
;   start phases): pulse rising edge 3,892 cycles after the start edge at
;   the pin at every phase (B / 2 + 8 B + D + 2 synchronizer cycles + 1),
;   jitter 0 cycles; with FIRE = fire_n 3,737 to 3,740 cycles (jitter 3,
;   the slot phase). Smallest D 44 cycles (D 40 sets LATE). A real input
;   edge between two clock edges adds up to 1 cycle (20 ns) of jitter in
;   the synchronizer; the model works in whole cycles.
; Not done: stop-bit check, other events (SPI command, I2C address).

        .equ    RX, UI3
        .equ    TRG, UO5
        .equ    WIDTH, 50       ; pulse width (cycles)

trig:   PULL    r0              ; B
        PULL    r1              ; match byte
        PULL    r2              ; D
        ST      r2, DLY
        MOV     r2, r0
        SHR     r2, 1
        ST      r2, HALF
        TNOW    8
        OUT     TRG, 0
wt:     WAIT    RX, FALL, r3, CT    ; start edge
        LD      r2, HALF
        TADD    r2              ; middle of the start bit
        LDI     r3, 8
bit:    TADD    r0
        SAMPLE  RX, r2>>        ; data bits 0 to 7 into bits 8 to 15
        DJNZ    r3, bit
        SHR     r2, 8
        CMP     r2, r1
        BNE     wt
        LD      r3, DLY
        TADD    r3              ; T = middle of data bit 7 + D
        JMP     FIRE
fire_n: TNOW    8
fire_t: OUT     TRG, 1
        TADD    WIDTH
        OUT     TRG, 0
        PUSH    r2
        JMP     wt
HALF:   .word   0
DLY:    .word   0

        .equ    FIRE, fire_t

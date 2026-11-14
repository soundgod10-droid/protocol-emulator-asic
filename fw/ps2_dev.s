; fw/ps2_dev.s - PS/2 device (keyboard or mouse side): makes the clock,
; sends frames to the host, and receives host commands with acknowledge.
;
; What it does:
;   Pins: CLK = uio[4], DATA = uio[5], both open drain (pull-ups at the
;   host). The firmware reads the line levels back (docs/isa.md 5.1).
;   Each clock: DATA changes while CLK is high, CLK falls TSU cycles later,
;   stays low for TLO cycles, and stays high for THI + TSU cycles (IBM
;   Personal System/2 Hardware Interface Technical Reference, May 1988,
;   Keyboards section, pages 38 to 40). Defaults: 15 us, 40 us, 40 us
;   (12.5 kHz).
;   Send (CLK and DATA high): 11 clocks, the frame from the host word. 40
;   cycles after each CLK release the firmware reads CLK; if the host holds
;   it low before the 10th clock (line contention, page 39), the frame
;   stops; after the 10th clock the frame is completed.
;   Receive (CLK high and DATA low: request to send): 11 clocks; DATA is
;   sampled at each rising edge; if the stop bit is high, DATA goes low for
;   a 12th clock (acknowledge, page 40).
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility).
;
; Host words to the thread: a frame to send: the 11 bits LSB first (bit 0
;   start = 0, bits 1 to 8 data, bit 9 odd parity, bit 10 stop = 1).
; Words from the thread: 0x0000 = frame sent; 0xFFFF = frame stopped by
;   the host (send it again); for each received frame the 11 bits in bits
;   5 to 15 (start 5, data 6 to 13, parity 14, stop 15).
; Limits (model, test/test_flex.py test_flex_ps2): correct at 10, 12.5 and
;   16.7 kHz (TSU, TLO, THI = 900, 2500, 1600; 750, 2000, 1250; 500, 1500,
;   1000 cycles at 50 MHz; clock period TSU + TLO + THI exactly), with the
;   host inhibiting a frame before the 10th clock (stopped, 0xFFFF) and at
;   the 10th clock (completed), and a host command with acknowledge.
; Not done: framing-error recovery (clock until DATA is high, then Resend,
;   page 40); the 20 ms response rule is up to the host program.

        .equ    CLK, UIO4
        .equ    DATA, UIO5
        .equ    TSU, 750        ; DATA change to CLK fall (15 us at 50 MHz)
        .equ    TLO, 2000       ; CLK low (40 us)
        .equ    THI, 1250       ; CLK rise to the next DATA change (25 us)

dev:    TNOW    12              ; two OUT events follow (docs/isa.md 7: 4 x 2 + 4)
        OUT     CLK, 1
        OUT     DATA, 1
idle:   TNOW    16
        TADD    500
        TWAIT
        IOR     r3, PINS
        ANDI    r3, 0x30        ; CLK (input 4), DATA (input 5)
        CMPI    r3, 0x10        ; CLK high, DATA low: request to send
        BEQ     rx
        CMPI    r3, 0x30
        BNE     idle            ; CLK low: inhibited
        PULL.NB r0
        BEC     idle
        LDI     r2, 11
        TNOW    16
tb:     OUT     DATA, r0>>
        TADD    TSU
        OUT     CLK, 0
        TADD    TLO
        OUT     CLK, 1          ; release
        TADD    40
        SAMPLE  CLK, r1>>       ; the line must be high now
        BES     nc
        CMPI    r2, 3           ; r2 = 12 - clock number: contention before the 10th clock
        BCS     stop
nc:     TADD    THI - 40
        DJNZ    r2, tb
        LDI     r3, 0
        PUSH    r3              ; sent
        JMP     idle
stop:   TNOW    8
        OUT     DATA, 1
        LDI     r3, 0
        NOT     r3, r3
        PUSH    r3              ; stopped by the host
        JMP     idle
rx:     LDI     r2, 11          ; start, data 0 to 7, parity, stop
        TNOW    16
rb:     TADD    TSU
        OUT     CLK, 0
        TADD    TLO
        OUT     CLK, 1
        SAMPLE  DATA, r1>>, 2   ; DATA at the rising edge
        TADD    THI
        DJNZ    r2, rb
        MOV     r1, r1          ; N = stop bit
        BMI     ack
        PUSH    r1              ; framing error: stop bit 0 in the word
        JMP     idle
ack:    OUT     DATA, 0         ; acknowledge in clock 12
        TADD    TSU
        OUT     CLK, 0
        TADD    TLO
        OUT     CLK, 1
        TADD    THI
        OUT     DATA, 1
        TWAIT                   ; DATA released: else idle reads CLK high, DATA low as a new request
        PUSH    r1
        JMP     idle

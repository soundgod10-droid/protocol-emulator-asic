; fw/jtag.s - JTAG (IEEE Std 1149.1 test access port) controller: drives
; TCK, TMS and TDI and reads TDO for a sequence of TCK cycles that the host
; gives.
;
; What it does:
;   Pins: TCK = uo_out[0], TMS = uo_out[1], TDI = uo_out[2] (push-pull),
;   TDO = ui_in[0] (pull-up on the board: TDO is not driven outside the
;   shift states). TCK is low between sequences. In each TCK cycle the
;   firmware sets TMS and TDI 1 clock cycle after the falling TCK edge,
;   makes the rising edge H1 cycles after the falling edge, samples TDO at
;   the rising edge, and makes the falling edge H2 cycles after the rising
;   edge. The device samples TMS and TDI at the rising edge and changes TDO
;   after the falling edge (TI SSYA002C, IEEE Std 1149.1 (JTAG)
;   Testability Primer, page 3-5).
;   The host moves the TAP state machine with the TMS bits (state diagram,
;   Figure 3-3, page 3-5). The firmware does not know the TAP states, so it
;   does any scan, with Pause states, of any length, on a chain of devices.
;   Between two groups of 16 cycles TCK stays low for about 60 cycles more
;   (host words; the TAP is static while TCK is low).
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility).
;   The host-driven TMS keeps the firmware small: 31 words. fw/jtag_tap.s
;   makes the TAP sequences itself: 67 words, a 36-cycle TCK period, one
;   host word for each 16 bits instead of two, but no Pause states.
;
; Host words to the thread:
;   1. n, the number of TCK cycles (1 to 32,767).
;   2. For each group of 16 cycles (the last group can be shorter): the TMS
;      word, then the TDI word; the first cycle of the group is bit 0.
; Word from the thread for each group: the TDO bits. With k cycles in the
;   group, the TDO bit of the first cycle is bit 16 - k and that of the
;   last cycle is bit 15.
; Limits (model, test/test_flex.py test_flex_jtag): smallest correct TCK
;   period 40 cycles (1.25 MHz at 50 MHz) with H1 10 to 13 and H1 + H2 = 40;
;   H1 below 10 or a shorter period sets LATE. A 64-cycle scan with its
;   header runs at 1.157 MHz on average (group gaps). H1 - 1 is the TMS and
;   TDI set-up time before the rising edge; the TDO delay of the device
;   after the falling edge must be H1 - 2 cycles or less, because the
;   sample reads the TDO level of the rising-edge cycle (docs/isa.md 6.2).
;   The IEEE 1149.1 primer gives no times; use the device data sheet.
; Not done: TRST* (use TMS high for 5 TCK cycles, primer page 3-6), adaptive
;   clocking (RTCK).

        .equ    TCK, UO0
        .equ    TMS, UO1
        .equ    TDI, UO2
        .equ    TDO, UI0
        .equ    H1, 12          ; falling to rising TCK edge (cycles)
        .equ    H2, 28          ; rising to falling TCK edge (cycles)

jtag:   TNOW    8
        OUT     TCK, 0
idle:   TSYNC   12
        PULL.NB r3              ; n
        BEC     idle
        ST      r3, LEFT
grp:    PULL    r0              ; TMS bits
        PULL    r1              ; TDI bits
        LD      r3, LEFT
        ADDI    r3, -16
        ST      r3, LEFT
        BMI     part
        LDI     r3, 16
        JMP     go
part:   ADDI    r3, 16          ; 1 to 15 cycles in the last group
go:     TSYNC   12              ; T is the last falling edge, or later after a wait for the host
bit:    OUT     TMS, r0>>, 1
        OUT     TDI, r1>>, 1
        TADD    H1
        OUT     TCK, 1
        SAMPLE  TDO, r2>>, 2    ; TDO level in the cycle of the rising edge
        TADD    H2
        OUT     TCK, 0
        DJNZ    r3, bit
        PUSH    r2
        LD      r3, LEFT
        MOV     r3, r3
        BMI     idle            ; the last group was shorter than 16
        BNE     grp
        JMP     idle
LEFT:   .word   0

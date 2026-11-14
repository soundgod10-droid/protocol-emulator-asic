; fw/jtag_tap.s - JTAG controller that makes the TAP state sequences itself
; (structure for comparison with fw/jtag.s; same pins and TCK timing).
;
; What it does:
;   Pins: TCK = uo_out[0], TMS = uo_out[1], TDI = uo_out[2], TDO = ui_in[0].
;   Command 0: TMS high for 5 TCK cycles (Test-Logic-Reset), then TMS low
;   for 1 cycle (Run-Test/Idle) (TI SSYA002C, page 3-6). Other commands:
;   one IR or DR scan from Run-Test/Idle, the last bit with TMS high (to
;   Exit1), then Update and Run-Test/Idle (state diagram, page 3-5).
;
; Host words to the thread: the command (bit 15: 1 = IR scan, 0 = DR scan;
;   bits 9:0: n = 1 to 1,023 bits; 0 = reset), then for a scan the TDI
;   words, 16 bits each, the first bit in bit 0.
; Word from the thread for each TDI word: the TDO bits, as in fw/jtag.s.
; Limits (model, test/test_flex.py test_flex_jtag): smallest correct TCK
;   period 36 cycles (1.389 MHz at 50 MHz) with H1 10 to 13 and H1 + H2 =
;   36; a 64-bit IDCODE scan with its header runs at 1.211 MHz on average.
;   67 words (fw/jtag.s: 31 words, 40-cycle period).
; Not done: Pause states, scans that end in a state other than
;   Run-Test/Idle.

        .equ    TCK, UO0
        .equ    TMS, UO1
        .equ    TDI, UO2
        .equ    TDO, UI0
        .equ    H1, 12          ; falling to rising TCK edge (cycles)
        .equ    H2, 28          ; rising to falling TCK edge (cycles)

jtag:   TNOW    8
        OUT     TCK, 0
cmd:    TSYNC   12
        PULL.NB r2
        BEC     cmd
        LDI     r1, 0x1F        ; reset: TMS 1, 1, 1, 1, 1, 0
        LDI     r3, 6
        MOV     r2, r2
        BEQ     hdr
        LDI     r1, 1           ; DR scan: TMS 1, 0, 0
        LDI     r3, 3
        BMI     ir
        JMP     hdr
ir:     LDI     r1, 3           ; IR scan: TMS 1, 1, 0, 0
        LDI     r3, 4
hdr:    SHL     r2, 6
        SHR     r2, 6           ; n
        ST      r2, LEFT
        JAL     r0, tms
        LD      r3, LEFT
        MOV     r3, r3
        BEQ     cmd             ; reset only
grp:    PULL    r1              ; TDI bits
        LD      r3, LEFT
        ADDI    r3, -16
        ST      r3, LEFT
        LDI     r0, 0
        BMI     lastg
        BEQ     lastg
        LDI     r3, 16
        JMP     go
lastg:  ADDI    r3, 15          ; k - 1 bits with TMS 0, then the last bit
        LDI     r0, 1
        BEQ     lastb
go:     TSYNC   12
sh:     OUT     TDI, r1>>, 1
        TADD    H1
        OUT     TCK, 1
        SAMPLE  TDO, r2>>, 2
        TADD    H2
        OUT     TCK, 0
        DJNZ    r3, sh
        MOV     r0, r0
        BEQ     next
lastb:  TSYNC   24
        OUT     TMS, 1, 1       ; last bit: to Exit1
        LDI     r0, 0
        LDI     r3, 1
        JMP     sh
next:   PUSH    r2
        LD      r3, LEFT
        ADDI    r3, -1
        BMI     tail
        JMP     grp
tail:   LDI     r1, 1           ; TMS 1, 0: Update, Run-Test/Idle
        LDI     r3, 2
        JAL     r0, tms
        JMP     cmd
tms:    TSYNC   12              ; r3 cycles with TMS from r1 (bit 0 first)
tm1:    OUT     TMS, r1>>, 1
        TADD    H1
        OUT     TCK, 1
        TADD    H2
        OUT     TCK, 0
        DJNZ    r3, tm1
        JR      r0
LEFT:   .word   0

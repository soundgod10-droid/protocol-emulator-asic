; fw/helper/jtag_idcode.s - JTAG IDCODE scan of a two-device chain through the USB helper's clocked mode.
;
; What it does:
;   TCK = helper clock (TXB), TDI = helper data (TXA), TDO = ui_in[0] (helper DIN, sampled 5 cycles after each rising
;   TCK edge), TMS = uio2 driven by thread 0 with OUT events at bit starts, timed from the frame's TXTIME.
;   Frame A, 9 bits at TXPER 4 (8 cycles a bit, 7.5 MHz TCK), TMS 1 1 1 1 1 0 1 0 0: Test-Logic-Reset,
;   Run-Test/Idle, Select-DR-Scan, Capture-DR, Shift-DR (the IDCODE register is selected after reset).
;   Frame B, 64 bits at TXPER 2 (4 cycles a bit, 15 MHz TCK), TDI 1, TMS 0 and 1 at the last bit (to Exit1-DR):
;   both 32-bit IDCODEs come out on TDO. Thread 0 pushes the helper STATUS word at the end.
;   Thread 1 (starts at 120) stores every received word from data address 768 (word 0: the TDO samples of frame A;
;   words 1 to 4: the scan) and writes the next free address to address 500.
;
; Why:
;   A thread alone reaches about 1.5 MHz TCK (fw/jtag.s, 40-cycle TCK); the helper shifts at 15 MHz while one thread
;   places the few TMS changes. docs/helpers.md section 6.
;
; Host calls (fw/helper/host_calls.py): jtag_idcode_values(words, 2) returns the two IDCODEs from the words read at
; 768 (first device on TDO first). Tested by test_slu_chip_jtag_idcode (RTL and gate level) against a two-TAP chain
; model with TDO 7 ns after the falling TCK edge, at slow pads with 10 ns of board multiplexer and at fast pads.

        .equ    E, 16           ; USB helper ports 16-31
        .equ    TMS, 2          ; uio2: TMS, a thread-0 pin (OUT events at the bit starts)
t0:     TNOW    8
        OUT     TMS, 1
        LDI     r0, 0
        IOW     r0, E+12        ; CFGADR 0
        LDI     r0, 0x00B
        IOW     r0, E+13        ; MODE: clocked code, RX on
        LDI     r0, 4
        IOW     r0, E+13        ; TXPER 4: 8 cycles a bit (7.5 MHz TCK) for the TMS navigation
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0x17F
        SHL     r0, 6
        ORI     r0, 0x38
        IOW     r0, E+13        ; SEL 0x5FF8: DIN = input 8 (ui_in[0], TDO), sample delay 5
        LDI     r0, 18
        IOW     r0, E+12
        LDI     r0, 0
        IOW     r0, E+13        ; LTC 0: TCK idle low, TDI out at the falling edge, the TAP samples at the rising edge
        LDI     r0, 9
        IOW     r0, E+4         ; TXCTRL: 9 bits (frame A)
        LDI     r0, 0x1FF
        IOW     r0, E+14        ; TXLAST: TDI 1 in all 9 bits
        TNOW    100
        TGET    r1
        IOW     r1, E+5         ; TXTIME = T; TMS 1 1 1 1 1 0 1 0 0: Test-Logic-Reset, Run-Test/Idle, to Shift-DR
        TADD    40              ; bit 5 starts at T + 5 x 8
        OUT     TMS, 0
        TADD    8
        OUT     TMS, 1
        TADD    8
        OUT     TMS, 0
wa:     IOR     r0, E+4         ; transmitter state: wait for the end of frame A and its tail
        ANDI    r0, 15
        BNE     wa
        LDI     r0, 1
        IOW     r0, E+12
        LDI     r0, 2
        IOW     r0, E+13        ; TXPER 2: 4 cycles a bit, 15 MHz TCK for the scan
        LDI     r0, 0
        IOW     r0, E+4         ; TXCTRL: 16 bits in the last word (frame B: 64 bits, both IDCODEs)
        LDI     r3, 4
        TNOW    100
        TGET    r1
        IOW     r1, E+5         ; TXTIME = T; TMS 0 in Shift-DR
        TADD    252             ; bit 63 starts at T + 63 x 4: TMS 1 (to Exit1-DR after the last bit)
        OUT     TMS, 1
txb:    IOR     r0, E+0         ; TX buffer free words
        CMPI    r0, 0
        BEQ     txb
        LDI     r0, 0
        NOT     r0, r0          ; TDI 1
        CMPI    r3, 1
        BEQ     lastb
        IOW     r0, E+0
        ADDI    r3, -1
        JMP     txb
lastb:  IOW     r0, E+14
wb:     IOR     r0, E+4
        ANDI    r0, 15
        BNE     wb
        IOR     r1, E+2
        PUSH    r1
        HALT
        .org    120
t1:     LDI     r1, 768         ; thread 1: received words to the data memory from 768
rx:     IOR     r0, E+6
        ANDI    r0, 0x60
        BEQ     rx
        IOR     r0, E+1
        STX     r0, [r1]
        ADDI    r1, 1
        ST      r1, 500         ; next free address, for the host
        JMP     rx

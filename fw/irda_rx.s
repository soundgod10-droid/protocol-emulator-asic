; fw/irda_rx.s - IrDA SIR receiver (RZI pulses of a UART frame, 2.4 to 115.2 kbit/s).
;
; What it does:
;   Receives IrDA SIR frames on IR_RXD = ui_in[3] (input pin 11, UI3) from the
;   RXD output of an IrDA transceiver (high when idle, low for each received
;   light pulse). A frame starts with the falling edge of the start bit's
;   pulse at t0. Bit k (1 to 8 data bits LSB first, 9 stop bit) is 0 when a
;   pulse starts in its window from t0 + (k - 0.5) B to t0 + (k + 0.5) B, and
;   1 when none does; the thread waits for a falling edge with a time-out at
;   the end of each window, so it does not depend on where in the cell the
;   sender puts the pulse or how long the pulse is (Vishay "IrDA, Physical
;   Layer" application note, document 82513, Table 2, from IrPHY 1.4: pulses
;   of 1.41 us to 3/16 of the bit time plus tolerance; rate tolerance
;   0.87 %). A pulse in the stop bit window is a framing error.
;
; Why:
;   The receiver must find pulses of 1.4 us that can sit anywhere in a bit
;   cell; a sampling UART cannot see them. Edge times against windows make the
;   decision independent of the pulse position and width.
;
; Host words to this thread: B (bit time, cycles, at most 32,767), once.
; Words to the host, for each frame: the byte (bits 7..0), bit 8 = 1 for a
;   framing error (a pulse in the stop bit window).
; Registers: r0 bit counter, r1 byte, r2 B, r3 work.
; Memory: HB (B / 2) after the code.

        .equ    RX, UI3

        PULL    r2              ; B
        MOV     r3, r2
        SHR     r3, 1
        ST      r3, HB
idle:   WAIT    RX, FALL, r3, CT        ; start bit pulse: T = t0
        LD      r3, HB
        TADD    r3              ; end of the start bit window
        LDI     r1, 0
        LDI     r0, 8
data:   TWAIT                   ; end of the window before (T stays within B of now,
        TADD    r2              ; so that the time-out lead fits up to B = 32,767); end of this one
        SHR     r1, 1
        WAIT    RX, FALL, r3, TO
        BES     zero            ; a pulse: 0
        ORI     r1, 128
zero:   DJNZ    r0, data
        TADD    r2              ; stop bit window
        WAIT    RX, FALL, r3, TO
        BEC     ok
        LDI     r3, 256         ; framing error
        OR      r1, r3
ok:     PUSH    r1
        JMP     idle

HB:     .word   0

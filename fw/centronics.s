; fw/centronics.s - Centronics parallel printer host: one byte per host word, STROBE / BUSY / ACK handshake.
;
; What it does:
;   Sends bytes to a printer on the Centronics parallel interface as the
;   Centronics 730-1 Printer Owner's Manual (December 1979) describes it:
;   DATA BIT 1..8 = uio[0..7] (UIO0..UIO7, driven, 1 = high), DATA STROBE =
;   uo_out[4] (UO4, negative pulse; uo pins are 1 from reset, so the strobe
;   is idle), BUSY = ui_in[3] (UI3, high = the printer cannot accept data),
;   ACK = ui_in[4] (UI4, negative pulse; not needed by the host: BUSY ends
;   within 50 ns of the ACK leading edge).
;   For each byte (Figure 9): wait until BUSY is low, put the byte on the
;   data lines, after SU cycles (1.1 us; Figure 9: 1 us min) pull STROBE low
;   for SW cycles (1.1 us; 1 us min), keep the data HOLD cycles (1.1 us; 1 us
;   min) after the strobe ends; BUSY is read only after that, so its rise
;   (within 50 ns of the strobe's trailing edge) is never missed.
;   The data lines are driven from the first byte on.
;
; Why:
;   The parallel printer interface is a strobed handshake with minimum set-up,
;   pulse and hold times and a printer-paced BUSY; a timed thread meets the
;   times and follows BUSY with no glue logic.
;
; Host words to this thread: bits 7..0 = a byte to print, one per word.
; Words to the host: none.
; Registers: r0 byte / work.

        .equ    STB, UO4
        .equ    BUSY, UI3
        .equ    SU, 66          ; data to strobe leading edge (1.1 us)
        .equ    SW, 66          ; strobe width (1.1 us)
        .equ    HOLD, 66        ; strobe trailing edge to the next data change (1.1 us)

        TNOW    16
        OUT     STB, 1
next:   TNOW    8
        PULL.NB r0
        BEC     next
chk:    IOR     r1, PINS        ; wait until the printer is not busy
        SHR     r1, 11
        ANDI    r1, 1
        BEQ     ready
        TNOW    120             ; a fall between the read and the WAIT is not lost: read the level again
        WAIT    BUSY, FALL, r1, TO
        JMP     chk
ready:  TNOW    48              ; eight OUTs take 32 cycles to issue: all eight lines change at T
        OUT     UIO0, r0>>      ; DATA BIT 1 .. 8
        OUT     UIO1, r0>>
        OUT     UIO2, r0>>
        OUT     UIO3, r0>>
        OUT     UIO4, r0>>
        OUT     UIO5, r0>>
        OUT     UIO6, r0>>
        OUT     UIO7, r0>>
        TADD    SU
        OUT     STB, 0          ; strobe leading edge
        TADD    SW
        OUT     STB, 1          ; trailing edge: the printer raises BUSY within 50 ns
        TADD    HOLD
        TWAIT                   ; data held, BUSY settled
        JMP     next

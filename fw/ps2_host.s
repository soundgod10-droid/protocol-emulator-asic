; fw/ps2_host.s - PS/2 host (system side of a keyboard or mouse port):
; receives the frames that the device clocks, and sends command bytes with
; the request-to-send sequence.
;
; What it does:
;   Pins: CLK = uio[4], DATA = uio[5], both open drain (pull-ups on the
;   board). An open-drain uio pin reads the line level (docs/isa.md 5.1).
;   Receive: 16 cycles after each falling CLK edge the firmware samples
;   DATA (the device keeps DATA valid from before the falling edge to past
;   the rising edge: IBM Personal System/2 Hardware Interface Technical
;   Reference, May 1988, Keyboards section, page 39). A frame is 11 bits:
;   start, 8 data bits LSB first, odd parity, stop (Figure 18, page 38).
;   Between frames the thread waits for a falling edge in windows of POLL
;   cycles and checks the host FIFO between two windows; if CLK is already
;   low after a window, the start edge came between two windows and the
;   frame is taken from there.
;   Send: CLK low for RTS cycles (more than 60 us, page 40), DATA low (start
;   bit), CLK released; the device clocks the frame. After falling edges 2
;   to 11 the firmware sets the next bit (8 data bits, parity, stop =
;   released); after falling edge 12 it samples the device acknowledge
;   (DATA low, page 40).
;
; Why:
;   A protocol that the instruction set was not designed for (flexibility):
;   a device-driven clock, open-drain lines and a request from the host.
;
; Host words to the thread: a command: bits 7:0 the byte, bit 8 the odd
;   parity bit, bit 9 = 1 (stop).
; Words from the thread: for each received frame the 11 bits in bits 5 to
;   15 (start bit 5, data bits 6 to 13, parity 14, stop 15); a word below
;   32 is a frame that stopped (no falling edge for 164 us): the number of
;   bits that did not come. For each command: bit 15 = DATA after falling
;   edge 12 (0 = acknowledged); 0xFFFF = no clock from the device in 16 ms.
; Not done: inhibit (CLK held low) while the host cannot take frames,
;   Resend handling (the host program does it).

        .equ    CLK, UIO4
        .equ    DATA, UIO5
        .equ    RTS, 4000       ; CLK low before the start bit (80 us at 50 MHz)
        .equ    POLL, 2000      ; start-edge window between two host-FIFO checks

host:   TNOW    12              ; two OUT events follow (docs/isa.md 7: 4 x 2 + 4)
        OUT     CLK, 1          ; released
        OUT     DATA, 1
idle:   TNOW    16
        TADD    POLL
        WAIT    DATA, FALL, r3, TO  ; the device sets DATA low (start bit) before the first CLK fall
        BES     st
        IOR     r3, PINS
        ANDI    r3, 0x20        ; DATA (input 5)
        BEQ     LATE            ; DATA is low: its edge came between two windows
nolate: PULL.NB r0
        BEC     idle
        JMP     tx
st:     TADD    4095
        TADD    4095
        WAIT    CLK, FALL, r3, TO, CT   ; falling edge of the start bit
        BEC     idle
        LDI     r2, 11
rs:     TADD    28              ; up to 3 instructions after the WAIT: lead 4 or more (docs/isa.md 4.2)
        SAMPLE  DATA, r1>>
        DJNZ    r2, rw
rend:   PUSH    r1              ; frame: start bit 5 ... stop bit 15; command: the acknowledge in bit 15
        WAIT    CLK, RISE, r3   ; end of the last clock: else the late check takes it as a start bit
        JMP     idle
rw:     TADD    4095
        TADD    4095
        WAIT    CLK, FALL, r3, TO, CT
        BES     rs
        PUSH    r2              ; no falling edge for 164 us: bits that did not come
        JMP     idle
tx:     TNOW    12
        OUT     CLK, 0          ; request to send
        TADD    RTS
        OUT     DATA, 0         ; start bit
        TADD    16
        OUT     CLK, 1          ; the device clocks the frame
        LDI     r1, 200         ; first falling edge within 200 x 4,095 cycles (16 ms)
tw:     TADD    4095
        WAIT    CLK, FALL, r3, TO, CT
        BES     t1
        DJNZ    r1, tw
tf:     TNOW    8
        OUT     DATA, 1
        LDI     r3, 0
        NOT     r3, r3
        PUSH    r3              ; 0xFFFF: no clock
        JMP     idle
t1:     LDI     r2, 10          ; falling edges 2 to 11: data bits 0 to 7, parity, stop
tn:     TADD    4095
        TADD    4095
        WAIT    CLK, FALL, r3, TO, CT
        BEC     tf
        TADD    24
        OUT     DATA, r0>>      ; next bit while CLK is low
        DJNZ    r2, tn
        TADD    4095
        TADD    4095
        WAIT    CLK, FALL, r3, TO, CT
        BEC     tf
        TADD    24
        SAMPLE  DATA, r1>>      ; falling edge 12: acknowledge
        JMP     rend

        .equ    LATE, st        ; the test sets LATE = nolate to measure the firmware without the check

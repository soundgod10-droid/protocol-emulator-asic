; fw/irda_tx.s - IrDA SIR transmitter (RZI pulses of a UART frame, 2.4 to 115.2 kbit/s).
;
; What it does:
;   Sends bytes as IrDA SIR (serial infrared, IrPHY) frames on IR_TXD =
;   uio[4] (output pin 4, UIO4, push-pull; a pull-down on the board holds it
;   low, LED off, while the chip is in reset or stopped and the pin is
;   released) to the TXD input of an IrDA transceiver (1 = LED on). Each byte
;   is a UART frame of 10 bit cells (start bit 0, 8 data bits LSB first,
;   stop bit 1); RZI modulation: a 0 bit is one pulse of P cycles at the start
;   of its bit cell, a 1 bit is no pulse (Vishay "IrDA, Physical Layer"
;   application note, document 82513, Fig. 2 and Table 2, which reproduce
;   IrPHY 1.4: pulse duration 1.41 us minimum, 3/16 of the bit time nominal,
;   rate tolerance 0.87 %).
;   The host gives the bit time B and the pulse time P in cycles once, before
;   the first byte: for 115.2 kbit/s at 60 MHz B = 521 (rate error +0.03 %)
;   and P = 98 (1.633 us, 3/16 of the bit time); for 9.6 kbit/s B = 6250,
;   P = 1172. Frames follow each other without a gap while the host keeps
;   bytes in the FIFO.
;
; Why:
;   IrDA SIR is a UART frame in which each 0 bit is a short light pulse; a
;   UART pin cannot make the pulses, and the pulse width has limits of its
;   own. The firmware places each pulse to the cycle.
;
; Host words to this thread: B (bit time, cycles, at most 32,767), P (pulse
;   time, cycles, less than B), then one word for each byte (bits 7..0).
; Words to the host: none.
; Registers: r0 frame bits, r1 bit counter, r2 B - P, r3 P.

        .equ    TX, UIO4

        TSYNC   32
        OUT     TX, 0           ; LED off
        PULL    r2              ; B
        PULL    r3              ; P
        SUB     r2, r3          ; B - P
idle:   TSYNC   32
        PULL.NB r0
        BEC     idle
        ANDI    r0, 255
        SHL     r0, 1           ; start bit 0 in bit 0
        LDI     r1, 512
        OR      r0, r1          ; stop bit 1 in bit 9
        LDI     r1, 10
        TSYNC   32              ; lead for the first pulse
bit:    SHR     r0, 1           ; C = the bit
        BCS     one
        OUT     TX, 1           ; 0 bit: pulse at the start of the cell
        TADD    r3
        OUT     TX, 0
        TADD    r2
        DJNZ    r1, bit
        JMP     idle
one:    TADD    r2              ; 1 bit: no pulse
        TADD    r3
        DJNZ    r1, bit
        JMP     idle

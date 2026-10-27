; uart_tx.s: UART transmitter firmware for one thread (milestone M2a).
;
; What it does:
;   The thread takes each word from its host FIFO. It sends the low 8 bits
;   of the word as one UART frame on UART_TX (uo_out[4], output pin UO4):
;   1 start bit (0), 8 data bits (LSB first), 1 stop bit (1), no parity
;   (8N1). One bit is BIT = 434 clock cycles. At 50 MHz this is
;   115,207 bit/s, +0.0064 % from 115,200 bit/s (A1 section 3).
;
; Why the bit time is correct:
;   Each OUT gives the pin value and the time T of the change (docs/isa.md
;   section 4). TADD moves T by exactly one bit time. Thus, the bit time
;   comes from T only, not from the time that the instructions take. When
;   the next word is in the FIFO before the stop bit ends, its start bit
;   starts exactly at the end of the stop bit (back-to-back frames). OUT
;   waits while the pin has an event that did not occur, so the thread is
;   at most one bit in front of the pin.
;
; Why the idle loop has TSYNC:
;   T has 16 bits. If T stays more than 32,767 cycles in the past, it looks
;   like a time in the future (docs/isa.md section 7). TSYNC keeps T at
;   least MARGIN cycles after now while the FIFO is empty. TSYNC never
;   moves T back, so the stop bit before an idle line is complete.
;
; Host configuration before RUN: output pin UO4 belongs to this thread,
; push-pull. Registers: r0 = frame bits not sent yet, r1 = bit counter.

        .equ    TX, UO4         ; UART_TX pin, uo_out[4] (A1 section 2.2)
        .equ    BIT, 434        ; cycles for one bit: 50,000,000 / 115,200 = 434.03
        .equ    MARGIN, 24      ; the first OUT is 5 instructions after TSYNC: 5 x 4 + 4 = 24 cycles

start:  TNOW    MARGIN          ; T = now + MARGIN: T starts near now
idle:   TSYNC   MARGIN          ; if T is less than MARGIN cycles after now, T = now + MARGIN
        PULL.NB r0              ; get a word from the host FIFO; E = 1 if there was one
        BEC     idle            ; E = 0: the FIFO is empty, try again
        SHL     r0, 1           ; bit 0 = 0 (start bit), bits 8 to 1 = data bits 0 to 7
        LDI     r1, 9           ; 9 bits to send from r0: start bit and 8 data bits
bit:    OUT     TX, r0>>        ; event: pin = bit 0 of r0 at time T, then r0 = r0 >> 1
        TADD    BIT             ; T = time of the next bit
        DJNZ    r1, bit         ; r1 = r1 - 1; if r1 is not 0, send the next bit
        OUT     TX, 1           ; stop bit: pin = 1 at time T
        TADD    BIT             ; T = end of the stop bit = earliest time of the next start bit
        JMP     idle            ; wait for the next word

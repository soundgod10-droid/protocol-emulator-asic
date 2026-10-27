; uart_rx.s: UART receiver firmware for one thread (milestone M2a).
;
; What it does:
;   The thread receives UART frames (8N1, LSB first) on UART_RX (ui_in[3],
;   input pin UI3). One bit is BIT = 434 clock cycles (115,207 bit/s at
;   50 MHz). For each frame, the thread writes one word to the host FIFO:
;   bits 7 to 0 = data, bit 8 = stop bit. Bit 8 = 0 is a framing error.
;
; Why it samples at these times:
;   WAIT gives the cycle of the falling edge of the start bit and puts it
;   in T (option CT). That cycle is 2 cycles after the pin edge, and
;   SAMPLE at time t reads the pin level of cycle t - 2 (synchronizer,
;   docs/isa.md section 6). These two delays cancel. Thus, T + HALF is the
;   middle of the start bit on the pin, and each next bit is BIT later.
;   The sample times come from T only, not from the instruction times.
;   If the start bit is 1 at its middle, the edge was a glitch and the
;   thread waits for the next falling edge.
;
; Host configuration before RUN: none (input pins need no owner).
; Registers: r0 = data bits, r1 = bit counter and stop bit, r2 = edge time.

        .equ    RX, UI3         ; UART_RX pin, ui_in[3] (A1 section 2.2)
        .equ    BIT, 434        ; cycles for one bit: 50,000,000 / 115,200 = 434.03
        .equ    HALF, 217       ; half a bit: middle of the start bit

rx:     WAIT    RX, FALL, r2, CT ; wait for a falling edge (no timeout); T = edge cycle
        TADD    HALF            ; T = middle of the start bit
        SAMPLE  RX, r1>>        ; read the start bit; E = its value
        BES     rx              ; E = 1: not a start bit (glitch), wait again
        LDI     r1, 8           ; 8 data bits
rbit:   TADD    BIT             ; T = middle of the next data bit
        SAMPLE  RX, r0>>        ; data bit into bit 15 of r0, r0 shifts right (LSB first)
        DJNZ    r1, rbit        ; next data bit if r1 is not 0
        TADD    BIT             ; T = middle of the stop bit
        SAMPLE  RX, r1>>        ; stop bit into bit 15 of r1 (r1 is 0 here)
        SHR     r0, 8           ; data bits 7 to 0 from bits 15 to 8
        SHR     r1, 7           ; stop bit from bit 15 to bit 8
        OR      r0, r1          ; word = stop bit (bit 8) and data (bits 7 to 0)
        PUSH    r0              ; give the word to the host
        JMP     rx              ; wait for the next start bit

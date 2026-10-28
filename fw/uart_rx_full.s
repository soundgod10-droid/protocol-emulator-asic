; fw/uart_rx_full.s - UART receiver for bit times up to 32,767 cycles.
;
; What it does:
;   Receives UART frames on UART_RX = ui_in[3] (input pin 11, UI3). It
;   waits for the falling start edge, checks the start bit, samples ND bits
;   (data and parity) and the stop bit, and sends one word to the host for
;   each frame. Bit time B and first-sample time H come from the host.
;   B up to 32,767 cycles: 50 MHz, 1,526 bit/s to 1 Mbit/s (isa 4.2
;   horizon). Lower rates: fw/uart_rx_slow.s.
;
; Why:
;   G1 step 3a: measured word count of the full firmware for the memory
;   size (decision D2, D4 item 13). The WAIT for the next start edge comes
;   12 to 16 cycles after the stop-bit sample (DJNZ, PUSH, slot phase). H
;   sets the sample point. G1 measured in the model the sender bit-time
;   error that this firmware accepts (test/test_fw_full.py): slow sender up
;   to H / 9B, fast sender up to (B - H - 18) / 10B. 1 Mbit/s at 50 MHz,
;   H 15: -4.0 % to +3.4 %. 1 Mbit/s at 40 MHz, H 12: -3.1 % to +3.4 %.
;
; Host words to this thread:
;   1. B: bit time in cycles, 40 to 32,767.
;   2. H: cycles from the start edge to the first sample point, 12 or more.
;      Use H = 9 x (B - 18) / 19 (equal fast and slow limits).
;      Bit k (start bit k = 0) is sampled at start edge + H + k x B; the
;      start bit is sampled 4 cycles later than that.
; Word to the host, one for each frame (ND = 8): bit 15 stop bit, bits 14
;   to 7 data bits 7 to 0, bit 6 start bit (0). Bits 5 to 0 are not
;   defined. ND = 9 (8E1): bit 15 stop, bit 14 parity, bits 13 to 6 data.
;   Stop bit 0 = framing error.
; Registers: r0 frame, r1 bit count and WAIT time, r2 B, r3 H.

        .equ    RX, UI3
        .equ    ND1, 9          ; ND + 1: data and parity bits, and the stop bit

        PULL    r2              ; B
        PULL    r3              ; H
        JMP     idle
bit:    TADD    r2
        SAMPLE  RX, r0>>
        DJNZ    r1, bit
        PUSH    r0
idle:   WAIT    RX, FALL, r1, CT ; T = start edge
        TADD    r3
        SAMPLE  RX, r0>>, 4     ; start bit
        BES     idle            ; 1: glitch, not a start bit
        LDI     r1, ND1
        JMP     bit

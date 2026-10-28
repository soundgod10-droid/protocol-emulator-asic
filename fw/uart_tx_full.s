; fw/uart_tx_full.s - UART transmitter for all rates and frame formats of A1 section 3.
;
; What it does:
;   Sends UART frames on UART_TX = uo_out[4] (output pin 12, UO4, push-pull).
;   The bit time is B = K x C cycles. C and K come from the host at start.
;   K = 1 for bit times up to 32,767 cycles (50 MHz: 1,526 bit/s to
;   1 Mbit/s). K > 1 for the low rates (50 MHz, 300 bit/s: B = 166,667 =
;   6 x 27,778), because an event must be issued 32,767 cycles or less
;   before its time (isa 4.2): the thread waits (TWAIT) after each chunk.
;
; Why:
;   G1 step 3a: measured word count of the full firmware for the memory
;   size (decision D2, D4 item 13). The host makes the frame word, so the
;   same 16 words send 8N1, 8E1, 8N2 and other formats (A1 section 8.2).
;
; Host words to this thread:
;   1. C: chunk time in cycles, 32 to 32,767.
;   2. K: number of chunks in each bit, 1 or more.
;   3. Then one frame word for each frame. Bit 0 is the first bit on the
;      line (start bit 0), then the data bits LSB first, the parity bit if
;      used, the stop bits (1), and one more 1 above the last bit (end
;      mark). Examples, data d: 8N1 (d << 1) | 0x600, 8N2 (d << 1) | 0xE00,
;      8E1 (d << 1) | (p << 9) | 0xC00 (p = even parity of d).
;   The firmware sends bits until only the end mark stays (r0 < 2). The
;   next frame word starts the next start bit at the end of the last stop
;   bit (back-to-back) if it is in the FIFO in time.
;
; Words to the host: none.
; Registers: r0 frame, r1 chunk count, r2 C, r3 K.

        .equ    TX, UO4

        PULL    r2              ; C
        PULL    r3              ; K
idle:   TSYNC   16              ; keep T near now while the line is idle (isa 7)
        PULL.NB r0
        BEC     idle
bit:    OUT     TX, r0>>        ; this bit at time T
        MOV     r1, r3
chunk:  TADD    r2
        DJNZ    r1, long        ; K > 1: wait after each chunk
        CMPI    r0, 2
        BCS     bit             ; bits left
        PULL.NB r0              ; next frame at the end of the stop bit
        BES     bit
        JMP     idle
long:   TWAIT
        JMP     chunk

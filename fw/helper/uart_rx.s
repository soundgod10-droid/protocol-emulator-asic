; fw/helper/uart_rx.s - UART receiver at 15 Mbaud through the USB helper's clocked mode (edge-started frames).
;
; What it does:
;   Thread 0 sets the USB helper (ports 16-31) to the clocked code with receive on, TXPER 2 (4 cycles a bit: 15 Mbaud
;   at 60 MHz), DIN = input 8 (ui_in[0]) with sample delay 0, LTC[10] (a frame starts at a falling edge of DIN, the
;   start bit) and 10 samples a frame (start, 8 data, stop). After 100 cycles it drops any word received while the
;   line settled, then stores every received word from data address 768: one word a character, start sample in bit
;   6, data in bits 7 to 14 (LSB first), stop sample in bit 15. The helper re-arms at the last sample of a frame, so
;   back-to-back characters need no idle time.
;
; Why:
;   A thread alone needs 16 cycles a bit (TADD, and an on-time SAMPLE that holds it 3 slots); the helper samples
;   every bit at a fixed delay from the start edge. Tolerance by calculation (0.5 - 1/4) / 9.5 = 2.6 % of the bit
;   time; the chip test passes at -2.5 % and +2.5 %. docs/helpers.md section 6.
;
; Host calls (fw/helper/host_calls.py): uart_rx_buffer(n) gives the empty words for address 768; uart_rx_bytes(words)
; returns the characters and the indices of framing errors. Tested by test_slu_chip_uart_rx (RTL and gate level).

        .equ    E, 16           ; USB helper ports 16-31
        LDI     r0, 0
        IOW     r0, E+12        ; CFGADR 0
        LDI     r0, 0x00B
        IOW     r0, E+13        ; MODE: clocked code, RX on
        LDI     r0, 2
        IOW     r0, E+13        ; TXPER 2: 4 cycles a bit (15 Mbaud at 60 MHz)
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0xFF
        SHL     r0, 4
        ORI     r0, 8
        IOW     r0, E+13        ; SEL 0x0FF8: DIN = input 8 (ui_in[0]), sample delay 0
        LDI     r0, 18
        IOW     r0, E+12
        LDI     r0, 1
        SHL     r0, 10
        IOW     r0, E+13        ; LTC[10]: a frame starts at a falling edge of DIN (the start bit)
        LDI     r0, 10
        IOW     r0, E+4         ; TXCTRL: 10 samples a frame (start, 8 data, stop)
        TNOW    100             ; drop any word received before the line settles (as test_slc_uart_rx empties
        TWAIT                   ; the RX buffer before its first byte)
drain:  IOR     r0, E+6
        ANDI    r0, 0x60
        BEQ     go
        IOR     r0, E+1
        JMP     drain
go:     LDI     r1, 768         ; received words to the data memory from 768
rx:     IOR     r0, E+6         ; RXINFO [6:5]: words in the RX buffer
        ANDI    r0, 0x60
        BEQ     rx
        IOR     r0, E+1         ; RXDATA (pop)
        STX     r0, [r1]
        ADDI    r1, 1
        JMP     rx

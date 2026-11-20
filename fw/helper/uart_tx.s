; fw/helper/uart_tx.s - UART transmitter at 15 Mbaud through the USB helper's NRZ code.
;
; What it does:
;   Thread 0 sets the USB helper (ports 16-31) to the NRZ code with receive off and TXPER 4 (4 cycles a bit: 15 Mbaud
;   at 60 MHz), then sends the frame that the host put in the data memory (bit count at 512, the words from 513,
;   first line bit in bit 0) from TXTIME = T + 100, feeding the 2-word TX buffer while the frame runs. At TXDONE it
;   pushes the helper STATUS word (TXDONE set, TXUNDER clear when the feed kept up).
;   Pin: TXA (helper data) on the pin that the host configures as helper output 0.
;
; Why:
;   A thread alone sends one bit every 8 cycles at best (OUT and TADD); the helper's bit timer gives 4 cycles a bit
;   with every edge on the bit grid. docs/helpers.md section 6.
;
; Host calls (fw/helper/host_calls.py): uart_tx_frame(data) gives the words for address 512 (8N1 characters back to
; back; the host packs the start and stop bits). Tested by test_slu_chip_uart_tx (RTL and gate level) with an
; independent receiver at each bit middle.

        .equ    E, 16           ; USB helper ports 16-31
        LDI     r0, 0
        IOW     r0, E+12        ; CFGADR 0
        LDI     r0, 0
        IOW     r0, E+13        ; MODE: NRZ code, RX off, no stuffing, not the USB line
        LDI     r0, 4
        IOW     r0, E+13        ; TXPER 4: 4 cycles a bit (15 Mbaud at 60 MHz)
        LDI     r2, 512         ; frame: bit count N at 512, then the words (first bit in bit 0)
        LDX     r3, [r2]
        MOV     r0, r3
        ANDI    r0, 15
        IOW     r0, E+4         ; TXCTRL: bits in the last word
        ADDI    r3, 15
        SHR     r3, 4           ; words
        ADDI    r2, 1
        TNOW    40
        TGET    r0
        ADDI    r0, 100
        IOW     r0, E+5         ; TXTIME = T + 100: the loop fills the TX buffer first
tx:     IOR     r0, E+0         ; TX buffer free words
        CMPI    r0, 0
        BEQ     tx
        LDX     r0, [r2]
        ADDI    r2, 1
        CMPI    r3, 1
        BEQ     last
        IOW     r0, E+0         ; TXDATA
        ADDI    r3, -1
        JMP     tx
last:   IOW     r0, E+14        ; TXLAST
done:   IOR     r1, E+2         ; STATUS: TXDONE
        ANDI    r1, 1
        BEQ     done
        IOR     r1, E+2
        PUSH    r1
        HALT

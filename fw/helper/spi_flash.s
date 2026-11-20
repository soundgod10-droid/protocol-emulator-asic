; fw/helper/spi_flash.s - SPI flash read through the USB helper's clocked mode (SPI mode 0, 15 MHz SCK).
;
; What it does:
;   Thread 0 sets the USB helper (ports 16-31) to its clocked code with receive on, TXPER 2 (4 cycles a bit:
;   15 MHz SCK at 60 MHz), DIN = input 8 (ui_in[0], MISO) sampled 5 cycles after each rising SCK edge, SCK idle
;   low with data at the trailing edge (mode 0). It drives /CS on uio2 with OUT events, sends the frame that the
;   host put in the data memory (bit count at 512, the words from 513, first bit in bit 0) from TXTIME = T + 100,
;   waits for the frame end (RXEOP), raises /CS and pushes the helper STATUS word.
;   Thread 1 (starts at 120) stores every received word from data address 768 and writes the next free address to
;   address 500, so the host can read the result after STOP.
;   Pins: SCK = helper clock (TXB), MOSI = helper data (TXA), /CS = uio2 (thread 0), MISO = ui_in[0].
;
; Why:
;   The helper shifts one bit every 4 cycles with the sample delay set for the pad and board round trip; a thread
;   alone needs 25 or more cycles a half period (fw/spi_ctl.s). docs/helpers.md section 6.
;
; Host calls (fw/helper/host_calls.py): spi_flash_read_frame(addr, n) gives the words for address 512 (03h, 24-bit
; address, n data bytes of clocks); spi_flash_read_bytes(words, n) takes the n bytes out of the words read from 768.
; Tested by test_slu_chip_spi_flash (RTL and gate level) against a flash model with W25Q128JV times at its pins.

        .equ    E, 16           ; USB helper ports 16-31
        .equ    CS, 2           ; uio2: /CS, a thread pin
t0:     TNOW    8
        OUT     CS, 1
        LDI     r0, 0
        IOW     r0, E+12        ; CFGADR 0
        LDI     r0, 0x00B
        IOW     r0, E+13        ; MODE: clocked code, RX on
        LDI     r0, 2
        IOW     r0, E+13        ; TXPER 2: 4 cycles a bit (15 MHz at 60 MHz)
        LDI     r0, 8
        IOW     r0, E+12
        LDI     r0, 0x17F
        SHL     r0, 6
        ORI     r0, 0x38
        IOW     r0, E+13        ; SEL 0x5FF8: DIN = input 8 (ui_in[0]), sample delay 5
        LDI     r0, 18
        IOW     r0, E+12
        LDI     r0, 0
        IOW     r0, E+13        ; LTC: clock idle low, data at the trailing edge (SPI mode 0)
        LDI     r2, 512         ; frame: bit count N at 512, then the words (first bit in bit 0)
        LDX     r3, [r2]
        MOV     r0, r3
        ANDI    r0, 15
        IOW     r0, E+4         ; TXCTRL: bits in the last word
        ADDI    r3, 15
        SHR     r3, 4           ; words
        ADDI    r2, 1
        TNOW    40
        OUT     CS, 0           ; /CS low at T
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
done:   IOR     r1, E+2         ; STATUS: RXEOP
        ANDI    r1, 0x20
        BEQ     done
        TNOW    20
        OUT     CS, 1
        IOR     r1, E+2
        PUSH    r1
        HALT
        .org    120
t1:     LDI     r1, 768         ; thread 1 (starts at 120): received words to the data memory from 768
rx:     IOR     r0, E+6         ; RXINFO [6:5]: words in the RX buffer
        ANDI    r0, 0x60
        BEQ     rx
        IOR     r0, E+1         ; RXDATA (pop)
        STX     r0, [r1]
        ADDI    r1, 1
        ST      r1, 500         ; next free address, for the host
        JMP     rx

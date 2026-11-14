; fw/spi_tgt.s - SPI target, all four modes, 8-bit bytes, MSB first.
;
; What it does:
;   Pins: CS = uio[0], MOSI = uio[1], SCK = uio[3] (inputs from the SPI
;   controller); MISO = uio[2] (push-pull output of this thread, released
;   while CS is high). For each byte the thread sends a byte from the host
;   on MISO and gives the byte that it receives on MOSI to the host.
;   The thread follows only the sample edges of SCK (WAIT SCK, SE): the
;   controller samples MISO at that edge, so MISO can change after it, and
;   MOSI is valid from the shift edge before it to the shift edge after it.
;   At each sample edge the thread samples MOSI 20 cycles after the edge
;   cycle and sets the next MISO bit (16 cycles after the edge cycle in a
;   byte, 36 at the first bit of a byte). The first MISO bit is set 16
;   cycles after the CS edge cycle (all modes).
;   SE is set with .equ when the host assembles the file:
;   SE = 1 (RISE) for modes 0 (CPOL 0, CPHA 0) and 3 (CPOL 1, CPHA 1),
;   SE = 0 (FALL) for modes 1 (CPOL 0, CPHA 1) and 2 (CPOL 1, CPHA 0).
;   A sample edge is followed by a timeout of GAP cycles only at the start
;   of a byte: if no sample edge comes, the transfer is at its end (CS
;   high): MISO is released and the thread waits for the next CS low.
;
; Why:
;   The target must be correct in all four modes and at 1 MHz
;   (test_m3_spi_tgt, docs/decisions.md entry 11). One WAIT for each bit: a thread issues one
;   instruction every 4 cycles, and an event needs a lead of 4 cycles (isa
;   4.2), so a WAIT for the shift edge too would halve the rate (measured:
;   smallest SCK half period 49 cycles with it, 25 without).
;
; Host words to this thread: one word for each MISO byte, the byte in bits
;   9 to 2 (byte x 4: the thread then needs no shift on the byte path).
;   The thread takes the first word when it waits for CS low, and the word
;   for byte k + 1 when byte k starts. For a transfer of N bytes the host
;   gives N + 1 words before the controller starts it (the FIFO holds 4); the
;   last word is not sent. With no word the thread sends 0xFF.
; Words to the host: one for each byte: bits 7 to 0 the MOSI byte, bits 15
;   to 8 the MOSI byte before it.
; Controller limits in clock cycles (model, test_m3_spi_tgt): SCK half
;   period >= 25 (1 MHz at 50 MHz); MOSI held >= 21 after the sample edge;
;   CS low to the first sample edge >= 21; 2 x half period + pause between
;   bytes <= GAP + 16; next CS low >= GAP + 57 after the last sample edge;
;   MISO released <= GAP + 39 after it. GAP <= 4,095 (TADD), so the half
;   period must be <= 2,055. Table and times: docs/host_port.md section 13.
; Registers: r0 MISO bits, r1 MOSI bits, r2 bit count, r3 edge time and
;   the next MISO byte.

        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    SE, 1           ; sample edge: 1 RISE (modes 0, 3), 0 FALL (modes 1, 2)
        .equ    LAT, 16         ; MISO event after the sample edge cycle in the bit loop (the smallest on time)
        .equ    LATF, 20        ; MOSI sample after the sample edge cycle at the first bit of a byte (the BEC is before it)
        .equ    GAP, 200        ; byte start: no sample edge for GAP cycles = end of the transfer

        TNOW    8
        OUT     MISO, Z         ; released while CS is high
idle:   LDI     r0, 0x3FC
        PULL.NB r0              ; first byte x 4, or 0xFF x 4
        SHL     r0, 6           ; first byte to bits 15 to 8
        LDI     r2, 7
        WAIT    CS, FALL, r3, CT ; T = CS low
        TADD    LAT
        OUT     MISO, r0<<      ; bit 7 of the first byte
        TADD    GAP
first:  WAIT    SCK, SE, r3, TO, CT ; sample edge of bit 7
        BEC     done            ; no sample edge for GAP cycles
        TADD    LATF            ; MOSI sample time: edge + 20, as in the bit loop
        SAMPLE  MOSI, r1<<, 0
        TADD    12
        OUT     MISO, r0<<, 4   ; edge + 36: the first time with a lead of 4 after the SAMPLE
        LDI     r3, 0x3FC
        PULL.NB r3              ; next byte x 4, or 0xFF x 4
        OR      r0, r3          ; to bits 9 to 2: bits 1 and 0 and 15 to 10 are other bits
bit:    WAIT    SCK, SE, r3, CT ; sample edge of bits 6 to 0
        TADD    LAT
        OUT     MISO, r0<<      ; next bit (after bit 0: bit 7 of the next byte)
        SAMPLE  MOSI, r1<<, 4
        DJNZ    r2, bit
        PUSH    r1
        LDI     r2, 7
        TADD    GAP             ; timeout of the next byte start
        JMP     first
done:   TNOW    8
        OUT     MISO, Z
        JMP     idle

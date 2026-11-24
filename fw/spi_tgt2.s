; fw/spi_tgt2.s - SPI target on two threads, all four modes, 8-bit bytes, MSB first, for SCK up to 2 MHz
; at 60 MHz (half period 15 cycles; the measured limit is in test/test_spi_tgt2.py).
;
; What it does:
;   Pins as fw/spi_tgt.s: CS = uio[0], MOSI = uio[1], SCK = uio[3] (inputs from the SPI controller);
;   MISO = uio[2] (push-pull output of thread 0, released while CS is high). Thread 0 (label tx) drives MISO:
;   it waits for each sample edge of SCK and puts the next bit on MISO 16 cycles after the edge cycle (20 at the
;   first bit of a byte, 16 after the CS edge cycle for the first bit of a transfer). Thread 1 (label rx) receives
;   MOSI: it waits for each sample edge and reads the pins with IOR PINS in its next slot, 5 to 8 cycles after the
;   edge cycle (the synchronized level of the pin 5 to 8 cycles after the pin edge, docs/isa.md 6.1, 10), so the
;   controller must hold MOSI 9 cycles (150 ns) after the sample edge; a controller that changes MOSI at the
;   shift edge holds it for one half period.
;   SE is set with .equ when the host assembles the file: SE = 1 (RISE) for modes 0 (CPOL 0, CPHA 0) and 3
;   (CPOL 1, CPHA 1), SE = 0 (FALL) for modes 1 and 2, as fw/spi_tgt.s.
;   End of a transfer: no sample edge for GAP cycles after the last sample edge of a byte: thread 0 releases MISO
;   and waits for the next CS low; thread 1 gives the last byte to the host.
;
; Why:
;   fw/spi_tgt.s on one thread is correct up to a half period of 25 cycles (1.2 MHz at 60 MHz, docs/host_port.md
;   section 13): the first bit of each byte has 11 instructions between two sample edges (WAIT, timeout check,
;   SAMPLE of 3 slots, OUT, the next host word), and a SAMPLE cannot read a pin earlier than 16 cycles after the
;   edge cycle (TADD, then a lead of 4 after a slot of up to 4 cycles). Two threads split the work: thread 1 has at
;   most 7 instructions between two sample edges (the first bit of a byte), thread 0 at most 7, and IOR PINS reads
;   MOSI in the slot after the WAIT with no lead. test/test_spi_tgt2.py measures the smallest half period and the
;   margins against an SPI controller model, and the single-thread alternatives (fw/spi_tgt.s, a reordered loop
;   with SAMPLE first, a loop with IOR PINS) with their limits.
;
; Host words to thread 0, as fw/spi_tgt.s: one word for each MISO byte, the byte in bits 9 to 2; for a transfer
;   of N bytes the host gives N + 1 words before the controller starts it (the FIFO holds 4); the last word is not
;   sent; with no word the thread sends 0xFF; after START the thread waits for the first word.
; Words from thread 1: one for each MOSI byte, the byte in bits 7 to 0 (bits 15 to 8 are 0). Byte k is given when
;   byte k + 1 starts, the last byte of a transfer GAP cycles after its last sample edge. The words are given with
;   PUSH.NB: the host must read them before four are complete.
; Controller limits in clock cycles at 60 MHz, measured in the model (test_spi_tgt2_sweep): the test log and
;   docs/protocols.md give them: half period, MOSI hold after the sample edge, CS low to the first sample edge,
;   CS high between transfers, MISO set-up and hold at the sample edge.
; Not done: SCK edges while CS is high (another target's transfer on a shared bus) are taken as bits, as in
;   fw/spi_tgt.s; a transfer must be whole bytes.
; Registers: thread 0: r0 MISO bits, r2 bit count, r3 edge time and the next MISO byte; thread 1: r1 MOSI bits,
;   r2 edge time and the pin levels.

        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    SE, 1           ; sample edge: 1 RISE (modes 0, 3), 0 FALL (modes 1, 2)
        .equ    GAP, 200        ; no sample edge for GAP cycles after a byte = end of the transfer

; ----- thread 0: MISO. r0 MISO bits, r2 bit count, r3 temporary.
tx:     TNOW    8
        OUT     MISO, Z         ; released while CS is high
        PULL    r0              ; after START: wait for the first byte
        JMP     got
idle:   LDI     r0, 0x3FC
        PULL.NB r0              ; first byte x 4, or 0xFF x 4
got:    SHL     r0, 6           ; first byte to bits 15 to 8
        LDI     r2, 7
        WAIT    CS, FALL, r3, CT ; T = CS low
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 7 of the first byte, 16 cycles after the CS edge cycle
        TADD    GAP
first:  WAIT    SCK, SE, r3, TO, CT ; sample edge of bit 7
        BEC     done            ; no sample edge for GAP cycles
        TADD    16
        OUT     MISO, r0<<, 4   ; bit 6: edge + 20 (the first OUT after the timeout check)
        LDI     r3, 0x3FC
        PULL.NB r3              ; next byte x 4, or 0xFF x 4
        OR      r0, r3          ; to bits 9 to 2 (bits 15 to 10 hold bits 5 to 0 of this byte)
bit:    WAIT    SCK, SE, r3, CT ; sample edge of bits 6 to 0
        TADD    12
        OUT     MISO, r0<<, 4   ; next bit: edge + 16 (after bit 0: bit 7 of the next byte)
        DJNZ    r2, bit
        LDI     r2, 7
        TADD    GAP             ; timeout of the next byte start
        JMP     first
done:   TNOW    8
        OUT     MISO, Z
        JMP     idle

; ----- thread 1: MOSI. r1 MOSI bits (the byte in bits 8 to 1 until the shift), r2 temporary.
rx:     WAIT    CS, FALL, r2    ; a transfer starts: SCK idles high in modes 2 and 3, so no SCK edge counts before
        WAIT    SCK, SE, r2, CT ; sample edge of bit 7 of the first byte
        IOR     r1, PINS        ; pin levels 5 to 8 cycles after the edge cycle
        ANDI    r1, 2           ; MOSI (uio[1]) in bit 1
        JMP     b6
first1: WAIT    SCK, SE, r2, TO, CT ; sample edge of bit 7 of the next byte, or no edge for GAP cycles
        IOR     r2, PINS
        BEC     flush           ; end of the transfer (IOR does not change E)
        ANDI    r2, 2
        SHR     r1, 1           ; the byte before: bits 8 to 1 to bits 7 to 0
        PUSH.NB r1
        MOV     r1, r2
b6:     WAIT    SCK, SE, r2, CT ; bit 6
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 5
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 4
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 3
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 2
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 1
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 0
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        TADD    GAP             ; timeout of the next byte start
        JMP     first1
flush:  SHR     r1, 1
        PUSH.NB r1              ; last byte of the transfer
        WAIT    SCK, SE, r2, CT ; bit 7 of the next transfer
        IOR     r1, PINS
        ANDI    r1, 2
        JMP     b6

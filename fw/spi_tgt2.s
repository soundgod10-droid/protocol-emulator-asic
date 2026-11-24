; fw/spi_tgt2.s - SPI target on two threads, all four modes, 8-bit bytes, MSB first, for the fastest SCK that two
; threads allow: the half period is measured in test/test_spi_tgt2.py (structure D), with the rated value
; H_RATED_D there; the sweep table gives the controller limits. fw/spi_tgt3.s is the same target on three
; threads with a shorter half period.
;
; What it does:
;   Pins as fw/spi_tgt.s: CS = uio[0], MOSI = uio[1], SCK = uio[3] (inputs from the SPI controller);
;   MISO = uio[2] (push-pull output of thread 0, released while CS is high).
;   Thread 0 (label tx) drives MISO: it waits for each sample edge of SCK and puts the next bit on MISO 16
;   cycles after the edge cycle (16 after the CS edge cycle for the first bit of a transfer). The byte is
;   unrolled, so that at most 4 instructions follow a WAIT (the next host word is fetched over three bits).
;   Thread 1 (label rx) receives MOSI: it waits for each sample edge and reads the pins with IOR PINS in its
;   next slot, 5 to 8 cycles after the edge cycle (the synchronized level of the pin, docs/isa.md 6.1 and 10),
;   so the controller must hold MOSI 9 cycles (150 ns) after the sample edge; a controller that changes MOSI at
;   the shift edge holds it for one half period. The bits are shifted into one 16-bit word that is never
;   cleared; the word is given to the host at the second sample edge of the next byte, so at most 5
;   instructions follow a WAIT (the host word, the timeout check, the timeout and the loop jump are in four
;   different bits).
;   SE is set with .equ when the host assembles the file: SE = 1 (RISE) for modes 0 (CPOL 0, CPHA 0) and 3
;   (CPOL 1, CPHA 1), SE = 0 (FALL) for modes 1 and 2, as fw/spi_tgt.s.
;   End of a transfer: no sample edge for GAP cycles after the last sample edge of a byte: thread 0 releases MISO
;   and waits for the next CS low; thread 1 gives the last byte to the host with bit 0 = 1. A transfer must be
;   whole bytes.
;
; Why:
;   The next WAIT must be issued before the next sample edge (isa 6.3), and a thread has one slot every 4
;   cycles, so with n instructions between two WAITs the SCK half period H must be 2 H >= 4 n + 9.
;   fw/spi_tgt.s on one thread has n = 10 at the first bit of a byte (with the slots of its SAMPLE): H >= 25
;   (1.2 MHz at 60 MHz, docs/host_port.md section 13). The first two-thread version had n = 6 at the byte
;   boundary of both threads: H >= 17 (measured 17). This version has n <= 4 in thread 0 and n <= 5 in
;   thread 1. A MOSI bit alone costs IOR, ANDI, SHL, OR (n = 4, H >= 13): fw/spi_tgt3.s reaches that with a
;   third thread. test/test_spi_tgt2.py measures all of them against an SPI controller model.
;
; Host words to thread 0: one word for each MISO byte, the byte in bits 11 to 4 (byte x 16); for a transfer of N
;   bytes the host gives N + 1 words before the controller starts it (the FIFO holds 4); the last word is not
;   sent; with no word the thread sends 0xFF; after START the thread waits for the first word.
; Words from thread 1 (PUSH.NB; the host must read them before four are complete): one for each MOSI byte: byte
;   k in bits 10 to 3 of the word given at the second sample edge of byte k + 1, bits 2 and 1 = bits 7 and 6 of
;   byte k + 1, bit 0 = 0; the last byte of a transfer GAP cycles after its last sample edge, in bits 10 to 3 with
;   bit 0 = 1 and bits 2 and 1 = 0. The host takes (word >> 3) and 0xFF.
; Controller limits in clock cycles at 60 MHz, measured in the model (test_spi_tgt2_sweep): half period, MOSI hold
;   after the sample edge, CS low to the first sample edge, CS high between transfers, MISO set-up and hold.
; Not done: SCK edges while CS is high (another target's transfer on a shared bus) are taken as bits, as in
;   fw/spi_tgt.s.
; Registers: thread 0: r0 MISO bits, r1 edge time, r2 the word for a missing byte, r3 the next word;
;   thread 1: r1 MOSI bits, r2 edge time and pin levels.

        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    SE, 1           ; sample edge: 1 RISE (modes 0, 3), 0 FALL (modes 1, 2)
        .equ    GAP, 200        ; no sample edge for GAP cycles after a byte = end of the transfer

; ----- thread 0: MISO. r0 MISO bits, r1 edge time, r2 0x0FF0, r3 the next word.
tx:     TNOW    8
        OUT     MISO, Z         ; released while CS is high
        LDI     r2, 0xFF
        SHL     r2, 4           ; 0x0FF0: the word for a missing byte
        PULL    r0              ; after START: wait for the first byte
        JMP     got
idle:   MOV     r0, r2
        PULL.NB r0              ; first byte x 16, or 0xFF x 16
got:    SHL     r0, 4           ; first byte to bits 15 to 8
        WAIT    CS, FALL, r1, CT ; T = CS edge cycle
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 7 of the first byte: CS edge cycle + 16
        TADD    GAP             ; timeout of the first sample edge
first:  WAIT    SCK, SE, r1, TO, CT ; sample edge of bit 7, or no sample edge for GAP cycles
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 6: edge + 16 (after a timeout: one more bit, released about 12 cycles later)
        BEC     done
        MOV     r3, r2          ; 4 instructions after the WAIT
        WAIT    SCK, SE, r1, CT ; sample edge of bit 6
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 5: edge + 16
        PULL.NB r3              ; next byte x 16, or 0xFF x 16
        WAIT    SCK, SE, r1, CT ; bit 5
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 4
        OR      r0, r3          ; next byte to bits 11 to 4; bits 15 to 12 hold bits 3 to 0 of this byte
        WAIT    SCK, SE, r1, CT ; bit 4
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 3
        WAIT    SCK, SE, r1, CT ; bit 3
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 2
        WAIT    SCK, SE, r1, CT ; bit 2
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 1
        WAIT    SCK, SE, r1, CT ; bit 1
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 0
        WAIT    SCK, SE, r1, CT ; bit 0
        TADD    12
        OUT     MISO, r0<<, 4   ; bit 7 of the next byte: edge + 16
        TADD    GAP             ; timeout of the next byte start
        JMP     first
done:   TNOW    8
        OUT     MISO, Z
        JMP     idle

; ----- thread 1: MOSI. r1 MOSI bits (never cleared), r2 edge time and pin levels.
rx:     WAIT    CS, FALL, r2, CT ; a transfer starts (SCK idles high in modes 2 and 3: no edge counts before CS)
        TADD    GAP             ; timeout of the first sample edge
        WAIT    SCK, SE, r2, TO, CT ; sample edge of bit 7 of the first byte
        BEC     rx              ; no clock after CS low
        IOR     r1, PINS        ; pin levels 5 to 8 cycles after the edge cycle
        ANDI    r1, 2           ; MOSI (uio[1]) in bit 1
        WAIT    SCK, SE, r2, CT ; bit 6 of the first byte (no word to give yet)
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, CT ; bit 5 of the first byte
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
b4:     WAIT    SCK, SE, r2, CT ; bit 4
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
        OR      r1, r2          ; the byte in bits 8 to 1
        TADD    GAP             ; timeout of the next byte start: 5 instructions after the WAIT
first1: WAIT    SCK, SE, r2, TO, CT ; sample edge of bit 7 of the next byte, or no edge for GAP cycles
        IOR     r2, PINS
        BEC     flush           ; end of the transfer (IOR does not change E)
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2          ; 5
        WAIT    SCK, SE, r2, CT ; bit 6
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        PUSH.NB r1              ; the byte before in bits 10 to 3, bits 7 and 6 of this byte in bits 2 and 1
        WAIT    SCK, SE, r2, CT ; bit 5
        IOR     r2, PINS
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        JMP     b4              ; 5
flush:  SHL     r1, 2           ; last byte of the transfer to bits 10 to 3
        ORI     r1, 1           ; bit 0 = 1: the last byte
        PUSH.NB r1
        JMP     rx

; fw/spi_tgt3.s - SPI target on three threads, all four modes, 8-bit bytes, MSB first, for the fastest SCK that
; the instruction rate allows: the half period is measured in test/test_spi_tgt2.py (structure E), with the
; rated value H_RATED_E there; the sweep table gives the controller limits.
;
; What it does:
;   Pins as fw/spi_tgt.s: CS = uio[0], MOSI = uio[1], SCK = uio[3] (inputs from the SPI controller);
;   MISO = uio[2] (push-pull output of thread 0, released while CS is high).
;   Thread 0 (label tx) drives MISO: it waits for each sample edge of SCK and puts the next bit on MISO 16
;   cycles after the edge cycle (16 after the CS edge cycle for the first bit of a transfer). The byte is unrolled,
;   so that at most 4 instructions follow a WAIT. It is the same thread 0 as fw/spi_tgt2.s.
;   Thread 1 (label rx0) receives the MOSI bytes 0, 2, 4, ... of a transfer, thread 2 (label rx1) the bytes 1, 3,
;   5, ... Each waits for every sample edge: in its own byte it reads the pins with IOR PINS in the slot after
;   the WAIT (5 to 8 cycles after the edge cycle, docs/isa.md 6.1 and 10), masks MOSI and shifts it in: 4
;   instructions after the WAIT. In the other thread's byte it only counts the edges, and there it gives its byte
;   to the host and sets the timeout of the next byte. So no WAIT has more than 4 instructions before the next one.
;   SE is set with .equ when the host assembles the file: SE = 1 (RISE) for modes 0 (CPOL 0, CPHA 0) and 3
;   (CPOL 1, CPHA 1), SE = 0 (FALL) for modes 1 and 2, as fw/spi_tgt.s.
;   End of a transfer: no sample edge for GAP cycles after the last sample edge of a byte: thread 0 releases MISO
;   and waits for the next CS low; the thread whose byte it was gives it to the host with bit 0 = 1; the other
;   thread waits for the next CS low. A transfer must be whole bytes.
;
; Why:
;   The next WAIT must be issued before the next sample edge (isa 6.3), and a thread has one slot every 4 cycles,
;   so with n instructions between two WAITs the SCK half period H must be 2 H >= 4 n + 9. A MOSI bit needs IOR,
;   ANDI, SHL, OR: n = 4, H >= 13 (2.3 MHz at 60 MHz). With two threads (fw/spi_tgt2.s) the host word, the
;   timeout check and the loop jump of each byte make n = 5 in some iteration: H >= 15. The third thread takes
;   those instructions into the idle byte. The cost: 3 of the 4 threads. test/test_spi_tgt2.py measures both
;   programs and the single-thread alternatives against an SPI controller model.
;
; Host words to thread 0: one word for each MISO byte, the byte in bits 11 to 4 (byte x 16); for a transfer of N
;   bytes the host gives N + 1 words before the controller starts it (the FIFO holds 4); the last word is not
;   sent; with no word the thread sends 0xFF; after START the thread waits for the first word.
; Words from threads 1 and 2 (PUSH.NB; the host must read them before four are complete): one for each MOSI byte,
;   from thread 1 for the bytes 0, 2, 4, ... and from thread 2 for the bytes 1, 3, 5, ... of each transfer; the
;   byte in bits 10 to 3, bit 0 = 1 for the last byte of a transfer, the other bits 0. Byte k is given at the
;   second sample edge of byte k + 1, the last byte GAP cycles after its last sample edge.
; Controller limits in clock cycles at 60 MHz, measured in the model (test_spi_tgt2_sweep): half period, MOSI hold
;   after the sample edge, CS low to the first sample edge, CS high between transfers, MISO set-up and hold.
; Not done: SCK edges while CS is high (another target's transfer on a shared bus) are taken as bits, as in
;   fw/spi_tgt.s.
; Registers: thread 0: r0 MISO bits, r1 edge time, r2 the word for a missing byte, r3 the next word; threads 1
;   and 2: r1 MOSI bits, r2 edge time and pin levels, r3 edge time.

        .equ    CS, UIO0
        .equ    MOSI, UIO1
        .equ    MISO, UIO2
        .equ    SCK, UIO3
        .equ    SE, 1           ; sample edge: 1 RISE (modes 0, 3), 0 FALL (modes 1, 2)
        .equ    GAP, 200        ; no sample edge for GAP cycles after a byte = end of the transfer

; ----- thread 0: MISO (the same as fw/spi_tgt2.s). r0 MISO bits, r1 edge time, r2 0x0FF0, r3 the next word.
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

; ----- thread 1: MOSI bytes 0, 2, 4, ... r1 MOSI bits, r2 edge time and pin levels, r3 edge time.
rx0:    WAIT    CS, FALL, r2, CT ; a transfer starts (SCK idles high in modes 2 and 3: no edge counts before CS)
        TADD    GAP             ; timeout of the first sample edge
act0:   WAIT    SCK, SE, r2, TO, CT ; sample edge of bit 7 of an own byte, or no edge for GAP cycles
        IOR     r1, PINS        ; pin levels 5 to 8 cycles after the edge cycle (IOR does not change E)
        BEC     rx0             ; the transfer ended (the byte before was given in the idle byte), or no clock
        ANDI    r1, 2           ; MOSI (uio[1]) in bit 1
        WAIT    SCK, SE, r2, CT ; bit 6
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
        IOR     r2, PINS        ; masked and shifted in at the next edge or at the end
        TADD    GAP             ; timeout of the next byte start
; the other thread's byte: count the edges, give the own byte to the host at the second edge
        WAIT    SCK, SE, r3, TO ; bit 7 of the other byte (T = own bit 0 edge + GAP; r2 keeps the pin levels)
        BEC     end0            ; no edge: the transfer ended with the own byte
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2          ; own byte in bits 8 to 1
        WAIT    SCK, SE, r2, TO, CT ; bit 6 of the other byte
        BEC     rx0             ; a byte that is not whole
        SHL     r1, 2           ; own byte to bits 10 to 3
        PUSH.NB r1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 5
        BEC     rx0
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 4
        BEC     rx0
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 3
        BEC     rx0
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 2
        BEC     rx0
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 1
        BEC     rx0
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 0 of the other byte
        BEC     rx0
        TADD    GAP             ; timeout of the next own byte
        JMP     act0
end0:   ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        SHL     r1, 2
        ORI     r1, 1           ; bit 0 = 1: the last byte of the transfer
        PUSH.NB r1
        JMP     rx0

; ----- thread 2: MOSI bytes 1, 3, 5, ... (the same as thread 1 after the first byte of a transfer).
rx1:    WAIT    CS, FALL, r2, CT ; a transfer starts
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; byte 0 (thread 1): bit 7
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 6
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 5
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 4
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 3
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 2
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 1
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 0 of byte 0
        BEC     rx1
        TADD    GAP             ; timeout of byte 1
act1:   WAIT    SCK, SE, r2, TO, CT ; sample edge of bit 7 of an own byte, or no edge for GAP cycles
        IOR     r1, PINS
        BEC     rx1
        ANDI    r1, 2
        WAIT    SCK, SE, r2, CT ; bit 6
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
        TADD    GAP
        WAIT    SCK, SE, r3, TO ; bit 7 of the other byte
        BEC     end1
        ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        WAIT    SCK, SE, r2, TO, CT ; bit 6 of the other byte
        BEC     rx1
        SHL     r1, 2
        PUSH.NB r1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 5
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 4
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 3
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 2
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 1
        BEC     rx1
        TADD    GAP
        WAIT    SCK, SE, r2, TO, CT ; bit 0 of the other byte
        BEC     rx1
        TADD    GAP
        JMP     act1
end1:   ANDI    r2, 2
        SHL     r1, 1
        OR      r1, r2
        SHL     r1, 2
        ORI     r1, 1
        PUSH.NB r1
        JMP     rx1
